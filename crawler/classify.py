"""Find each district's board-agenda platform from its website. Result cached in data/platforms.json.

platforms.json: { "<nces id>": {"platform": "boarddocs|boardbook|simbli|legistar|site|blocked|none",
                                "agenda_url": "...", "checked": "YYYY-MM-DD", "note": "..."} }
"""
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from . import http

PLATFORM_HOSTS = [
    ("boarddocs", re.compile(r"go\.boarddocs\.com/[a-z]{2}/[^/\"']+/Board\.nsf", re.I)),
    ("boardbook", re.compile(r"meetings\.boardbook\.org/Public/Organization/\d+", re.I)),
    ("simbli", re.compile(r"simbli\.eboardsolutions\.com/[^\"' ]*SB_MeetingListing\.aspx\?S=\d+", re.I)),
    ("legistar", re.compile(r"https?://[a-z0-9-]+\.legistar\.com", re.I)),
]
AGENDA_WORDS = re.compile(r"agenda|board\s*meeting|minutes|boarddocs|boardbook|school\s*board|board\s*of\s*(?:education|trustees)", re.I)


def classify_one(district: dict, today: str) -> dict:
    web = (district.get("web") or "").strip()
    if not web:
        return {"platform": "none", "agenda_url": "", "checked": today, "note": "no website in NCES"}
    if not web.startswith("http"):
        web = "http://" + web
    try:
        r = http.get(web)
    except http.Blocked as e:
        return {"platform": "blocked", "agenda_url": web, "checked": today, "note": str(e)[:120]}
    except Exception as e:
        return {"platform": "none", "agenda_url": web, "checked": today, "note": f"unreachable: {type(e).__name__}"}
    html = r.text
    for name, rx in PLATFORM_HOSTS:
        m = rx.search(html)
        if m:
            url = m.group(0)
            if not url.startswith("http"):
                url = "https://" + url
            if name == "boarddocs":
                url = url + "/Public"
            return {"platform": name, "agenda_url": url, "checked": today, "note": ""}
    # Second hop: follow the first board/agenda link on the site and look again.
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        label = " ".join(a.get_text(" ").split())
        if AGENDA_WORDS.search(label) or AGENDA_WORDS.search(a["href"]):
            href = http.absolute(r.url, a["href"])
            for name, rx in PLATFORM_HOSTS:
                if rx.search(href):
                    url = rx.search(href).group(0)
                    if not url.startswith("http"):
                        url = "https://" + url
                    return {"platform": name, "agenda_url": url + ("/Public" if name == "boarddocs" else ""),
                            "checked": today, "note": "via link"}
            if http.host_of(href) == http.host_of(r.url):
                try:
                    sub = http.get(href).text
                except Exception:
                    continue
                for name, rx in PLATFORM_HOSTS:
                    m = rx.search(sub)
                    if m:
                        url = m.group(0)
                        if not url.startswith("http"):
                            url = "https://" + url
                        return {"platform": name, "agenda_url": url + ("/Public" if name == "boarddocs" else ""),
                                "checked": today, "note": "via board page"}
                return {"platform": "site", "agenda_url": href, "checked": today, "note": "board page on district site"}
    return {"platform": "site", "agenda_url": r.url, "checked": today, "note": "no board link found; homepage only"}


def load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=0, sort_keys=True))
