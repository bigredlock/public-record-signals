"""Nov 19, 2026 fixes after the Kane County SD miss: website overrides + URL variants, Utah PMN reader,
yyyy/mm/dd dates, first-read backfill."""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run  # noqa: E402
from crawler import classify, config, http, overrides  # noqa: E402
from crawler.ledger import Ledger  # noqa: E402
from crawler.sources import boards  # noqa: E402

PUBLIC_BODY = """<html><body><span id="publicBodyId">739</span>
<table><caption>Upcoming Notices</caption><tbody>
<tr><td><a href="/pmn/sitemap/notice/1121627.html">Kane County School District Regular Board Meeting</a></td>
<td>2026/11/10 06:00 PM</td><td><ul><li><a href="/pmn/files/1514831.MP3">Board Regular Meeting.MP3</a></li></ul></td></tr>
<tr><td><a href="/pmn/sitemap/notice/1114761.html">Kane County School District Regular Board Meeting</a></td>
<td>2026/06/23 06:00 PM</td><td><ul><li><a href="/pmn/files/1377155.pdf">Minutes - June 23 2026.pdf</a></li></ul></td></tr>
<tr><td><a href="/pmn/sitemap/notice/900000.html">Old meeting</a></td><td>2024/01/09 06:00 PM</td><td></td></tr>
</tbody></table></body></html>"""
NOTICE = "<html><body><main>Agenda 5. Reports I. School Security Update</main></body></html>"


class Resp:
    def __init__(self, content, ctype="text/html"):
        self.content = content.encode() if isinstance(content, str) else content
        self.text = self.content.decode("utf-8", "ignore")
        self.headers = {"Content-Type": ctype}
        self.url = ""


def test_ymd_slash_dates():
    assert boards.parse_date("2026/11/10 06:00 PM") == dt.date(2026, 11, 10)
    assert boards.parse_date("11/10/2026") == dt.date(2026, 11, 10)
    assert boards.parse_date("June 23, 2026") == dt.date(2026, 6, 23)


def test_url_variants_try_https_and_www():
    v = overrides.variants("http://www.kane.k12.ut.us")
    assert v[0] == "http://www.kane.k12.ut.us"
    assert "https://www.kane.k12.ut.us" in v and "https://kane.k12.ut.us" in v and "http://kane.k12.ut.us" in v
    assert overrides.variants("") == []


def test_overrides_apply_keeps_nces_value():
    ds = [{"id": "4900480", "n": "Kane", "web": "http://www.kane.k12.ut.us"}, {"id": "1", "n": "Other", "web": "x"}]
    out = overrides.apply(ds, {"4900480": {"web": "https://kanek12.org", "pmn": 739}})
    assert out[0]["web"] == "https://kanek12.org" and out[0]["web_nces"] == "http://www.kane.k12.ut.us" and out[0]["pmn"] == 739
    assert out[1] == ds[1]


def test_classify_falls_back_to_a_working_variant(monkeypatch):
    calls = []

    def get(url, **kw):
        calls.append(url)
        if url != "https://kane.k12.ut.us":
            raise http.requests.ConnectionError("dead")
        r = Resp('<a href="https://go.boarddocs.com/ut/kane/Board.nsf/Public">Agendas</a>')
        r.url = url
        return r

    monkeypatch.setattr(http, "get", get)
    out = classify.classify_one({"id": "1", "web": "http://www.kane.k12.ut.us"}, "2026-11-19")
    assert out["platform"] == "boarddocs" and out["web_tried"] == "http://www.kane.k12.ut.us"
    assert calls[0] == "http://www.kane.k12.ut.us"


def test_pmn_reader_reads_notices_and_minutes_within_lookback(monkeypatch):
    fetched = []

    def get(url, **kw):
        fetched.append(url)
        if url.endswith("/publicbody/739.html"):
            return Resp(PUBLIC_BODY)
        if "/pmn/files/1377155.pdf" in url:
            return Resp("<p>School Safety Grant approved for classroom door locks and cameras</p>")
        return Resp(NOTICE)

    monkeypatch.setattr(http, "get", get)
    boards.PMN_SEEN.clear()
    since = dt.date(2025, 11, 19)
    out = boards.pmn({"id": "4900480", "n": "Kane County SD", "s": "Utah", "st": 1484}, 739, since, "2026-11-19")
    assert not any("900000" in u for u in fetched)          # outside the lookback
    assert not any(u.endswith(".MP3") for u in fetched)      # audio is never downloaded
    urls = {m["url"] for m in out}
    assert "https://www.utah.gov/pmn/files/1377155.pdf" in urls
    assert any(m["date"] == "2026-06-23" and m["strong"] for m in out)
    assert all(m["platform"] == "pmn" for m in out)
    # second read skips the already-read minutes PDF
    fetched.clear()
    boards.pmn({"id": "4900480", "n": "Kane County SD", "s": "Utah", "st": 1484}, 739, since, "2026-11-19")
    assert not any(u.endswith(".pdf") for u in fetched)


