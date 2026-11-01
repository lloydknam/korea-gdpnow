"""품목별 수출 — data.go.kr 응답을 흉내 낸 가짜 세션(네트워크 없음)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import adjust, exports_items as ex  # noqa: E402

# HS 코드별 월 수출액(달러): 반도체 100억, 자동차 60억, … 2단위 류 합 = 전체 600억(2026년엔 반도체 2배)
BASE = {"8541": 5e8, "8542": 9.5e9, "8703": 4e9, "8704": 2e8, "8708": 1.8e9, "2710": 3e9, "29": 1.5e9, "39": 2e9,
        "89": 2e9, "72": 2e9, "73": 5e8}


def usd(hs, m):
    v = BASE.get(hs)
    if v is None:  # 2단위 류: 85류에 반도체, 87류에 자동차 포함 + 나머지 고르게
        extra = {"85": BASE["8541"] + BASE["8542"] + 3e9, "87": BASE["8703"] + BASE["8704"] + BASE["8708"]}
        v = extra.get(hs, 3.5e10 / 90)
        if hs == "85" and m.startswith("2026"):
            v += BASE["8541"] + BASE["8542"]
    elif hs in ("8541", "8542") and m.startswith("2026"):
        v *= 2
    return v


class Resp:
    def __init__(self, body):
        self.content = body.encode()

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, last="2026-09"):
        self.calls, self.last = 0, last

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        hs = params["hsSgn"]
        ms = [str(p) for p in pd.period_range(pd.Period(params["strtYymm"], "M"), pd.Period(params["endYymm"], "M"), freq="M")
              if str(p) <= self.last]
        items = "".join(f"<item><year>{m.replace('-', '.')}</year><hsCd>{hs}</hsCd><statKor>x</statKor>"
                        f"<expDlr>{usd(hs, m):.0f}</expDlr><impDlr>0</impDlr></item>" for m in ms)
        items += "<item><year>총계</year><hsCd>-</hsCd><expDlr>999999999999</expDlr></item>"
        return Resp(f"<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>"
                    f"<body><items>{items}</items></body></response>")


def test():
    ex.PAUSE = 0
    d = Path(tempfile.mkdtemp())
    (d / "customs_flash.csv").write_text(
        "release_date,ref_month,period,exports_yoy_pct,workdays,workdays_prev,exports_usd_mn,chip_yoy_pct,items_yoy\n"
        "2026-11-01,2026-10,m,20,,,72000,100,선박:+50|승용차:-10\n")
    flash = adjust.load_customs(d / "customs_flash.csv")
    s = FakeSession()
    out = ex.section("KEY", d, datetime(2026, 11, 1, 12), flash, sess=s)
    n1 = s.calls
    assert out and out["year"] == 2026, out
    rows = {r["m"]: r for r in out["rows"]}
    a = rows["2026-09"]
    assert abs(a["chip"] - 20000) < 1 and abs(a["auto"] - 6000) < 1 and abs(a["total"] - sum(usd(c, "2026-09") for c in ex.CHAPTERS) / 1e6) < 1, a
    assert abs(a["other"] - (a["total"] - sum(a[g] for g in ("chip", "auto", "petro", "ship", "steel")))) < 0.5
    # 10월: 확정치 없음 → 잠정치. 반도체 = 전년 × 2, 선박 +50%, 자동차 −10%, 나머지 = 전체 − 합
    o = rows["2026-10"]
    assert o["est"] and o["total"] == 72000 and abs(o["chip"] - 20000) < 1, o
    assert abs(o["ship"] - 3000) < 1 and abs(o["auto"] - 5400) < 1, o
    assert abs(sum(o[g] for g in ("chip", "auto", "petro", "ship", "steel", "other")) - 72000) < 1
    # 다시 실행: 캐시에 9월까지 있고 10월 확정치는 아직 → 같은 날엔 재조회 안 함
    s2 = FakeSession()
    ex.section("KEY", d, datetime(2026, 11, 1, 16), flash, sess=s2)
    assert s2.calls == 0, s2.calls
    # 11/16: 10월 확정치 공개 → 최근 2개월만 다시 조회, 잠정치 대신 확정치
    s3 = FakeSession(last="2026-10")
    out3 = ex.section("KEY", d, datetime(2026, 11, 16, 9), flash, sess=s3)
    assert 0 < s3.calls < n1 and not {r["m"]: r for r in out3["rows"]}["2026-10"]["est"]
    # 키 없음: 캐시만으로 표시, 캐시도 없으면 None
    assert ex.section(None, d, datetime(2026, 11, 16), flash)["rows"]
    assert ex.section(None, Path(tempfile.mkdtemp()), datetime(2026, 11, 16), flash) is None
    print("exports_items OK", n1, s3.calls)


if __name__ == "__main__":
    test()
