"""실험용(사이트 계산에는 안 씀): 브리지에 실질 수입을 넣었을 때 백테스트 비교, ECOS GDI 항목 탐색.
사용: ECOS_API_KEY=... python -m nowcast.experiment
"""
from __future__ import annotations

import copy
import os
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

from . import ecos, model
from .bop import Ecos
from .run import KST, ROOT, build_levels, collect

IM = {"label": "실질수입", "stat": "403Y004", "items": ["*AA"], "seasonal_adjust": True, "source": "한은 수입물량지수"}


def main():
    key = os.environ["ECOS_API_KEY"]
    cfg = yaml.safe_load((ROOT / "config" / "series.yaml").read_text())
    cfg2 = copy.deepcopy(cfg)
    cfg2["bridge"]["im"] = IM
    fetcher = lambda stat, cyc, s, e, items: ecos.fetch(key, stat, cyc, s, e, items)
    raw = collect(cfg2, fetcher)
    now = datetime.now(KST)
    levels, meta, _ = build_levels(cfg2, raw, now)
    gdp, esi, mcfg = raw["gdp"], raw["esi"], cfg["model"]
    base = list(cfg["bridge"].keys())
    variants = {"A 기존": base, "B +실질수입": base + ["im"]}
    res = {}
    for name, keys in variants.items():
        bt = model.backtest(levels, esi, gdp, mcfg, keys)
        X = pd.DataFrame({k: model.q_growth(levels[k]) for k in keys})
        br = model.fit_bridge(gdp, X, mcfg["bridge_start"], mcfg["exclude_quarters"])
        res[name] = bt
        print(f"\n### {name}: RMSE k0..k3 = " + " / ".join(f"{bt['rmse'][str(k)]:.3f}" for k in range(4))
              + f" (AR {bt['ar_rmse']:.3f}, n={bt['n']}) · R² {br.get('r2', float('nan')):.3f}")
        print("    계수: const %.3f · " % br["const"] + " · ".join(f"{k} {v:+.4f}" for k, v in br["coef"].items()))
        df = pd.DataFrame(bt["rows"])
        rec = df[df["q"] >= "2024Q1"]
        for k in range(4):
            e = rec["actual"] - rec[f"k{k}"]
            print(f"    최근(2024~) k{k}: RMSE {np.sqrt(np.mean(e ** 2)):.3f}, 평균오차(실적−추정) {e.mean():+.3f}")
        print("    분기별(2025~):", " | ".join(f"{r.q} 실적 {r.actual:+.1f} k3 {r.k3:+.2f}" for r in df[df['q'] >= '2025Q1'].itertuples()))
    # GDI 항목 탐색(200Y102 주요지표 분기)
    e = Ecos(key)
    try:
        its = [i for i in e.items("200Y102") if "소득" in (i.get("ITEM_NAME") or "")]
        print("\n### 200Y102 소득 항목:", [(i["ITEM_CODE"], i["ITEM_NAME"], i.get("UNIT_NAME")) for i in its][:20])
        for i in its[:8]:
            s = ecos.fetch(key, "200Y102", "Q", "2025Q1", f"{now.year}Q4", [i["ITEM_CODE"]])
            print("   ", i["ITEM_CODE"], i["ITEM_NAME"], s.tail(3).round(2).to_dict())
    except Exception as ex:  # noqa: BLE001
        print("GDI 탐색 실패", type(ex).__name__, ex)


if __name__ == "__main__":
    main()
