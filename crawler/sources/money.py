"""Money feeds: federal awards (USAspending), open federal grant windows (Grants.gov).

Both are free, keyless APIs. Each returns match records in the same shape as the agenda readers,
with source_type "award" or "grant_window". State grant pages are handled in run.py as a diff of
fixed URLs (see config STATE_GRANT_PAGES in run.py) because each state formats them differently.
"""
import datetime as dt
import json
import re

from .. import http
from ..filter import clean

# Assistance listings (CFDA) that fund school safety hardening.
CFDA = {
    "16.839": "BJA STOP School Violence",
    "16.710": "COPS Hiring / SVPP (School Violence Prevention)",
    "84.184": "ED School Emergency Response / Safe and Supportive Schools",
    "84.938": "ED Project SERV",
    "97.067": "DHS Homeland Security Grant Program",
}

_NAME_WORDS = re.compile(r"\b(independent|consolidated|unified|union|public|school|schools|district|dist|isd|cisd|usd|sd|county|city|community|area|the|of|no|elementary|high|central|local|joint|board|education)\b")


def _core(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _NAME_WORDS.sub(" ", n.lower()))


def match_district(name: str, state: str, index: dict) -> dict | None:
    return index.get((state or "").upper() + "|" + _core(name))


def usaspending(index: dict, since: dt.date, today: str) -> list[dict]:
    """Awards to school districts under the safety listings in the last LOOKBACK window."""
    out = []
    for cfda, label in CFDA.items():
        body = {
            "filters": {
                "time_period": [{"start_date": since.isoformat(), "end_date": dt.date.today().isoformat()}],
                "program_numbers": [cfda],
                "award_type_codes": ["02", "03", "04", "05"],
            },
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date", "Description", "recipient_id",
                       "Place of Performance State Code", "generated_internal_id"],
            "limit": 100, "page": 1, "sort": "Start Date", "order": "desc",
        }
        for page in range(1, 6):
            body["page"] = page
            try:
                r = http.post("https://api.usaspending.gov/api/v2/search/spending_by_award/", json=body,
                              headers={"Content-Type": "application/json"})
                data = r.json()
            except Exception:
                break
            rows = data.get("results") or []
            for row in rows:
                name = row.get("Recipient Name") or ""
                st = row.get("Place of Performance State Code") or ""
                d = match_district(name, st, index)
                if not d:
                    continue
                gid = row.get("generated_internal_id") or ""
                url = f"https://www.usaspending.gov/award/{gid}" if gid else "https://www.usaspending.gov/search"
                amt = row.get("Award Amount") or 0
                desc = clean(row.get("Description") or "")
                out.append({
                    "id": ("award-" + (gid or name))[:40].lower().replace("/", "-"),
                    "district": d["n"], "state": d["s"], "nces_id": d["id"], "students": d.get("st", 0),
                    "source_type": "award", "platform": "usaspending", "url": url, "page": None,
                    "title": f"{label}: ${amt:,.0f}", "date": (row.get("Start Date") or "")[:10],
                    "matched": [label], "strong": True, "amount": amt,
                    "snippet": f"{name} received ${amt:,.0f} under {cfda} {label}. {desc}"[:400],
                    "found": today,
                })
            if not data.get("page_metadata", {}).get("hasNext"):
                break
    return out


def grants_gov(today: str) -> list[dict]:
    """Open or forecasted federal opportunities about school safety. Not district-specific: each becomes a
    'grant_window' record the runs turn into a watchlist topic with the close date."""
    out = []
    for kw in ["school safety", "school violence", "school security", "safe schools"]:
        body = {"keyword": kw, "oppStatuses": "forecasted|posted", "rows": 50, "startRecordNum": 0}
        try:
            r = http.post("https://api.grants.gov/v1/api/search2", json=body, headers={"Content-Type": "application/json"})
            hits = (r.json().get("data") or {}).get("oppHits") or []
        except Exception:
            continue
        for h in hits:
            num = h.get("number") or h.get("id")
            out.append({
                "id": f"grant-{num}".lower(),
                "district": "", "state": "", "nces_id": "", "students": 0,
                "source_type": "grant_window", "platform": "grants.gov",
                "url": f"https://www.grants.gov/search-results-detail/{h.get('id')}",
                "page": None, "title": clean(h.get("title") or ""),
                "date": h.get("openDate") or "", "close_date": h.get("closeDate") or "",
                "agency": h.get("agencyName") or h.get("agency") or "", "status": h.get("oppStatus") or "",
                "matched": [kw], "strong": True,
                "snippet": f"{h.get('agencyName') or ''} {num}: {h.get('title')} (opens {h.get('openDate')}, closes {h.get('closeDate')})",
                "found": today,
            })
    # de-duplicate across keywords
    seen, uniq = set(), []
    for o in out:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        uniq.append(o)
    return uniq
