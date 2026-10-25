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


TREND_DECAY = 0.7   # 추세 연장 시 달마다 기울기를 70%로 줄임(먼 달일수록 보수적)


def extend_split(total: pd.Series, chip: pd.Series | None, through: pd.Period, w: float | None = None,
                 months: int = 3, decay: float = TREND_DECAY):
    """물가 총지수를 through 월까지 연장. 총지수 ≈ w·반도체 + (1−w)·비반도체(지수 수준)로 나눠
    각자 최근 months개월 로그 추세를 감쇠시키며 연장 → 합침. 반환: (연장된 총지수, 정보 dict)."""
    t = total.dropna()
    last = t.index.max()
    if last >= through:
        return t, {"method": "actual", "added": 0}
    if chip is None or chip.dropna().empty:
        return t, {"method": "none", "added": 0}
    c = chip.dropna()
    t0 = min(last, c.index.max())
    w = chip_weight(t, c) if w is None else w
    n = ((t - w * c.reindex(t.index)) / (1 - w)).dropna()
    n = n[n > 0]
    gc = float(np.log(c[c.index <= t0]).diff().tail(months).mean())
    gn = float(np.log(n[n.index <= t0]).diff().tail(months).mean())
    out = t[t.index <= t0].copy()
    lc, ln, cum = c[t0], n[t0], 0.0
    p, k = t0 + 1, 1
    while p <= through:
        cum += decay ** (k - 1)
        out.loc[p] = w * lc * np.exp(gc * cum) + (1 - w) * ln * np.exp(gn * cum)
        p, k = p + 1, k + 1
    return out.sort_index(), {"method": f"split(w={w:.2f})", "w": w, "chip_trend": gc * 100, "other_trend": gn * 100,
                              "added": k - 1, "from": str(t0 + 1), "to": str(through)}


def extend_krw_by_fx(krw: pd.Series, usd: pd.Series, chip_usd: pd.Series | None, fx_daily: pd.Series,
                     through: pd.Period, recent_days: int = 5):
    """원화 가격지수 미발표 달 = 달러 가격지수(반도체·기타 추세 연장) × 환율.
    환율: 원화÷달러 지수 비율(실효 환율)을 원/달러 월평균에 맞춘 뒤(k = F/E, 마지막 실적 달),
    미발표 달은 실제 원/달러 월평균(이번 달은 지금까지 평균), 아직 안 온 달은 최근 5영업일 평균 유지."""
    t = krw.dropna()
    last = t.index.max()
    if last >= through:
        return t, {"method": "actual", "added": 0}
    d_ext, info = extend_split(usd, chip_usd, through)
    fx = fx_daily.dropna()
    fx = fx[(fx > 300) & (fx < 5000)]              # 원/달러 범위 밖 값은 버림
    em = fx.groupby(fx.index.asfreq("M")).mean()
    em.index = pd.PeriodIndex(em.index, freq="M")
    F = (t / usd.reindex(t.index)).dropna()
    if last not in em.index or last not in F.index:
        return t, {"method": "fx-missing", "added": 0}
    k = F[last] / em[last]
    cur = float(fx.tail(recent_days).mean())
    out = t.copy()
    used = {}
    p = last + 1
    while p <= through:
        e = float(em[p]) if p in em.index else cur
        used[str(p)] = round(e, 1)
        out.loc[p] = d_ext[p] * k * e
        p += 1
    e0 = float(em[last])
    parts = [f"{int(m[5:])}월 {v:,.0f}원" + ("(지금까지 평균)" if pd.Period(m, "M") == fx.index.max().asfreq("M") else "")
             for m, v in used.items() if pd.Period(m, "M") in em.index]
    tail = f", 이후 최근 {recent_days}영업일 {cur:,.0f}원 유지" if any(pd.Period(m, "M") not in em.index for m in used) else ""
    info.update({"fx_base": round(e0, 1), "fx_used": used, "fx_recent": round(cur, 1), "fx_k": round(float(k), 4),
                 "fx_text": f"{last.month}월 {e0:,.0f}원 → " + ", ".join(parts) + tail,
                 "added": len(used), "from": str(last + 1), "to": str(through)})
    return out.sort_index(), info


def price_yoy_nowcast(px_usd: pd.Series, chip_px: pd.Series | None, ref: pd.Period, w: float | None = None):
    """미발표 달의 수출물가(달러) 전년동월비. 반도체·비반도체를 나눠 각자 최근 추세(감쇠)로 연장.
    반환: (전년동월비, 방법). 발표된 달이면 실적 그대로."""
    px = px_usd.dropna()
    if ref in px.index and (ref - 12) in px.index:
        return float(px[ref] / px[ref - 12] - 1), "actual"
    if chip_px is None or chip_px.dropna().empty or (ref - 12) not in px.index:
        yoy = (px / px.shift(12) - 1).dropna()
        return (float(yoy.iloc[-1]) if len(yoy) else 0.0), "last"
    ext, info = extend_split(px, chip_px, ref, w)
    return float(ext[ref] / px[ref - 12] - 1), info["method"]


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
