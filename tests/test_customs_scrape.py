"""관세청 속보 수집기 — 정책브리핑 페이지 모양을 흉내 낸 고정 HTML로 검증 (네트워크 없음)."""
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nowcast import customs_scrape as cs  # noqa: E402

LIST = """
<ul class="list">
<li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156785249&pageIndex=1&repCodeType=&repCode=&startDate=2026-08-01&endDate=2026-10-18&srchWord=수출입현황&period=">
  <span class="text"><strong>2026년 9월 수출입 운송비용 현황</strong></span><span class="source"><span>2026-10-15</span><span>관세청</span></span></a></li>
<li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156785248&pageIndex=1">
  <strong>2026년 9월 월간 수출입 현황 [확정치]</strong><span>2026-10-15</span><span>관세청</span></a></li>
<li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156784799&pageIndex=1">
  <strong>2026년 10월 1일 ~ 10월 10일 수출입 현황 [잠정치]</strong><span>2026-10-12</span><span>관세청</span></a></li>
<li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156783912&pageIndex=1">
  <strong>2026년 9월 수출입 동향</strong><span>2026-10-01</span><span>산업통상부</span></a></li>
<li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156783871&pageIndex=1">
  <strong>2026년 9월 수출입 현황 [잠정치]</strong><span>2026-10-01</span><span>관세청</span></a></li>
<li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156782594&pageIndex=1">
  <strong>2026년 9월 1일 ~ 9월 20일 수출입 현황 [잠정치]</strong><span>2026-09-21</span><span>관세청</span></a></li>
</ul>"""

VIEW = {
    "156784799": """<head><meta name="description" content="2026년 10월 1일 ~ 10월 10일 수출입 현황 [잠정치]-◇관세청은 12일, 10월 1일~10일 기간의 수출입 현황 잠정치를 발표했다.◇ 동기간 수출은 261억 달러로 전년동기대비 102.0% 증가, 수입은 223억 달러로 65.5% - 정책브리핑">
      </head><body><a href="/common/download.do?fileId=111&amp;tblKey=GMN">자료.hwpx</a>
      <a href="/common/download.do?fileId=112&amp;tblKey=GMN">자료.pdf</a></body>""",
    "156783871": """<meta property="og:description" content="2026년 9월 수출입 현황 [잠정치]-수출 및 무역수지 역대최대/반도체 역대최대 수출실적 / &#39;26년 누적수출액 8천억 달러 돌파 ◇ 관세청은 1일, 9월 1일~ - 정책브리핑">
      <a href="/common/download.do?fileId=198563262&amp;tblKey=GMN">261001 2026년 9월 수출입 현황[잠정치].hwpx</a>
      <a href="/common/download.do?fileId=198563263&amp;tblKey=GMN">261001 2026년 9월 수출입 현황[잠정치].pdf</a>""",
}
PDF_TEXT = {
    "112": "주요 수출 품목은 반 도체(170.9%), 석유제품(45.6%), 승용차(82.7%) 등에서 큰 폭 증가함.",
    "198563263": "□ 9월 수출은 1,209억 달러로 전년동기대비 83.5% 증가, 수입은 711억 달러로\n26.0% 증가. 반도체(263.0%) 등",
}


class Resp:
    def __init__(self, text="", content=b""):
        self.text, self.content = text, content

    def raise_for_status(self):
        pass


class FakeSession:
    def get(self, url, params=None, **kw):
        if url == cs.LIST_URL:
            return Resp(LIST if params["pageIndex"] == 1 else "")
        if url == cs.VIEW_URL:
            return Resp(VIEW.get(params["newsId"], "<html></html>"))
        if url == cs.DOWN_URL:
            fid = params["fileId"]
            return Resp(content=(b"%PDF-fake:" + fid.encode()) if fid in PDF_TEXT else b"PK..")
        raise AssertionError(url)


class RelaySession(FakeSession):
    """korea.kr 직접 접속은 시간 초과, r.jina.ai 경유는 성공하는 GitHub 서버 상황."""
    def get(self, url, params=None, headers=None, **kw):
        from urllib.parse import parse_qsl, urlsplit
        import requests
        if url.startswith(cs.BASE):
            raise requests.ConnectTimeout("timed out")
        assert url.startswith("https://r.jina.ai/https://www.korea.kr/"), url
        u = urlsplit(url[len("https://r.jina.ai/"):])
        q = dict(parse_qsl(u.query))
        if "pageIndex" in q:
            q["pageIndex"] = int(q["pageIndex"])
        r = super().get(f"{u.scheme}://{u.netloc}{u.path}", params=q)
        if u.path.endswith("download.do") and r.content.startswith(b"%PDF"):
            return Resp(text=PDF_TEXT[q["fileId"]], content=PDF_TEXT[q["fileId"]].encode())  # 리더가 PDF를 텍스트로
        return Resp(text=r.text + " " * 300, content=r.content)


