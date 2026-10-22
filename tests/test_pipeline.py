"""합성 데이터로 두 번 실행 → 변동 분해가 정확히 합산되는지 확인.
실행: python -m pytest tests  (또는 python tests/test_pipeline.py)"""
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from nowcast import run  # noqa: E402
from tests.synthetic import fetcher_from, make  # noqa: E402


def test_pipeline(tmp_root=None):
    tmp = Path(tmp_root or tempfile.mkdtemp())
    for d in ("config", "web", "data"):
        shutil.copytree(ROOT / d, tmp / d, dirs_exist_ok=True)
    for f in ("state.json", "history.csv", "backtest.csv", "annual_history.csv"):
        (tmp / "data" / f).unlink(missing_ok=True)
    run.ROOT = tmp
    data = make()
    p1 = run.main(fetcher=fetcher_from(data), now=datetime(2026, 10, 2, 9, 0, tzinfo=run.KST))
    # 두 번째 실행: 9월 산업활동동향 발표를 흉내(9월 값 추가)
    for k in [("901Y033", c, "2") for c in ("AB00", "AC00", "AD00", "AE00", "A00")]:
        s = data[k]
        s.loc[pd.Period("2026-09", "M")] = s.iloc[-1] * 1.01
        data[k] = s.sort_index()
    p2 = run.main(fetcher=fetcher_from(data), now=datetime(2026, 10, 30, 9, 30, tzinfo=run.KST))
    t1, t2 = p1["targets"][0], p2["targets"][0]
    assert t1["quarter"] == t2["quarter"] == "2026Q3"
    assert t1["months_known"] == 2 and t2["months_known"] == 3
    ch = t2["last_change"]
    s = sum(i["delta"] for i in ch["items"])
    assert abs((t2["nowcast"] - ch["from"]) - s) < 0.02, (t2["nowcast"], ch["from"], s)
    assert "광공업" in ch["new_data"]
    assert len(t2["history"]) == 2
    assert p2["customs"] and p2["customs"][0]["month"] == "2026-09"
    a = p2["annual"]
    assert a and a["year"] == 2026 and a["nominal"] > a["real"], a
    assert len(a["path"]) == 4 and a["path"][2]["real_est"] and a["path"][1]["real_est"] is False
    assert a["change"] and len(a["history"]) == 2
    yo = a["yoy"]
    assert yo[-1]["q"] == "2026Q4" and yo[-1]["real_band"] and not yo[0]["est"]
    # 부가 분석 ❹❺❻
    g = p2["gni"]
    assert g and g["annual"] and any(r["est"] for r in g["rows"]) and g["fit"]["r2"] > 0.5, g and g["fit"]
    d = p2["dist"]
    assert d and len(d["rows"]) == 12 and abs(d["rows"][-1]["resid"]) < 3, d and d["rows"][-1]
    e = p2["expenditure"]
    assert e and [r["q"] for r in e["rows"] if r["est"]] == ["2026Q3", "2026Q4"]
    for row in e["rows"]:  # 항목 기여도 합 = GDP 증가율
        assert abs(sum(row[k] for k in e["order"]) - row["gdp"]) < 0.02, row
    lb = p2["labor"]
    assert lb and lb["est"] and lb["est"]["lo"] < lb["est"]["ls"] < lb["est"]["hi"], lb and lb.get("est")
    al = {a["key"] for a in p2["alerts"]}
    assert {"prices", "esi", "gdp_adv"} <= al and "cpi" not in al, p2["alerts"]
    rel = p2["releases"]
    assert rel and rel[0]["date"] > "2026-10-30" and any(x["key"] == "activity" for x in rel), rel
    html = (tmp / "site" / "index.html").read_text()
    assert "/*__DATA__*/" not in html
    return tmp, p1, p2


if __name__ == "__main__":
    tmp, p1, p2 = test_pipeline(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({"deflator": p2["deflator"], "annual": {k: v for k, v in p2["annual"].items() if k not in ("yoy",)}}, ensure_ascii=False, indent=1))
    print("site:", tmp / "site" / "index.html")
