"""실행 진입점: python -m nowcast.run

ECOS에서 데이터 수집 → 실시간 추정 → 이전 실행 대비 변동 요인 분해
→ data/state.json, data/history.csv 갱신 → site/index.html, site/nowcast.json 생성.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import adjust, bop, ecos, extras, model, releases

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
CONST_KEY = "_model"


def r(x, n=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


# ------------------------------------------------------------------ 수집
def collect(cfg: dict, fetcher) -> dict:
    t = cfg["target"]
    today = datetime.now(KST)
    end_m = f"{today.year}{today.month:02d}"
    end_q = f"{today.year}Q4"
    raw = {"gdp": fetcher(t["stat"], "Q", t["start"], end_q, t["items"])}
    n = cfg["nominal"]
    raw["nominal"] = fetcher(n["stat"], "Q", n["start"], end_q, n["items"])
    groups = [("bridge", cfg["bridge"]), ("aux", cfg["aux"]), ("deflator", cfg["deflator"]["bridge"])]
    for _, grp in groups:
        for k, s in grp.items():
            cyc = s.get("cycle", "M")
            start, end = ((f"{today.year - 1}0101", today.strftime("%Y%m%d")) if cyc == "D"
                          else (cfg["monthly_start"], end_m))
            try:
                raw[k] = fetcher(s["stat"], cyc, start, end, s["items"])
            except Exception as e:  # noqa: BLE001
                if not s.get("optional"):
                    raise
                print(f"::warning::선택 지표 {k} 수집 실패({type(e).__name__}) — 없이 진행")
                raw[k] = pd.Series(dtype=float)
            if raw[k].empty and not s.get("optional"):
                raise RuntimeError(f"빈 시계열: {k} ({s['stat']} {s['items']})")
    return raw


def build_levels(cfg: dict, raw: dict, now: datetime | None = None):
    levels, meta = {}, {}
    customs_used = []
    for k, s in cfg["bridge"].items():
        x = raw[k]
        actual_last = x.index.max()
        if s.get("customs_flash"):
            flash = adjust.load_customs(ROOT / "data" / "customs_flash.csv")
            x, customs_used = adjust.extend_with_customs(x, raw["xp_usd"], flash, raw.get("chip_px"))
            for c in customs_used:
                print(f"::notice::속보 환산 {c['month']}({c['period']}): 금액 {c['value_yoy']:+.1f}% ÷ 단가 {c['price_yoy']:+.1f}%"
                      f"[{c['price_method']}, 기존 방식 {c['price_yoy_naive']}%] → 물량 {c['volume_yoy']:+.1f}% (반영 {c['weight']})")
        if s.get("seasonal_adjust"):
            x = adjust.seasonal_adjust(x)
        levels[k] = x
        meta[k] = {"actual_last": actual_last, "last": x.index.max()}
    ext_info = {}
    for k, s in cfg["deflator"]["bridge"].items():
        x = raw[k]
        actual_last = x.index.max()
        fx = raw.get(s.get("fx", ""))
        if s.get("fx") and fx is not None and not fx.empty and raw.get(s["usd"]) is not None:
            # 원화 수출물가 = 달러 가격(반도체·기타 추세 연장) × 실제 환율(일별 원/달러) — 환율은 추세로 늘리지 않음
            through = pd.Period(f"{(now or datetime.now(KST)).year}-12", "M")
            x, info = adjust.extend_krw_by_fx(x, raw[s["usd"]], raw.get(s.get("usd_chip", "")), fx, through)
            ext_info[k] = info
            if info.get("added"):
                print(f"::notice::{s['label']} {info['from']}~{info['to']} = 달러가격 추세[{info['method']}, 반도체 월 "
                      f"{info['chip_trend']:+.1f}% · 기타 월 {info['other_trend']:+.1f}%] × 실제 환율 {info['fx_text']}")
        elif s.get("split_chip") and raw.get(s["split_chip"]) is not None:
            # 수출물가 미발표 달(연말까지): 반도체 단가 추세를 따로 반영해 연장 → AR 외삽 대신 사용
            through = pd.Period(f"{(now or datetime.now(KST)).year}-12", "M")
            x, info = adjust.extend_split(x, raw[s["split_chip"]], through)
            ext_info[k] = info
            if info.get("added"):
                print(f"::notice::{s['label']} {info['from']}~{info['to']} 추세 연장 [{info['method']}] "
                      f"반도체 월 {info['chip_trend']:+.1f}% · 기타 월 {info['other_trend']:+.1f}% (달마다 70%로 감쇠)")
        levels[k] = adjust.seasonal_adjust(x) if s.get("seasonal_adjust") else x
        meta[k] = {"actual_last": actual_last, "last": x.index.max(), "ext": ext_info.get(k)}
    return levels, meta, customs_used


def deflator_series(gdp: pd.Series, nominal: pd.Series) -> pd.Series:
    """계절조정 전기비 기준 GDP 디플레이터 변화율(%)."""
    j = pd.concat([gdp.rename("r"), nominal.rename("n")], axis=1).dropna()
    return ((1 + j["n"] / 100) / (1 + j["r"] / 100) - 1) * 100


def annual_year(gdp: pd.Series) -> int:
    """헤드라인 연도 = 아직 발표 안 된 가장 이른 분기의 연도 (1월엔 전년도, 5월부턴 당해)."""
    return (gdp.dropna().index.max() + 1).year


def chain_annual(year: int, real: dict, defl: dict):
    """분기 전기비(계절조정)를 연쇄 → 연평균 비교로 연간 성장률. 하나라도 비면 None."""
    qs = list(pd.period_range(f"{year - 1}Q1", f"{year}Q4", freq="Q"))
    if any(q not in real or q not in defl for q in qs[1:]):
        return None
    L, D = [1.0], [1.0]
    for q in qs[1:]:
        L.append(L[-1] * (1 + real[q] / 100))
        D.append(D[-1] * (1 + defl[q] / 100))
    L, D = np.array(L), np.array(D)
    g = lambda x: (x[4:].mean() / x[:4].mean() - 1) * 100
    gr, gn = g(L), g(L * D)
    return {"real": gr, "nominal": gn, "deflator": ((1 + gn / 100) / (1 + gr / 100) - 1) * 100}


def yoy_path(real: dict, defl: dict, sd_r: dict, sd_d: dict, start: str, end: pd.Period) -> list[dict]:
    """전년동기비(계절조정 수준 기준) 실질·경상·디플레이터 + 추정 구간 밴드(±1 RMSE 누적)."""
    first = max(min(real), min(defl))
    L, D, lv = 1.0, 1.0, {first - 1: (1.0, 1.0)}
    q = first
    while q <= end:
        if q not in real or q not in defl:
            break
        L *= 1 + real[q] / 100
        D *= 1 + defl[q] / 100
        lv[q] = (L, D)
        q += 1
    out = []
    for q in pd.period_range(start, end, freq="Q"):
        if q not in lv or (q - 4) not in lv:
            continue
        (l1, d1), (l0, d0) = lv[q], lv[q - 4]
        rr, nn = (l1 / l0 - 1) * 100, (l1 * d1 / (l0 * d0) - 1) * 100
        win = [q - i for i in range(4)]
        srr = float(np.sqrt(sum(sd_r.get(w, 0) ** 2 for w in win)))
        snn = float(np.sqrt(sum(sd_r.get(w, 0) ** 2 + sd_d.get(w, 0) ** 2 for w in win)))
        out.append({"q": str(q), "real": r(rr), "nom": r(nn), "defl": r(((1 + nn / 100) / (1 + rr / 100) - 1) * 100),
                    "est": q in sd_r or q in sd_d,
                    "real_band": [r(rr - srr), r(rr + srr)] if srr else None,
                    "nom_band": [r(nn - snn), r(nn + snn)] if snn else None})
    return out


def annual_sd(year: int, sd_by_q: dict) -> float:
    """분기 성장률 오차가 연간 성장률에 주는 영향: 당해 p분기 → (5−p)/4, 전년 p분기 → (p−1)/4."""
    v = 0.0
    for q, sd in sd_by_q.items():
        w = (5 - q.quarter) / 4 if q.year == year else (q.quarter - 1) / 4 if q.year == year - 1 else 0
        v += (w * sd) ** 2
    return float(np.sqrt(v))


# ------------------------------------------------------------------ 상태
def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"runs": {}, "actuals": {}, "bridge": None}


def gdp_hash(gdp: pd.Series) -> str:
    s = ",".join(f"{p}:{v:.3f}" for p, v in gdp.dropna().items())
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def target_quarters(gdp: pd.Series, now: datetime) -> list[pd.Period]:
    last = gdp.dropna().index.max()
    cur = pd.Period(now.strftime("%Y-%m"), "M").asfreq("Q")
    qs, q = [], last + 1
    while q <= cur:
        qs.append(q)
        q += 1
    return qs or [last + 1]


def decompose(prev: dict | None, cur: dict, bridge_prev: dict | None, bridge_cur: dict) -> list[dict] | None:
    """직전 실행 대비 변화 = Σ b_new·Δx_i + [모형 재추정 효과]."""
    if not prev:
        return None
    out = []
    for k, b in bridge_cur["coef"].items():
        dx = cur["x"][k] - prev["x"].get(k, cur["x"][k])
        out.append({"key": k, "delta": b * dx})
    if bridge_prev and bridge_prev.get("id") != bridge_cur.get("id"):
        d = (bridge_cur["const"] - bridge_prev["const"]) + sum(
            (bridge_cur["coef"][k] - bridge_prev["coef"].get(k, 0.0)) * prev["x"].get(k, 0.0)
            for k in bridge_cur["coef"])
        out.append({"key": CONST_KEY, "delta": d})
    return out


# ------------------------------------------------------------------ 메인
def main(fetcher=None, now: datetime | None = None, out_dir: Path | None = None) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "series.yaml").read_text())
    mcfg = cfg["model"]
    now = now or datetime.now(KST)
    data_dir = ROOT / "data"
    site_dir = out_dir or (ROOT / "site")
    data_dir.mkdir(exist_ok=True)
    site_dir.mkdir(exist_ok=True)

    if fetcher is None:
        key = os.environ.get("ECOS_API_KEY")
        if not key:
            sys.exit("ECOS_API_KEY 환경변수가 없습니다.")
        fetcher = lambda stat, cyc, s, e, items: ecos.fetch(key, stat, cyc, s, e, items)

    raw = collect(cfg, fetcher)
    gdp = raw["gdp"]
    levels, meta, customs_used = build_levels(cfg, raw, now)
    esi = raw["esi"]
    keys = list(cfg["bridge"].keys())
    labels = {k: cfg["bridge"][k]["label"] for k in keys}
    labels[CONST_KEY] = "모형 재추정·GDP 개정"

    state = load_state(data_dir / "state.json")

    # 1) 브리지: GDP 계열이 바뀔 때(신규 발표·개정)만 재추정 → 분기 중 변동은 순수하게 지표 효과
    h = gdp_hash(gdp)
    X = pd.DataFrame({k: model.q_growth(levels[k]) for k in keys})
    bridge_prev = state.get("bridge")
    if not bridge_prev or bridge_prev.get("gdp_hash") != h or set(bridge_prev["coef"]) != set(keys):
        bridge = model.fit_bridge(gdp, X, mcfg["bridge_start"], mcfg["exclude_quarters"])
        bridge["gdp_hash"] = h
        bridge["id"] = f"{gdp.dropna().index.max()}@{h}"
        bridge["estimated_at"] = now.isoformat(timespec="minutes")
    else:
        bridge = bridge_prev

    # 2) 속보치 기록 (처음 관측된 값 = 속보치)
    new_actuals = []  # 이번 실행에서 처음 반영된 한은 GDP 속보치 (이력 주석용)
    for q, v in gdp.dropna().items():
        qs = str(q)
        if qs not in state["actuals"] and qs in state["runs"]:
            state["actuals"][qs] = {"advance": r(v), "final_nowcast": state["runs"][qs]["value"],
                                    "recorded_at": now.isoformat(timespec="minutes")}
            new_actuals.append(f"한은 {q.quarter}분기 GDP 속보치({r(v):.1f}%)")

    # 3) 백테스트 (정보량별 오차)
    bt = model.backtest(levels, esi, gdp, mcfg, keys)

    # 4) 대상 분기별 실시간 추정
    hist_path = data_dir / "history.csv"
    hist = pd.read_csv(hist_path) if hist_path.exists() else pd.DataFrame()
    targets_out, new_hist, real_nc = [], [], {}
    for q in target_quarters(gdp, now):
        nc = model.nowcast(levels, esi, q, bridge, mcfg)
        k_known = model.months_known(raw["ip"], q)
        rmse = bt["rmse"].get(str(k_known)) or bridge["sd"]
        real_nc[q] = (nc["value"], rmse, k_known)
        qs = str(q)
        through = {k: f"{meta[k]['actual_last']}|{meta[k]['last']}" for k in keys}
        prev = state["runs"].get(qs)
        changes = decompose(prev, nc, prev and prev.get("bridge"), bridge)
        # 예전 state(마지막 월만 저장)와도 비교되도록: '|'가 없으면 보강 후 월만 비교
        _same = lambda old, new: old == new if (old is None or "|" in old) else old == new.split("|")[1]
        new_data = [labels[k] for k in keys
                    if prev and not _same(prev.get("through", {}).get(k), through[k])]
        new_data = new_data + [x for x in new_actuals if x not in new_data]
        changed = (prev is None or abs(prev["value"] - nc["value"]) >= 0.005 or new_data
                   or (prev.get("bridge", {}).get("id") != bridge["id"]))
        if changed:
            new_hist.append({"run_at": now.isoformat(timespec="minutes"), "quarter": qs,
                             "nowcast": r(nc["value"], 3),
                             **{f"c_{k}": r(bridge["coef"][k] * nc["x"][k], 3) for k in keys},
                             "new_data": "|".join(new_data)})
            last_change = {"at": now.isoformat(timespec="minutes"), "from": prev and prev["value"],
                           "items": [{"key": c["key"], "label": labels[c["key"]], "delta": r(c["delta"], 3)}
                                     for c in (changes or [])],
                           "new_data": new_data}
        else:
            last_change = prev.get("last_change")

        # 월별 상세 (최근 15개월)
        ind = []
        for k in keys:
            ext, filled = nc["months"][k]
            d = model.mlog_diff(ext).dropna()
            d = d[d.index > q.asfreq("M", "end") - 15]
            ind.append({
                "key": k, "label": labels[k], "source": cfg["bridge"][k]["source"],
                "coef": r(bridge["coef"][k], 3), "x": r(nc["x"][k], 2),
                "contrib": r(bridge["coef"][k] * nc["x"][k], 3),
                "actual_last": str(meta[k]["actual_last"]),
                "months": [{"m": str(p), "mom": r(v, 2), "filled": p in filled,
                            "flash": (k == "ex" and p > meta[k]["actual_last"] and p not in filled)}
                           for p, v in d.items()],
            })
        state["runs"][qs] = {"value": r(nc["value"], 3), "x": {k: nc["x"][k] for k in keys},
                             "through": through, "bridge": {kk: bridge[kk] for kk in ("id", "const", "coef")},
                             "last_change": last_change, "at": now.isoformat(timespec="minutes")}
        targets_out.append({
            "quarter": qs, "label": f"{q.year}년 {q.quarter}분기",
            "nowcast": r(nc["value"]), "rmse": r(rmse), "months_known": k_known,
            "band": [r(nc["value"] - rmse), r(nc["value"] + rmse)],
            "const": r(bridge["const"], 3), "indicators": ind, "last_change": last_change,
        })

    # 5) 디플레이터 브리지 → 연간 실질·경상 성장률
    dcfg = cfg["deflator"]
    dkeys = list(dcfg["bridge"].keys())
    dlabels = {k: dcfg["bridge"][k]["label"] for k in dkeys}
    defl = deflator_series(gdp, raw["nominal"])
    mcfg_d = {**mcfg, "bridge_start": dcfg["bridge_start"]}
    Xd = pd.DataFrame({k: model.q_growth(levels[k]) for k in dkeys})
    dbridge = model.fit_bridge(defl, Xd, mcfg_d["bridge_start"], mcfg["exclude_quarters"])
    dbt = model.backtest(levels, None, defl, mcfg_d, dkeys)

    year = annual_year(gdp)
    real_map = {q: float(v) for q, v in gdp.dropna().items()}
    defl_map = {q: float(v) for q, v in defl.dropna().items()}
    sd_r, sd_d, src = {}, {}, {}
    q = min(max(real_map), max(defl_map)) + 1
    while q <= pd.Period(f"{year}Q4", "Q"):
        if q not in real_map:
            if q in real_nc:
                v, sd, _ = real_nc[q]
            else:  # 아직 탭에 없는 먼 분기: 지표 전부 AR 외삽
                v, sd = model.nowcast(levels, esi, q, bridge, mcfg)["value"], bt["rmse"]["0"]
            real_map[q], sd_r[q] = v, sd
            src.setdefault(q, {})["real"] = "n"
        if q not in defl_map:
            dn = model.nowcast(levels, None, q, dbridge, mcfg_d)
            kd = model.months_known(raw["px"], q)
            defl_map[q] = dn["value"]
            sd_d[q] = dbt["rmse"].get(str(kd)) or dbridge["sd"]
            src.setdefault(q, {})["defl"] = "n"
            src[q]["defl_k"] = kd
        q += 1

    annual = None
    ann = chain_annual(year, real_map, defl_map)
    if ann:
        rs, ds = annual_sd(year, sd_r), annual_sd(year, sd_d)
        prev_y = chain_annual(year - 1, real_map, defl_map)
        path = []
        for qq in pd.period_range(f"{year}Q1", f"{year}Q4", freq="Q"):
            rv, dv = real_map[qq], defl_map[qq]
            path.append({"q": str(qq), "real": r(rv), "defl": r(dv),
                         "nom": r(((1 + rv / 100) * (1 + dv / 100) - 1) * 100),
                         "real_est": src.get(qq, {}).get("real") == "n",
                         "defl_est": src.get(qq, {}).get("defl") == "n"})
        annual = {
            "year": year, "real": r(ann["real"]), "nominal": r(ann["nominal"]), "deflator": r(ann["deflator"]),
            "real_sd": r(rs), "defl_sd": r(ds), "nom_sd": r(np.hypot(rs, ds)),
            "prev": {"year": year - 1, **{k: r(v) for k, v in prev_y.items()}} if prev_y else None,
            "path": path,
            "nominal_last": str(raw["nominal"].dropna().index.max()),
            "yoy": yoy_path(real_map, defl_map, sd_r, sd_d, "2019Q1", pd.Period(f"{year}Q4", "Q")),
        }
        # 이번 실행에서 새로 반영된 지표 (변화 원인 주석용)
        run_new = []
        for row in new_hist:
            for x in str(row.get("new_data") or "").split("|"):
                if x and x not in run_new:
                    run_new.append(x)
        dthrough = {k: str(meta[k]["actual_last"]) for k in dkeys}
        dprev = state.get("defl_through") or {}
        if dprev:
            run_new += [dlabels[k] for k in dkeys if dprev.get(k) != dthrough[k] and dlabels[k] not in run_new]
        state["defl_through"] = dthrough
        if bridge_prev and bridge_prev.get("id") != bridge.get("id"):
            run_new.append("GDP 발표·개정")

        # 연간 추정치 이력 (값이 바뀔 때만 한 줄)
        ah_path = data_dir / "annual_history.csv"
        ah = pd.read_csv(ah_path) if ah_path.exists() else pd.DataFrame(columns=["run_at", "year", "real", "nominal"])
        last = ah[ah["year"] == year].tail(1)
        run_new += [x for x in new_actuals if x not in run_new]
        if last.empty or new_actuals or abs(float(last["real"].iloc[0]) - ann["real"]) >= 0.005 \
                or abs(float(last["nominal"].iloc[0]) - ann["nominal"]) >= 0.005:
            if not last.empty:
                annual["change"] = {"at": now.isoformat(timespec="minutes"),
                                    "real_from": r(float(last["real"].iloc[0])),
                                    "nominal_from": r(float(last["nominal"].iloc[0]))}
            ah = pd.concat([ah, pd.DataFrame([{"run_at": now.isoformat(timespec="minutes"), "year": year,
                                               "real": r(ann["real"], 3), "nominal": r(ann["nominal"], 3),
                                               "real_sd": r(rs, 3), "nom_sd": r(float(np.hypot(rs, ds)), 3),
                                               "new_data": "|".join(run_new)}])],
                           ignore_index=True)
            ah.to_csv(ah_path, index=False)
        else:
            annual["change"] = state.get("annual_change")
        state["annual_change"] = annual.get("change")
        ay = ah[ah["year"] == year]
        num = lambda v: None if pd.isna(v) else r(float(v), 3)
        annual["history"] = [{"at": row["run_at"], "real": num(row["real"]), "nominal": num(row["nominal"]),
                              "real_sd": num(row.get("real_sd")), "nom_sd": num(row.get("nom_sd")),
                              "new": [x for x in str(row.get("new_data") if pd.notna(row.get("new_data")) else "").split("|") if x]}
                             for _, row in ay.iterrows()]

    if new_hist:
        hist = pd.concat([hist, pd.DataFrame(new_hist)], ignore_index=True)
    if not hist.empty:
        hist.to_csv(hist_path, index=False)

    state["bridge"] = bridge
    (data_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1))

    # 5) 페이지용 JSON
    for t in targets_out:
        hq = hist[hist["quarter"] == t["quarter"]] if not hist.empty else hist
        t["history"] = [{"at": a, "v": r(v)} for a, v in zip(hq.get("run_at", []), hq.get("nowcast", []))]

    chips = []
    cv, cp = raw["chip_vol"], raw["chip_px"]
    for p in cv.index[-25:]:
        if (p - 12) in cv.index and p in cp.index and (p - 12) in cp.index:
            vy, py = cv[p] / cv[p - 12] - 1, cp[p] / cp[p - 12] - 1
            chips.append({"m": str(p), "vol": r(vy * 100, 1), "px": r(py * 100, 1),
                          "val": r(((1 + vy) * (1 + py) - 1) * 100, 1)})

    # 6) 부가 분석 ❹ 실질 GNI ❺ 분배 ❻ 지출 측 — 실패해도 본 추정은 그대로
    ext = {"gni": None, "dist": None, "labor": None, "bop": None, "exp": None, "errors": []}
    if "extras" in cfg:
        xr = extras.collect(cfg, fetcher, f"{now.year}Q4", f"{now.year}{now.month:02d}")
        ext["errors"] = xr.pop("_errors", [])
        prod = {q: v for q, (v, _, _) in real_nc.items()}
        gdi_adv = {}
        gp = data_dir / "gdi_advance.csv"  # 한은 속보치 실질 GDI(전년동기비) — GNI 잠정치 전까지 3Q 등 보정용
        if gp.exists():
            gdf = pd.read_csv(gp)
            gdi_adv = {pd.Period(str(q), "Q"): float(v) for q, v in zip(gdf["quarter"], gdf["gdi_yoy"]) if pd.notna(v)}
        for name, fn in [("gni", lambda: extras.gni_section(xr, raw["px"], raw["pm"], annual, gdi_adv)),
                         ("dist", lambda: extras.dist_section(xr)),
                         ("labor", lambda: extras.labor_section(xr, annual)),
                         ("bop", lambda: bop.section(os.environ.get("ECOS_API_KEY"), data_dir, now)),
                         ("exp", lambda: extras.exp_section(cfg, xr, levels, esi, list(real_nc), prod))]:
            try:
                ext[name] = fn()
            except Exception as e:  # noqa: BLE001
                ext["errors"].append(f"{name}: {type(e).__name__} {e}")
        for e in ext["errors"]:
            print(f"::warning::부가 분석 일부 생략 — {e}")

    upcoming = []
    rel_path = ROOT / "config" / "releases.yaml"
    if rel_path.exists():
        try:
            upcoming = releases.upcoming(yaml.safe_load(rel_path.read_text()), now, bt["rmse"])
        except Exception as e:  # noqa: BLE001
            print(f"::warning::발표 일정 생략 — {type(e).__name__} {e}")

    # 미수집 감시: 발표일이 지났는데 반영 안 된 자료 → 사이트 배너 + data/alerts.json(→ GitHub 이슈)
    alerts = []
    if rel_path.exists():
        try:
            flash = adjust.load_customs(ROOT / "data" / "customs_flash.csv")
            ex_off = meta["ex"]["actual_last"]

            def got_flash(min_rank):
                ms = list(flash.loc[flash["rank"] >= min_rank, "ref"]) if not flash.empty else []
                return max(ms + [ex_off])

            have = {"customs_d10": got_flash(0), "customs_d20": got_flash(1), "customs_m": got_flash(2),
                    "prices": raw["px"].index.max(), "cpi": raw["cpi"].index.max(), "esi": esi.index.max(),
                    "activity": raw["ip"].index.max(), "gdp_adv": gdp.dropna().index.max()}
            alerts = releases.overdue(yaml.safe_load(rel_path.read_text()), now, have)
        except Exception as e:  # noqa: BLE001
            print(f"::warning::미수집 감시 생략 — {type(e).__name__} {e}")
    (data_dir / "alerts.json").write_text(json.dumps(alerts, ensure_ascii=False, indent=1))
    for a in alerts:
        print(f"::warning::미수집 — {a['name']} {a['ref']} ({a['date']} 발표{' 예상' if a['est'] else ''}, {a['days']}일 경과)")

    g = gdp.dropna()
    payload = {
        "generated_at": now.isoformat(timespec="minutes"),
        "gdp": {"last_quarter": str(g.index.max()), "last_value": r(g.iloc[-1]),
                "series": [{"q": str(p), "v": r(v)} for p, v in g[-12:].items()]},
        "targets": targets_out,
        "bridge": {"const": r(bridge["const"], 3), "coef": {k: r(v, 3) for k, v in bridge["coef"].items()},
                   "labels": labels, "r2": r(bridge["r2"], 3), "sd": r(bridge["sd"], 2),
                   "n": bridge["n"], "sample": bridge["sample"], "estimated_at": bridge.get("estimated_at")},
        "backtest": {kk: bt[kk] for kk in ("rmse", "mae", "ar_rmse", "n", "period")},
        "annual": annual,
        "deflator": {"ext": {k: meta[k].get("ext") for k in dkeys if meta[k].get("ext")},
                     "const": r(dbridge["const"], 3), "coef": {k: r(v, 3) for k, v in dbridge["coef"].items()},
                     "labels": dlabels, "r2": r(dbridge["r2"], 3), "sd": r(dbridge["sd"], 2),
                     "sample": dbridge["sample"], "rmse": dbt["rmse"],
                     "last": {"q": str(defl.dropna().index.max()), "v": r(defl.dropna().iloc[-1])}},
        "actuals": [{"q": q, **v} for q, v in sorted(state["actuals"].items())][-8:],
        "customs": customs_used,
        "chips": chips,
        "esi": {"last": str(esi.index.max()), "value": r(esi.iloc[-1], 1)},
        "gni": ext["gni"], "dist": ext["dist"], "labor": ext["labor"], "bop": ext["bop"], "expenditure": ext["exp"],
        "releases": upcoming,
        "alerts": alerts,
    }
    (site_dir / "nowcast.json").write_text(json.dumps(payload, ensure_ascii=False))
    for f in (ROOT / "web").iterdir():  # 아이콘 등 정적 파일
        if f.is_file() and f.name != "index.html":
            (site_dir / f.name).write_bytes(f.read_bytes())
    tpl = (ROOT / "web" / "index.html").read_text()
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    (site_dir / "index.html").write_text(tpl.replace("/*__DATA__*/null", data))
    pd.DataFrame(bt["rows"]).to_csv(data_dir / "backtest.csv", index=False)

    if annual:
        print(f"{year}년 연간: 실질 {annual['real']:+.2f}% (±{annual['real_sd']:.2f}) · "
              f"경상 {annual['nominal']:+.2f}% (±{annual['nom_sd']:.2f}) · 디플레이터 {annual['deflator']:+.2f}%")
    for t in targets_out:
        print(f"{t['label']}: {t['nowcast']:+.2f}% (±{t['rmse']:.2f}, 정보 {t['months_known']}/3개월)")
    if ext["gni"] and ext["gni"].get("annual"):
        a = ext["gni"]["annual"]
        print(f"{a['year']}년 실질 GNI ≈ {a['gni']:+.1f}% (실질 GDP {a['gdp']:+.1f}%, 차이 {a['gap']:+.1f}%p)")
    if ext["labor"] and ext["labor"].get("est"):
        e = ext["labor"]["est"]
        print(f"{e['year']}년 노동소득분배율 ≈ {e['ls']:.1f}% ({e['year'] - 1}년 {e['prev']:.1f}%)")
    if ext["bop"] and ext["bop"].get("quarters"):
        b = ext["bop"]["quarters"][-1]
        print(f"경상수지 {b['q']}: 한국 {b['KR']} · 중국 {b['CN']} · 독일 {b['DE']} · 일본 {b['JP']} (억달러)")
    elif "extras" in cfg:
        print("경상수지: 자료 없음 — data/bop_codes.json 후보 목록 확인")
    if ext["exp"]:
        for row in ext["exp"]["rows"]:
            if row["est"]:
                print(f"{row['q']} 지출 측 합산 {row['gdp']:+.2f}% (생산 측 {row['prod']:+.2f}%)")
    return payload


if __name__ == "__main__":
    main()
