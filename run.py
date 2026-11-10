#!/usr/bin/env python3
"""Public Record Signals crawler.

Usage:
  python run.py classify  [--limit N]                  # find each ICP district's agenda platform (cached, resumable)
  python run.py agendas   [--limit N] [--budget-min M] # read agendas for the next N districts due a read
  python run.py money                                  # USAspending awards + Grants.gov windows
  python run.py all       [--limit N] [--budget-min M] # what the nightly job runs

Outputs (committed by the GitHub Action):
  data/ledger/             the durable feed: every match with a permanent seq and a disposition (see crawler/ledger.py)
  data/runs.json           last 30 runs with counts, plus per-source last_attempted / last_success
  data/diagnostics.json    every district whose read failed, with a reason code and retry date (not capped)
  data/platforms.json      platform per district (with history when a district moves portals)
  data/state.json          walked (last SUCCESSFUL read per district), failures, deferred
  data/report.json         summary for humans (kept for compatibility)
  data/matches.json        last 90 days of matches (kept for compatibility; consumers should use the ledger)

Completeness rules:
  - A district counts as checked only when its read succeeded. Failures get a reason code and a retry date.
  - Districts not reached before the time budget are written to state.deferred and go first next run.
  - Ledger + state are checkpointed every CHECKPOINT_EVERY districts, so a killed job loses at most one batch.
  - A run that deferred anything or failed a source is recorded as "partial", never "complete".
"""
import argparse
import datetime as dt
import json
import sys
import time
import traceback
from collections import Counter
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path

from crawler import classify, config, http
from crawler.ledger import Ledger
from crawler.sources import boards, money

START = time.monotonic()
ROOT = Path(__file__).parent
DATA = ROOT / "data"
TODAY = dt.date.today().isoformat()
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def NOW():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def save_json(p: Path, obj):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=0, ensure_ascii=False))
    tmp.replace(p)


def districts():
    d = load_json(DATA / "directory.json", [])
    return [x for x in d if x.get("icp")]


STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District Of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def index_by_name(d):
    st_abbr = {v: k for k, v in STATES.items()}
    return {st_abbr.get(x["s"], x["s"]).upper() + "|" + money._core(x["n"]): x for x in d}


# ---------------------------------------------------------------- run bookkeeping
class Run:
    def __init__(self):
        self.runs = load_json(DATA / "runs.json", {"runs": [], "sources": {}})
        self.rec = {"run_id": RUN_ID, "started": NOW(), "finished": "", "status": "running",
                    "counts": Counter(), "sources": {}, "notes": []}

    def source(self, name, ok: bool, note: str = ""):
        s = self.runs["sources"].setdefault(name, {"last_attempted": "", "last_success": ""})
        s["last_attempted"] = NOW()
        if ok:
            s["last_success"] = NOW()
        self.rec["sources"][name] = {"ok": ok, "note": note}

    def save(self, final=False):
        if final:
            self.rec["finished"] = NOW()
            partial = self.rec["counts"].get("deferred", 0) > 0 or any(not v["ok"] for v in self.rec["sources"].values())
            self.rec["status"] = "partial" if partial else "complete"
        rec = dict(self.rec, counts=dict(self.rec["counts"]), http=dict(http.STATS))
        self.runs["runs"] = [r for r in self.runs["runs"] if r["run_id"] != RUN_ID][-29:] + [rec]
        save_json(DATA / "runs.json", self.runs)


# ---------------------------------------------------------------- ledger bootstrap
def ensure_ledger() -> Ledger:
    """First run after the upgrade: seed the ledger from the old matches.json in discovery order."""
    led = Ledger(DATA / "ledger")
    if led.index["next_seq"] == 1:
        old = load_json(DATA / "matches.json", {"matches": []})["matches"]
        old.sort(key=lambda m: (m.get("found", ""), -(m.get("students") or 0), m.get("id", "")))
        for m in old:
            led.add(m, m.get("page_hash", ""))
        led.save()
        print(f"ledger seeded from matches.json: {len(old)} records")
    return led


