"""관세청 수출 속보(10일·20일·월) 자동 수집 → data/customs_flash.csv에 없는 줄만 추가.

출처 (1순위 → 2순위)
  ❶ 정책브리핑(korea.kr) 보도자료 검색 '수출입 현황' → 관세청 잠정치 보도자료 찾기
  ❷ 보도자료 페이지 meta description: "수출은 261억 달러로 전년동기대비 102.0% 증가"
  ❸ 첨부 PDF 본문(pypdf): 월간 잠정치(meta가 잘림)와 반도체 증감률 보강

실패해도 예외로 죽지 않음 → 경고만 남기고 기존 CSV(수동 입력 포함)로 계속 진행.
수동으로 넣은 줄이 있으면 (ref_month, period)가 같은 자동 수집분은 건너뜀 → 수동 우선.

사용: python -m nowcast.customs_scrape [--days 60] [--dry-run]
"""
from __future__ import annotations

import argparse
import html
import io
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "customs_flash.csv"
BASE = "https://www.korea.kr"
LIST_URL = BASE + "/briefing/pressReleaseList.do"
VIEW_URL = BASE + "/briefing/pressReleaseView.do"
DOWN_URL = BASE + "/common/download.do"
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
      "Accept-Language": "ko-KR,ko;q=0.9"}
TIMEOUT = 25

# ── 제목 → (연, 월, 기간) ─────────────────────────────────────────────
# "2026년 10월 1일 ~ 10월 10일 수출입 현황 [잠정치]" / "2026년 9월 수출입 현황 [잠정치]"
# 제외: "월간 수출입 현황 [확정치]"(한은 물량지수와 같은 시점이라 불필요), "운송비용", 산업부 "수출입 동향"
RE_PART = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*1\s*일\s*[~∼～-]\s*\d{1,2}\s*월\s*(10|20)\s*일\s*수출입\s*현황")
RE_MONTH = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*수출입\s*현황\s*\[?\s*잠정")

# ── 본문 숫자 ─────────────────────────────────────────────────────────
# 공백 제거 후 매칭 (PDF 추출은 띄어쓰기가 들쭉날쭉)
RE_EXP = [
    # "수출은 261억 달러로 전년동기대비 102.0% 증가"
    re.compile(r"수출은([\d,]+(?:\.\d+)?)억(?:달러|불)로?전년동(?:기|월)대비([\d.]+)%(증가|감소)"),
    # "수출은 전년 동월 대비 83.7% 증가한 1,211억 달러"
    re.compile(r"수출은전년동(?:기|월)대비([\d.]+)%(증가|감소)한([\d,]+(?:\.\d+)?)억(?:달러|불)"),
]
RE_CHIP = re.compile(r"반도체\(([+\-△▲▼]?)([\d.]+)%\)")


def _get(sess, url, **kw):
    r = sess.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _text(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def classify(title: str):
    """제목 → (ref_month 'YYYY-MM', period) 또는 None."""
    t = re.sub(r"\s+", " ", title)
    if "확정" in t or "운송" in t or "월간" in t:
        return None
    m = RE_PART.search(t)
    if m:
        y, mo, d = int(m[1]), int(m[2]), m[3]
        return f"{y:04d}-{mo:02d}", f"d{d}"
    m = RE_MONTH.search(t)
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}", "m"
    return None


def parse_list(page: str):
    """검색 결과 HTML → [{news_id, title, date}] (관세청 잠정치만)."""
    out, seen = [], set()
    hits = list(re.finditer(r"pressReleaseView\.do\?newsId=(\d+)", page))
    for i, m in enumerate(hits):
        nid = m[1]
        if nid in seen:
            continue
        # 이 링크 ~ 다음 링크 사이 텍스트에 제목·날짜가 있음
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(page), m.end() + 3000)
        chunk = _text(page[m.end():end])
        cls = classify(chunk)
        if not cls:
            continue
        d = re.search(r"(\d{4})[-.](\d{2})[-.](\d{2})", chunk)
        seen.add(nid)
        out.append({"news_id": nid, "ref_month": cls[0], "period": cls[1],
                    "date": f"{d[1]}-{d[2]}-{d[3]}" if d else ""})
    return out


def parse_numbers(text: str) -> dict:
    """본문/설명 텍스트 → {exports_yoy_pct, exports_usd_mn, chip_yoy_pct} (찾은 것만)."""
    s = re.sub(r"\s+", "", html.unescape(text))
    res = {}
    m = RE_EXP[0].search(s)
    if m:
        amt, pct, sign = m[1], m[2], m[3]
    else:
        m = RE_EXP[1].search(s)
        if m:
            pct, sign, amt = m[1], m[2], m[3]
    if m:
        v = float(pct) * (-1 if sign == "감소" else 1)
        if -95 < v < 1000:
            res["exports_yoy_pct"] = v
            res["exports_usd_mn"] = round(float(amt.replace(",", "")) * 100)
    c = RE_CHIP.search(s)
    if c:
        res["chip_yoy_pct"] = float(c[2]) * (-1 if c[1] in ("-", "△", "▼") else 1)
    return res


