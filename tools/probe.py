"""Diagnose a reader on a few districts: what each request returned and how many matches came out.
Usage: python tools/probe.py <platform> [n]   (run by the "Reader probe" workflow; prints only, writes nothing)"""
import datetime as dt
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler import http  # noqa: E402
from crawler.sources import boards  # noqa: E402

plat, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3
D = {x["id"]: x for x in json.loads((ROOT / "data/directory.json").read_text()) if x.get("icp")}
P = json.loads((ROOT / "data/platforms.json").read_text())
ids = sorted([k for k in D if P.get(k, {}).get("platform") == plat], key=lambda k: -(D[k].get("st") or 0))[:n]

_get, _post = http.get, http.post


def log(kind, fn):
    def w(url, *a, **kw):
        try:
            r = fn(url, *a, **kw)
            body = r.content[:160].decode("utf-8", "ignore").replace("\n", " ")
            print(f"    {kind} {r.status_code} {r.headers.get('Content-Type', '')[:30]} {len(r.content)}B {url[:110]} | {body[:120]}")
            return r
        except Exception as e:
            print(f"    {kind} EXC {type(e).__name__}: {str(e)[:120]} {url[:110]}")
            raise
    return w


http.get, http.post = log("GET", _get), log("POST", _post)
since = dt.date.today() - dt.timedelta(days=45)
for k in ids:
    d, p = D[k], P[k]
    print(f"== {d['n']} ({d['s']}) {p['agenda_url']}")
    try:
        out = boards.READERS[plat](d, p["agenda_url"], since, dt.date.today().isoformat())
        print(f"  -> {len(out)} matches")
    except Exception:
        print("  -> EXCEPTION", traceback.format_exc()[-400:])
