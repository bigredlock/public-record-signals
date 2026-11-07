"""Polite HTTP: one session, per-host spacing, robots.txt, size caps, PDF text extraction."""
import io
import threading
import time
import urllib.parse
import urllib.robotparser

import requests
from bs4 import BeautifulSoup

from . import config

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_lock = threading.Lock()


class Blocked(Exception):
    """The site refuses automated reading (robots.txt or a 401/403/429). Record it as a gap; never work around it."""


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def _robots_for(url: str):
    h = host_of(url)
    with _lock:
        if h in _robots:
            return _robots[h]
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = _session.get(f"{urllib.parse.urlsplit(url).scheme}://{h}/robots.txt", timeout=10)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        else:
            rp = None
    except requests.RequestException:
        rp = None
    with _lock:
        _robots[h] = rp
    return rp


def _wait_turn(url: str):
    h = host_of(url)
    with _lock:
        last = _last_hit.get(h, 0.0)
        gap = config.PER_HOST_DELAY - (time.time() - last)
        _last_hit[h] = max(time.time(), last + config.PER_HOST_DELAY)
    if gap > 0:
        time.sleep(gap)


def get(url: str, *, allow_pdf: bool = True, **kw) -> requests.Response:
    rp = _robots_for(url)
    if rp is not None and not rp.can_fetch(config.USER_AGENT, url) and not rp.can_fetch("*", url):
        raise Blocked(f"robots.txt disallows {url}")
    _wait_turn(url)
    r = _session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True, **kw)
    if r.status_code in (401, 403, 429):
        raise Blocked(f"HTTP {r.status_code} at {url}")
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    limit = config.MAX_PDF_BYTES if "pdf" in ctype else 3_000_000
    buf = io.BytesIO()
    for chunk in r.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > limit:
            break
    r._content = buf.getvalue()
    return r


def post(url: str, data=None, **kw) -> requests.Response:
    _wait_turn(url)
    r = _session.post(url, data=data, timeout=config.REQUEST_TIMEOUT, **kw)
    if r.status_code in (401, 403, 429):
        raise Blocked(f"HTTP {r.status_code} at {url}")
    r.raise_for_status()
    return r


def html_text(html: str | bytes) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    return " ".join(soup.get_text(" ").split())


def pdf_text(data: bytes) -> list[tuple[int, str]]:
    """Return [(page_number, text)] for the first MAX_PDF_PAGES pages. Image-only pages come back empty."""
    from pypdf import PdfReader

    out = []
    try:
        reader = PdfReader(io.BytesIO(data))
        for i, page in enumerate(reader.pages[: config.MAX_PDF_PAGES], start=1):
            try:
                out.append((i, " ".join((page.extract_text() or "").split())))
            except Exception:
                out.append((i, ""))
    except Exception:
        return []
    return out


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)
