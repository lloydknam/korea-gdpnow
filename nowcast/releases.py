"""다음 발표 일정 — config/releases.yaml 규칙으로 날짜 추정(+확정 일정 덮어쓰기).

추정 날짜는 '예상'으로 표시. 주말·공휴일이면 다음 영업일로 미룸.
산업활동동향은 '분기 내 실적 개월 수'를 늘리므로 백테스트 RMSE로 오차범위가 얼마나 줄지 함께 표시.
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, datetime, timedelta

import holidays
import pandas as pd

WD = "월화수목금토일"


def _is_bday(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays.KR(years=d.year)


def _next_bday(d: date) -> date:
    while not _is_bday(d):
        d += timedelta(days=1)
    return d


def rule_date(rule: dict, y: int, m: int) -> date | None:
    if "day" in rule:
        d = date(y, m, min(rule["day"], _cal.monthrange(y, m)[1]))
        return d if rule.get("no_shift") else _next_bday(d)
    if rule.get("last_bday"):
        d = date(y, m, _cal.monthrange(y, m)[1])
        while not _is_bday(d):
            d -= timedelta(days=1)
        return d
    if "nth_weekday" in rule:
        n, w, months = (rule["nth_weekday"] + [None])[:3]
        if months and m not in months:
            return None
        d = date(y, m, 1)
        d += timedelta(days=(w - d.weekday()) % 7 + 7 * (n - 1))
        return _next_bday(d) if d.month == m else None
    if "last_weekday" in rule:
        w, months = (rule["last_weekday"] + [None])[:2]
        if months and m not in months:
            return None
        d = date(y, m, _cal.monthrange(y, m)[1])
        d -= timedelta(days=(d.weekday() - w) % 7)
        return _next_bday(d)
    return None


def _ref_label(ref: str, key: str, d: date) -> tuple[str, pd.Period]:
    p = pd.Period(d.strftime("%Y-%m"), "M")
    if ref == "prev":
        p -= 1
        return f"{p.month}월분", p
    if ref == "prevq":
        q = p.asfreq("Q") - 1
        return f"{q.year}년 {q.quarter}분기", q
    if key == "customs_d10":
        return f"{p.month}월 1~10일", p
    if key == "customs_d20":
        return f"{p.month}월 1~20일", p
    return f"{p.month}월분", p


def upcoming(cfg: dict, now: datetime, rmse: dict | None = None, horizon: int = 40, limit: int = 8) -> list[dict]:
    today = now.date()
    fixed = {k: [date.fromisoformat(str(x)) for x in v] for k, v in (cfg.get("fixed") or {}).items()}
    out = []
    months = pd.period_range(pd.Period(today.strftime("%Y-%m"), "M"), periods=3, freq="M")
    for r in cfg["releases"]:
        for p in months:
            d = rule_date(r["rule"], p.year, p.month)
            fx = [x for x in fixed.get(r["key"], []) if x.year == p.year and x.month == p.month]
            est = not fx
            if fx:
                d = fx[0]
            if d is None or d < today or (d - today).days > horizon:
                continue
            if d == today and r.get("time") and now.strftime("%H:%M") >= r["time"]:
                continue  # 오늘 이미 발표된 것
            ref, rp = _ref_label(r.get("ref", "prev"), r["key"], d)
            item = {"date": d.isoformat(), "dow": WD[d.weekday()], "time": r.get("time"), "key": r["key"],
                    "name": r["name"], "org": r.get("org"), "ref": ref, "est": est, "affects": r.get("affects", [])}
            if r["key"] == "activity" and rmse:
                q = rp.asfreq("Q")
                k_to = rp.month - q.asfreq("M", "start").month + 1
                a, b = rmse.get(str(k_to - 1)), rmse.get(str(k_to))
                if a and b:
                    item["impact"] = {"quarter": str(q), "k_from": k_to - 1, "k_to": k_to,
                                      "rmse_from": round(a, 2), "rmse_to": round(b, 2)}
            out.append(item)
    out.sort(key=lambda x: (x["date"], x["time"] or ""))
    return out[:limit]


# ------------------------------------------------------------------ 미수집 감시
FIX = {
    "customs": "data/customs_flash.csv에 한 줄 추가(휴대폰 GitHub에서 편집 → 저장하면 자동 재계산)",
    "ecos": "ECOS 반영 지연 — 보통 하루 안에 자동 해소. 사흘 넘게 계속되면 통계코드 변경 여부 확인",
    "gdp": "예상 발표일 기준 — 실제 일정이 다르면 config/releases.yaml의 fixed에 날짜 기입",
}


def overdue(cfg: dict, now: datetime, have: dict, grace_days: int = 2) -> list[dict]:
    """지난 발표인데 데이터가 아직 없는 항목.
    have: release key → 이미 반영된 가장 최근 대상 기간(pd.Period, 월 또는 분기). 키가 없으면 감시 안 함."""
    today = now.date()
    fixed = {k: [date.fromisoformat(str(x)) for x in v] for k, v in (cfg.get("fixed") or {}).items()}
    out = []
    for r in cfg["releases"]:
        k = r["key"]
        if k not in have:
            continue
        cur = pd.Period(today.strftime("%Y-%m"), "M")
        for p in (cur, cur - 1):  # 가장 최근에 지난 발표 하나만
            fx = [x for x in fixed.get(k, []) if x.year == p.year and x.month == p.month]
            d = fx[0] if fx else rule_date(r["rule"], p.year, p.month)
            if d is None or d > today:
                continue
            ref, rp = _ref_label(r.get("ref", "prev"), k, d)
            grace = grace_days + (2 if k == "gdp_adv" and not fx else 0)
            got = have[k]
            if (today - d).days >= grace and (got is None or got < rp):
                kind = "customs" if k.startswith("customs") else ("gdp" if k == "gdp_adv" else "ecos")
                item = {"key": k, "name": r["name"], "org": r.get("org"), "ref": ref, "ref_period": str(rp),
                        "date": d.isoformat(), "est": not fx, "days": (today - d).days, "fix": FIX[kind]}
                if kind == "customs":
                    per = {"customs_d10": "d10", "customs_d20": "d20", "customs_m": "m"}[k]
                    item["template"] = f"{d.isoformat()},{rp},{per},<수출 전년동기비 %>,,,<수출액 백만달러>,<반도체 %>"
                out.append(item)
            break
    return out
