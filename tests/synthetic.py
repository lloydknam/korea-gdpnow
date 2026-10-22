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
    # ── 부가 분석용 (❹ GNI ❺ 분배 ❻ 지출) ──
    qidx = gdp.index
    lvl_gdp = 500_000 * np.exp(np.cumsum(gdp.fillna(0) / 100))
    yoy = lambda lv: (lv / lv.shift(4) - 1) * 100
    tot_q = (px_krw / pm_krw).groupby((px_krw / pm_krw).index.asfreq("Q")).mean()
    tot_q.index = pd.PeriodIndex(tot_q.index, freq="Q")
    gap = 0.35 * yoy(tot_q).reindex(qidx) + rng.normal(0.2, 0.3, len(qidx))
    q11 = qidx >= pd.Period("2011Q1", "Q")
    gdp_yoy = yoy(lvl_gdp)
    nom_lvl = lvl_gdp * np.exp(np.cumsum(defl.reindex(qidx).fillna(0) / 100))
    nom_yoy = yoy(nom_lvl)
    comp_g = nom_yoy * 0.6 + rng.normal(1.5, 0.8, len(qidx))
    tax_g = nom_yoy * 0.8 + rng.normal(0, 1.5, len(qidx))
    sur_g = (nom_yoy - 0.45 * comp_g - 0.10 * tax_g) / 0.45
    eq_m = lvl(0.25, 1.5, 2.5)
    mv_nsa = lvl(0.3, 1.2, 1.6) * np.exp(seas[[p.month - 1 for p in idx]])
    qg_ = lambda sm: qg(sm).reindex(qidx).fillna(0)
    comps = {"1010110": 0.48 * np.exp(np.cumsum((0.1 + 0.8 * qg_(S["sv"]) + rng.normal(0, .3, len(qidx))) / 100)),
             "1010120": 0.18 * np.exp(np.cumsum((0.3 + 0.9 * qg_(S["pa"]) + rng.normal(0, .3, len(qidx))) / 100)),
             "1020111": 0.15 * np.exp(np.cumsum((0.9 * qg_(S["cn"]) + rng.normal(0, 1, len(qidx))) / 100)),
             "1020112": 0.09 * np.exp(np.cumsum((1.0 * qg_(eq_m) + rng.normal(0, 1.5, len(qidx))) / 100)),
             "1020113": 0.05 * np.exp(np.cumsum(rng.normal(0.8, .5, len(qidx)) / 100)),
             "10301": 0.45 * np.exp(np.cumsum((0.35 * qg_(S["exsa"]) + rng.normal(0, .4, len(qidx))) / 100)),
             "10401": 0.40 * np.exp(np.cumsum((0.35 * qg_(mv_nsa) + rng.normal(0, .4, len(qidx))) / 100))}
    ex_lv = {k: pd.Series(np.asarray(v), index=qidx) * float(lvl_gdp.iloc[0]) for k, v in comps.items()}
    inv = pd.Series(rng.normal(0, 3000, len(qidx)), index=qidx)
    sd_ = lvl_gdp - (sum(v for k, v in ex_lv.items() if k != "10401") - ex_lv["10401"] + inv)
    exp_out = {("200Y108", k): v[qidx >= pd.Period("2005Q1", "Q")] for k, v in
               {**ex_lv, "1020120": inv, "10501": sd_, "10601": lvl_gdp}.items()}
    ann = pd.period_range("2010", "2025", freq="Y")
    base = pd.Series(np.linspace(1_200_000, 2_600_000, len(ann)), index=ann)
    dist_ann = {"10106": base, "10101": base * 0.45, "10102": base * 0.25, "10103": base * 0.20,
                "10104": base * 0.11, "10105": base * 0.01}
    ls_ann = {"10301": base * 0.70, "10101": base * 0.47, "10201": base * 0.23}
    extras_out = {
        **{("200Y116", k): v for k, v in ls_ann.items()},
        ("200Y102", "10211"): gdp_yoy[q11].round(1), ("200Y102", "20122"): (gdp_yoy + gap)[q11].round(1),
        ("200Y102", "60211"): nom_yoy[q11].round(1), ("200Y102", "60212"): comp_g[q11].round(1),
        ("200Y102", "60213"): sur_g[q11].round(1), ("200Y102", "60214"): tax_g[q11].round(1),
        **{("200Y113", k): v for k, v in dist_ann.items()},
        **exp_out,
        ("901Y066", "I15B"): eq_m[m], ("403Y004", "*AA"): mv_nsa[idx <= CPI_END],
    }
    for k in list(extras_out):
        if k[0] in ("200Y102", "200Y108"):
            extras_out[k] = extras_out[k].dropna()
            extras_out[k] = extras_out[k][extras_out[k].index <= Q_END]
    return {**extras_out,
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
