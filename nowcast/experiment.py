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
    # C: 순수출 합성지수 — 국민계정 비중으로 가중한 수출−수입 로그 증가(계수 부호를 이론대로 고정)
    lx, lm = np.log(levels["ex"]), np.log(levels["im"])
    j = pd.concat([lx.rename("x"), lm.rename("m")], axis=1).dropna()
    d = 0.45 * j["x"].diff() - 0.40 * j["m"].diff()
    levels["nx"] = np.exp(d.fillna(0).cumsum()) * 100
    variants = {"A 기존": base, "B +실질수입": base + ["im"],
                "C 수출→순수출": [k for k in base if k != "ex"] + ["nx"]}
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
        # D: 절편 보정(직전 8개 분기 평균 오차를 더함) — 실시간에도 쓸 수 있는 정보만 사용
        for k in range(4):
            err = df["actual"] - df[f"k{k}"]
            corr = df[f"k{k}"] + err.shift(1).rolling(8, min_periods=4).mean().fillna(0)
            e2 = (df["actual"] - corr)
            r2_all = np.sqrt(np.mean(e2 ** 2)); r2_rec = np.sqrt(np.mean(e2[df["q"] >= "2024Q1"] ** 2))
            print(f"    [절편 보정] k{k}: 전체 RMSE {r2_all:.3f} · 최근 {r2_rec:.3f}")
        print("    분기별(2025~):", " | ".join(f"{r.q} 실적 {r.actual:+.1f} k3 {r.k3:+.2f}" for r in df[df['q'] >= '2025Q1'].itertuples()))


if __name__ == "__main__":
    main()
