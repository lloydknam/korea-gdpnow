"""주요국 1인당 국민총소득(GNI, 경상 달러) — 세계은행 API.

  1인당 GNI = GNI(경상 달러, NY.GNP.MKTP.CD) ÷ 인구(SP.POP.TOTL)
  - 한은 방식(원화 GNI ÷ 연평균 환율 ÷ 인구)과 같은 개념. 세계은행 'Atlas 방식'(3년 평균 환율)은 쓰지 않음
  - 일본·중국·미국(+ 비교용 한국). 한국 본 선은 한은 ECOS 값 사용
  - 연간 자료라 7일에 한 번만 받음 → data/world_gni.csv, 실패하면 기존 파일로 계속
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

URL = "https://api.worldbank.org/v2/country/{c}/indicator/{i}"
COUNTRIES = {"JPN": "일본", "CHN": "중국", "USA": "미국", "KOR": "한국"}
IND = {"gni": "NY.GNP.MKTP.CD", "pop": "SP.POP.TOTL"}
REFRESH_DAYS = 7


def _get(sess, url: str, params: dict, tries: int = 2):
    """세계은행 API는 가끔 매우 느림 → 나라별 작은 요청 + 넉넉한 시간 + 재시도."""
    last = None
    for k in range(tries):
        try:
            r = sess.get(url, params=params, timeout=(10, 60))
            r.raise_for_status()
            js = r.json()
            if not isinstance(js, list) or len(js) < 2 or not js[1]:
                raise ValueError(f"세계은행 응답 형식 이상: {str(js)[:120]}")
            return js[1]
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(3 * (k + 1))
    raise last


def _fetch(sess, ind: str, y0: int, y1: int) -> dict[tuple[str, int], float]:
    rows = []
    for c in COUNTRIES:
        rows += _get(sess, URL.format(c=c, i=ind), {"date": f"{y0}:{y1}", "format": "json", "per_page": 100})
    out = {}
    for row in rows:
        v, c = row.get("value"), row.get("countryiso3code") or (row.get("country") or {}).get("id")
        if v is not None and c in COUNTRIES:
            out[(c, int(row["date"]))] = float(v)
    return out


def update(path: Path, now: datetime, y0: int, sess=None) -> pd.DataFrame:
    cache = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["country", "year", "gni_usd", "pop", "fetched"])
    if not cache.empty and "fetched" in cache:
        last = pd.to_datetime(cache["fetched"]).max()
        if pd.notna(last) and (pd.Timestamp(now.replace(tzinfo=None)) - last).days < REFRESH_DAYS:
            return cache
    sess = sess or requests.Session()
    try:
        g = _fetch(sess, IND["gni"], y0, now.year)
        p = _fetch(sess, IND["pop"], y0, now.year)
    except (requests.RequestException, ValueError) as e:
        print(f"::warning::세계은행 1인당 GNI 조회 실패 — {type(e).__name__} {e} (기존 파일 사용)")
        return cache
    rows = [{"country": c, "year": y, "gni_usd": v, "pop": p[(c, y)], "fetched": now.strftime("%Y-%m-%d")}
            for (c, y), v in sorted(g.items()) if (c, y) in p]
    if not rows:
        return cache
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"::notice::세계은행 1인당 GNI: {df['year'].min()}~{df['year'].max()}년 {df['country'].nunique()}개국 갱신")
    return df


def section(data_dir: Path, now: datetime, years: int = 21, sess=None) -> dict | None:
    y0 = now.year - years
    df = update(data_dir / "world_gni.csv", now, y0, sess)
    if df.empty:
        return None
    df = df[df["year"] >= y0]
    out = {}
    for c, name in COUNTRIES.items():
        d = df[df["country"] == c].sort_values("year")
        if not d.empty:
            out[c] = {"name": name, "pts": [{"y": int(r.year), "v": round(r.gni_usd / r.pop)} for r in d.itertuples()]}
    return {"countries": out, "src": "세계은행 WDI (GNI 경상 달러 ÷ 인구)"} if out else None
