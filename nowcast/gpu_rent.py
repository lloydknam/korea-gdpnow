"""AI 수요 온도계: GPU 임대료 지수(Silicon Data Silicon Index)의 1주 변화 방향.

  - 공개 페이지에 매 영업일 갱신되는 H100(SDH100RT)·B200(SDB200RT) 임대료 지수와 '1주 대비 변화'가 표시됨
  - 상업 지수라 지수 값 자체는 저장·게시하지 않고, 1주 변화의 방향(▲/▼)과 크기 구간만 기록 → 값은 원 사이트 링크
  - 실패해도 예외로 죽지 않음(경고만)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from . import customs_scrape as cs

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "gpu_rent.csv"
PAGE = "https://www.silicondata.com/products/silicon-index?gpu={g}"
ITEMS = {"h100": ("H100", "SDH100RT"), "b200": ("B200", "SDB200RT")}
KST = timezone(timedelta(hours=9))


def parse(page: str, ticker: str) -> tuple[int, float] | None:
    """페이지 원문(HTML 또는 웹 리더 마크다운) → (방향 +1/−1/0, 1주 변화 %의 절댓값).
    메뉴에도 티커가 나오므로 '…%vs 7D' 표시마다 바로 앞에서 가장 가까운 티커를 찾아 짝지음."""
    for m in re.finditer(r"([+\-−]?)(\d+(?:\.\d+)?)\s*%\s*(?:<[^>]*>\s*)*vs\s*7\s*D", page, re.I):
        pre = page[max(0, m.start() - 2500):m.start()]
        tks = list(re.finditer(r"SD[A-Z0-9]+RT", pre))
        if not tks or tks[-1][0] != ticker:
            continue
        seg = pre[tks[-1].end():]
        arr = re.findall(r"arrow[-_]?(up|down)", seg, re.I)
        v = float(m[2])
        if arr:
            return (1 if arr[-1].lower() == "up" else -1), v
        return (0 if v == 0 else (-1 if m[1] in ("-", "−") else 1)), v
    return None


def bucket(v: float) -> str:
    return "보합" if v < 1 else ("소폭" if v < 5 else ("큰 폭" if v < 15 else "급격히"))


def collect(path: Path = CSV, sess=None, now: datetime | None = None) -> int:
    sess = sess or requests.Session()
    day = (now or datetime.now(KST)).strftime("%Y-%m-%d")
    rows = []
    for k, (g, tk) in ITEMS.items():
        try:
            try:
                r = sess.get(PAGE.format(g=k), headers=cs.UA, timeout=(8, 30))
                r.raise_for_status()
                page = r.text
                if tk not in page:
                    raise requests.RequestException("ticker not in page")
            except requests.RequestException:
                cs._route["mode"] = 0
                page = cs._get(sess, PAGE.format(g=k)).text
        except requests.RequestException as e:
            print(f"::warning::GPU 임대료({g}) 수집 실패 — {type(e).__name__}")
            continue
        p = parse(page, tk)
        if not p:
            j = page.find(tk)
            print(f"::warning::GPU 임대료({g}) 표를 못 읽음 — 부분: {page[max(j, 0):max(j, 0) + 160]!r}" if j >= 0
                  else f"::warning::GPU 임대료({g}) 페이지에 {tk} 없음")
            continue
        rows.append({"date": day, "item": k, "dir": p[0], "chg7": round(p[1], 1)})
        print(f"GPU 임대료 {g}: 1주 {'▲' if p[0] > 0 else '▼' if p[0] < 0 else '–'} {bucket(p[1])}")
    if not rows:
        return 0
    old = pd.read_csv(path, comment="#", dtype={"date": str}) if path.exists() else pd.DataFrame(columns=["date", "item", "dir", "chg7"])
    old = old[~((old["date"] == day) & old["item"].isin([r["item"] for r in rows]))]
    df = pd.concat([old, pd.DataFrame(rows)], ignore_index=True).sort_values(["date", "item"])
    path.write_text("# GPU 임대료 지수(Silicon Data) 1주 변화의 방향·크기만 기록(지수 값은 저장 안 함). dir: +1 상승, -1 하락\n"
                    + df.to_csv(index=False), encoding="utf-8")
    return len(rows)


def section(data_dir: Path) -> dict | None:
    p = data_dir / "gpu_rent.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, comment="#", dtype={"date": str})
    if df.empty:
        return None
    out = {}
    for k, (g, tk) in ITEMS.items():
        d = df[df["item"] == k].sort_values("date")
        if d.empty:
            continue
        last = d.iloc[-1]
        out[k] = {"label": g, "ticker": tk, "date": last["date"], "dir": int(last["dir"]),
                  "size": bucket(float(last["chg7"])), "url": PAGE.format(g=k),
                  "hist": [{"d": r.date, "dir": int(r.dir)} for r in d.tail(20).itertuples()]}
    return {"items": out, "src": "Silicon Data · Silicon Index(GPU 시간당 임대료)"} if out else None
