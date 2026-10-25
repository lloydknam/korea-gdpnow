"""경상수지 통계표 자동 찾기 — ECOS 응답을 흉내 낸 가짜 세션으로 검증(네트워크 없음)."""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import bop  # noqa: E402

TABLES = [{"STAT_CODE": "111Y111", "STAT_NAME": "1.1 무관한 표", "CYCLE": "M"},
          {"STAT_CODE": "301Y999", "STAT_NAME": "6.1.1 국제수지", "CYCLE": "M"},
          {"STAT_CODE": "902Y999", "STAT_NAME": "9.1.4 국제 주요국 경상수지", "CYCLE": "Q"}]
ITEMS = {
    "301Y999": [{"GRP_CODE": "Group1", "ITEM_CODE": "000000", "ITEM_NAME": "경상수지", "CYCLE": "M", "UNIT_NAME": "백만달러"},
                {"GRP_CODE": "Group1", "ITEM_CODE": "100000", "ITEM_NAME": "상품수지", "CYCLE": "M", "UNIT_NAME": "백만달러"}],
    "902Y999": [{"GRP_CODE": "Group1", "ITEM_CODE": c, "ITEM_NAME": n, "CYCLE": "Q", "UNIT_NAME": "백만달러", "START_TIME": "1996Q1"}
                for c, n in [("KR", "한국"), ("CN", "중국"), ("DE", "독일"), ("JP", "일본"), ("US", "미국")]],
}
VAL = {"KR": 116630, "CN": 193720, "DE": 52280, "JP": 50620}


class Resp:
    def __init__(self, js):
        self.js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self.js


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        parts = url.split("/")
        if "StatisticTableList" in url:
            return Resp({"StatisticTableList": {"row": TABLES}})
        if "StatisticItemList" in url:
            return Resp({"StatisticItemList": {"row": ITEMS.get(parts[-1], [])}})
        i = parts.index("StatisticSearch")
        stat, cyc, item = parts[i + 6], parts[i + 7], parts[-1]
        if stat == "902Y999":
            rows = [{"TIME": f"2026Q{q}", "DATA_VALUE": str(VAL[item] * q / 2)} for q in (1, 2)]
        elif stat == "301Y999":
            rows = [{"TIME": f"2025{m:02d}", "DATA_VALUE": "8000"} for m in range(1, 13)] + \
                   [{"TIME": f"2026{m:02d}", "DATA_VALUE": "30000"} for m in range(1, 9)]
        else:
            rows = []
        return Resp({"StatisticSearch": {"row": rows}} if rows else {"RESULT": {"CODE": "INFO-200"}})


def test():
    d = Path(tempfile.mkdtemp())
    s = FakeSession()
    out = bop.section("KEY", d, datetime(2026, 10, 25), session=s)
    codes = json.loads((d / "bop_codes.json").read_text())
    assert codes["imf"]["stat"] == "902Y999" and codes["kr"]["stat"] == "301Y999", codes
    q = out["quarters"][-1]
    assert q["q"] == "2026Q2" and q["KR"] == 1166.3 and q["DE"] == 522.8, q  # 백만달러 → 억달러
    assert out["kr_ytd"] == {"year": 2026, "through": 8, "v": 2400.0, "prev": 640.0}, out["kr_ytd"]
    # 두 번째 실행: 캐시 사용 → 표 목록 다시 안 부름
    s2 = FakeSession()
    bop.section("KEY", d, datetime(2026, 10, 25), session=s2)
    assert not any("StatisticTableList" in u for u in s2.calls)
    # 표 번호가 바뀐 경우: 캐시 코드로 자료 없음 → 다시 찾음
    codes["imf"]["stat"] = "OLD000"
    codes["kr"]["stat"] = "OLD001"
    (d / "bop_codes.json").write_text(json.dumps(codes))
    s3 = FakeSession()
    out3 = bop.section("KEY", d, datetime(2026, 10, 25), session=s3)
    assert any("StatisticTableList" in u for u in s3.calls) and out3["quarters"][-1]["KR"] == 1166.3
    assert bop.section(None, d, datetime(2026, 10, 25)) is None
    print("bop OK")


if __name__ == "__main__":
    test()
