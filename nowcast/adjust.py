"""수출물량 계절·영업일 조정, 관세청 속보 반영."""
from __future__ import annotations

import calendar
from datetime import date
from functools import lru_cache

import holidays
import numpy as np
import pandas as pd
from statsmodels.tsa.seasonal import STL


@lru_cache(maxsize=None)
def _kr_holidays(year: int) -> frozenset:
    return frozenset(holidays.KR(years=year).keys())


def workdays(year: int, month: int, upto_day: int | None = None, customs: bool = True) -> float:
    """영업일수. customs=True면 관세청 기준(토요일 0.5일)."""
    last = calendar.monthrange(year, month)[1]
    n = last if upto_day is None else min(upto_day, last)
    hol = _kr_holidays(year)
    wd = 0.0
    for d in range(1, n + 1):
        dt = date(year, month, d)
        if dt in hol:
            continue
        wk = dt.weekday()
        if wk < 5:
            wd += 1
        elif wk == 5 and customs:
            wd += 0.5
    return wd


def seasonal_adjust(nsa: pd.Series) -> pd.Series:
    """log 수준에 STL(12) → 잔차를 영업일 편차에 회귀해 제거. 반환: 계절·영업일 조정 수준."""
    s = nsa.dropna()
    y = np.log(s.values)
    idx = s.index
    wd = np.array([workdays(p.year, p.month, customs=False) for p in idx])
    month = np.array([p.month for p in idx])
    wd_dev = wd - pd.Series(wd).groupby(month).transform("mean").values

    stl = STL(pd.Series(y, index=idx.to_timestamp()), period=12, robust=True).fit()
    resid = stl.resid.values
    X = np.column_stack([np.ones_like(wd_dev), wd_dev])
    beta = np.linalg.lstsq(X, resid, rcond=None)[0]
    adj = y - stl.seasonal.values - beta[1] * wd_dev
    return pd.Series(np.exp(adj), index=idx)


PERIOD_DAY = {"d10": 10, "d20": 20, "m": None}
PERIOD_RANK = {"d10": 0, "d20": 1, "m": 2}


def load_customs(path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, comment="#", dtype={"ref_month": str, "period": str})
    except FileNotFoundError:
        return pd.DataFrame()
    df = df.dropna(subset=["ref_month", "period", "exports_yoy_pct"])
    if df.empty:
        return df
    df["period"] = df["period"].str.strip().str.lower()
    df["rank"] = df["period"].map(PERIOD_RANK)
    df["ref"] = pd.PeriodIndex(df["ref_month"].str.strip(), freq="M")
    return df


def customs_full_month_yoy(row) -> float:
    """관세청 속보(누적 전년동기비)를 월 전체 전년동월비로 환산. 조업일수 차이 보정."""
    yoy = float(row["exports_yoy_pct"]) / 100.0
    p = row["ref"]
    day = PERIOD_DAY[row["period"]]
    if day is None:
        return yoy
    wd_cur = row.get("workdays")
    wd_prev = row.get("workdays_prev")
    if pd.isna(wd_cur) or pd.isna(wd_prev):
        wd_cur = workdays(p.year, p.month, day)
        wd_prev = workdays(p.year - 1, p.month, day)
    daily = (1 + yoy) * float(wd_prev) / float(wd_cur)
    full_ratio = workdays(p.year, p.month) / workdays(p.year - 1, p.month)
    return daily * full_ratio - 1


def extend_with_customs(vol_nsa: pd.Series, px_usd: pd.Series, flash: pd.DataFrame):
    """수출물량지수(원계열)가 없는 월을 관세청 속보 금액 증가율 ÷ 수출물가(달러) 증가율로 보강."""
    used = []
    if flash.empty or vol_nsa.empty:
        return vol_nsa, used
    last = vol_nsa.index.max()
    out = vol_nsa.copy()
    px_yoy_all = (px_usd / px_usd.shift(12) - 1).dropna()
    for ref in sorted(flash.loc[flash["ref"] > last, "ref"].unique()):
        if ref != out.index.max() + 1:  # 연속된 월만
            break
        cand = flash[flash["ref"] == ref].sort_values(["rank"])
        row = cand.iloc[-1]
        val_yoy = customs_full_month_yoy(row)
        px_yoy = px_yoy_all.get(ref, px_yoy_all.iloc[-1] if len(px_yoy_all) else 0.0)
        base = out.get(ref - 12)
        if base is None or np.isnan(base):
            break
        vol = base * (1 + val_yoy) / (1 + px_yoy)
        out.loc[ref] = vol
        out = out.sort_index()
        used.append({
            "month": str(ref), "period": row["period"],
            "value_yoy": round(val_yoy * 100, 1),
            "price_yoy": round(float(px_yoy) * 100, 1),
            "volume_yoy": round(((1 + val_yoy) / (1 + px_yoy) - 1) * 100, 1),
            "price_assumed": ref not in px_yoy_all.index,
        })
    return out, used
