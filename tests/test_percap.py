"""1인당 GNI 추정 — ECOS 응답 흉내(네트워크 없음)."""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import percap  # noqa: E402

ITEMS = [{"ITEM_CODE": "10101", "ITEM_NAME": "국내총생산(명목, 원화표시)", "CYCLE": "A", "UNIT_NAME": "십억원"},
         {"ITEM_CODE": "1010301", "ITEM_NAME": "1인당 국민총소득(명목, 원화표시)", "CYCLE": "A", "UNIT_NAME": "만원"},
         {"ITEM_CODE": "1010302", "ITEM_NAME": "1인당 국민총소득(명목, 달러표시)", "CYCLE": "A", "UNIT_NAME": "달러"},
         {"ITEM_CODE": "10107", "ITEM_NAME": "추계인구", "CYCLE": "A", "UNIT_NAME": "천명"}]
VAL = {"1010302": {2024: 36624, 2025: 37000}, "1010301": {2024: 4995, 2025: 5300},
       "10107": {2024: 51750, 2025: 51700, 2026: 51648.3}}


class R:
    def __init__(self, js):
        self.js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self.js


class S:
    def get(self, url, timeout=None):
        p = url.split("/")
        if "StatisticItemList" in url:
            return R({"StatisticItemList": {"row": ITEMS if p[-1] == "200Y101" else []}})
        if "StatisticTableList" in url:
            return R({"StatisticTableList": {"row": []}})
        code = p[-1]
        return R({"StatisticSearch": {"row": [{"TIME": str(y), "DATA_VALUE": str(v)} for y, v in VAL.get(code, {}).items()]}})


def test():
    d = Path(tempfile.mkdtemp())
    idx = pd.period_range("2025-01-01", "2026-11-17", freq="D")
    idx = idx[[p.dayofweek < 5 for p in idx]]
    fx = pd.Series([1400.0 if p.year == 2025 else 1350.0 for p in idx], index=idx)
    o = percap.section("K", d, datetime(2026, 11, 18), 2026, 24.6, 0.8, fx, session=S())
    codes = json.loads((d / "percap_codes.json").read_text())
    assert codes["usd"] == "1010302" and codes["pop"] == "10107", codes
    exp = 37000 * 1.246 / (51648.3 / 51700) * 1400 / 1350
    assert abs(o["usd"] - exp) < 2 and o["lo"] < o["usd"] < o["hi"] and o["prev_usd"] == 37000, o
    assert abs(o["fx_effect"] - (1400 / 1350 - 1) * 100) < 0.1 and o["pop_g"] < 0
    assert percap.section(None, d, datetime(2026, 11, 18), 2026, 24.6, 0.8, fx) is None
    print("percap OK", o["usd"])


if __name__ == "__main__":
    test()
