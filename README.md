# 코스피 수급 추이

네이버 금융에서 코스피 투자자별 일별 순매수(개인·외국인·기관)를 수집해,
월별 / 주별 / 최근 20영업일 세 개의 라인 차트로 된 HTML 리포트를 만듭니다.

## 구성

| 파일 | 역할 |
|---|---|
| `fetch_kospi.py` | 수집 + 렌더 (리눅스·클라우드용, 표준 라이브러리만 사용) |
| `Get-KospiInvestorTrend.ps1` | 같은 일을 하는 Windows PowerShell 5.1 버전 |
| `template.html` | 차트 페이지. 두 스크립트가 공유하며 `/*__DATA__*/null` 자리에 데이터를 주입 |
| `data/kospi_investor_daily.csv` | 일별 데이터 캐시 (커밋해서 재수집량을 줄임) |
| `dist/index.html` | 생성물 — 직접 수정하지 말 것 |

## 실행

```
python3 fetch_kospi.py --months 14
```

```
./Get-KospiInvestorTrend.ps1 -Months 14
```

`--force` / `-Force` 를 주면 캐시를 무시하고 창 전체를 다시 받습니다.
`--render-only` 는 네트워크를 전혀 쓰지 않고 커밋된 CSV만으로 리포트를 다시 만듭니다.

## 자동화 구조

수집과 발행이 두 곳으로 나뉘어 있습니다.

| 시각 (KST) | 주체 | 하는 일 |
|---|---|---|
| 평일 07:50 | GitHub Actions (`.github/workflows/fetch-kospi.yml`) | 네이버에서 새 영업일 수집 → CSV 커밋 |
| 평일 08:00 | Claude 클라우드 루틴 | 저장소 클론 → `--render-only` 렌더 → 아티팩트 갱신 → 푸시 알림 |

**왜 나눴나:** Claude 클라우드 샌드박스의 이그레스 프록시는 허용 목록 방식이라
`finance.naver.com` 요청이 403으로 거부됩니다 (`WebFetch`도 동일). GitHub 러너는
아웃바운드가 열려 있으므로 스크래핑을 그쪽에 두고, 루틴은 커밋된 CSV만 읽습니다.

Actions 워크플로는 `workflow_dispatch` 로 수동 실행할 수 있습니다.

## 데이터

- 출처: `https://finance.naver.com/sise/investorDealTrendDay.naver` — `sosok=01`이 코스피, `sosok=02`가 코스닥
- 페이지 인코딩은 EUC-KR, 한 번에 10행씩만 내려오며 페이징 링크가 없습니다.
  화면에 보이는 가장 오래된 날짜의 하루 전을 `bizdate`로 넣어 거슬러 올라갑니다.
- 단위는 억원. 순매수 = 매수 − 매도.
- CSV 컬럼: `date, individual, foreign, institution, fin_invest, insurance,
  trust, bank, other_fin, pension, other_corp`

`institution`은 기관계이며, 그 뒤 여섯 컬럼(금융투자·보험·투신·은행·기타금융·연기금)의
합입니다. 리포트는 현재 개인·외국인·기관계 세 계열만 그리지만, 세부 주체도 CSV에 모두
들어 있습니다.

## 차트를 바꾸려면

`template.html`을 고치고 스크립트를 다시 실행하세요. `dist/index.html`은 매번
덮어써지므로 직접 편집하면 사라집니다.
