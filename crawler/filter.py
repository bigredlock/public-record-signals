"""Keyword filter: turn a block of text into zero or more match records."""
import hashlib
import re

from . import config


def scan(text: str, *, district: dict, source_type: str, platform: str, url: str, page: int | None,
         title: str, date: str, found: str) -> list[dict]:
    """Return one match per distinct keyword hit region (hits within 400 chars of each other are merged)."""
    if not text:
        return []
    hits = [(m.start(), m.end(), m.group(0)) for m in config.KEYWORD_RE.finditer(text)]
    if not hits:
        return []
    regions: list[list] = []
    for s, e, w in hits:
        if regions and s - regions[-1][1] < 400:
            regions[-1][1] = e
            regions[-1][2].add(w.lower())
        else:
            regions.append([s, e, {w.lower()}])
    out = []
    for s, e, words in regions[:8]:
        a = max(0, s - config.SNIPPET_CHARS // 2)
        b = min(len(text), e + config.SNIPPET_CHARS // 2)
        snippet = text[a:b].strip()
        strong = bool(config.STRONG_RE.search(text[a:b]))
        key = hashlib.sha1(f"{district['id']}|{url}|{page}|{snippet[:120]}".encode()).hexdigest()[:16]
        out.append({
            "id": key,
            "district": district["n"],
            "state": district["s"],
            "nces_id": district["id"],
            "students": district.get("st", 0),
            "source_type": source_type,
            "platform": platform,
            "url": url,
            "page": page,
            "title": title[:200],
            "date": date,
            "matched": sorted(words),
            "strong": strong,
            "snippet": snippet,
            "found": found,
        })
    return out


_WS = re.compile(r"\s+")


def clean(s: str) -> str:
    return _WS.sub(" ", s or "").strip()
