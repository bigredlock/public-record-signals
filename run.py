#!/usr/bin/env python3
"""Public Record Signals crawler.

Usage:
  python run.py classify  [--limit N]        # find each ICP district's agenda platform (cached, resumable)
  python run.py agendas   [--limit N] [--budget-min M]   # read agendas for the next N unwalked districts
  python run.py money                          # USAspending awards + Grants.gov windows
  python run.py all       [--limit N] [--budget-min M]   # what the weekly job runs

Outputs (committed by the GitHub Action):
  data/platforms.json   platform per district
  data/matches.json     the feed the review step reads: {"generated", "matches": [...]}
  data/state.json       walk position, so each run continues where the last stopped
  data/report.json      counts, blocked hosts, errors (the runs list these as gaps)
"""
import argparse
import datetime as dt
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from crawler import classify, config, http
from crawler.sources import boards, money

ROOT = Path(__file__).parent
DATA = ROOT / "data"
TODAY = dt.date.today().isoformat()


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def save_json(p: Path, obj):
    p.write_text(json.dumps(obj, indent=0, ensure_ascii=False))


def districts():
    d = load_json(DATA / "directory.json", [])
    return [x for x in d if x.get("icp")]


def index_by_name(d):
    st_abbr = {v: k for k, v in STATES.items()}
    idx = {}
    for x in d:
        idx[st_abbr.get(x["s"], x["s"]).upper() + "|" + money._core(x["n"])] = x
    return idx


STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District Of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def cmd_classify(args):
    plat = classify.load(DATA / "platforms.json")
    recheck = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    todo = [d for d in districts() if d["id"] not in plat
            or (plat[d["id"]].get("platform") in ("none", "blocked") and plat[d["id"]].get("checked", "") < recheck)][: args.limit]
    print(f"classify: {len(todo)} districts to check ({len(plat)} done)")
    done = 0
    with ThreadPoolExecutor(config.WORKERS) as ex:
        futs = {ex.submit(classify.classify_one, d, TODAY): d for d in todo}
        for f in as_completed(futs):
            d = futs[f]
            try:
                plat[d["id"]] = f.result()
            except Exception as e:
                plat[d["id"]] = {"platform": "none", "agenda_url": "", "checked": TODAY, "note": f"error {type(e).__name__}"}
            done += 1
            if done % 50 == 0:
                classify.save(DATA / "platforms.json", plat)
                print(f"  {done}/{len(todo)}")
    classify.save(DATA / "platforms.json", plat)
    from collections import Counter
    print("platforms:", Counter(v["platform"] for v in plat.values()))


def read_one(d, p, since):
    reader = boards.READERS.get(p["platform"])
    if not reader:
        return d, [], None
    try:
        return d, reader(d, p["agenda_url"], since, TODAY), None
    except http.Blocked as e:
        # The portal refuses automated reading. Fall back to the district's own board page, if it has one,
        # and record the block either way so the runs can list it as a gap.
        fb = p.get("fallback") or ""
        if fb and fb != p["agenda_url"]:
            try:
                return d, boards.generic(d, fb, since, TODAY), f"blocked: {e} (read district site instead)"
            except Exception:
                pass
        return d, [], f"blocked: {e}"
    except Exception as e:
        return d, [], f"error: {type(e).__name__}: {str(e)[:100]}"


def cmd_agendas(args):
    plat = classify.load(DATA / "platforms.json")
    state = load_json(DATA / "state.json", {"walked": {}, "cycle_started": TODAY})
    feed = load_json(DATA / "matches.json", {"generated": "", "matches": []})
    report = load_json(DATA / "report.json", {})
    since = dt.date.today() - dt.timedelta(days=config.LOOKBACK_DAYS)
    walked = state["walked"]
    # Every district gets re-read 7 days after its last read; the biggest first.
    cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    todo = [d for d in districts() if d["id"] in plat and plat[d["id"]]["platform"] in boards.READERS
            and walked.get(d["id"], "") < cutoff][: args.limit]
    print(f"agendas: {len(todo)} districts (lookback {config.LOOKBACK_DAYS} days)")
    existing = {m["id"] for m in feed["matches"]}
    new, blocked, errors = [], [], []
    t0 = time.time()
    with ThreadPoolExecutor(config.WORKERS) as ex:
        futs = [ex.submit(read_one, d, plat[d["id"]], since) for d in todo]
        for f in as_completed(futs):
            d, matches, err = f.result()
            walked[d["id"]] = TODAY
            if err:
                (blocked if err.startswith("blocked") else errors).append({"district": d["n"], "state": d["s"], "nces_id": d["id"], "why": err})
            for m in matches:
                if m["id"] not in existing:
                    existing.add(m["id"])
                    new.append(m)
            if args.budget_min and (time.time() - t0) / 60 > args.budget_min:
                print("  time budget reached; stopping after in-flight reads")
                ex.shutdown(cancel_futures=True)
                break
    # Keep 90 days of matches in the feed.
    keep_after = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    feed["matches"] = [m for m in feed["matches"] if m.get("found", "") >= keep_after] + new
    feed["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    feed["matches"].sort(key=lambda m: (not m.get("strong"), -(m.get("students") or 0)))
    save_json(DATA / "matches.json", feed)
    save_json(DATA / "state.json", state)
    report.update({
        "last_agenda_run": TODAY, "districts_read": len(todo), "new_matches": len(new),
        "feed_size": len(feed["matches"]), "walked_total": len(walked),
        "icp_with_reader": sum(1 for d in districts() if d["id"] in plat and plat[d["id"]]["platform"] in boards.READERS),
        "blocked": blocked[:200], "errors": errors[:200], "minutes": round((time.time() - t0) / 60, 1),
    })
    save_json(DATA / "report.json", report)
    print(f"  new matches {len(new)}, feed {len(feed['matches'])}, blocked {len(blocked)}, errors {len(errors)}, {report['minutes']} min")


def cmd_money(args):
    feed = load_json(DATA / "matches.json", {"generated": "", "matches": []})
    report = load_json(DATA / "report.json", {})
    idx = index_by_name(load_json(DATA / "directory.json", []))
    since = dt.date.today() - dt.timedelta(days=120)
    existing = {m["id"] for m in feed["matches"]}
    new = []
    for m in money.usaspending(idx, since, TODAY) + money.grants_gov(TODAY):
        if m["id"] not in existing:
            existing.add(m["id"])
            new.append(m)
    feed["matches"] += new
    feed["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_json(DATA / "matches.json", feed)
    report.update({"last_money_run": TODAY, "new_money_matches": len(new)})
    save_json(DATA / "report.json", report)
    print(f"money: {len(new)} new")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["classify", "agendas", "money", "all"])
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--budget-min", type=float, default=0)
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    try:
        if args.cmd in ("classify", "all"):
            cmd_classify(args)
        if args.cmd in ("agendas", "all"):
            cmd_agendas(args)
        if args.cmd in ("money", "all"):
            cmd_money(args)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
