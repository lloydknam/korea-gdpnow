"""한국은행 ECOS Open API 클라이언트."""
from __future__ import annotations

import time

import pandas as pd
import requests

BASE = "https://ecos.bok.or.kr/api"


class EcosError(RuntimeError):
    pass


def _to_period(t: str, cycle: str) -> pd.Period:
    if cycle == "M":
        return pd.Period(f"{t[:4]}-{t[4:6]}", freq="M")
    if cycle == "Q":
        return pd.Period(t, freq="Q")
    if cycle == "A":
        return pd.Period(t, freq="Y")
    raise ValueError(cycle)


def fetch(key: str, stat: str, cycle: str, start: str, end: str, items: list[str],
          retries: int = 3, session: requests.Session | None = None) -> pd.Series:
    """ECOS StatisticSearch → PeriodIndex Series (float). 데이터 없으면 빈 Series."""
    path = "/".join([BASE, "StatisticSearch", key, "json", "kr", "1", "100000",
                     stat, cycle, start, end, *[str(i) for i in items]])
    s = session or requests
    last_err = None
    for attempt in range(retries):
        try:
            r = s.get(path, timeout=30)
            r.raise_for_status()
            js = r.json()
            break
        except Exception as e:  # 네트워크·JSON 오류 재시도
            last_err = e
            time.sleep(2 * (attempt + 1))
    else:
        raise EcosError(f"{stat}/{items}: {last_err}")

    if "RESULT" in js:
        code = js["RESULT"].get("CODE", "")
        if code == "INFO-200":  # 해당 데이터 없음
            return pd.Series(dtype=float)
        raise EcosError(f"{stat}/{items}: {code} {js['RESULT'].get('MESSAGE')}")

    rows = js.get("StatisticSearch", {}).get("row", [])
    idx, vals = [], []
    for row in rows:
        v = row.get("DATA_VALUE")
        if v in (None, "", "-"):
            continue
        idx.append(_to_period(row["TIME"], cycle))
        vals.append(float(str(v).replace(",", "")))
    out = pd.Series(vals, index=pd.PeriodIndex(idx), dtype=float).sort_index()
    return out[~out.index.duplicated(keep="last")]