def parse_view(page: str):
    """보도자료 페이지 → (meta 설명 텍스트, 첨부 fileId 목록[pdf 우선])."""
    desc = ""
    for pat in (r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']*)',
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\'](?:og:)?description'):
        for m in re.finditer(pat, page, re.I):
            if len(m[1]) > len(desc):
                desc = m[1]
    files = []
    for m in re.finditer(r"download\.do\?fileId=(\d+)(?:&amp;|&)tblKey=(\w+)", page):
        near = page[max(0, m.start() - 400):m.end() + 400].lower()
        files.append((0 if ".pdf" in near else 1, m[1], m[2]))
    files = [(f, k) for _, f, k in sorted(set(files))]
    return html.unescape(desc), files


def pdf_text(data: bytes, max_pages: int = 3) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        rd = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in rd.pages[:max_pages])
    except Exception:
        return ""


def existing_keys(path: Path) -> set:
    keys = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("release_date"):
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 4 and p[3]:
            keys.add((p[1], p[2].lower()))
    return keys


def fetch_release(sess, item) -> dict | None:
    page = _get(sess, VIEW_URL, params={"newsId": item["news_id"]}).text
    desc, files = parse_view(page)
    nums = parse_numbers(desc)
    src = "meta"
    # 금액·증감률이 meta에 없거나(월간) 반도체가 없으면 PDF로 보강
    if "exports_yoy_pct" not in nums or "chip_yoy_pct" not in nums:
        for fid, key in files[:3]:
            try:
                r = _get(sess, DOWN_URL, params={"fileId": fid, "tblKey": key})
            except requests.RequestException:
                continue
            if not r.content.startswith(b"%PDF"):
                continue
            extra = parse_numbers(pdf_text(r.content))
            if extra:
                src = "pdf" if "exports_yoy_pct" not in nums else "meta+pdf"
            for k, v in extra.items():
                nums.setdefault(k, v)
            break
    if "exports_yoy_pct" not in nums:
        return None
    return {**item, **nums, "src": src}


def row_line(r: dict) -> str:
    f = lambda v: "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))
    return ",".join([r["date"], r["ref_month"], r["period"], f(r["exports_yoy_pct"]),
                     "", "", f(r.get("exports_usd_mn")), f(r.get("chip_yoy_pct"))])


def scrape(days: int = 60, path: Path = CSV, dry_run: bool = False, sess=None) -> list[dict]:
    sess = sess or requests.Session()
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days)
    items = []
    for page_no in (1, 2):
        params = {"srchWord": "수출입 현황", "startDate": start.isoformat(), "endDate": end.isoformat(),
                  "pageIndex": page_no, "repCodeType": "", "repCode": "", "period": ""}
        page = _get(sess, LIST_URL, params=params).text
        got = parse_list(page)
        items += [g for g in got if g["news_id"] not in {i["news_id"] for i in items}]
        if "pageIndex=" + str(page_no + 1) not in page:
            break
    have = existing_keys(path)
    new = []
    for it in sorted(items, key=lambda x: (x["ref_month"], x["period"])):
        if (it["ref_month"], it["period"]) in have:
            continue
        try:
            rec = fetch_release(sess, it)
        except requests.RequestException as e:
            print(f"  ! {it['ref_month']} {it['period']} 본문 실패: {e}")
            continue
        if rec is None:
            print(f"  ! {it['ref_month']} {it['period']} (newsId {it['news_id']}) 숫자 못 찾음 → 수동 입력 필요")
            continue
        new.append(rec)
        have.add((it["ref_month"], it["period"]))
    for r in new:
        print(f"  + {r['ref_month']} {r['period']}: 수출 {r['exports_yoy_pct']:+.1f}%"
              f" 반도체 {r.get('chip_yoy_pct', '—')} ({r['src']}, newsId {r['news_id']})")
    if new and not dry_run:
        txt = path.read_text(encoding="utf-8") if path.exists() else ""
        if txt and not txt.endswith("\n"):
            txt += "\n"
        txt += "\n".join(row_line(r) for r in new) + "\n"
        path.write_text(txt, encoding="utf-8")
    print(f"관세청 속보: 검색 {len(items)}건, 신규 {len(new)}건{' (dry-run)' if dry_run else ''}")
    return new


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        scrape(a.days, dry_run=a.dry_run)
    except Exception as e:  # 사이트 차단·구조 변경 → 경고만
        print(f"::warning::관세청 속보 자동 수집 실패 ({type(e).__name__}: {e}) — 기존 CSV로 진행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
