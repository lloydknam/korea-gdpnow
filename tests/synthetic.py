"""ECOS 없이 파이프라인을 돌려보기 위한 합성 데이터 fetcher.
구조(코드·기간·발표 시차)는 실제와 같고 값만 가짜."""
import numpy as np
import pandas as pd

from nowcast.adjust import workdays

M_END = pd.Period("2026-08", "M")      # 산업활동·무역지수·수출입물가: 8월까지
CPI_END = pd.Period("2026-09", "M")    # 소비자물가: 9월까지
ESI_END = pd.Period("2026-09", "M")    # ESI: 9월까지
Q_END = pd.Period("2026Q2", "Q")       # 실질·명목 GDP: 2분기까지


def make(seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.period_range("2000-01", ESI_END, freq="M")
    n = len(idx)
    f = np.zeros(n)
    for t in range(1, n):
        f[t] = 0.6 * f[t - 1] + rng.normal(0, 0.5)
    covid = (idx >= pd.Period("2020-03", "M")) & (idx <= pd.Period("2020-05", "M"))
    f[covid] -= 3

    def lvl(mu, beta, sd):
        e = rng.normal(0, sd, n)
        return pd.Series(100 * np.exp(np.cumsum((mu + beta * f + e) / 100)), index=idx)

    S = {"ip": lvl(0.25, 1.2, 1.3), "sv": lvl(0.22, 0.5, 0.5), "cn": lvl(0.05, 0.4, 2.0),
         "pa": lvl(0.2, 0.0, 0.4), "exsa": lvl(0.35, 1.5, 1.8)}
    seas = np.array([-.08, -.12, .05, .0, .02, .03, -.02, -.06, .04, .03, .05, .06])
    wd = np.array([workdays(p.year, p.month, customs=False) for p in idx])
    wd_dev = wd - pd.Series(wd).groupby([p.month for p in idx]).transform("mean").values
    ex_nsa = S["exsa"] * np.exp(seas[[p.month - 1 for p in idx]] + 0.03 * wd_dev)
    esi = pd.Series(100 + 4 * f + rng.normal(0, 1, n), index=idx)
    px_usd = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.1, 1, n) / 100)), index=idx)
    chip_px = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.3, 3, n) / 100)), index=idx)
    chip_vol = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.8, 4, n) / 100)), index=idx)
    # 물가: 2026년 수출물가 급등(교역조건 개선) 재현
    cpi_seas = np.array([.1, .2, .1, 0, -.1, -.1, .1, .2, .1, -.1, -.2, -.2])
    cpi = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.18, 0.2, n) / 100) + cpi_seas[[p.month - 1 for p in idx]] / 100), index=idx)
    boom = np.array([3.0 if p >= pd.Period("2026-03", "M") and p <= pd.Period("2026-07", "M") else 0 for p in idx])
    px_krw = pd.Series(100 * np.exp(np.cumsum((rng.normal(0.1, 1.5, n) + boom) / 100)), index=idx)
    pm_krw = pd.Series(100 * np.exp(np.cumsum((rng.normal(0.1, 1.8, n) + boom * 0.3) / 100)), index=idx)

    def qg(s):
        q = s.groupby(s.index.asfreq("Q")).mean()
        g = 100 * np.log(q).diff()
        g.index = pd.PeriodIndex(g.index, freq="Q")
        return g

    gdp = (0.05 + 0.25 * qg(S["ip"]) + 0.5 * qg(S["sv"]) + 0.05 * qg(S["cn"])
           + 0.1 * qg(S["pa"]) + 0.05 * qg(S["exsa"]))
    gdp = gdp + rng.normal(0, 0.2, len(gdp))
    defl = 0.1 + 0.6 * qg(cpi) + 0.4 * qg(px_krw) - 0.35 * qg(pm_krw) + rng.normal(0, 0.3, len(gdp))
    nom = ((1 + gdp / 100) * (1 + defl / 100) - 1) * 100
    gdp, nom = gdp[gdp.index <= Q_END].dropna(), nom[(nom.index <= Q_END) & (nom.index >= pd.Period("2010Q2", "Q"))]
    m = idx <= M_END
    return {
        ("200Y102", "10111"): gdp.round(1), ("200Y102", "60111"): nom.round(1),
        ("901Y033", "AB00", "2"): S["ip"][m], ("901Y033", "AC00", "2"): S["sv"][m],
        ("901Y033", "AD00", "2"): S["cn"][m], ("901Y033", "AE00", "2"): S["pa"][m],
        ("901Y033", "A00", "2"): (S["ip"] * .3 + S["sv"] * .7)[m],
        ("403Y002", "*AA"): ex_nsa[m], ("403Y002", "3091AA"): chip_vol[m],
        ("402Y014", "*AA", "D"): px_usd[m], ("402Y014", "3091AA", "D"): chip_px[m],
        ("402Y014", "*AA", "W"): px_krw[m], ("401Y015", "*AA", "W"): pm_krw[m],
        ("901Y009", "0"): cpi[idx <= CPI_END],
        ("513Y001", "E1000"): esi,
    }


def fetcher_from(data):
    def fetch(stat, cycle, start, end, items):
        return data[(stat, *[str(i) for i in items])].copy()
    return fetch
