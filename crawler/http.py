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
    """The site refuses automated reading (robots.txt or a 401/403/429). Record it as a gap; never work around it.

    code is one of: forbidden (401/403), rate_limited (429), robots (robots.txt disallows)."""

    def __init__(self, msg: str, code: str = "forbidden"):
        super().__init__(msg)
        self.code = code


# Counters for the run report (fetched pages, PDFs parsed, OCR pages). Thread-safe enough for +=1 under the lock.
STATS = {"fetched": 0, "pdfs": 0, "pdf_pages": 0, "ocr_pages": 0, "ocr_docs": 0, "image_only_pages": 0}


def _bump(k, n=1):
    with _lock:
        STATS[k] = STATS.get(k, 0) + n


def diagnose(exc: Exception) -> str:
    """Map a failure to one reason code: forbidden, rate_limited, robots, timeout, server_error, not_found,
    js_required, obsolete_portal, error."""
    if isinstance(exc, Blocked):
        return exc.code
    if isinstance(exc, JsRequired):
        return "js_required"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        sc = exc.response.status_code
        if sc in (404, 410):
            return "not_found"
        if sc >= 500:
            return "server_error"
    if isinstance(exc, requests.ConnectionError):
        return "timeout"
    return "error"


class JsRequired(Exception):
    """The page only renders with JavaScript (no readable content in the HTML)."""


_JS_ONLY = ("enable javascript", "requires javascript", "javascript is required", "you need to enable javascript")


def looks_js_only(html: bytes | str) -> bool:
    t = html.decode("utf-8", "ignore") if isinstance(html, bytes) else html
    low = t.lower()
    if any(p in low for p in _JS_ONLY):
        return len(" ".join(BeautifulSoup(t, "lxml").get_text(" ").split())) < 400
    return False


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
        raise Blocked(f"robots.txt disallows {url}", "robots")
    _wait_turn(url)
    r = _session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True, **kw)
    if r.status_code in (401, 403, 429):
        raise Blocked(f"HTTP {r.status_code} at {url}", "rate_limited" if r.status_code == 429 else "forbidden")
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    limit = config.MAX_PDF_BYTES if "pdf" in ctype else 3_000_000
    buf = io.BytesIO()
    for chunk in r.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > limit:
            break
    r._content = buf.getvalue()
    _bump("fetched")
    return r


def post(url: str, data=None, **kw) -> requests.Response:
    _wait_turn(url)
    r = _session.post(url, data=data, timeout=config.REQUEST_TIMEOUT, **kw)
    if r.status_code in (401, 403, 429):
        raise Blocked(f"HTTP {r.status_code} at {url}", "rate_limited" if r.status_code == 429 else "forbidden")
    _bump("fetched")
    r.raise_for_status()
    return r


def html_text(html: str | bytes) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    return " ".join(soup.get_text(" ").split())


def pdf_text(data: bytes) -> list[tuple[int, str]]:
    """Return [(page_number, text)] for the first MAX_PDF_PAGES pages, keeping page numbers.

    Image-only (scanned) pages are OCR'd when tesseract is installed, up to OCR_MAX_PAGES_PER_DOC pages per
    document and OCR_MAX_DOCS_PER_RUN documents per run. OCR'd text is prefixed "[OCR] " so readers know it
    may contain recognition errors. Pages that stay empty are counted as image_only_pages."""
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
    _bump("pdfs")
    _bump("pdf_pages", len(out))
    empty = [i for i, t in out if len(t) < 40]
    if empty:
        ocr = _ocr_pages(data, empty[: config.OCR_MAX_PAGES_PER_DOC])
        out = [(i, ("[OCR] " + ocr[i]) if ocr.get(i) else t) for i, t in out]
        _bump("image_only_pages", sum(1 for i in empty if not ocr.get(i)))
    return out


def _ocr_pages(data: bytes, pages: list[int]) -> dict[int, str]:
    """OCR the given 1-based pages. Returns {} when tesseract/pdf2image are not installed or the run cap is hit."""
    with _lock:
        if STATS.get("ocr_docs", 0) >= config.OCR_MAX_DOCS_PER_RUN:
            return {}
        STATS["ocr_docs"] = STATS.get("ocr_docs", 0) + 1
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except Exception:
        return {}
    out = {}
    for p in pages:
        try:
            img = convert_from_bytes(data, dpi=200, first_page=p, last_page=p)[0]
            txt = " ".join(pytesseract.image_to_string(img).split())
            if txt:
                out[p] = txt
                _bump("ocr_pages")
        except Exception:
            continue
    return out


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)
