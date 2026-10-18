"""부가 분석 3종 — 헤드라인(생산 측 브리지)과 별개. 실패하면 해당 섹션만 빠짐.

❹ gni_section   실질 GDP vs 실질 GNI → 차이(교역조건 효과 + 국외순수취요소소득)
                 차이 = a + b·교역조건(수출물가÷수입물가, 원화) 전년동기비 → 추정 분기 실질 GNI
❺ dist_section  경상 GDP 전년동기비 = Σ 소득 비중(전년 연간) × 소득 항목 증가율
                 피용자보수 / 총영업잉여(영업잉여+고정자본소모) / 순생산·수입세
❻ exp_section   실질 GDP 전기비의 지출 항목별 기여도(계절조정 수준)
                 + 추정 분기: 항목별 브리지(민간소비←서비스업, 설비←설비투자지수 …)로 지출 측 합산
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import adjust, model


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


# ------------------------------------------------------------------ 수집
def collect(cfg: dict, fetcher, end_q: str, end_m: str) -> dict:
    """그룹별로 따로 수집 → 한 그룹이 실패해도 나머지는 계속."""
    ex, raw, errs = cfg["extras"], {}, []

    def grab(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {type(e).__name__} {e}")

    inc = ex["income"]
    grab("income", lambda: raw.update({f"inc_{k}": fetcher(inc["stat"], "Q", inc["start"], end_q, [c])
                                       for k, c in inc["items"].items()}))
    dw = ex["dist_weights"]
    grab("dist_weights", lambda: raw.update({f"dw_{k}": fetcher(dw["stat"], "A", dw["start"], end_q[:4], [c])
                                             for k, c in dw["items"].items()}))
    e = ex["expenditure"]
    grab("expenditure", lambda: raw.update({f"exp_{k}": fetcher(e["stat"], "Q", e["start"], end_q, [it["code"]])
                                            for k, it in e["items"].items()}))
    for k, s in ex["exp_monthly"].items():
        grab(k, lambda k=k, s=s: raw.update({k: fetcher(s["stat"], "M", cfg["monthly_start"], end_m, s["items"])}))
    raw["_errors"] = errs
    return raw


def _ols(y: np.ndarray, x: np.ndarray) -> dict:
    A = np.column_stack([np.ones(len(x)), x])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    res = y - A @ b
    return {"a": float(b[0]), "b": float(b[1]), "n": int(len(y)),
            "r2": float(1 - (res ** 2).sum() / ((y - y.mean()) ** 2).sum()),
            "sd": float(np.std(res, ddof=2))}


# ------------------------------------------------------------------ ❹ 실질 GNI·교역조건
def tot_yoy(px: pd.Series, pm: pd.Series, q: pd.Period) -> tuple[float | None, int]:
    """분기 교역조건(수출물가÷수입물가) 전년동기비. 발표된 달만으로 같은 달끼리 비교 → (값, 개월 수)."""
    tot = (px / pm).dropna()
    ms = [m for m in pd.period_range(q.asfreq("M", "start"), q.asfreq("M", "end"), freq="M")
          if m in tot.index and (m - 12) in tot.index]
    if not ms:
        return None, 0
    return (tot[ms].mean() / tot[[m - 12 for m in ms]].mean() - 1) * 100, len(ms)


def gni_section(raw: dict, px: pd.Series, pm: pd.Series, annual: dict | None) -> dict | None:
    gdp, gni = raw.get("inc_gdp_r"), raw.get("inc_gni_r")
    if gdp is None or gni is None or gdp.empty or gni.empty:
        return None
    j = pd.concat([gdp.rename("gdp"), gni.rename("gni")], axis=1).dropna()
    j["gap"] = j["gni"] - j["gdp"]
    j["tot"] = [tot_yoy(px, pm, q)[0] for q in j.index]
    fitdf = j.dropna()
    fitdf = fitdf[~fitdf.index.isin(pd.PeriodIndex(["2020Q2", "2020Q3"], freq="Q"))]
    fit = _ols(fitdf["gap"].values, fitdf["tot"].values)
    last = j.index.max()
    rows = [{"q": str(q), "gdp": _r(v.gdp), "gni": _r(v.gni), "gap": _r(v.gap), "tot": _r(v.tot, 1), "est": False}
            for q, v in j[j.index >= pd.Period("2019Q1", "Q")].iterrows()]
    out = {"rows": rows, "fit": {k: _r(v, 3) for k, v in fit.items()}, "last": str(last)}
    if not annual:
        return out
    # 추정 분기: 실질 GDP 전년동기비(실시간 추정 경로) + 교역조건으로 추정한 차이
    yoy = {pd.Period(r["q"], "Q"): r for r in annual.get("yoy", [])}
    last_tot = j["tot"].dropna().iloc[-1] if j["tot"].notna().any() else 0.0
    gaps = {}
    for q in pd.period_range(last + 1, f"{annual['year']}Q4", freq="Q"):
        if q not in yoy or yoy[q]["real"] is None:
            continue
        t, k = tot_yoy(px, pm, q)
        if t is None:
            t = last_tot
        else:
            last_tot = t
        gap = fit["a"] + fit["b"] * t
        gaps[q] = gap
        rows.append({"q": str(q), "gdp": _r(yoy[q]["real"]), "gni": _r(yoy[q]["real"] + gap), "gap": _r(gap),
                     "tot": _r(t, 1), "tot_months": k, "est": True})
    y = annual["year"]
    qs = pd.period_range(f"{y}Q1", f"{y}Q4", freq="Q")
    g4 = [float(j.loc[q, "gap"]) if q in j.index else gaps.get(q) for q in qs]
    if all(g is not None for g in g4):
        gap_y = float(np.mean(g4))
        out["annual"] = {"year": y, "gdp": annual["real"], "gni": _r(annual["real"] + gap_y), "gap": _r(gap_y),
                         "n_actual": int(sum(q in j.index for q in qs))}
    return out


# ------------------------------------------------------------------ ❺ 분배
def dist_section(raw: dict) -> dict | None:
    need = ["inc_gdp_n", "inc_comp", "inc_surplus", "inc_tax", "dw_gdp", "dw_comp", "dw_os", "dw_cfc", "dw_tax", "dw_sub"]
    if any(raw.get(k) is None or raw[k].empty for k in need):
        return None
    A = pd.concat({k: raw[f"dw_{k}"] for k in ("gdp", "comp", "os", "cfc", "tax", "sub")}, axis=1).dropna()
    W = pd.DataFrame({"comp": A["comp"] / A["gdp"], "surplus": (A["os"] + A["cfc"]) / A["gdp"],
                      "tax": (A["tax"] - A["sub"]) / A["gdp"]})
    W.index = [p.year for p in W.index]
    Q = pd.concat({k: raw[f"inc_{k}"] for k in ("gdp_n", "comp", "surplus", "tax")}, axis=1).dropna()
    rows = []
    for q, v in Q.tail(12).iterrows():
        yb = q.year - 1
        w = W.loc[yb] if yb in W.index else W.iloc[-1]
        c = {k: float(w[k] * v[k]) for k in ("comp", "surplus", "tax")}
        rows.append({"q": str(q), "gdp_n": _r(v.gdp_n), **{f"g_{k}": _r(v[k], 1) for k in c},
                     **{f"c_{k}": _r(c[k]) for k in c}, "resid": _r(v.gdp_n - sum(c.values())),
                     "w_year": int(yb if yb in W.index else W.index[-1])})
    share = [{"y": int(y), "comp": _r(w.comp * 100, 1), "surplus": _r(w.surplus * 100, 1), "tax": _r(w.tax * 100, 1)}
             for y, w in W.tail(12).iterrows()]
    return {"rows": rows, "share": share, "last": str(Q.index.max()),
            "labels": {"comp": "피용자보수", "surplus": "총영업잉여", "tax": "순생산·수입세"}}


# ------------------------------------------------------------------ ❻ 지출 측
MIN_R2 = 0.1   # 이보다 낮은 지출 항목 브리지는 쓰지 않음
EXP_SHOW = ["pc", "gc", "ci", "ei", "nx", "other"]
EXP_LABEL = {"pc": "민간소비", "gc": "정부소비", "ci": "건설투자", "ei": "설비투자", "nx": "순수출", "other": "기타"}


def exp_section(cfg: dict, raw: dict, levels: dict, esi: pd.Series, targets: list[pd.Period],
                prod: dict) -> dict | None:
    ecfg = cfg["extras"]
    keys = list(ecfg["expenditure"]["items"])
    if any(raw.get(f"exp_{k}") is None or raw[f"exp_{k}"].empty for k in keys):
        return None
    L = pd.concat({k: raw[f"exp_{k}"] for k in keys}, axis=1).dropna()
    mcfg = cfg["model"]
    last = L.index.max()

    # 실적 기여도(%p) = Δ수준 / 전기 GDP. 수입은 차감. 기타 = GDP 증가율 − 주요 항목 합 → 정확히 합산
    def contrib(cur: dict, prev: dict) -> dict:
        g = (cur["gdp"] / prev["gdp"] - 1) * 100
        c = {k: (cur[k] - prev[k]) / prev["gdp"] * 100 for k in ("pc", "gc", "ci", "ei")}
        c["nx"] = ((cur["x"] - prev["x"]) - (cur["m"] - prev["m"])) / prev["gdp"] * 100
        c["other"] = g - sum(c.values())
        return {"gdp": g, **c}

    hist = {q: contrib(L.loc[q].to_dict(), L.loc[q - 1].to_dict()) for q in L.index[1:] if (q - 1) in L.index}

    # 항목별 브리지: 항목 로그 전기비 = a + b·지표 분기 증가율
    lv = dict(levels)
    for k, s in ecfg["exp_monthly"].items():
        if raw.get(k) is None or raw[k].empty:
            return None
        lv[k] = adjust.seasonal_adjust(raw[k]) if s.get("seasonal_adjust") else raw[k]
    bridges, stats, trend = {}, {}, {}
    excl = {pd.Period(q, "Q") for q in mcfg["exclude_quarters"]}
    for comp, ind in ecfg["exp_bridge"].items():
        y = 100 * np.log(L[comp] / L[comp].shift(1))
        X = pd.DataFrame({ind: model.q_growth(lv[ind])})
        br = model.fit_bridge(y, X, mcfg["bridge_start"], mcfg["exclude_quarters"])
        if br["r2"] < MIN_R2:  # 지표 설명력 없음 → 최근 8개 분기 평균 증가율
            yy = y.dropna()
            trend[comp] = float(yy[~yy.index.isin(excl)].tail(8).mean())
            stats[comp] = {"ind": "trend", "coef": None, "const": _r(trend[comp], 3), "r2": _r(br["r2"], 2)}
            continue
        bridges[comp] = br
        stats[comp] = {"ind": ind, "coef": _r(br["coef"][ind], 3), "const": _r(br["const"], 3), "r2": _r(br["r2"], 2)}
    recent = [q for q in sorted(hist)[-12:] if q not in excl][-8:]
    other_mean = float(np.mean([hist[q]["other"] for q in recent]))

    est, cur = {}, L.loc[last].to_dict()
    for q in [t for t in targets if t > last]:
        prev = cur
        g = {comp: model.nowcast(lv, esi, q, br, mcfg)["value"] for comp, br in bridges.items()}
        g.update(trend)
        nxt = {k: prev[k] * np.exp(g[k] / 100) if k in g else prev[k] for k in prev}
        c = {k: (nxt[k] - prev[k]) / prev["gdp"] * 100 for k in ("pc", "gc", "ci", "ei")}
        c["nx"] = ((nxt["x"] - prev["x"]) - (nxt["m"] - prev["m"])) / prev["gdp"] * 100
        c["other"] = other_mean
        tot = sum(c.values())
        nxt["gdp"] = prev["gdp"] * (1 + tot / 100)
        est[q] = {"gdp": tot, **c, "growth": {k: _r((np.exp(v / 100) - 1) * 100) for k, v in g.items()}}
        cur = nxt

    def row(q, d, is_est):
        out = {"q": str(q), "est": is_est, "gdp": _r(d["gdp"]), **{k: _r(d[k], 3) for k in EXP_SHOW}}
        if is_est:
            out["prod"] = _r(prod.get(q))
            out["growth"] = d["growth"]
        return out

    rows = [row(q, hist[q], False) for q in sorted(hist)[-6:]] + [row(q, est[q], True) for q in sorted(est)]
    return {"rows": rows, "labels": EXP_LABEL, "order": EXP_SHOW, "last": str(last),
            "bridge": stats, "other_mean": _r(other_mean, 3),
            "ind_labels": {"sv": "서비스업생산", "pa": "공공행정", "cn": "건설업생산", "eq": "설비투자지수",
                           "ex": "실질수출", "mv": "수입물량", "trend": "최근 8분기 평균"},
            "comp_labels": {"pc": "민간소비", "gc": "정부소비", "ci": "건설투자", "ei": "설비투자", "x": "수출", "m": "수입"}}
