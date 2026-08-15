<#
.SYNOPSIS
    Scrapes KOSPI daily investor net-buy data from Naver Finance and renders an HTML report.

.DESCRIPTION
    Source : https://finance.naver.com/sise/investorDealTrendDay.naver (sosok=01 = KOSPI)
    Unit   : 100 million KRW (억원)
    Cache  : data/kospi_investor_daily.csv  - only missing days are fetched on later runs.
    Output : dist/index.html  - template.html with the dataset injected.

    This file is intentionally ASCII-only. All Korean UI text lives in template.html
    (UTF-8), because Windows PowerShell 5.1 reads .ps1 files as ANSI without a BOM.

.PARAMETER Months
    How far back to keep history. Default 14.

.PARAMETER Force
    Ignore the early-stop and re-fetch the whole window.
#>
[CmdletBinding()]
param(
    [int]$Months = 14,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir  = Join-Path $Root 'data'
$DistDir  = Join-Path $Root 'dist'
$CsvPath  = Join-Path $DataDir 'kospi_investor_daily.csv'
$Template = Join-Path $Root 'template.html'
$OutHtml  = Join-Path $DistDir 'index.html'

foreach ($d in @($DataDir, $DistDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# Column order as rendered by Naver, left to right.
$Columns = @(
    'date','individual','foreign','institution',
    'fin_invest','insurance','trust','bank','other_fin','pension',
    'other_corp'
)

function Get-TrendPage {
    param([string]$BizDate)

    $uri = "https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate=$BizDate&sosok=01"
    $res = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 30 -Headers @{
        'User-Agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        'Referer'    = 'https://finance.naver.com/sise/sise_deal_rank.naver'
    }
    $html = [System.Text.Encoding]::GetEncoding(51949).GetString($res.RawContentStream.ToArray())

    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($tr in [regex]::Matches($html, '(?s)<tr[^>]*>(.*?)</tr>')) {
        $cells = @()
        foreach ($td in [regex]::Matches($tr.Groups[1].Value, '(?s)<td[^>]*>(.*?)</td>')) {
            $txt = $td.Groups[1].Value -replace '<[^>]+>', '' -replace '&nbsp;', ' '
            $cells += $txt.Trim()
        }
        if ($cells.Count -lt $Columns.Count) { continue }
        if ($cells[0] -notmatch '^(\d{2})\.(\d{2})\.(\d{2})$') { continue }

        $rec = [ordered]@{ date = '20{0}-{1}-{2}' -f $Matches[1], $Matches[2], $Matches[3] }
        for ($i = 1; $i -lt $Columns.Count; $i++) {
            $raw = $cells[$i] -replace ',', ''
            $n = 0
            if (-not [int]::TryParse($raw, [ref]$n)) { $n = 0 }
            $rec[$Columns[$i]] = $n
        }
        $rows.Add([pscustomobject]$rec)
    }
    return $rows
}

# --- load cache -------------------------------------------------------------
$byDate = [ordered]@{}
if (Test-Path $CsvPath) {
    foreach ($r in (Import-Csv -Path $CsvPath)) {
        foreach ($c in $Columns[1..($Columns.Count - 1)]) { $r.$c = [int]$r.$c }
        $byDate[$r.date] = $r
    }
    Write-Host ("Cache: {0} rows loaded." -f $byDate.Count)
}

# --- page backwards until the window is covered -----------------------------
$targetStart  = (Get-Date).AddMonths(-$Months).ToString('yyyy-MM-dd')
# Keys arrive from the CSV oldest-first, so [0] is the start of cached history.
$cacheCovers  = ($byDate.Count -gt 0) -and (@($byDate.Keys)[0] -le $targetStart)
$bizdate      = (Get-Date).ToString('yyyyMMdd')
$seenBizdates = @{}
$fetched      = 0
$added        = 0

for ($page = 1; $page -le 60; $page++) {
    if ($seenBizdates.ContainsKey($bizdate)) { break }
    $seenBizdates[$bizdate] = $true

    $rows = Get-TrendPage -BizDate $bizdate
    $fetched++
    if ($rows.Count -eq 0) {
        Write-Warning "No rows at bizdate=$bizdate; stopping."
        break
    }

    $newOnPage = 0
    foreach ($r in $rows) {
        if (-not $byDate.Contains($r.date)) { $newOnPage++ }
        $byDate[$r.date] = $r
    }
    $added += $newOnPage

    $oldest = ($rows | ForEach-Object { $_.date } | Sort-Object)[0]
    Write-Host ("Page {0}: bizdate={1} rows={2} new={3} oldest={4}" -f $page, $bizdate, $rows.Count, $newOnPage, $oldest)

    if ($oldest -le $targetStart) { break }
    if (-not $Force -and $cacheCovers -and $newOnPage -eq 0) {
        Write-Host 'Reached cached history; stopping early.'
        break
    }

    $bizdate = ([datetime]::ParseExact($oldest, 'yyyy-MM-dd', $null)).AddDays(-1).ToString('yyyyMMdd')
    Start-Sleep -Milliseconds 350
}

# --- persist ----------------------------------------------------------------
$all = $byDate.Values | Sort-Object date
$all | Export-Csv -Path $CsvPath -NoTypeInformation -Encoding UTF8
Write-Host ("Fetched {0} page(s), {1} new row(s). Total {2} rows ({3} .. {4})." -f `
    $fetched, $added, @($all).Count, @($all)[0].date, @($all)[-1].date)

# --- render -----------------------------------------------------------------
$window = $all | Where-Object { $_.date -ge $targetStart }
$json = $window | Select-Object $Columns | ConvertTo-Json -Compress -Depth 3
if ($json -notmatch '^\[') { $json = "[$json]" }

$payload = [ordered]@{
    generatedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm')
    latestDate  = @($window)[-1].date
    rows        = 'ROWS_PLACEHOLDER'
} | ConvertTo-Json -Compress
$payload = $payload -replace '"ROWS_PLACEHOLDER"', $json

$html = [System.IO.File]::ReadAllText($Template, [System.Text.Encoding]::UTF8)
$html = $html.Replace('/*__DATA__*/null', $payload)
[System.IO.File]::WriteAllText($OutHtml, $html, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Wrote $OutHtml"
