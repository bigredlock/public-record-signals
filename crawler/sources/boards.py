"""Board agenda readers, one per platform, plus the generic district-site reader.

Each reader takes (district, agenda_url, since: date, today: str) and returns a list of match records.
Readers raise http.Blocked when a site refuses automated reading; run.py records that as a gap.
Endpoints are the platforms' public pages as of Nov 2026; if a platform changes, the reader fails
closed (returns nothing and logs) rather than guessing.
"""
import datetime as dt
import json
import re
import urllib.parse

from bs4 import BeautifulSoup

from .. import config, http
from ..filter import clean, scan

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})|(\d{4})-(\d{2})-(\d{2})|([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})")
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], start=1)}


def parse_date(s: str) -> dt.date | None:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    try:
        if m.group(1):
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
            return dt.date(y, int(m.group(1)), int(m.group(2)))
        if m.group(4):
            return dt.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        mon = MONTHS.get(m.group(7)[:9]) or MONTHS.get({"Sept": "September"}.get(m.group(7), m.group(7)))
        if mon:
            return dt.date(int(m.group(9)), mon, int(m.group(8)))
    except ValueError:
        return None
    return None


def _pdf_matches(district, url, title, date, today, platform, source_type="agenda"):
    r = http.get(url)
    if "pdf" not in r.headers.get("Content-Type", "").lower() and not url.lower().endswith(".pdf"):
        return scan(http.html_text(r.content), district=district, source_type=source_type, platform=platform,
                    url=url, page=None, title=title, date=date, found=today)
    out = []
    for page, text in http.pdf_text(r.content):
        out += scan(text, district=district, source_type=source_type, platform=platform, url=url, page=page,
                    title=title, date=date, found=today)
    return out


# ---------------------------------------------------------------- BoardDocs
def boarddocs(district, agenda_url, since, today):
    """go.boarddocs.com/<st>/<org>/Board.nsf/Public. Uses the public JSON endpoints behind that page."""
    m = re.search(r"go\.boarddocs\.com/([a-z]{2})/([^/]+)/Board\.nsf", agenda_url, re.I)
    if not m:
        return []
    base = f"https://go.boarddocs.com/{m.group(1)}/{m.group(2)}/Board.nsf"
    out = []
    committees = []
    try:
        r = http.post(f"{base}/BD-GetCommittees?open")
        committees = [c.get("UNID") for c in json.loads(r.text) if c.get("UNID")]
    except Exception:
        pass
    if not committees:
        # Fall back to the committee id embedded in the public page.
        page = http.get(f"{base}/Public").text
        m2 = re.search(r"current_committee_id['\"]?\s*[:=]\s*['\"]([A-Z0-9]+)", page)
        committees = [m2.group(1)] if m2 else []
    for cid in committees[:3]:
        try:
            r = http.post(f"{base}/BD-GetMeetingsList?open", data={"current_committee_id": cid})
            meetings = json.loads(r.text)
        except Exception:
            continue
        for mt in meetings:
            nd = str(mt.get("numberdate") or "")
            if len(nd) != 8:
                continue
            d = dt.date(int(nd[:4]), int(nd[4:6]), int(nd[6:]))
            if d < since or d > dt.date.today() + dt.timedelta(days=45):
                continue
            mid = mt.get("unique")
            if not mid:
                continue
            try:
                a = http.post(f"{base}/BD-GetAgenda?open", data={"id": mid, "current_committee_id": cid})
            except Exception:
                continue
            url = f"{base}/Public#tab-meetings?meeting={mid}"
            text = http.html_text(a.text)
            out += scan(text, district=district, source_type="agenda", platform="boarddocs", url=url, page=None,
                        title=clean(mt.get("name") or "Board meeting"), date=d.isoformat(), found=today)
    return out


