"""수출물가 미발표 달 추정: 반도체·비반도체 분리 연장이 '직전 달 그대로'보다 반도체 급등을 잘 따라가는지."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import adjust  # noqa: E402


def test():
    rng = np.random.default_rng(1)
    idx = pd.period_range("2023-01", "2026-09", freq="M")
    g = np.where(idx >= pd.Period("2026-03", "M"), 0.08, 0.003)          # 반도체: 올해 3월부터 월 8% 급등
    chip = pd.Series(100 * np.exp(np.cumsum(g)), index=idx)
    non = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.002, 0.004, len(idx)))), index=idx)
    total = 0.2 * chip + 0.8 * non
    w = adjust.chip_weight(total, chip)
    assert abs(w - 0.2) <= 0.03, w
    ref = pd.Period("2026-10", "M")
    yoy, how = adjust.price_yoy_nowcast(total, chip, ref)
    # 참값: 반도체 한 달 더 8%
    true = (0.2 * chip.iloc[-1] * np.exp(0.08) + 0.8 * non.iloc[-1] * np.exp(0.002)) / total[ref - 12] - 1
    naive = total.iloc[-1] / total.iloc[-13] - 1
    assert how.startswith("split") and abs(yoy - true) < abs(naive - true) and abs(yoy - true) < 0.01, (yoy, true, naive)
    # 발표된 달은 실적 그대로
    assert adjust.price_yoy_nowcast(total, chip, pd.Period("2026-09", "M"))[1] == "actual"
    print(f"price_nowcast OK  w={w:.2f}  추정 {yoy*100:.1f}% · 참값 {true*100:.1f}% · 기존 {naive*100:.1f}%")


if __name__ == "__main__":
    test()
