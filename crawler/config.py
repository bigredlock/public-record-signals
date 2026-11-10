"""Settings for the Public Record Signals crawler.

Everything here is a plain constant so the job is easy to tune without touching code.
"""
import re

# Words that mark an agenda item, bond list, bid or posting as worth a human (or Claude) look.
# Kept wide on purpose: the daily runs grade; the crawler only filters.
KEYWORDS = [
    r"classroom\s+door", r"door\s+lock", r"door\s+hardware", r"lock(?:s|ing|down)?\b",
    r"secur(?:e|ity|ing)\b", r"safety", r"intruder", r"harden(?:ing)?", r"barricade",
    r"access\s+control", r"vestibule", r"school\s+resource\s+officer|\bSRO\b",
    r"active\s+shooter", r"threat\s+assessment", r"safe\s+room", r"shelter[- ]in[- ]place",
    r"SAFE\s+grant", r"STOP\s+School\s+Violence", r"COPS\s+(?:grant|SVPP)",
    r"night\s?lock", r"teacher\s?lock", r"bear[- ]?acade", r"barracuda\s+(?:intruder|door|defense)", r"lock\s?blok",
]
KEYWORD_RE = re.compile("|".join(f"(?:{k})" for k in KEYWORDS), re.IGNORECASE)

# Stronger terms: a hit on one of these is flagged `strong: true` so the runs can grade it first.
STRONG_RE = re.compile(
    r"classroom\s+door|door\s+lock|door\s+hardware|lock(?:ing)?\s+hardware|barricade|intruder|access\s+control|"
    r"night\s?lock|teacher\s?lock|bear[- ]?acade|lock\s?blok|secur(?:e|ing)\s+(?:classroom|the\s+classroom)\s+doors?",
    re.IGNORECASE,
)

# Pages on a district site that usually lead to agendas or bonds.
BOARD_LINK_RE = re.compile(r"agenda|board\s*meeting|minutes|boarddocs|boardbook|simbli|legistar|school\s*board\b", re.I)
BOND_LINK_RE = re.compile(r"\bbond\b|\blevy\b|referendum|facilit(?:y|ies)\s+plan|master\s+plan|capital\s+(?:plan|improvement)", re.I)

LOOKBACK_DAYS = 45          # how far back to read agendas
MAX_PDFS_PER_DISTRICT = 6   # packets are big; read the first few linked PDFs only
MAX_PDF_BYTES = 12_000_000
MAX_PDF_PAGES = 60
REQUEST_TIMEOUT = 25
PER_HOST_DELAY = 1.0        # seconds between requests to the same host
WORKERS = 16           # Nov 10: 8 -> 16; per-host spacing still keeps each site at 1 request/sec
USER_AGENT = "PublicRecordSignals/1.0 (school-safety public-records research; contact sales@fliplok.com)"
SNIPPET_CHARS = 260


# Competitor brands for flagging (crawler only flags; the sweep and the competitor module verify).
# Keep each pattern tight; Barracuda needs door/lock context so Barracuda Networks (email/firewall) never matches.
COMPETITOR_PATTERNS = {
    "Nightlock": r"night\s?lock",
    "TeacherLock": r"teacher\s?lock",
    "Barracuda (door)": r"barracuda\s+(?:intruder|door|lockdown|defense)|(?:intruder|door|lockdown)[^.]{0,40}barracuda(?![^.]{0,40}(?:networks|email|firewall|backup|cloudgen|sentinel))",
    "Bearacade": r"bear[- ]?acade",
    "LockBlok": r"lock\s?blok",
}
COMPETITOR_RES = {k: re.compile(v, re.I) for k, v in COMPETITOR_PATTERNS.items()}

# Throughput (Nov 10, 2026: the Nov 9 crawl read 475 of 1,164 due districts and deferred 640).
DISTRICT_SOFT_CAP_SEC = 150  # after this long on one district, stop opening new PDFs for it and keep what was found
# Re-read cadence: big districts and districts that produced a reviewable record recently every 7 days;
# everyone else every 14 days (most small districts post nothing relevant for months).
READ_EVERY_DAYS = 7
READ_EVERY_DAYS_QUIET = 14
QUIET_MAX_STUDENTS = 10_000   # districts with fewer students than this can go quiet
YIELD_WINDOW_DAYS = 60        # a review/changed record within this many days keeps a district on the 7-day cadence

# Run budget and checkpoints.
CHECKPOINT_EVERY = 100      # save ledger + state after this many districts
OCR_MAX_PAGES_PER_DOC = 5   # image-only PDF pages to OCR per document
OCR_MAX_DOCS_PER_RUN = 25
RETRY_AFTER_DAYS = {"forbidden": 14, "robots": 30, "rate_limited": 2, "timeout": 1, "server_error": 1,
                    "js_required": 14, "not_found": 7, "obsolete_portal": 30, "error": 2}


# Platform survey (Nov 10, 2026): agenda vendors linked from district board pages read by the generic "site"
# reader. Counted per district into data/platform_survey.json so we can see which vendors deserve a reader
# (rule: build one when a vendor covers 50+ ICP districts). Detection only; no extra requests.
VENDOR_PATTERNS = {
    "civicclerk": r"civicclerk\.com",
    "civicplus_agendacenter": r"/AgendaCenter\b",
    "granicus": r"granicus\.com|iqm2\.com",
    "legistar": r"legistar\.com",
    "escribe": r"escribemeetings\.com",
    "onboard": r"onboardmeetings\.com|app\.onboardmeetings",
    "municode_meetings": r"municodemeetings\.com|meetings\.municode\.com",
    "swagit": r"swagit\.com",
    "youtube": r"youtube\.com/(?:@|channel/|c/|user/|watch|live)|youtu\.be/",
    "boarddocs": r"boarddocs\.com",
    "boardbook": r"boardbook\.org",
    "simbli_eboard": r"eboardsolutions\.com",
    "diligent": r"diligentoneplatform\.com",
    "google_drive": r"drive\.google\.com|docs\.google\.com",
    "agendaquick": r"agendaquick\.com",
    "novusagenda": r"novusagenda\.com",
    "meetingdocs_other": r"agendasuite|boardpaq|meetingpackets|agendaonline",
}
VENDOR_RES = {k: re.compile(v, re.I) for k, v in VENDOR_PATTERNS.items()}
