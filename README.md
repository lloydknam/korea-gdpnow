# 한국 GDP 나우 (Korea GDPNow)

애틀랜타 연준 GDPNow처럼, 한국 실질·경상 GDP 성장률(당해 연간 + 분기)을 월간 지표가 나올 때마다 갱신해 **모바일 웹페이지(GitHub Pages)**로 보여주는 실시간 추정 페이지.

## 설치 (5분)

1. GitHub에 새 저장소 생성(예: `korea-gdpnow`, Public 권장 — Private이면 Pages에 유료 플랜 필요) → 이 폴더 내용을 그대로 push
2. **Settings → Secrets and variables → Actions → New repository secret**: `ECOS_API_KEY` = ECOS 인증키
3. **Settings → Pages → Build and deployment → Source: GitHub Actions**
4. **Actions → nowcast → Run workflow** 한 번 실행
5. 페이지 주소: `https://<계정>.github.io/korea-gdpnow/` → 휴대폰 홈 화면에 추가

이후 평일 하루 3번(09:15·12:40·17:30 KST) 자동 실행. 값이 바뀔 때만 이력이 쌓임.

## 관세청 10일·20일 속보 넣기

관세청 속보는 ECOS에 없어서 `data/customs_flash.csv`에 한 줄씩 추가합니다(휴대폰 GitHub 앱/웹에서 편집 가능). 저장하면 워크플로우가 자동으로 다시 돌아 페이지가 갱신됩니다.

```
release_date,ref_month,period,exports_yoy_pct,workdays,workdays_prev,exports_usd_mn,chip_yoy_pct
2026-10-11,2026-10,d10,  <누적 증감률>, <올해 조업일수>, <작년 조업일수>, <금액>, <반도체 증감률>
```

`period`: `d10`(1~10일) / `d20`(1~20일) / `m`(월 전체). 조업일수 칸을 비우면 공휴일 달력으로 자동 계산(토요일 0.5일).
한은 수출물량지수가 그 달 실적을 내면 속보는 자동으로 무시됩니다.

## 모형 (1단계: 브리지 방정식)

```
GDP 전기비_q = α + β₁·광공업_q + β₂·서비스업_q + β₃·건설업_q + β₄·공공행정_q + β₅·실질수출_q + ε
```

| 단계 | 내용 |
|---|---|
| 지표 | 통계청 전산업생산지수(901Y033, 계절조정) 광공업·서비스업·건설업·공공행정 + 한은 수출물량지수(403Y002) |
| 실질수출 | 원계열 → STL 계절조정 + 영업일(추석·설 이동) 보정. **반도체 단가 효과는 물량지수에서 제거** |
| 미발표 월 | 관세청 속보(금액 증가율 ÷ 수출물가 달러기준 증가율 → 물량) → 그래도 비면 AR(2)+ESI 변화로 보간 |
| 브리지 | 2006Q1~ OLS, 코로나 4개 분기 제외. **GDP 계열이 바뀔 때(새 발표·개정)만 재추정** |
| 헤드라인 | **당해 연간 실질·경상 성장률(전년비)** = 발표 분기 + 실시간 추정 분기를 연쇄. 분기 실시간 추정는 연율(SAAR)/전기비 전환 |
| 디플레이터 | (1+명목 전기비)/(1+실질 전기비)−1 을 소비자물가·수출물가(원화)·수입물가(원화) 분기 증가율로 브리지(2010Q3~). 경상 = 실질 × 디플레이터 |
| 변동 분해 | 직전 실행 대비 변화 = Σ βᵢ·Δ지표ᵢ (+ 재추정 시 '모형 재추정·GDP 개정' 항목) → 정확히 합산 |
| 오차범위 | 2015Q1~ 의사 실시간 백테스트에서 분기 내 실적 개월 수(0~3)별 RMSE |
| 속보치 비교 | 한은 속보치가 처음 잡히는 순간 그 분기 마지막 추정치와 함께 기록 |

ECOS 통계코드는 `config/series.yaml`에 있음(2026-10-11 ECOS 메타데이터로 검증).

## 파일

```
config/series.yaml        통계코드·모형 설정
nowcast/ecos.py           ECOS API
nowcast/adjust.py         계절·영업일 조정, 관세청 속보 환산
nowcast/model.py          보간·브리지·백테스트
nowcast/run.py            실행 → data/ 갱신, site/ 생성
web/index.html            페이지 템플릿(라이브러리 없음, SVG 차트)
data/state.json           직전 추정·계수(자동)
data/history.csv          갱신 이력(자동)
data/backtest.csv         백테스트 분기별 결과(자동)
tests/                    합성 데이터 검증: python tests/test_pipeline.py
```

## 한계와 2단계

- 백테스트는 개정된 최종 통계 사용 → 실제 실시간 오차보다 작게 나옴. 실전 성적은 '속보치와 비교' 표로 확인
- 지출 측(소비·투자·재고) 정보 없음 → 소매판매·설비투자지수·카드승인액 추가 여지
- 2단계: 동적요인모형(뉴욕 연준 Staff Nowcast 방식)을 같은 데이터로 돌려 RMSE 비교
