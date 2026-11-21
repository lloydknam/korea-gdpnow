"""D램 현물가(트렌드포스·DRAMeXchange) 일별 수집 → data/dram_spot.csv. 선행 경고 지표용(모형 계산엔 안 씀).

현물가는 유통시장 시세라 대형 고객 계약가(한은 수출물가에 반영)보다 1~3개월 먼저 움직임.
  ❶ trendforce.com/price/dram/dram_spot 표의 'Session Average'(매일 18:10 대만시각 갱신)
  ❷ 직접 접속이 막히면 관세청 수집기와 같은 공개 웹 리더 경유
  ❸ 실패해도 예외로 죽지 않음 → 경고만, 기존 CSV로 계속

사용: python -m nowcast.dram_spot
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from . import customs_scrape as cs

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "dram_spot.csv"
URL = "https://www.trendforce.com/price/dram/dram_spot"
ITEMS = {  # 키: (세대, 용량, 구성) — 속도 표기(4800/5600, 3200)가 붙은 일반품만(eTT 제외)
    "ddr5_16g": ("DDR5", "16", "2Gx8"),
    "ddr4_16g": ("DDR4", "16", "2Gx8"),
    "ddr4_8g": ("DDR4", "8", "1Gx8"),
}
LABELS = {"ddr5_16g": "DDR5 16Gb", "ddr4_16g": "DDR4 16Gb", "ddr4_8g": "DDR4 8Gb"}
LEAD = "ddr5_16g"   # 대표지표(경고 판정 기준)
NUM = r"([\d,]+(?:\.\d+)?)"


def parse(page: str) -> tuple[str | None, dict[str, float]]:
    """페이지(HTML 또는 웹 리더 마크다운) → (기준일 YYYY-MM-DD, {키: 세션 평균가})."""
    t = cs._text(page).replace("|", " ")
    t = re.sub(r"\s+", " ", t)
    m = re.search(r"Last Update\s*(\d{4}-\d{2}-\d{2})", t)
    day = m[1] if m else None
    out = {}
    for k, (gen, gb, org) in ITEMS.items():
        pat = (rf"{gen}\s*{gb}\s*Gb\s*\(\s*{org}\s*\)\s*\d{{4}}(?:\s*/\s*\d{{4}})?\s+"
               + r"\s+".join([NUM] * 5))
        mm = re.search(pat, t)
        if mm:
            v = float(mm[5].replace(",", ""))   # 일고·일저·세션고·세션저 다음 = 세션 평균
            if 0.5 < v < 10000:
                out[k] = v
    return day, out


def fetch(sess, url: str = URL) -> str:
    """직접 → (막히거나 403이면) 공개 웹 리더 경유."""
    try:
        r = sess.get(url, headers=cs.UA, timeout=(8, 30))
        r.raise_for_status()
        if "Session Average" in r.text or "DDR5" in r.text or "DDR4" in r.text:
            return r.text
    except requests.RequestException as e:
        print(f"  · 트렌드포스 직접 접속 실패({type(e).__name__}) → 웹 리더 경유")
    cs._route["mode"] = 0
    return cs._get(sess, url).text


# ------------------------------------------------------------------ 계약가(월) — 장기 그래프용, data/dram_hist.csv에 자동 추가
CONTRACT_URL = "https://www.trendforce.com/price/dram/dram_contract"
HIST = ROOT / "data" / "dram_hist.csv"
MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_contract(page: str) -> tuple[str | None, float | None]:
    """계약가 표 → (대상 월 YYYY-MM, DDR4 8Gb 1Gx8 세션 평균). 표 제목 '(2H Sep)'·'(Oct)'의 달, 없으면 갱신일의 달."""
    t = re.sub(r"\s+", " ", cs._text(page).replace("|", " "))
    up = re.search(r"Last Update\s*(\d{4})-(\d{2})-(\d{2})", t)
    if not up:
        return None, None
    y, m = int(up[1]), int(up[2])
    lab = re.search(r"Contract Price\s*\(\s*(?:[12]H\s*)?([A-Z][a-z]{2})[a-z]*\.?\s*\)", t)
    if lab and lab[1] in MON:
        mm = MON[lab[1]]
        if mm > m:          # 1월에 갱신된 12월분 등
            y -= 1
        m = mm
    # 계약가 품목명은 'DDR4 8Gb 1Gx8'(속도 표기 없음). 현물 표의 'DDR4 8Gb (1Gx8) 3200'과 섞이지 않게 속도 표기 붙은 것은 제외
    mv = re.search(r"DDR4\s*8\s*Gb\s*\(?\s*1Gx8\s*\)?\s+(?!\d{4}\b)" + r"\s+".join([NUM] * 3), t)
    if not mv:
        return f"{y}-{m:02d}", None
    v = float(mv[3].replace(",", ""))   # 세션 고·저 다음 = 세션 평균
    return f"{y}-{m:02d}", (v if 0.3 < v < 1000 else None)


def collect_contract(path: Path = HIST, sess=None) -> str | None:
    """계약가 표에서 DDR4 8Gb 월 계약가를 읽어 dram_hist.csv에 추가·갱신(같은 달이면 최신 값으로 교체)."""
    sess = sess or requests.Session()
    try:
        page = fetch(sess, CONTRACT_URL)
    except requests.RequestException as e:
        print(f"::warning::D램 계약가 수집 실패 — {type(e).__name__} {e}")
        return None
    month, v = parse_contract(page)
    t = re.sub(r"\s+", " ", cs._text(page).replace("|", " "))
    i = t.find("DDR4 8Gb")
    print(f"  · 계약가 페이지 확인: 제목 '{(re.search(r'Contract Price[^|]{0,30}', t) or [''])[0]}' · "
          f"품목 부분 '{t[max(i, 0):max(i, 0) + 90] if i >= 0 else '없음'}'")
    if not month or v is None:
        print(f"::warning::D램 계약가 표를 못 읽음(대상 월 {month}) — 페이지 구조 확인 필요")
        return None
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["date,item,avg,src"]
    key = f"{month}-01,ddr4_8g_contract,"
    row = f"{key}{v:g},trendforce"
    old = next((ln for ln in lines if ln.startswith(key)), None)
    # 안전장치: 같은 달 기존 값과 25% 넘게, 또는 직전 달과 40% 넘게 다르면 잘못 읽은 것으로 보고 기록 안 함
    prev = sorted((ln.split(",")[0], float(ln.split(",")[2])) for ln in lines
                  if ",ddr4_8g_contract," in ln and ln.split(",")[0] < f"{month}-01")
    ref = float(old.split(",")[2]) if old else (prev[-1][1] if prev else None)
    lim = 0.25 if old else 0.40
    if ref and abs(v / ref - 1) > lim:
        print(f"::warning::D램 계약가 {month} {v:g}가 기준값 {ref:g}와 {abs(v / ref - 1) * 100:.0f}% 차이 → 잘못 읽었을 수 있어 기록 안 함")
        return None
    if old == row:
        print(f"D램 계약가 {month}: DDR4 8Gb {v:g} (변화 없음)")
        return month
    lines = [ln for ln in lines if not ln.startswith(key)] + [row]
    head = [ln for ln in lines if ln.startswith("#") or ln.startswith("date,")]
    body = sorted((ln for ln in lines if ln and not (ln.startswith("#") or ln.startswith("date,"))),
                  key=lambda ln: (ln.split(",")[1], ln.split(",")[0]))
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    print(f"D램 계약가 {month}: DDR4 8Gb {v:g}" + (f" (기존 {old.split(',')[2]} → 교체)" if old else " (신규)"))
    return month


def load(path: Path = CSV) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["date", "item", "avg", "src"])
    return pd.read_csv(path, comment="#", dtype={"date": str, "item": str})


def collect(path: Path = CSV, sess=None) -> int:
    sess = sess or requests.Session()
    try:
        page = fetch(sess)
    except requests.RequestException as e:
        print(f"::warning::D램 현물가 수집 실패 — {type(e).__name__} {e}")
        return 0
    day, vals = parse(page)
    if not day or not vals:
        print(f"::warning::D램 현물가 표를 못 읽음(기준일 {day}, 품목 {len(vals)}개) — 페이지 구조 확인 필요")
        return 0
    df = load(path)
    have = set(zip(df["date"], df["item"]))
    new = [{"date": day, "item": k, "avg": v, "src": "trendforce"} for k, v in vals.items() if (day, k) not in have]
    if new:
        txt = path.read_text(encoding="utf-8") if path.exists() else "date,item,avg,src\n"
        if not txt.endswith("\n"):
            txt += "\n"
        txt += "".join(f"{r['date']},{r['item']},{r['avg']:g},{r['src']}\n" for r in new)
        path.write_text(txt, encoding="utf-8")
    print(f"D램 현물가 {day}: " + ", ".join(f"{LABELS[k]} {v:g}" for k, v in vals.items()) + f" (신규 {len(new)}줄)")
    return len(new)


def _ago(s: pd.Series, days: int) -> float | None:
    """마지막 날짜 기준 days일 전(그 이전 가장 가까운 관측) 값."""
    if s.empty:
        return None
    cut = s.index.max() - timedelta(days=days)
    past = s[s.index <= cut]
    return float(past.iloc[-1]) if len(past) else None


def section(data_dir: Path, chip_trend: float | None = None) -> dict | None:
    """화면용 시계열 + 경고 신호. chip_trend = 한은 반도체 수출물가(달러) 최근 월 추세(%)."""
    df = load(data_dir / "dram_spot.csv")
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    series, sig = {}, []
    for k in ITEMS:
        s = df[df["item"] == k].drop_duplicates("date", keep="last").set_index("date")["avg"].sort_index()
        if s.empty:
            continue
        series[k] = [{"d": d.strftime("%Y-%m-%d"), "v": round(float(v), 3)} for d, v in s.items()]
        last = float(s.iloc[-1])
        c7, c30 = _ago(s, 7), _ago(s, 28)
        sig.append({"k": k, "label": LABELS[k], "last": round(last, 3), "date": s.index.max().strftime("%Y-%m-%d"),
                    "d7": None if not c7 else round((last / c7 - 1) * 100, 2),
                    "d30": None if not c30 else round((last / c30 - 1) * 100, 2)})
    if not sig:
        return None
    # 대표지표 = DDR5 16Gb(최신 서버·PC 주메모리, AI 서버 CPU 쪽). DDR4는 단종으로 공급이 줄어
    # 가격이 AI 수요보다 공급 요인에 크게 좌우 → 참고용. 1개월 자료가 없으면 수집 시작일 대비 변화(7일 이상일 때)
    lead = next((x for x in sig if x["k"] == LEAD), None)
    if lead is not None:
        s = df[df["item"] == LEAD].drop_duplicates("date", keep="last").set_index("date")["avg"].sort_index()
        span = (s.index.max() - s.index.min()).days
        if lead["d30"] is not None:
            lead["chg"], lead["win"] = lead["d30"], "1개월"
        elif span >= 7:
            lead["chg"], lead["win"] = round((float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100, 2), f"{span}일"
        else:
            lead = None
    if lead is not None:
        c, w, lab = lead["chg"], lead["win"], lead["label"]
        if c <= -3:
            level, text = "warn", f"{lab} 현물 {w} {c:+.1f}% → 1~3개월 뒤 계약가·수출물가 둔화 가능"
        elif c < 0 or (lead["d7"] or 0) <= -1.5:
            level, text = "watch", f"{lab} 현물 {w} {c:+.1f}% → 상승 탄력 약화, 지켜볼 구간"
        elif c >= 3:
            level, text = "up", f"{lab} 현물 {w} {c:+.1f}% → 계약가 추가 상승 여지"
        else:
            level, text = "flat", f"{lab} 현물 {w} {c:+.1f}% 안팎 보합"
    else:
        level, text = "na", f"{LABELS[LEAD]} 비교 자료 쌓이는 중(7일 이상 필요)"
    if chip_trend is not None and level in ("warn", "watch") and chip_trend > 0:
        text += f" · 모형은 수출물가 월 {chip_trend:+.1f}% 추세 연장 중"
    series = {k: v for k, v in series.items() if k == LEAD}
    sig = [x for x in sig if x["k"] == LEAD]
    if not series:
        return None
    return {"series": series, "signal": sig, "level": level, "text": text, "lead": LEAD if lead is not None else None,
            "hist": long_term(data_dir / "dram_hist.csv"),
            "gpu": _gpu(data_dir),
            "src": "TrendForce·DRAMeXchange 현물 세션 평균가(달러/개)"}


def _gpu(data_dir: Path):
    try:
        from . import gpu_rent
        return gpu_rent.section(data_dir)
    except Exception:  # noqa: BLE001
        return None


def long_term(path: Path = HIST, spot_path: Path | None = None) -> dict | None:
    """장기 시계열(월). DDR5 16Gb(대표): 현물·계약 앵커 + 일별 수집 월말 값. DDR4 8Gb(참고): 계약가(트렌드포스 자동 추가)·현물."""
    if not path.exists():
        return None
    df = pd.read_csv(path, comment="#", dtype={"date": str, "item": str})
    if df.empty:
        return None
    out = {}
    for item, key in (("ddr4_8g_contract", "contract"), ("ddr4_8g_spot", "spot"),
                      ("ddr5_16g_contract", "ddr5_contract"), ("ddr5_16g_spot", "ddr5_spot")):
        d = df[df["item"] == item].sort_values("date")
        out[key] = [{"d": r.date, "v": round(float(r.avg), 3), "src": r.src} for r in d.itertuples()]
    # 현물: 일별 수집 자료가 있는 달은 그 달 마지막 관측값(정리본에 없는 달만)
    sp = load(spot_path or (path.parent / "dram_spot.csv"))
    for daily, key in (("ddr4_8g", "spot"), ("ddr5_16g", "ddr5_spot")):
        x = sp[sp["item"] == daily] if not sp.empty else sp
        if x.empty:
            continue
        have = {p["d"][:7] for p in out[key]}
        x = x.assign(m=x["date"].str[:7]).sort_values("date")
        for m, g in x.groupby("m"):
            if m not in have:
                out[key].append({"d": f"{m}-01", "v": round(float(g["avg"].iloc[-1]), 3), "src": "daily"})
        out[key].sort(key=lambda p: p["d"])
    auto = [x["d"][:7] for x in out["contract"] if x["src"] == "trendforce"]
    out["auto_from"] = min(auto) if auto else None
    return out if any(out[k] for k in ("contract", "spot", "ddr5_spot", "ddr5_contract")) else None

def main(argv=None) -> int:
    collect()
    collect_contract()
    try:
        from . import gpu_rent
        gpu_rent.collect()
    except Exception as e:  # noqa: BLE001
        print(f"::warning::GPU 임대료 수집 생략 — {type(e).__name__} {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
