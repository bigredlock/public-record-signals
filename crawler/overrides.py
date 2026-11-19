"""Corrections to the NCES directory, kept in data/web_overrides.json.

NCES website fields go stale (Kane County SD, UT: kane.k12.ut.us is dead; the district moved to kanek12.org),
and about 500 ICP districts have no website in NCES at all. Those districts were classified "none" and never read.

web_overrides.json: { "<nces id>": {"web": "https://...",          # current district homepage (optional)
                                    "pmn": 739,                     # Utah Public Notice Website public body id (optional)
                                    "source": "who/what found it", "added": "YYYY-MM-DD", "note": "..."} }

The weekly Claude website-repair task adds entries (web search, HubSpot domain); the crawler verifies them by
fetching. An override that fails to load is simply retried by the classifier like any other "none" district.
"""
import json
from pathlib import Path


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def apply(districts: list[dict], ov: dict) -> list[dict]:
    """Return districts with overrides applied. The NCES value is kept as web_nces for the record."""
    out = []
    for d in districts:
        o = ov.get(d["id"])
        if not o:
            out.append(d)
            continue
        d = dict(d)
        if o.get("web"):
            d["web_nces"] = d.get("web", "")
            d["web"] = o["web"]
        if o.get("pmn"):
            d["pmn"] = int(o["pmn"])
        out.append(d)
    return out


def variants(web: str) -> list[str]:
    """URL forms to try when a district homepage will not load: https/http and with/without www."""
    web = (web or "").strip()
    if not web:
        return []
    if not web.startswith("http"):
        web = "http://" + web
    scheme, rest = web.split("://", 1)
    host, _, path = rest.partition("/")
    hosts = [host, host[4:] if host.startswith("www.") else "www." + host]
    out = []
    for s in ("https", "http") if scheme == "https" else ("http", "https"):
        for h in hosts:
            u = f"{s}://{h}/{path}" if path else f"{s}://{h}"
            if u not in out:
                out.append(u)
    # the original exactly as given goes first
    if web in out:
        out.remove(web)
    return [web] + out
