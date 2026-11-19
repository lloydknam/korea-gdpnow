"""D램 현물가 수집·신호 — 가짜 페이지(네트워크 없음)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import dram_spot as ds  # noqa: E402

HTML = """<div>Last Update 2026-11-13 18:10 (GMT+8)</div><table>
<tr><td>DDR5 16Gb (2Gx8) 4800/5600</td><td>72.50</td><td>45.50</td><td>72.50</td><td>45.50</td><td>60.267</td><td>▲ 0.06 %</td></tr>
<tr><td>DDR5 16Gb (2Gx8) eTT</td><td>31.50</td><td>29.70</td><td>31.50</td><td>29.70</td><td>30.50</td><td>▲ 0.16 %</td></tr>
<tr><td>DDR4 16Gb (2Gx8) 3200</td><td>114.00</td><td>45.00</td><td>114.00</td><td>45.00</td><td>79.50</td><td>▼ -0.31 %</td></tr>
<tr><td>DDR4 8Gb (1Gx8) 3200</td><td>82.00</td><td>24.80</td><td>82.00</td><td>24.80</td><td>44.536</td><td>▼ -0.40 %</td></tr>
</table>"""
MD = """Last Update 2026-11-14 18:10 (GMT+8)
| Item | Daily High | Daily Low | Session High | Session Low | Session Average | Session Change |
| DDR5 16Gb (2Gx8) 4800/5600 | 72.00 | 45.00 | 72.00 | 45.00 | 59.900 | ▼ -0.61 % |
| DDR4 8Gb (1Gx8) 3200 | 80.00 | 24.00 | 80.00 | 24.00 | 44.100 | ▼ -0.98 % |"""


class R:
    def __init__(self, t, code=200):
        self.text, self.content, self.code = t, t.encode(), code

    def raise_for_status(self):
        if self.code >= 400:
            import requests
            raise requests.HTTPError(str(self.code))


class Direct:
    def get(self, url, **kw):
        return R(HTML)


class Blocked:   # 직접 접속 403 → 웹 리더(마크다운)
    def get(self, url, **kw):
        return R("forbidden", 403) if url.startswith("https://www.trendforce") else R(MD + " " * 300)


def test():
    day, v = ds.parse(HTML)
    assert day == "2026-11-13" and v == {"ddr5_16g": 60.267, "ddr4_16g": 79.5, "ddr4_8g": 44.536}, (day, v)
    d = Path(tempfile.mkdtemp())
    p = d / "dram_spot.csv"
    p.write_text("date,item,avg,src\n2026-10-15,ddr4_8g,46.5,seed\n2026-11-06,ddr4_8g,46.0,seed\n")
    assert ds.collect(p, Direct()) == 3 and ds.collect(p, Direct()) == 0   # 같은 날 중복 안 붙음
    assert ds.collect(p, Blocked()) == 2                                   # 웹 리더 경유
    o = ds.section(d, 3.8)
    s8 = next(x for x in o["signal"] if x["k"] == "ddr4_8g")
    assert s8["last"] == 44.1 and s8["d30"] < -3 and o["level"] == "warn" and "월 +3.8%" in o["text"], o
    assert ds.section(Path(tempfile.mkdtemp())) is None
    # 계약가 표 → dram_hist.csv 자동 추가·같은 달 교체
    C1 = """DRAM Contract Price (2H Oct) Last Update 2026-10-30 17:00 (GMT+8)
| Item | Session High | Session Low | Session Average | Average Change | Low Change |
| DDR4 8GB SO-DIMM | 151.00 | 132.00 | 148.00 | ▲ 4.23 % | ▲ 5.60 % |
| DDR4 16Gb 2Gx8 | 61.00 | 49.00 | 52.00 | ▲ 4.00 % | ▲ 4.26 % |
| DDR4 8Gb 1Gx8 | 35.50 | 23.50 | 27.50 | ▲ 5.77 % | ▲ 4.44 % |"""
    assert ds.parse_contract(C1) == ("2026-10", 27.5)
    assert ds.parse_contract(C1.replace("(2H Oct)", "(Dec)").replace("2026-10-30", "2027-01-02")) == ("2026-12", 27.5)
    hp = d / "dram_hist.csv"
    hp.write_text("# x\ndate,item,avg,src\n2026-09-01,ddr4_8g_contract,26.0,siliconanalysts\n"
                  "2026-10-01,ddr4_8g_contract,27.0,siliconanalysts\n2026-09-01,ddr4_8g_spot,46.32,siliconanalysts\n")

    class CS:
        def __init__(self, page):
            self.page = page

        def get(self, url, **kw):
            return R(self.page)
    assert ds.collect_contract(hp, CS(C1)) == "2026-10"
    assert ds.collect_contract(hp, CS(C1.replace("(2H Oct)", "(Nov)").replace("2026-10-30", "2026-11-28").replace("27.50", "29.00"))) == "2026-11"
    # 현물 표 줄(속도 표기 3200)은 계약가로 안 읽음, 터무니없는 값은 기록 안 함
    assert ds.parse_contract(C1.replace("| DDR4 8Gb 1Gx8 | 35.50 | 23.50 | 27.50 |", "| DDR4 8Gb (1Gx8) 3200 | 82.00 | 24.80 | 80.00 |"))[1] is None
    assert ds.collect_contract(hp, CS(C1.replace("27.50", "80.00"))) is None
    lt = ds.long_term(hp)
    cm = {x["d"]: x["v"] for x in lt["contract"]}
    assert cm == {"2026-09-01": 26.0, "2026-10-01": 27.5, "2026-11-01": 29.0} and lt["auto_from"] == "2026-10", lt
    assert any(x["d"] == "2026-11-01" and x["src"] == "daily" for x in lt["spot"]), lt["spot"]   # 일별 현물 → 월말 값
    assert hp.read_text().startswith("# x\ndate,item,avg,src\n")
    print("dram_spot OK")


if __name__ == "__main__":
    test()
