"""품목별 월별 수출액 (연간 누적 그래프용).

출처
  ❶ 공공데이터포털 '관세청_품목별 수출입실적(GW)' (확정치, 매월 15일경 전월분)
     apis.data.go.kr/1220000/Itemtrade/getItemtradeList?serviceKey&strtYymm&endYymm&hsSgn
     - 품목군 = HS 코드 묶음(아래 GROUPS), 전체 = HS 2단위 97개 류(類) 합
     - 키: GitHub Secret DATA_GO_KR_KEY (없으면 이 부분 건너뜀)
     - 받은 값은 data/exports_items.csv에 쌓음 → 새 달이 없을 때만 다시 조회
  ❷ API에 아직 없는 달: 관세청 월간 잠정치 보도자료(data/customs_flash.csv의 period=m 줄)
     - 전체 = 수출액, 반도체 = 전년 같은 달 × (1 + 반도체 증감률)
     - 나머지 품목군 = 전년 같은 달 × (1 + 보도자료 품목 증감률), 없으면 최근 3개월 추세 → 합계에 맞게 비례 조정
     - 확정치가 들어오면 자동 교체

품목군은 HS 기준 근사 → 산업부 MTI 15대 품목 분류와 조금 다름.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
GROUPS = [  # (키, 이름, HS 코드('-'는 빼기), 속보 품목명 후보) — 산업부 15대 품목을 HS로 근사
    ("chip", "반도체", ["8541", "8542"], ["반도체"]),
    ("auto", "자동차·부품", ["8703", "8704", "8708"], ["자동차", "승용차"]),
    ("mach", "일반기계", ["84", "-8471", "-8473", "-8415", "-8418", "-8450"], ["일반기계"]),
    ("chem", "석유화학", ["29", "39"], ["석유화학"]),
    ("petro", "석유제품", ["2710"], ["석유제품"]),
    ("steel", "철강", ["72", "73"], ["철강", "철강제품"]),
    ("ship", "선박", ["89"], ["선박"]),
    ("ict", "IT기기", ["8517", "8471", "8473", "8524"], ["무선통신기기", "무선통신", "컴퓨터"]),
    ("bio", "바이오·화장품", ["30", "3304"], ["바이오헬스"]),
    ("batt", "이차전지", ["8507"], ["이차전지"]),
    ("power", "전력기기·전선", ["8501", "8502", "8504", "8535", "8536", "8537", "8544"], []),
    ("appl", "가전", ["8415", "8418", "8450", "8516", "8528"], ["가전"]),
    ("prec", "정밀기기", ["90"], ["정밀기기"]),
    ("nonfe", "비철금속", ["74", "75", "76", "78", "79", "80", "81"], []),
    ("gold", "귀금속", ["71"], []),
    ("tex", "섬유", [f"{i}" for i in range(50, 64)], ["섬유"]),
    ("food", "농수산식품", [f"{i:02d}" for i in range(1, 25)], []),
]
OTHER = ("other", "기타")
CHAPTERS = [f"{i:02d}" for i in range(1, 98) if i != 77]   # HS 2단위(77류는 없음)
TIMEOUT, PAUSE = 30, 0.15


class ApiError(RuntimeError):
    pass


def _query(sess, key: str, start: str, end: str, hs: str) -> dict[str, float]:
    """한 HS 코드의 월별 수출액(달러) {YYYY-MM: usd}. 응답 줄이 여러 자리수로 섞이면 가장 짧은 코드만 합산."""
    r = sess.get(URL, params={"serviceKey": key, "strtYymm": start, "endYymm": end, "hsSgn": hs}, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    code = root.findtext(".//resultCode")
    if code and code not in ("00", "0", "000"):
        raise ApiError(f"{code} {root.findtext('.//resultMsg')}")
    rows = []
    for it in root.iter("item"):
        d = {c.tag: (c.text or "").strip() for c in it}
        ym = d.get("year") or d.get("yymm") or ""
        ym = ym.replace(".", "").replace("-", "")
        if len(ym) != 6 or not ym.isdigit():   # '총계' 줄 등
            continue
        try:
            usd = float(d.get("expDlr") or d.get("expdlr") or "nan")
        except ValueError:
            continue
        rows.append((f"{ym[:4]}-{ym[4:]}", d.get("hsCd") or d.get("hsSgn") or hs, usd))
    if not rows:
        return {}
    shortest = min(len(h) for _, h, _ in rows)
    out: dict[str, float] = {}
    for m, h, usd in rows:
        if len(h) == shortest and usd == usd:
            out[m] = out.get(m, 0.0) + usd
    return out


def _windows(start: pd.Period, end: pd.Period):
    p = start
    while p <= end:
        q = min(p + 11, end)
        yield p, q
        p = q + 1


def fetch(key: str, start: pd.Period, end: pd.Period, sess=None) -> pd.DataFrame:
    """start~end 품목군·전체 월별 수출액(백만달러) → long DataFrame(month, group, usd_mn)."""
    sess = sess or requests.Session()
    acc: dict[tuple[str, str], float] = {}
    for a, b in _windows(start, end):
        s, e = a.strftime("%Y%m"), b.strftime("%Y%m")
        memo: dict[str, dict] = {}

        def q(hs):   # 같은 코드(예: 8471은 기계에서 빼고 IT에 더함)는 한 번만 조회
            if hs not in memo:
                memo[hs] = _query(sess, key, s, e, hs)
                time.sleep(PAUSE)
            return memo[hs]

        for g, _, codes, _ in GROUPS:
            for code in codes:
                sign, hs = (-1, code[1:]) if code.startswith("-") else (1, code)
                for m, v in q(hs).items():
                    acc[(m, g)] = acc.get((m, g), 0.0) + sign * v
        for hs in CHAPTERS:
            for m, v in q(hs).items():
                acc[(m, "total")] = acc.get((m, "total"), 0.0) + v
                acc[(m, f"ch{hs}")] = v
    df = pd.DataFrame([{"month": m, "group": g, "usd_mn": round(v / 1e6, 1)} for (m, g), v in acc.items()])
    if df.empty:
        return df
    full = set(df.loc[df["group"] == "total", "month"])   # 전체가 있는 달만(부분 공개 달 제외)
    return df[df["month"].isin(full)].sort_values(["month", "group"]).reset_index(drop=True)


def update_cache(key: str | None, path: Path, now: datetime, first_year: int, sess=None) -> pd.DataFrame:
    """캐시에 없는 달(또는 최근 2개월 재조회)을 받아 붙임. 키가 없거나 실패하면 캐시 그대로."""
    cache = pd.read_csv(path, dtype={"month": str}) if path.exists() else pd.DataFrame(columns=["month", "group", "usd_mn"])
    if not key:
        return cache
    cur = pd.Period(now.strftime("%Y-%m"), "M")
    want_last = cur - 1
    need = {"total", "ch85"} | {g for g, *_ in GROUPS}
    by_m = cache.groupby("month")["group"].apply(set) if not cache.empty else pd.Series(dtype=object)
    have = {m for m, gs in by_m.items() if need <= gs}
    first = pd.Period(f"{first_year}-01", "M")
    missing = [p for p in pd.period_range(first, want_last, freq="M") if str(p) not in have]
    if not missing:
        return cache
    # 확정치는 매월 15일경 → 그 전엔 전월분이 없는 게 정상. 과거 공백만 있으면 언제든, 전월만 없으면 10일 이후
    if missing == [want_last] and now.day < 10:
        return cache
    # 같은 날 이미 헛걸음했으면 다시 안 부름(하루 1번 → 호출 수 절약)
    tried = path.with_name("exports_items_try.txt")
    stamp = f"{now:%Y-%m-%d} {missing[0]} {len(missing)} {len(need)}"   # 품목군이 바뀌면 같은 날이라도 다시
    if tried.exists() and tried.read_text().strip() == stamp:
        return cache
    start = min(missing[0], want_last - 1) if have else first
    try:
        new = fetch(key, start, want_last, sess)
    except (requests.RequestException, ET.ParseError, ApiError) as e:
        print(f"::warning::품목별 수출(data.go.kr) 조회 실패 — {type(e).__name__} {e}")
        return cache
    if new.empty or str(want_last) not in set(new["month"]):
        tried.write_text(stamp)
    if new.empty:
        print(f"::notice::품목별 수출: {start}~{want_last} 새 자료 없음(아직 미공개)")
        return cache
    got = set(new["month"])
    cache = pd.concat([cache[~cache["month"].isin(got)], new], ignore_index=True).sort_values(["month", "group"])
    cache.to_csv(path, index=False)
    print(f"::notice::품목별 수출: {min(got)}~{max(got)} 반영(확정치)")
    return cache


def _flash_rows(flash: pd.DataFrame, piv: pd.DataFrame) -> list[dict]:
    """확정치 없는 달 → 관세청 월간 잠정치로 품목군 추정."""
    out = []
    if flash is None or flash.empty:
        return out
    fm = flash[flash["period"] == "m"].dropna(subset=["exports_usd_mn"])
    last = piv.index.max() if not piv.empty else None
    for _, row in fm.sort_values("ref").iterrows():
        m = str(row["ref"])
        if last is not None and m <= last:
            continue
        base = str(row["ref"] - 12)
        if base not in piv.index:
            continue
        b = piv.loc[base]
        total = float(row["exports_usd_mn"])
        chip_yoy = row.get("chip_yoy_pct")
        items = parse_items(row.get("items_yoy"))
        keys = [g for g, *_ in GROUPS] + ["other"]
        # 품목군 최근 추세: 확정치 마지막 3개월의 전년동기비 (반도체發 메모리 붐처럼 품목마다 다른 흐름 반영)
        recent = [mm for mm in piv.index[-3:] if str(pd.Period(mm, "M") - 12) in piv.index]
        prior = [str(pd.Period(mm, "M") - 12) for mm in recent]

        def trend(g):
            den = float(piv.loc[prior, g].sum()) if prior else 0.0
            return float(piv.loc[recent, g].sum()) / den - 1 if den > 0 else 0.0

        vals, fixed = {}, set()
        if pd.notna(chip_yoy):
            vals["chip"] = b["chip"] * (1 + float(chip_yoy) / 100)
            fixed.add("chip")
        for g, _, _, names in GROUPS:
            yy = next((items[n] for n in names if n in items), None)
            if g != "chip" and yy is not None:
                vals[g] = b[g] * (1 + yy / 100)
                fixed.add(g)
        flex = [g for g in keys if g not in fixed]
        pre = {g: max(0.0, b[g] * (1 + trend(g))) for g in flex}
        room = total - sum(vals.values())
        sc = room / sum(pre.values()) if sum(pre.values()) > 0 and room > 0 else 1.0
        for g in flex:   # 보도자료에 없는 품목군: 추세로 나눈 뒤 잠정치 합계에 맞게 비례 조정
            vals[g] = pre[g] * sc
        out.append({"m": m, **{g: round(float(v), 1) for g, v in vals.items()}, "total": round(total, 1), "est": True,
                    "src": "관세청 월간 잠정치", "items_from": sorted(n for n in items)})
    return out


def _log_other(piv: pd.DataFrame, year: int) -> None:
    """기타에 남은 몫이 큰 HS 류(품목군에 한 코드도 안 쓰인 류)를 로그로 → 다음 분리 후보."""
    used = {c.lstrip("-")[:2] for _, _, codes, _ in GROUPS for c in codes}
    ytd = piv[piv.index.str.startswith(str(year))]
    if ytd.empty or "total" not in ytd:
        return
    tot = float(ytd["total"].sum())
    cand = sorted(((float(ytd[c].sum()), c[2:]) for c in ytd.columns if len(c) == 4 and c.startswith("ch") and c[2:].isdigit() and c[2:] not in used),
                  reverse=True)[:8]
    if cand and tot > 0:
        print("::notice::기타 중 큰 HS 류(올해 누적 비중): " + ", ".join(f"{c}류 {v / tot * 100:.1f}%" for v, c in cand)
              + f" · 기타 합 {float(ytd['other'].sum()) / tot * 100:.1f}%")


def parse_items(s) -> dict[str, float]:
    """'승용차:+5.2|선박:-3.0' → {'승용차': 5.2, '선박': -3.0}"""
    out = {}
    if not isinstance(s, str):
        return out
    for part in s.split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out


def section(key: str | None, data_dir: Path, now: datetime, flash: pd.DataFrame | None = None, sess=None) -> dict | None:
    year = now.year if now.month > 1 else now.year - 1
    cache = update_cache(key, data_dir / "exports_items.csv", now, year - 1, sess)
    if cache.empty:
        if not key:
            print("::notice::품목별 수출: DATA_GO_KR_KEY 없음 → 건너뜀")
        return None
    piv = cache.pivot_table(index="month", columns="group", values="usd_mn", aggfunc="sum").sort_index()
    for g, *_ in GROUPS:
        if g not in piv:
            piv[g] = 0.0
    piv = piv.fillna(0.0)
    piv["other"] = (piv["total"] - piv[[g for g, *_ in GROUPS]].sum(axis=1)).clip(lower=0)
    _log_other(piv, year)
    rows = [{"m": m, **{k: round(float(piv.loc[m, k]), 1) for k in [g for g, *_ in GROUPS] + ["other", "total"]},
             "est": False} for m in piv.index]
    rows += _flash_rows(flash, piv)
    keep = [r for r in rows if r["m"][:4] in (str(year - 1), str(year))]
    if not any(r["m"][:4] == str(year) for r in keep):
        return None
    return {"year": year, "groups": [{"k": g, "label": lab} for g, lab, *_ in GROUPS] + [{"k": OTHER[0], "label": OTHER[1]}],
            "rows": keep, "src": "관세청 품목별 수출입실적(HS 기준 품목군) · 최근 달은 관세청 월간 잠정치",
            "hs": {g: codes for g, _, codes, _ in GROUPS}}


def probe_ecos(ecos_key: str | None, data_dir: Path, now: datetime, session=None) -> None:
    """공공데이터포털 키가 없을 때: 한은 ECOS에 품목별 수출 통계표가 있는지 이름으로 찾아 기록(7일에 1번).
    결과 → data/ecos_export_probe.json (후보 표·항목 이름). 계산엔 아직 안 씀."""
    if not ecos_key:
        return
    import json
    from .bop import Ecos
    path = data_dir / "ecos_export_probe.json"
    try:
        old = json.loads(path.read_text()) if path.exists() else {}
        if old.get("at") and (now - datetime.fromisoformat(old["at"]).replace(tzinfo=now.tzinfo)).days < 7:
            return
    except (ValueError, TypeError):
        pass
    try:
        e = Ecos(ecos_key, session)
        keys = ("품목", "MTI", "SITC", "성질", "통관", "상품")
        cands = [t for t in e.tables() if "수출" in (t.get("STAT_NAME") or "") and any(k in t["STAT_NAME"] for k in keys)]
        out = []
        for t in cands[:15]:
            try:
                its = e.items(t["STAT_CODE"])
            except Exception:  # noqa: BLE001
                its = []
            out.append({"stat": t["STAT_CODE"], "name": t["STAT_NAME"], "cycle": t.get("CYCLE"),
                        "items": [f"{i.get('ITEM_CODE')} {i.get('ITEM_NAME')} [{i.get('CYCLE')}, {i.get('UNIT_NAME')}]"
                                  for i in its[:40]]})
            print(f"::notice::ECOS 품목별 수출 후보: {t['STAT_CODE']} {t['STAT_NAME']} ({len(its)}개 항목)")
        path.write_text(json.dumps({"at": now.isoformat(timespec="minutes"), "tables": out}, ensure_ascii=False, indent=1))
        if not out:
            print("::notice::ECOS 품목별 수출 후보 없음")
    except Exception as ex:  # noqa: BLE001
        print(f"::warning::ECOS 품목별 수출 탐색 실패 — {type(ex).__name__} {ex}")

