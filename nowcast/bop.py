"""경상수지 — 한국(월별) + IMF 주요국 비교(한국·중국·독일·일본, 분기).

ECOS 통계코드는 고정하지 않음: 실행 때 통계표 목록에서 이름으로 찾아 data/bop_codes.json에 저장(캐시).
캐시한 코드로 자료가 안 나오면 다시 찾음 → 한은이 표 번호를 바꿔도 코드 수정 없이 따라감.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE = "https://ecos.bok.or.kr/api"
COUNTRY = {"KR": ("한국", "대한민국", "Korea"), "CN": ("중국", "China"), "DE": ("독일", "Germany"), "JP": ("일본", "Japan")}


def _rows(js: dict, name: str) -> list[dict]:
    return (js.get(name) or {}).get("row") or []


class Ecos:
    def __init__(self, key: str, session=None):
        self.key, self.s = key, session or requests.Session()

    def _get(self, path: str) -> dict:
        r = self.s.get(f"{BASE}/{path}", timeout=40)
        r.raise_for_status()
        return r.json()

    def tables(self) -> list[dict]:
        return _rows(self._get(f"StatisticTableList/{self.key}/json/kr/1/10000/"), "StatisticTableList")

    def items(self, stat: str) -> list[dict]:
        return _rows(self._get(f"StatisticItemList/{self.key}/json/kr/1/5000/{stat}"), "StatisticItemList")

    def search(self, stat: str, cycle: str, start: str, end: str, items: list[str]) -> pd.Series:
        js = self._get("/".join(["StatisticSearch", self.key, "json", "kr", "1", "100000", stat, cycle, start, end, *items]))
        idx, val = [], []
        for row in _rows(js, "StatisticSearch"):
            v = row.get("DATA_VALUE")
            if v in (None, "", "-"):
                continue
            t = row["TIME"]
            p = pd.Period(f"{t[:4]}-{t[4:6]}", "M") if cycle == "M" else pd.Period(t, "Q")
            idx.append(p)
            val.append(float(str(v).replace(",", "")))
        return pd.Series(val, index=pd.PeriodIndex(idx), dtype=float).sort_index() if idx else pd.Series(dtype=float)


def _scale(unit: str | None) -> float:
    """→ 억달러."""
    u = unit or ""
    if "백만" in u:
        return 0.01
    if "십억" in u:
        return 10.0
    if "천" in u and "달러" in u:
        return 1e-5
    return 1.0


def _match(name: str, words) -> bool:
    n = name.replace(" ", "")
    return any(w in n for w in words)


def discover(ec: Ecos) -> dict:
    """이름으로 표·항목 찾기. 반환: {"imf": {...} | None, "kr": {...} | None, "candidates": [...]}"""
    tabs = ec.tables()
    cand = [t for t in tabs if "경상수지" in t.get("STAT_NAME", "") or "국제수지" in t.get("STAT_NAME", "")]
    out = {"candidates": [{"stat": t["STAT_CODE"], "name": t["STAT_NAME"], "cycle": t.get("CYCLE")} for t in cand][:40],
           "imf": None, "kr": None}
    for t in cand:
        nm = t["STAT_NAME"]
        try:
            its = ec.items(t["STAT_CODE"])
        except Exception:  # noqa: BLE001
            continue
        groups = sorted({i.get("GRP_CODE") for i in its})
        # IMF 주요국: 국가 항목이 한국·중국·독일·일본 모두 있고 분기 자료
        if out["imf"] is None and ("주요국" in nm or "국제" in nm or "국가" in nm):
            q = [i for i in its if i.get("CYCLE") == "Q"]
            codes = {}
            for cc, words in COUNTRY.items():
                hit = [i for i in q if _match(i["ITEM_NAME"], words)]
                if hit:
                    codes[cc] = hit[0]
            if len(codes) == 4:
                extra = []
                g0 = codes["KR"].get("GRP_CODE")
                others = [g for g in groups if g != g0]
                if others:  # 국가 × 지표 2차원 표면 '경상수지' 항목을 함께 지정
                    ca = [i for i in its if i.get("GRP_CODE") == others[0] and "경상수지" in i["ITEM_NAME"]]
                    if not ca:
                        continue
                    extra = [ca[0]["ITEM_CODE"]]
                out["imf"] = {"stat": t["STAT_CODE"], "name": nm, "unit": codes["KR"].get("UNIT_NAME"),
                              "items": {cc: [i["ITEM_CODE"], *extra] for cc, i in codes.items()},
                              "start": codes["KR"].get("START_TIME")}
        # 한국 월별 국제수지: '경상수지' 항목, 월 자료
        if out["kr"] is None and "주요국" not in nm and "국제수지" in nm:
            m = [i for i in its if i.get("CYCLE") == "M" and i["ITEM_NAME"].replace(" ", "") in ("경상수지", "경상수지(A)", "1.경상수지")]
            if m and len(groups) == 1:
                out["kr"] = {"stat": t["STAT_CODE"], "name": nm, "unit": m[0].get("UNIT_NAME"), "item": m[0]["ITEM_CODE"]}
        if out["imf"] and out["kr"]:
            break
    return out


def section(key: str | None, data_dir: Path, now, session=None) -> dict | None:
    if not key:
        return None
    ec = Ecos(key, session)
    cache = data_dir / "bop_codes.json"
    codes = json.loads(cache.read_text()) if cache.exists() else None

    def load(c):
        res = {}
        if c and c.get("imf"):
            imf, sc = c["imf"], _scale(c["imf"].get("unit"))
            q = {cc: ec.search(imf["stat"], "Q", "2015Q1", f"{now.year}Q4", it) * sc for cc, it in imf["items"].items()}
            if all(not s.empty for s in q.values()):
                res["imf"] = pd.DataFrame(q).dropna(how="all")
        if c and c.get("kr"):
            kr = c["kr"]
            s = ec.search(kr["stat"], "M", "201501", f"{now.year}{now.month:02d}", [kr["item"]]) * _scale(kr.get("unit"))
            if not s.empty:
                res["kr"] = s
        return res

    data = load(codes)
    if len(data) < 2:  # 캐시 없음·표 변경 → 다시 찾기
        codes = discover(ec)
        cache.write_text(json.dumps(codes, ensure_ascii=False, indent=1))
        data = load(codes)
    if not data:
        return None
    out = {"source": {k: codes[k]["name"] for k in ("imf", "kr") if codes.get(k)}}
    if "imf" in data:
        df = data["imf"].tail(12)
        out["quarters"] = [{"q": str(q), **{c: (None if pd.isna(v[c]) else round(float(v[c]), 1)) for c in COUNTRY}}
                           for q, v in df.iterrows()]
    if "kr" in data:
        s = data["kr"]
        out["kr_monthly"] = [{"m": str(p), "v": round(float(v), 1)} for p, v in s.tail(15).items()]
        last = s.index.max()
        ytd = s[(s.index.year == last.year)]
        prev = s[(s.index.year == last.year - 1) & (s.index.month <= last.month)]
        out["kr_ytd"] = {"year": last.year, "through": last.month, "v": round(float(ytd.sum()), 1),
                         "prev": round(float(prev.sum()), 1) if len(prev) else None}
    return out