def test():
    cs.pdf_text = lambda data, max_pages=3: PDF_TEXT.get(data.split(b":")[1].decode(), "")

    # 분류
    assert cs.classify("2026년 10월 1일 ~ 10월 10일 수출입 현황 [잠정치]") == ("2026-10", "d10")
    assert cs.classify("2026년 9월 1일 ~ 9월 20일 수출입 현황 [잠정치]") == ("2026-09", "d20")
    assert cs.classify("2026년 9월 수출입 현황 [잠정치]") == ("2026-09", "m")
    assert cs.classify("2026년 9월 월간 수출입 현황 [확정치]") is None
    assert cs.classify("2026년 9월 수출입 동향") is None
    # 숫자
    n = cs.parse_numbers("동기간 수출은 261억 달러로 전년동기대비 102.0% 증가, 수입은")
    assert n == {"exports_yoy_pct": 102.0, "exports_usd_mn": 26100}, n
    n = cs.parse_numbers("수출은 132억 달러로 전년동기대비 5.2% 감소, 반도체(△3.1%)")
    assert n["exports_yoy_pct"] == -5.2 and n["chip_yoy_pct"] == -3.1, n
    n = cs.parse_numbers("9월 수출은 전년 동월 대비 83.7% 증가한 1,211억 달러로")
    assert n["exports_yoy_pct"] == 83.7 and n["exports_usd_mn"] == 121100, n

    items = cs.parse_list(LIST)
    assert [(i["ref_month"], i["period"], i["date"]) for i in items] == [
        ("2026-10", "d10", "2026-10-12"), ("2026-09", "m", "2026-10-01"), ("2026-09", "d20", "2026-09-21")], items

    tmp = Path(tempfile.mkdtemp()) / "customs_flash.csv"
    shutil.copy(cs.CSV, tmp)
    before = tmp.read_text(encoding="utf-8")
    cs._route["mode"] = "direct"
    new = cs.scrape(path=tmp, sess=FakeSession())
    # 9월 d20·m은 이미 있음 → 10월 d10만 추가, 반도체는 PDF에서
    assert [(r["ref_month"], r["period"]) for r in new] == [("2026-10", "d10")], new
    after = tmp.read_text(encoding="utf-8")
    assert after.startswith(before) and after.endswith("2026-10-12,2026-10,d10,102,,,26100,170.9\n"), after[-80:]
    # 재실행 → 중복 없음
    assert cs.scrape(path=tmp, sess=FakeSession()) == []

    # 빈 CSV에서는 월간 잠정치를 PDF로 채움
    tmp.write_text("release_date,ref_month,period,exports_yoy_pct,workdays,workdays_prev,exports_usd_mn,chip_yoy_pct\n")
    new = cs.scrape(path=tmp, sess=FakeSession())
    m = [r for r in new if r["period"] == "m"][0]
    assert m["exports_yoy_pct"] == 83.5 and m["chip_yoy_pct"] == 263.0 and m["src"] == "pdf", m

    # 수집 결과가 기존 로더로 읽히는지
    from nowcast.adjust import load_customs
    df = load_customs(tmp)
    assert len(df) == 2 and set(df["period"]) == {"d10", "m"}, df  # d20은 VIEW 없음 → 건너뜀

    # GitHub 서버 상황: 직접 접속 실패 → 웹 리더 경유
    cs._route["mode"] = "direct"
    tmp.write_text("release_date,ref_month,period,exports_yoy_pct,workdays,workdays_prev,exports_usd_mn,chip_yoy_pct\n")
    new = cs.scrape(path=tmp, sess=RelaySession())
    got = {(r["ref_month"], r["period"]): (r["exports_yoy_pct"], r.get("chip_yoy_pct")) for r in new}
    assert got == {("2026-10", "d10"): (102.0, 170.9), ("2026-09", "m"): (83.5, 263.0)}, got
    print("customs_scrape OK")


if __name__ == "__main__":
    test()
