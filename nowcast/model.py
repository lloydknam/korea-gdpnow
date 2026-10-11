"""브리지 방정식 실시간 추정.

흐름: 월간 지표(계절조정 수준) → 미발표 월은 AR(p)+ESI로 보간(ragged edge)
      → 분기평균 전기비(로그 %) → 브리지 OLS → 지표별 기여도.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- 월간 보간
def mlog_diff(level: pd.Series) -> pd.Series:
    return 100 * np.log(level).diff()


def fit_ar(level: pd.Series, esi: pd.Series | None, p: int, start: str,
           excl: tuple[str, str] | None, end: pd.Period | None = None) -> dict:
    """d_t = c + Σφ_j d_{t-j} + γ·ΔESI_t  (d = 로그 전월비 %)."""
    d = mlog_diff(level).dropna()
    if end is not None:
        d = d[d.index <= end]
    df = pd.DataFrame({"d": d})
    for j in range(1, p + 1):
        df[f"l{j}"] = d.shift(j)
    use_esi = esi is not None and len(esi) > 0
    if use_esi:
        df["desi"] = esi.diff().reindex(df.index)
    df = df[df.index >= pd.Period(start, "M")].dropna()
    if excl:
        a, b = pd.Period(excl[0], "M"), pd.Period(excl[1], "M")
        # 제외 구간 + 그 구간을 시차로 물고 있는 직후 p개월
        df = df[~((df.index >= a) & (df.index <= b + p))]
    X = np.column_stack([np.ones(len(df))] + [df[c].values for c in df.columns if c != "d"])
    beta, *_ = np.linalg.lstsq(X, df["d"].values, rcond=None)
    resid = df["d"].values - X @ beta
    return {"beta": beta, "p": p, "use_esi": use_esi,
            "sd": float(np.std(resid, ddof=X.shape[1])), "n": len(df)}


def fill_ragged(level: pd.Series, through: pd.Period, ar: dict,
                esi: pd.Series | None) -> tuple[pd.Series, set]:
    """level을 through 월까지 AR로 연장. 반환: (연장된 수준, 보간된 월 집합)."""
    level = level.dropna()
    out = level.copy()
    filled = set()
    if level.index.max() >= through:
        return out[out.index <= through], filled
    d = mlog_diff(level).dropna()
    hist = list(d.values[-ar["p"]:])
    desi = esi.diff() if (ar["use_esi"] and esi is not None) else None
    cur = level.index.max()
    lvl = level.iloc[-1]
    b = ar["beta"]
    while cur < through:
        cur = cur + 1
        x = [1.0] + [hist[-j] for j in range(1, ar["p"] + 1)]
        if ar["use_esi"]:
            v = desi.get(cur, np.nan) if desi is not None else np.nan
            x.append(0.0 if pd.isna(v) else float(v))
        dhat = float(np.dot(b, x))
        hist.append(dhat)
        lvl = lvl * np.exp(dhat / 100)
        out.loc[cur] = lvl
        filled.add(cur)
    return out.sort_index(), filled


# ---------------------------------------------------------------- 분기화
def q_growth(level_m: pd.Series) -> pd.Series:
    """완전한 분기(3개월)만 평균 → 100·ln(Q/Q-1)."""
    s = level_m.dropna()
    q = s.groupby(s.index.asfreq("Q")).agg(["mean", "count"])
    q = q.loc[q["count"] == 3, "mean"]
    q.index = pd.PeriodIndex(q.index, freq="Q")
    g = 100 * np.log(q).diff()
    # 연속되지 않은 분기 사이의 증가율은 버림
    prev_ok = pd.Series(q.index, index=q.index).diff().apply(lambda x: getattr(x, "n", None) == 1)
    return g[prev_ok.values]


# ---------------------------------------------------------------- 브리지
def fit_bridge(y: pd.Series, X: pd.DataFrame, start: str, excl: list[str],
               end: pd.Period | None = None) -> dict:
    df = X.join(y.rename("y"), how="inner").dropna()
    df = df[df.index >= pd.Period(start, "Q")]
    if end is not None:
        df = df[df.index <= end]
    ex = {pd.Period(q, "Q") for q in excl}
    df = df[~df.index.isin(ex)]
    cols = list(X.columns)
    A = np.column_stack([np.ones(len(df))] + [df[c].values for c in cols])
    beta, *_ = np.linalg.lstsq(A, df["y"].values, rcond=None)
    fit = A @ beta
    resid = df["y"].values - fit
    ss_tot = ((df["y"] - df["y"].mean()) ** 2).sum()
    return {
        "const": float(beta[0]),
        "coef": {c: float(b) for c, b in zip(cols, beta[1:])},
        "r2": float(1 - (resid ** 2).sum() / ss_tot),
        "sd": float(np.std(resid, ddof=A.shape[1])),
        "n": int(len(df)),
        "sample": f"{df.index.min()}–{df.index.max()}",
    }


def predict(bridge: dict, x: dict) -> float:
    return bridge["const"] + sum(bridge["coef"][k] * x[k] for k in bridge["coef"])


# ---------------------------------------------------------------- 한 시점의 실시간 추정
def nowcast(levels: dict[str, pd.Series], esi: pd.Series | None, target: pd.Period,
            bridge: dict, cfg_model: dict, info_end: pd.Period | None = None,
            ar_end: pd.Period | None = None) -> dict:
    """target 분기 추정. info_end가 있으면 그 월까지 데이터만 사용(백테스트용)."""
    through = target.asfreq("M", "end")
    excl = (cfg_model["exclude_months_from"], cfg_model["exclude_months_to"])
    xs, months, ars = {}, {}, {}
    esi_use = esi if info_end is None or esi is None else esi[esi.index <= min(info_end + 1, through)]
    for k in bridge["coef"]:
        lv = levels[k].dropna()
        if info_end is not None:
            lv = lv[lv.index <= info_end]
        ar = fit_ar(lv, esi_use, cfg_model["ar_lags"], cfg_model["ar_start"], excl, end=ar_end)
        ext, filled = fill_ragged(lv, through, ar, esi_use)
        g = q_growth(ext)
        xs[k] = float(g.get(target, np.nan))
        months[k] = (ext, filled)
        ars[k] = ar
    value = predict(bridge, xs)
    return {"value": value, "x": xs, "months": months, "ar": ars}


def months_known(level: pd.Series, target: pd.Period) -> int:
    """target 분기 중 실제 발표된 월 수 (0~3)."""
    m = target.asfreq("M", "start")
    return int(sum((m + i) in level.dropna().index for i in range(3)))


# ---------------------------------------------------------------- 백테스트
def backtest(levels: dict[str, pd.Series], esi, gdp: pd.Series, cfg_model: dict,
             bridge_keys: list[str], anchor: str = "ip") -> dict:
    """의사 실시간(최종 통계 사용) 백테스트: 분기 내 k개월 정보(k=0..3)별 RMSE.
    한계: 데이터 개정 이력(빈티지)은 반영 못 함 → 실제 실시간 오차보다 작게 나옴."""
    start = pd.Period(cfg_model["backtest_start"], "Q")
    excl = {pd.Period(q, "Q") for q in cfg_model["exclude_quarters"]}
    last_q = gdp.dropna().index.max()
    X_full = pd.DataFrame({k: q_growth(levels[k]) for k in bridge_keys})
    errs = {k: [] for k in range(4)}
    ar_err, rows = [], []
    q = start
    while q <= last_q:
        if q in excl or (q - 1) in excl:
            q += 1
            continue
        # 브리지: q-1까지 / AR 벤치마크(GDP AR(1))
        br = fit_bridge(gdp, X_full, cfg_model["bridge_start"], cfg_model["exclude_quarters"], end=q - 1)
        g = gdp[(gdp.index < q) & (gdp.index >= pd.Period(cfg_model["bridge_start"], "Q"))]
        g = g[~g.index.isin(excl)]
        yy, yl = g.values[1:], g.shift(1).values[1:]
        ok = ~np.isnan(yl)
        A = np.column_stack([np.ones(ok.sum()), yl[ok]])
        b = np.linalg.lstsq(A, yy[ok], rcond=None)[0]
        ar_fc = b[0] + b[1] * gdp[q - 1]
        ar_err.append(gdp[q] - ar_fc)
        row = {"q": str(q), "actual": round(float(gdp[q]), 2), "ar": round(float(ar_fc), 2)}
        for k in range(4):
            info_end = q.asfreq("M", "start") + (k - 1)  # k=0 → 전분기 말월까지
            nc = nowcast(levels, esi, q, br, cfg_model, info_end=info_end,
                         ar_end=info_end)
            errs[k].append(gdp[q] - nc["value"])
            row[f"k{k}"] = round(float(nc["value"]), 2)
        rows.append(row)
        q += 1
    rmse = lambda e: float(np.sqrt(np.mean(np.square(e)))) if e else None
    return {
        "rmse": {str(k): rmse(v) for k, v in errs.items()},
        "mae": {str(k): float(np.mean(np.abs(v))) if v else None for k, v in errs.items()},
        "ar_rmse": rmse(ar_err),
        "n": len(ar_err),
        "period": f"{start}–{last_q} (코로나 분기 제외)",
        "rows": rows,
    }
