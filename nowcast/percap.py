"""1인당 국민총소득(GNI, 달러) 당해 추정.

  당해 1인당 GNI($) = 전년 실적($) × (1 + 명목 GNI 증가율) ÷ (1 + 인구 증가율) × (전년 평균환율 ÷ 당해 평균환율)
  - 명목 GNI 증가율 ≈ 명목 GDP 연간 추정치(이 모형). 국외순수취요소소득 비중은 전년과 같다고 가정
  - 환율: 한은 원/달러 매매기준율 일별 평균. 당해 남은 영업일은 최근 5영업일 평균 유지
  - 전년 실적·인구: 한은 ECOS '주요지표(연간)'에서 이름으로 찾음(data/percap_codes.json 캐시)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .bop import Ecos

PREFER = ["200Y101"]   # 2.1.1.1 주요지표(연간지표) — 없으면 이름으로 탐색


def _pick(items: list[dict]) -> dict:
    """항목 목록 → {usd: 코드, krw: 코드, krw_unit, pop: 코드, pop_unit}."""
    out = {}
    for it in items:
        if (it.get("CYCLE") or "A") != "A":
            continue
        nm, unit, code = it.get("ITEM_NAME") or "", it.get("UNIT_NAME") or "", it.get("ITEM_CODE")
        if "1인당" in nm and ("국민총소득" in nm or "GNI" in nm):
            if "달러" in unit or "달러" in nm:
                out.setdefault("usd", code)
            elif "원" in unit or "원" in nm:
                out.setdefault("krw", code)
                out.setdefault("krw_unit", unit)
        elif "인구" in nm and "pop" not in out:
            out["pop"], out["pop_unit"] = code, unit
    return out


def discover(e: Ecos) -> dict | None:
    for stat in PREFER:
        try:
            p = _pick(e.items(stat))
        except Exception:  # noqa: BLE001
            p = {}
        if "usd" in p:
            return {"stat": stat, **p}
    for t in e.tables():
        nm = t.get("STAT_NAME") or ""
        if t.get("CYCLE") == "A" and ("주요지표" in nm or "1인당" in nm):
            try:
                p = _pick(e.items(t["STAT_CODE"]))
            except Exception:  # noqa: BLE001
                continue
            if "usd" in p:
                return {"stat": t["STAT_CODE"], **p}
    return None


def _annual(e: Ecos, stat: str, code: str, y0: int, y1: int) -> dict[int, float]:
    js = e._get("/".join(["StatisticSearch", e.key, "json", "kr", "1", "1000", stat, "A", str(y0), str(y1), code]))
    out = {}
    for row in (js.get("StatisticSearch") or {}).get("row") or []:
        v = row.get("DATA_VALUE")
        if v not in (None, "", "-"):
            out[int(str(row["TIME"])[:4])] = float(str(v).replace(",", ""))
    return out


def fx_avg(fx: pd.Series, year: int, now: datetime, recent: int = 5) -> tuple[float | None, float | None, str]:
    """(전년 평균, 당해 평균[남은 영업일은 최근 5영업일 평균 유지], 설명)."""
    if fx is None or fx.empty:
        return None, None, ""
    s = fx.dropna()
    s = s[(s > 300) & (s < 5000)]
    yr = s.index.year
    prev, cur = s[yr == year - 1], s[yr == year]
    if prev.empty or cur.empty:
        return None, None, ""
    rec = float(cur.tail(recent).mean())
    last = cur.index.max().to_timestamp()
    rest = len(pd.bdate_range(last + pd.Timedelta(days=1), f"{year}-12-31"))
    avg = (float(cur.sum()) + rec * rest) / (len(cur) + rest)
    note = (f"{year - 1}년 평균 {prev.mean():,.0f}원 → {year}년 {avg:,.0f}원"
            f"(실제 {len(cur)}영업일 평균 {cur.mean():,.0f}원 + 남은 {rest}영업일 최근 {rec:,.0f}원 가정)")
    return float(prev.mean()), avg, note


def section(key: str | None, data_dir: Path, now: datetime, year: int, nominal: float, nom_sd: float,
            fx: pd.Series | None, session=None) -> dict | None:
    if not key or nominal is None:
        return None
    e = Ecos(key, session)
    path = data_dir / "percap_codes.json"
    codes = json.loads(path.read_text()) if path.exists() else None
    data = {}
    for attempt in (0, 1):
        if not codes:
            codes = discover(e)
            if not codes:
                print("::warning::1인당 GNI 통계표를 못 찾음 — 건너뜀")
                return None
            path.write_text(json.dumps(codes, ensure_ascii=False, indent=1))
        try:
            data = {k: _annual(e, codes["stat"], codes[k], year - 21, year) for k in ("usd", "krw", "pop") if codes.get(k)}
        except Exception:  # noqa: BLE001
            data = {}
        if data.get("usd"):
            break
        codes = None   # 캐시 코드로 자료가 안 나옴 → 다시 찾기
    usd = data.get("usd") or {}
    if (year - 1) not in usd:
        print(f"::notice::1인당 GNI: {year - 1}년 실적 미발표 → 건너뜀")
        return None
    fp, fc, fx_note = fx_avg(fx, year, now)
    if not fp:
        return None
    pop = data.get("pop") or {}
    pop_g = (pop[year] / pop[year - 1] - 1) if (year in pop and (year - 1) in pop) else 0.0
    base = usd[year - 1]
    calc = lambda g: base * (1 + g / 100) / (1 + pop_g) * fp / fc
    v, lo, hi = calc(nominal), calc(nominal - nom_sd), calc(nominal + nom_sd)
    out = {"year": year, "usd": round(v), "lo": round(lo), "hi": round(hi),
           "prev_year": year - 1, "prev_usd": round(base),
           "hist": [{"y": y, "usd": round(u)} for y, u in sorted(usd.items()) if y < year],
           "growth_usd": round((v / base - 1) * 100, 1), "nominal": round(nominal, 2),
           "pop_g": round(pop_g * 100, 2), "pop_src": "한은 주요지표 추계인구" if year in pop else "가정(전년과 같음)",
           "fx_prev": round(fp, 1), "fx_cur": round(fc, 1), "fx_note": fx_note,
           "fx_effect": round((fp / fc - 1) * 100, 1), "src": f"한은 ECOS {codes['stat']}"}
    krw = data.get("krw") or {}
    if (year - 1) in krw:
        out["krw"] = round(krw[year - 1] * (1 + nominal / 100) / (1 + pop_g), 1)
        out["krw_unit"] = codes.get("krw_unit") or ""
        out["prev_krw"] = krw[year - 1]
    print(f"::notice::{year}년 1인당 GNI ≈ ${out['usd']:,} (범위 {out['lo']:,}~{out['hi']:,}, "
          f"{year - 1}년 ${out['prev_usd']:,}) · 명목 {nominal:+.1f}%, 환율 효과 {out['fx_effect']:+.1f}%, 인구 {out['pop_g']:+.2f}%")
    return out