# ---------------------------------------------------------------- BoardBook
def boardbook(district, agenda_url, since, today):
    r = http.get(agenda_url)
    soup = BeautifulSoup(r.content, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/Public/Agenda/" not in href and "/Public/Minutes/" not in href:
            continue
        row = a.find_parent("tr") or a.parent
        d = parse_date(row.get_text(" ") if row else a.get_text(" "))
        if d and d < since:
            continue
        url = http.absolute(agenda_url, href)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = http.get(url)
        except http.Blocked:
            raise
        except Exception:
            continue
        text = http.html_text(page.content)
        title = clean(a.get_text(" ")) or "Board meeting"
        out += scan(text, district=district, source_type="agenda", platform="boardbook", url=url, page=None,
                    title=title, date=d.isoformat() if d else "", found=today)
        if len(seen) >= 8:
            break
    return out


# ---------------------------------------------------------------- Simbli
def simbli(district, agenda_url, since, today):
    r = http.get(agenda_url)
    soup = BeautifulSoup(r.content, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        if "MID=" not in a["href"]:
            continue
        row = a.find_parent("tr") or a.parent
        d = parse_date(row.get_text(" ") if row else a.get_text(" "))
        if d and d < since:
            continue
        url = http.absolute(agenda_url, a["href"])
        if url in seen:
            continue
        seen.add(url)
        try:
            page = http.get(url)
        except http.Blocked:
            raise
        except Exception:
            continue
        out += scan(http.html_text(page.content), district=district, source_type="agenda", platform="simbli",
                    url=url, page=None, title=clean(a.get_text(" ")) or "Board meeting",
                    date=d.isoformat() if d else "", found=today)
        if len(seen) >= 8:
            break
    return out


# ---------------------------------------------------------------- Legistar (Granicus)
def legistar(district, agenda_url, since, today):
    m = re.search(r"https?://([a-z0-9-]+)\.legistar\.com", agenda_url, re.I)
    if not m:
        return []
    client = m.group(1)
    api = f"https://webapi.legistar.com/v1/{client}/events?$filter=EventDate+ge+datetime'{since.isoformat()}'"
    r = http.get(api)
    out = []
    for ev in json.loads(r.text)[:12]:
        eid = ev.get("EventId")
        items_url = f"https://webapi.legistar.com/v1/{client}/events/{eid}/eventitems?AgendaNote=1&MinutesNote=1"
        try:
            items = json.loads(http.get(items_url).text)
        except Exception:
            continue
        text = " ".join(clean(i.get("EventItemTitle") or "") + " " + clean(i.get("EventItemAgendaNote") or "") for i in items)
        url = ev.get("EventInSiteURL") or agenda_url
        out += scan(text, district=district, source_type="agenda", platform="legistar", url=url, page=None,
                    title=clean(ev.get("EventBodyName") or "Board meeting"), date=(ev.get("EventDate") or "")[:10], found=today)
    return out


# ---------------------------------------------------------------- generic district site
def generic(district, site_url, since, today):
    """Read the district's own board/agenda and bond pages, and the first few PDFs they link."""
    out = []
    home = http.get(site_url)
    soup = BeautifulSoup(home.content, "lxml")
    pages = []
    for a in soup.find_all("a", href=True):
        label = clean(a.get_text(" "))
        href = http.absolute(site_url, a["href"])
        if http.host_of(href) != http.host_of(site_url):
            continue
        if config.BOARD_LINK_RE.search(label) or config.BOARD_LINK_RE.search(href):
            pages.append(("agenda", label, href))
        elif config.BOND_LINK_RE.search(label) or config.BOND_LINK_RE.search(href):
            pages.append(("bond", label, href))
    seen, pdfs = set(), []
    for kind, label, href in pages[:6]:
        if href in seen:
            continue
        seen.add(href)
        try:
            r = http.get(href)
        except http.Blocked:
            raise
        except Exception:
            continue
        text = http.html_text(r.content)
        out += scan(text, district=district, source_type=kind, platform="site", url=href, page=None,
                    title=label or kind, date="", found=today)
        sub = BeautifulSoup(r.content, "lxml")
        for a in sub.find_all("a", href=True):
            u = http.absolute(href, a["href"])
            lab = clean(a.get_text(" "))
            if u.lower().endswith(".pdf") and (config.BOARD_LINK_RE.search(lab + " " + u) or config.BOND_LINK_RE.search(lab + " " + u)):
                d = parse_date(lab) or parse_date(u)
                if d and d < since:
                    continue
                pdfs.append((kind, lab, u, d))
    for kind, lab, u, d in pdfs[: config.MAX_PDFS_PER_DISTRICT]:
        try:
            out += _pdf_matches(district, u, lab or kind, d.isoformat() if d else "", today, "site", kind)
        except http.Blocked:
            raise
        except Exception:
            continue
    return out


READERS = {"boarddocs": boarddocs, "boardbook": boardbook, "simbli": simbli, "legistar": legistar, "site": generic}
