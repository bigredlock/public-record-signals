"""Settings for the FlipLok District Signals crawler.

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
    r"nightlock", r"bearacade", r"barracuda\s+(?:intruder|door)", r"lockblok", r"fliplok",
]
KEYWORD_RE = re.compile("|".join(f"(?:{k})" for k in KEYWORDS), re.IGNORECASE)

# Stronger terms: a hit on one of these is flagged `strong: true` so the runs can grade it first.
STRONG_RE = re.compile(
    r"classroom\s+door|door\s+lock|door\s+hardware|lock(?:ing)?\s+hardware|barricade|intruder|access\s+control|"
    r"nightlock|bearacade|lockblok|fliplok|secur(?:e|ing)\s+(?:classroom|the\s+classroom)\s+doors?",
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
WORKERS = 8
USER_AGENT = "FlipLokSignals/1.0 (+https://fliplok.com; school-safety research; contact nikko@fliplok.com)"
SNIPPET_CHARS = 260
