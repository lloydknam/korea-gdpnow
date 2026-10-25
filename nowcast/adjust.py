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


# 속보 반영 비중: 기간이 짧을수록 잡음 큼 → 나머지는 직전 달 물량 증가율(추세)로 채움
FLASH_WEIGHT = {"d10": 0.5, "d20": 0.8, "m": 1.0}
WORKDAY_GAP_PENALTY = 0.5   # 누적 기간 조업일수가 작년과 2일 이상 차이(명절 이동) → 비중 절반


def flash_weight(row) -> float:
    w = FLASH_WEIGHT.get(row["period"], 1.0)
    day = PERIOD_DAY.get(row["period"])
    if day is not None:
        p = row["ref"]
        if abs(workdays(p.year, p.month, day) - workdays(p.year - 1, p.month, day)) >= 2:
            w *= WORKDAY_GAP_PENALTY
    return w


CHIP_W_DEFAULT = 0.19   # 수출물가지수 2020년 가중치에서 반도체 비중 근사


def chip_weight(px_usd: pd.Series, chip_px: pd.Series, months: int = 36) -> float:
    """총지수 ≈ w·반도체 + (1−w)·비반도체(지수 수준, 라스파이레스). 비반도체 월간 변동이 가장 매끄러운 w를 고름."""
    j = pd.concat([px_usd.rename("t"), chip_px.rename("c")], axis=1).dropna().tail(months + 1)
    if len(j) < 13:
        return CHIP_W_DEFAULT
    best, best_sd = CHIP_W_DEFAULT, np.inf
    for w in np.arange(0.05, 0.401, 0.01):
        n = (j["t"] - w * j["c"]) / (1 - w)
        if (n <= 0).any():
            continue
        sd = float(np.log(n).diff().std())
        if sd < best_sd:
            best, best_sd = float(w), sd
    return best


def price_yoy_nowcast(px_usd: pd.Series, chip_px: pd.Series | None, ref: pd.Period, w: float | None = None):
    """미발표 달의 수출물가(달러) 전년동월비. 반도체·비반도체를 나눠 각자 최근 3개월 추세로 연장.
    반환: (전년동월비, 방법). 발표된 달이면 실적 그대로."""
    px = px_usd.dropna()
    if ref in px.index and (ref - 12) in px.index:
        return float(px[ref] / px[ref - 12] - 1), "actual"
    last = px.index.max()
    if chip_px is None or chip_px.dropna().empty or (ref - 12) not in px.index:
        yoy = (px / px.shift(12) - 1).dropna()
        return (float(yoy.iloc[-1]) if len(yoy) else 0.0), "last"
    c = chip_px.dropna()
    t0 = min(last, c.index.max())
    w = chip_weight(px, c) if w is None else w
    n = (px - w * c.reindex(px.index)) / (1 - w)
    gc = float(np.log(c[c.index <= t0]).diff().tail(3).mean())
    gn = float(np.log(n[n.index <= t0].dropna()).diff().tail(3).mean())
    k = (ref - t0).n
    c_ref = c[t0] * np.exp(gc * k)
    n_ref = n[t0] * np.exp(gn * k)
    t_ref = w * c_ref + (1 - w) * n_ref
    return float(t_ref / px[ref - 12] - 1), f"split(w={w:.2f})"


def extend_with_customs(vol_nsa: pd.Series, px_usd: pd.Series, flash: pd.DataFrame, chip_px: pd.Series | None = None):
    """수출물량지수(원계열)가 없는 월을 관세청 속보 금액 증가율 ÷ 수출물가(달러) 증가율로 보강.
    물량 증가율 = w × 속보 환산치 + (1−w) × 직전 달 물량 증가율 (w: 10일 0.5 / 20일 0.8 / 월 1.0, 명절 이동 시 절반)."""
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
        # 수출물가 미발표 달: 반도체·비반도체 각자의 최근 추세로 연장(반도체 단가 급등기에 '직전 달 그대로'보다 정확)
        px_yoy, px_how = price_yoy_nowcast(px_usd, chip_px, ref)
        base, prev, prev12 = out.get(ref - 12), out.get(ref - 1), out.get(ref - 13)
        if base is None or np.isnan(base):
            break
        flash_vol = (1 + val_yoy) / (1 + px_yoy) - 1
        prior = prev / prev12 - 1 if prev is not None and prev12 is not None and prev12 > 0 else flash_vol
        w = flash_weight(row)
        vol_yoy = w * flash_vol + (1 - w) * prior
        out.loc[ref] = base * (1 + vol_yoy)
        out = out.sort_index()
        used.append({
            "month": str(ref), "period": row["period"],
            "value_yoy": round(val_yoy * 100, 1),
            "price_yoy": round(float(px_yoy) * 100, 1),
            "flash_volume_yoy": round(float(flash_vol) * 100, 1),
            "prior_yoy": round(float(prior) * 100, 1),
            "weight": round(w, 2),
            "volume_yoy": round(float(vol_yoy) * 100, 1),
            "price_assumed": ref not in px_yoy_all.index,
            "price_method": px_how,
            "price_yoy_naive": round(float(px_yoy_all.iloc[-1]) * 100, 1) if len(px_yoy_all) else None,
        })
    return out, used