# ---------------------------------------------------------------- classify
def cmd_classify(args, run: Run):
    plat = classify.load(DATA / "platforms.json")
    recheck = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    todo = [d for d in districts() if d["id"] not in plat
            or (plat[d["id"]].get("platform") in boards.NO_READER and plat[d["id"]].get("checked", "") < recheck)][: args.limit]
    print(f"classify: {len(todo)} districts to check ({len(plat)} done)")
    done = 0
    cap = (args.budget_min or 0) * 60 * 0.3  # classification gets at most 30% of the run budget
    t0 = time.monotonic()
    ex = ThreadPoolExecutor(config.WORKERS)
    futs = {ex.submit(classify.classify_one, d, TODAY): d for d in todo}
    try:
        for f in as_completed(futs, timeout=cap or None):
            d = futs[f]
            try:
                new = f.result()
            except Exception as e:
                new = {"platform": "none", "agenda_url": "", "checked": TODAY, "note": f"error {type(e).__name__}"}
            plat[d["id"]] = classify.merge(plat.get(d["id"]), new)
            done += 1
            if done % 50 == 0:
                classify.save(DATA / "platforms.json", plat)
            if cap and time.monotonic() - t0 > cap:
                print("  classify time cap reached; the rest go next run")
                break
    except FutTimeout:
        print("  classify time cap reached (reads still in flight); the rest go next run")
    ex.shutdown(wait=False, cancel_futures=True)
    classify.save(DATA / "platforms.json", plat)
    run.rec["counts"]["classified"] = done
    run.source("classify", True)
    print("platforms:", Counter(v["platform"] for v in plat.values()))


# ---------------------------------------------------------------- agendas
def read_one(d, p, since):
    """Returns (district, matches, failure_code or None, note)."""
    http.set_deadline(time.monotonic() + config.DISTRICT_SOFT_CAP_SEC)
    try:
        return _read_one(d, p, since)
    finally:
        http.set_deadline(None)


def _read_one(d, p, since):
    reader = boards.READERS.get(p["platform"])
    if not reader:
        return d, [], "no_reader", p["platform"]
    try:
        return d, reader(d, p["agenda_url"], since, TODAY), None, ""
    except Exception as e:
        code = http.diagnose(e)
        fb = p.get("fallback") or ""
        if code in ("forbidden", "robots", "rate_limited", "js_required") and fb and fb != p["agenda_url"]:
            try:
                return d, boards.generic(d, fb, since, TODAY), None, f"portal {code}; read district site instead"
            except Exception as e2:
                return d, [], code, f"{e} | fallback: {type(e2).__name__}"
        return d, [], code, str(e)[:200]


def record(led: Ledger, c: Counter, matches) -> bool:
    """Add matches to the ledger. Returns True when at least one new reviewable record came out of them."""
    got = False
    for m in matches:
        rec = led.add(m, m.get("page_hash", ""))
        c["parsed_matches"] += 1
        if rec is None:
            c["duplicated"] += 1
            continue
        c["new_records"] += 1
        c["disp_" + rec["disposition"]] += 1
        if rec["disposition"] in ("review", "changed"):
            c["qualified"] += 1
            got = True
        elif rec["disposition"] == "duplicate":
            c["duplicated"] += 1
    return got


