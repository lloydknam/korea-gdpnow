"""세계은행 1인당 GNI — 가짜 응답(네트워크 없음)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import world_gni as wg  # noqa: E402

GNI = {"JPN": 4.4e12, "CHN": 1.8e13, "USA": 2.9e13, "KOR": 1.9e12}
POP = {"JPN": 1.24e8, "CHN": 1.41e9, "USA": 3.4e8, "KOR": 5.17e7}


class R:
    def __init__(self, js):
        self.js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self.js


class S:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        src = GNI if "NY.GNP" in url else POP
        y0, y1 = map(int, params["date"].split(":"))
        rows = [{"countryiso3code": c, "date": str(y), "value": v * (1 + 0.03 * (y - 2005)) if y < 2026 else None}
                for c, v in src.items() for y in range(y0, y1 + 1)]
        return R([{"page": 1}, rows])


def test():
    d = Path(tempfile.mkdtemp())
    s = S()
    o = wg.section(d, datetime(2026, 11, 18), sess=s)
    assert s.calls == 2 and set(o["countries"]) == {"JPN", "CHN", "USA", "KOR"}, o
    us = o["countries"]["USA"]["pts"]
    assert us[0]["y"] == 2005 and us[-1]["y"] == 2025 and abs(us[0]["v"] - 2.9e13 / 3.4e8) < 1, us[:2]
    s2 = S()
    wg.section(d, datetime(2026, 11, 20), sess=s2)
    assert s2.calls == 0                      # 7일 안엔 다시 안 받음
    print("world_gni OK")


if __name__ == "__main__":
    test()