def test_pmn_district_with_no_website_is_due_and_backfilled(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    dirs = [{"id": "4900480", "n": "Kane County SD", "s": "Utah", "st": 1484, "icp": True, "web": "http://www.kane.k12.ut.us"},
            {"id": "2", "n": "Site District", "s": "Utah", "st": 900, "icp": True, "web": "https://x"}]
    (data / "directory.json").write_text(json.dumps(dirs))
    (data / "platforms.json").write_text(json.dumps({"4900480": {"platform": "none", "agenda_url": ""},
                                                     "2": {"platform": "site", "agenda_url": "https://x"}}))
    (data / "web_overrides.json").write_text(json.dumps({"4900480": {"web": "https://kanek12.org", "pmn": 739}}))
    (data / "state.json").write_text(json.dumps({"walked": {"2": "2026-01-01"}}))
    run.DATA = data
    config.WORKERS = 1
    seen = {}

    def reader(d, p, since):
        seen[d["id"]] = (since, d.get("pmn"))
        return d, [], None, ""

    run.cmd_agendas(argparse.Namespace(limit=10, budget_min=0), run.Run(), Ledger(data / "ledger"), reader=reader)
    assert seen["4900480"][1] == 739                                     # read via the state portal despite platform none
    assert seen["4900480"][0] <= dt.date.today() - dt.timedelta(days=364)  # never read before: one-year backfill
    assert seen["2"][0] >= dt.date.today() - dt.timedelta(days=config.LOOKBACK_DAYS)


def test_read_with_pmn_succeeds_when_only_the_portal_works(monkeypatch):
    monkeypatch.setattr(boards, "pmn", lambda d, i, since, today: [{"id": "x"}])
    d, m, code, note = run._read_one({"id": "1", "n": "K", "s": "Utah", "pmn": 739}, {"platform": "none", "agenda_url": ""},
                                     dt.date.today())
    assert code is None and m == [{"id": "x"}]

    def boom(*a):
        raise http.Blocked("x", "forbidden")
    monkeypatch.setattr(boards, "pmn", boom)
    d, m, code, note = run._read_one({"id": "1", "n": "K", "s": "Utah", "pmn": 739}, {"platform": "none", "agenda_url": ""},
                                     dt.date.today())
    assert code == "forbidden"


def test_bot_challenge_page_is_a_refusal_not_an_empty_read():
    wall = b'<html style="height:100%"><head><META NAME="ROBOTS" CONTENT="NOINDEX, NOFOLLOW"><script src="/_Incapsula_Resource?x=1"></script></head></html>'
    assert http._bot_wall(wall)
    assert not http._bot_wall(b"<html><body>Board agenda: classroom door locks</body></html>")
    assert http.diagnose(http.Blocked("x", "bot_wall")) == "bot_wall"


def test_portal_only_district_reads_its_own_board_page(monkeypatch):
    seen = []
    monkeypatch.setattr(boards, "generic", lambda d, url, since, today: seen.append(url) or [{"id": "m"}])
    d, m, code, note = run._read_one({"id": "1", "n": "A", "s": "Georgia"},
                                     {"platform": "eboard", "agenda_url": "https://simbli.x", "fallback": "https://district/board"},
                                     dt.date.today())
    assert code is None and seen == ["https://district/board"] and "district site" in note
    d, m, code, note = run._read_one({"id": "1", "n": "A", "s": "Georgia"}, {"platform": "eboard", "agenda_url": "x"}, dt.date.today())
    assert code == "no_reader"


def test_simbli_wall_falls_back_to_district_site(monkeypatch):
    def wall(*a):
        raise http.Blocked("bot protection page", "bot_wall")
    monkeypatch.setitem(boards.READERS, "simbli", wall)
    monkeypatch.setattr(boards, "generic", lambda d, url, since, today: [{"id": "ok"}])
    d, m, code, note = run._read_one({"id": "1", "n": "G", "s": "Georgia"},
                                     {"platform": "simbli", "agenda_url": "https://simbli.x", "fallback": "https://gcps/board"},
                                     dt.date.today())
    assert code is None and m == [{"id": "ok"}] and "bot_wall" in note
