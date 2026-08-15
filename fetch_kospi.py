#!/usr/bin/env python3
"""Scrape KOSPI daily investor net-buy data from Naver Finance and render the report.

Linux/cloud counterpart of Get-KospiInvestorTrend.ps1 — same source, same CSV
schema, same output, so either script can drive the same template.

  Source : https://finance.naver.com/sise/investorDealTrendDay.naver (sosok=01 = KOSPI)
  Unit   : 100 million KRW (억원)
  Cache  : data/kospi_investor_daily.csv — only missing days are fetched
  Output : dist/index.html — template.html with the dataset injected

Standard library only; no pip install required.

Usage:  python3 fetch_kospi.py [--months 14] [--force]
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(ROOT, "data", "kospi_investor_daily.csv")
TEMPLATE = os.path.join(ROOT, "template.html")
OUT_HTML = os.path.join(ROOT, "dist", "index.html")

# Column order as rendered by Naver, left to right.
COLUMNS = [
    "date", "individual", "foreign", "institution",
    "fin_invest", "insurance", "trust", "bank", "other_fin", "pension",
    "other_corp",
]

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})$")


def fetch_page(bizdate):
    """Return the (up to 10) rows Naver renders for the page ending at bizdate."""
    url = ("https://finance.naver.com/sise/investorDealTrendDay.naver"
           "?bizdate=%s&sosok=01" % bizdate)
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Referer": "https://finance.naver.com/sise/sise_deal_rank.naver",
    })
    with urlopen(req, timeout=30) as resp:
        html = resp.read().decode("euc-kr", "replace")

    rows = []
    for tr in ROW_RE.findall(html):
        cells = [TAG_RE.sub("", c).replace("&nbsp;", " ").strip()
                 for c in CELL_RE.findall(tr)]
        if len(cells) < len(COLUMNS):
            continue
        m = DATE_RE.match(cells[0])
        if not m:
            continue
        rec = {"date": "20%s-%s-%s" % (m.group(1), m.group(2), m.group(3))}
        for i, col in enumerate(COLUMNS[1:], start=1):
            try:
                rec[col] = int(cells[i].replace(",", ""))
            except ValueError:
                rec[col] = 0
        rows.append(rec)
    return rows


def months_back(d, n):
    """Same calendar-month arithmetic as PowerShell's AddMonths."""
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def load_cache():
    by_date = {}
    if os.path.exists(CSV_PATH):
        # utf-8-sig, not utf-8: PowerShell's Export-Csv writes a BOM, which would
        # otherwise turn the first header into "﻿date" and lose the column.
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                for c in COLUMNS[1:]:
                    r[c] = int(r[c])
                by_date[r["date"]] = r
    return by_date


def render(all_rows, target_start):
    """Write dist/index.html from rows already in hand. Never touches the network."""
    window = [r for r in all_rows if r["date"] >= target_start]
    if not window:
        print("ERROR: no rows on or after %s." % target_start, file=sys.stderr)
        return 1

    payload = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "latestDate": window[-1]["date"],
        "rows": [{c: r[c] for c in COLUMNS} for r in window],
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/null",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print("Wrote %s" % OUT_HTML)
    latest = all_rows[-1]
    print("LATEST %s individual=%d foreign=%d institution=%d"
          % (latest["date"], latest["individual"], latest["foreign"], latest["institution"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=14)
    ap.add_argument("--force", action="store_true",
                    help="ignore the early-stop and re-fetch the whole window")
    ap.add_argument("--render-only", action="store_true",
                    help="rebuild the report from the cached CSV without touching the "
                         "network (for environments whose egress cannot reach Naver)")
    args = ap.parse_args()

    for d in (os.path.dirname(CSV_PATH), os.path.dirname(OUT_HTML)):
        os.makedirs(d, exist_ok=True)

    by_date = load_cache()
    if by_date:
        print("Cache: %d rows loaded." % len(by_date))

    today = date.today()
    target_start = months_back(today, args.months).isoformat()

    if args.render_only:
        if not by_date:
            print("ERROR: --render-only needs %s, which is missing or empty."
                  % CSV_PATH, file=sys.stderr)
            return 1
        print("Render-only: %d cached rows, no network." % len(by_date))
        return render([by_date[k] for k in sorted(by_date)], target_start)

    # Keys come from the CSV oldest-first, so the first one starts cached history.
    cache_covers = bool(by_date) and min(by_date) <= target_start

    bizdate = today.strftime("%Y%m%d")
    seen = set()
    fetched = added = 0

    for page in range(1, 61):
        if bizdate in seen:
            break
        seen.add(bizdate)

        try:
            rows = fetch_page(bizdate)
        except (URLError, OSError) as e:
            print("ERROR: fetch failed at bizdate=%s: %s" % (bizdate, e), file=sys.stderr)
            return 1
        fetched += 1
        if not rows:
            print("WARNING: no rows at bizdate=%s; stopping." % bizdate, file=sys.stderr)
            break

        new_on_page = sum(1 for r in rows if r["date"] not in by_date)
        for r in rows:
            by_date[r["date"]] = r
        added += new_on_page

        oldest = min(r["date"] for r in rows)
        print("Page %d: bizdate=%s rows=%d new=%d oldest=%s"
              % (page, bizdate, len(rows), new_on_page, oldest))

        if oldest <= target_start:
            break
        if not args.force and cache_covers and new_on_page == 0:
            print("Reached cached history; stopping early.")
            break

        prev = datetime.strptime(oldest, "%Y-%m-%d").date() - timedelta(days=1)
        bizdate = prev.strftime("%Y%m%d")
        time.sleep(0.35)

    if not by_date:
        print("ERROR: no data collected.", file=sys.stderr)
        return 1

    all_rows = [by_date[k] for k in sorted(by_date)]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        # csv defaults to \r\n on every platform; .gitattributes pins this file to
        # LF, so without this the file reads as modified right after every run.
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(all_rows)
    print("Fetched %d page(s), %d new row(s). Total %d rows (%s .. %s)."
          % (fetched, added, len(all_rows), all_rows[0]["date"], all_rows[-1]["date"]))

    return render(all_rows, target_start)


if __name__ == "__main__":
    sys.exit(main())