def cmd_agendas(args, run: Run, led: Ledger, reader=read_one, clock=time.time):
    plat = classify.load(DATA / "platforms.json")
    state = load_json(DATA / "state.json", {})
    walked = state.setdefault("walked", {})
    failures = state.setdefault("failures", {})
    first = set(state.get("deferred", []))
    since = dt.date.today() - dt.timedelta(days=config.LOOKBACK_DAYS)
    cutoff = (dt.date.today() - dt.timedelta(days=config.READ_EVERY_DAYS)).isoformat()
    cutoff_quiet = (dt.date.today() - dt.timedelta(days=config.READ_EVERY_DAYS_QUIET)).isoformat()
    yld = state.setdefault("yield", {})
    if not yld:  # first run with cadence tiers: seed from the ledger
        for r in led.records():
            if r.get("disposition") in ("review", "changed") and r.get("nces_id"):
                yld[r["nces_id"]] = max(yld.get(r["nces_id"], ""), r.get("found") or TODAY)
    yield_since = (dt.date.today() - dt.timedelta(days=config.YIELD_WINDOW_DAYS)).isoformat()

    def quiet(d):
        return (d.get("st") or 0) < config.QUIET_MAX_STUDENTS and yld.get(d["id"], "") < yield_since

    def due(d):
        p = plat.get(d["id"])
        if not p or p["platform"] not in boards.READERS:
            return False
        f = failures.get(d["id"])
        if f and f.get("retry_after", "") > TODAY:
            return False
        if d["id"] in first:
            return True
        return walked.get(d["id"], "") < (cutoff_quiet if quiet(d) else cutoff)

    pool = [d for d in districts() if due(d)]
    pool.sort(key=lambda d: (d["id"] not in first, -(d.get("st") or 0)))
    todo = pool[: args.limit]
    c = run.rec["counts"]
    c["discovered"] += len(pool)
    c["queued"] += len(todo)
    print(f"agendas: {len(pool)} districts due, {len(todo)} queued (lookback {config.LOOKBACK_DAYS} days)")
    t0 = clock()
    finished = set()
    stopped = False

    def checkpoint():
        led.save()
        state["deferred"] = [x["id"] for x in todo if x["id"] not in finished]
        save_json(DATA / "state.json", state)
        run.save()

    real0 = time.monotonic()
    ex = ThreadPoolExecutor(config.WORKERS)
    futs = {ex.submit(reader, d, plat[d["id"]], since): d for d in todo}
    # Hard stop: a hung read can no longer hold the run past the budget, because as_completed gives up
    # when the budget runs out even if nothing finishes, and the pool is never joined.
    # Budget counts from process start, so classification time comes out of it too.
    wall = max(60.0, args.budget_min * 60 - (time.monotonic() - START)) if args.budget_min else None
    try:
        for f in as_completed(futs, timeout=wall):
            try:
                d, matches, code, note = f.result()
            except Exception as e:
                d, matches, code, note = futs[f], [], "server_error", f"reader crashed: {type(e).__name__}"
            finished.add(d["id"])
            if code:
                prev = failures.get(d["id"], {})
                days = config.RETRY_AFTER_DAYS.get(code, 2)
                failures[d["id"]] = {"code": code, "note": note, "attempts": prev.get("attempts", 0) + 1, "last": TODAY,
                                     "retry_after": (dt.date.today() + dt.timedelta(days=days)).isoformat(),
                                     "district": d["n"], "state": d["s"], "students": d.get("st", 0),
                                     "platform": plat[d["id"]]["platform"], "url": plat[d["id"]].get("agenda_url", "")}
                c["failed"] += 1
                c["failed_" + code] += 1
            else:
                walked[d["id"]] = TODAY
                failures.pop(d["id"], None)
                c["fetched"] += 1
                if note:
                    c["fallback_reads"] += 1
            if record(led, c, matches):
                yld[d["id"]] = TODAY
            if len(finished) % config.CHECKPOINT_EVERY == 0:
                checkpoint()
                print(f"  checkpoint: {len(finished)}/{len(todo)}", flush=True)
            if args.budget_min and ((clock() - t0) / 60 > args.budget_min or time.monotonic() - real0 > wall):
                print("  time budget reached; stopping (in-flight reads abandoned)", flush=True)
                stopped = True
                break
    except FutTimeout:
        print("  time budget reached while reads were hung; stopping", flush=True)
        stopped = True
    ex.shutdown(wait=False, cancel_futures=True)
    checkpoint()
    c["deferred"] = len(state["deferred"])
    c["not_queued"] = max(0, len(pool) - len(todo))
    state["last_run"] = RUN_ID
    save_json(DATA / "state.json", state)
    run.source("agendas", not stopped and c["failed"] < max(50, len(todo) // 2), "budget reached" if stopped else "")
    run.rec["minutes"] = round((clock() - t0) / 60, 1)
    diag = {"generated": NOW(), "by_code": Counter(v["code"] for v in failures.values()),
            "failures": sorted(({"nces_id": k, **v} for k, v in failures.items()), key=lambda x: -(x.get("students") or 0))}
    save_json(DATA / "diagnostics.json", diag)
    write_survey()
    print(f"  fetched {c['fetched']}, failed {c['failed']}, deferred {c['deferred']}, new records {c['new_records']}, "
          f"duplicates {c['duplicated']}, qualified {c['qualified']}")


def write_survey():
    """Merge this run's vendor sightings into data/platform_survey.json and recount by vendor (ICP districts)."""
    path = DATA / "platform_survey.json"
    sv = load_json(path, {"districts": {}, "summary": {}})
    for nid, vendors in boards.SURVEY.items():
        sv["districts"][nid] = {"vendors": vendors, "seen": TODAY}
    icp = {d["id"] for d in districts() if d.get("icp")}
    summ = Counter()
    for nid, v in sv["districts"].items():
        if nid in icp:
            for k in v["vendors"]:
                summ[k] += 1
    sv["summary"] = {"generated": NOW(), "districts_surveyed": len(sv["districts"]),
                     "icp_districts_by_vendor": dict(summ.most_common()),
                     "rule": "build a reader for any vendor at 50+ ICP districts"}
    save_json(path, sv)


# ---------------------------------------------------------------- money
def cmd_money(args, run: Run, led: Ledger):
    idx = index_by_name(load_json(DATA / "directory.json", []))
    since = dt.date.today() - dt.timedelta(days=120)
    c = run.rec["counts"]
    for name, fn in (("usaspending", lambda: money.usaspending(idx, since, TODAY)), ("grants_gov", lambda: money.grants_gov(TODAY))):
        try:
            got, ok = fn(), True
        except Exception as e:
            got, ok = [], False
            run.rec["notes"].append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
        c["money_matches"] += len(got)
        record(led, c, got)
        # money.* swallow per-request errors, so zero results is treated as a failed refresh, not a success
        run.source(name, ok and bool(got), "" if got else "no results returned")
    led.save()


# ---------------------------------------------------------------- compatibility outputs
def write_compat(run: Run, led: Ledger):
    keep_after = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    ms = [r for r in led.records() if r.get("found", "") >= keep_after and r["disposition"] != "duplicate"]
    ms.sort(key=lambda m: (m["disposition"] not in ("review", "changed"), not m.get("strong"), -(m.get("students") or 0)))
    save_json(DATA / "matches.json", {"generated": NOW(), "note": "compatibility copy; use data/ledger/ (stable seq) instead",
                                      "matches": ms})
    state = load_json(DATA / "state.json", {})
    diag = load_json(DATA / "diagnostics.json", {"failures": []})
    c = run.rec["counts"]
    wk = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    blocked = [f for f in diag["failures"] if f["code"] in ("forbidden", "robots", "rate_limited")]
    report = {"generated": NOW(), "run_id": RUN_ID, "status": run.rec.get("status", ""),
              "last_agenda_run": TODAY, "districts_read": c.get("fetched", 0), "new_matches": c.get("new_records", 0),
              "feed_size": len(ms), "walked_total": len(state.get("walked", {})),
              "checked_last_7_days": sum(1 for v in state.get("walked", {}).values() if v >= wk),
              "failed_total": len(diag["failures"]), "deferred": len(state.get("deferred", [])),
              "ledger": led.counts(), "counts": dict(c), "sources": run.runs["sources"],
              "blocked": blocked[:200], "blocked_count": len(blocked),
              "errors": [f for f in diag["failures"] if f not in blocked][:200], "minutes": run.rec.get("minutes", 0)}
    save_json(DATA / "report.json", report)
    write_recovery(diag)


def write_recovery(diag):
    """Every ICP district the crawler cannot read, with a reason code: failures (403, robots, 429, timeout, JS-only,
    scanned PDFs...) plus districts on a portal with no reader (Diligent Community, eBoard, CivicClerk, none found).
    The sweep ranks these (fit, timing, relationship, value) and works them through official alternates first."""
    plat = classify.load(DATA / "platforms.json")
    dirs = {d["id"]: d for d in districts()}
    out = {}
    for f in diag["failures"]:
        out[f["nces_id"]] = {"nces_id": f["nces_id"], "district": f["district"], "state": f["state"], "students": f.get("students", 0),
                             "code": f["code"], "note": f.get("note", ""), "platform": f.get("platform", ""), "url": f.get("url", ""),
                             "attempts": f.get("attempts", 1), "last": f.get("last", "")}
    for i, p in plat.items():
        if i in out or i not in dirs or p.get("platform") not in boards.NO_READER:
            continue
        d = dirs[i]
        code = {"none": "no_portal_found", "blocked": "site_blocked"}.get(p["platform"], "no_reader_" + p["platform"])
        out[i] = {"nces_id": i, "district": d["n"], "state": d["s"], "students": d.get("st", 0), "code": code,
                  "note": p.get("note", ""), "platform": p["platform"], "url": p.get("agenda_url", ""), "attempts": 0,
                  "last": p.get("checked", "")}
        if p.get("history"):
            out[i]["history"] = p["history"]
        if p.get("migrated_from"):
            out[i]["migrated_from"] = p["migrated_from"]
    rows = sorted(out.values(), key=lambda x: -(x.get("students") or 0))
    save_json(DATA / "recovery.json", {"generated": NOW(), "count": len(rows), "by_code": dict(Counter(r["code"] for r in rows)), "districts": rows})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["classify", "agendas", "money", "all"])
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--budget-min", type=float, default=0)
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    run = Run()
    led = ensure_ledger()
    try:
        if args.cmd in ("classify", "all"):
            cmd_classify(args, run)
        if args.cmd in ("agendas", "all"):
            cmd_agendas(args, run, led)
        if args.cmd in ("money", "all"):
            cmd_money(args, run, led)
        run.save(final=True)
    except Exception:
        traceback.print_exc()
        run.rec["notes"].append(traceback.format_exc()[-500:])
        run.rec["status"] = "failed"
        led.save()
        run.save()
        write_compat(run, led)
        sys.stdout.flush()
        os._exit(1)
    write_compat(run, led)
    sys.stdout.flush()
    os._exit(0)  # abandoned reader threads (hung sockets) must not keep the job alive


if __name__ == "__main__":
    main()
