"""GPU 임대료 방향 — 가짜 페이지(네트워크 없음)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import gpu_rent as g  # noqa: E402

H100 = '<h3>H100<span>(SDH100RT)</span></h3><p>$3.08</p><img src="/i/hub-arrow-up.svg"><span>6.9%</span><span>vs 7D</span>'
B200 = 'B200(SDB200RT) $7.10 ![](https://x/hub-arrow-down.svg) 12.9%vs 7D'


class R:
    def __init__(self, t):
        self.text = t

    def raise_for_status(self):
        pass


class S:
    def get(self, url, **kw):
        return R(H100 if "h100" in url else B200)


def test():
    assert g.parse(H100, "SDH100RT") == (1, 6.9) and g.parse(B200, "SDB200RT") == (-1, 12.9)
    d = Path(tempfile.mkdtemp())
    assert g.collect(d / "gpu_rent.csv", S(), datetime(2026, 11, 21)) == 2
    assert g.collect(d / "gpu_rent.csv", S(), datetime(2026, 11, 21)) == 2   # 같은 날 다시: 교체
    txt = (d / "gpu_rent.csv").read_text()
    assert "3.08" not in txt and "7.1" not in txt and txt.count("2026-11-21") == 2, txt   # 지수 값은 저장 안 함
    o = g.section(d)
    assert o["items"]["h100"]["dir"] == 1 and o["items"]["b200"]["size"] == "큰 폭", o
    print("gpu_rent OK")


if __name__ == "__main__":
    test()
