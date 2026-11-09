#!/usr/bin/env python3
"""Consumer side of the ledger: hand the daily sweep a batch of feed records and checkpoint what it finished.

The sweep (a Claude run) keeps its position in the District Signals database (meta/feed). It:
  1. saves that doc to a file          -> feedstate.json
  2. python tools/feed.py next   --state feedstate.json --limit 40 --out batch.json
  3. verifies each record in batch.json (opens the source, decides signal / no signal)
  4. python tools/feed.py commit --state feedstate.json --batch batch.json --done 12,15 --failed "18:403 on page" --out feedstate.json
     (records it did not get to are simply not listed: they stay pending and come back next run)
  5. writes feedstate.json back to meta/feed (pinned to the version it read)

Guarantees:
  - Every ledger record ends in exactly one state: auto (low/duplicate, counted not reviewed), done, gave_up, or pending.
  - Nothing is skipped: candidates are issued oldest-window-first, so the cursor can only advance over handled records.
  - Restarting is safe: a batch that was issued but never committed is simply issued again; committing the same
    seq twice is a no-op, so nothing is lost or double-counted.
  - Repeated coverage of one document page (same group_key) is handed out once; finishing it closes the whole group.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.ledger import Ledger  # noqa: E402

REVIEW = ("review", "changed")
MAX_ATTEMPTS = 3
WINDOW = 600  # candidates are prioritized only within the oldest WINDOW undone records, so nothing starves


def load_state(p):
    s = json.loads(Path(p).read_text()) if p and Path(p).exists() and Path(p).stat().st_size else {}
    s.setdefault("version", 1)
    s.setdefault("cursor", 0)
    s.setdefault("done", [])
    s.setdefault("failed", {})
    s.setdefault("gave_up", [])
    s.setdefault("outcomes", {})
    return s


def undone(led, s):
    closed = set(s["done"]) | set(s["gave_up"])
    for r in led.records(after_seq=s["cursor"]):
        if r["disposition"] in REVIEW and r["seq"] not in closed:
            yield r


def advance(led, s):
    """Move the cursor over every record that is auto-dispositioned or closed, stopping at the first open one."""
    closed = set(s["done"]) | set(s["gave_up"])
    cur = s["cursor"]
    for r in led.records(after_seq=cur):
        if r["disposition"] in REVIEW and r["seq"] not in closed:
            break
        cur = r["seq"]
    s["cursor"] = cur
    s["done"] = sorted(x for x in set(s["done"]) if x > cur)
    s["gave_up"] = sorted(set(s["gave_up"]))
    s["failed"] = {k: v for k, v in s["failed"].items() if int(k) > cur}


def priority(r):
    return (0 if r.get("competitor") else 1, 0 if r.get("source_type") == "award" else 1,
            0 if r.get("strong") else 1, -(r.get("students") or 0), r["seq"])


def cmd_next(a):
    led, s = Ledger(Path(a.ledger)), load_state(a.state)
    window = []
    for r in undone(led, s):
        window.append(r)
        if len(window) >= WINDOW:
            break
    groups, batch = {}, []
    for r in sorted(window, key=priority):
        g = groups.get(r["group_key"])
        if g is not None:
            g["also"].append(r["seq"])
            continue
        if len(batch) < a.limit:
            r = dict(r, also=[], attempts=s["failed"].get(str(r["seq"]), {}).get("attempts", 0))
            groups[r["group_key"]] = r
            batch.append(r)
    out = {"ledger_next_seq": led.index["next_seq"], "cursor": s["cursor"], "issued": len(batch), "records": batch}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({"issued": len(batch), "cursor": s["cursor"], "window": len(window)}))


def parse_list(v):
    return [x for x in (v or "").split(",") if x.strip()]


def cmd_commit(a):
    led, s = Ledger(Path(a.ledger)), load_state(a.state)
    batch = json.loads(Path(a.batch).read_text()) if a.batch else {"records": []}
    also = {r["seq"]: r.get("also", []) for r in batch["records"]}
    done = set(s["done"])
    for item in parse_list(a.done):
        seq, _, outcome = item.partition("=")
        seq = int(seq)
        for x in [seq] + also.get(seq, []):
            done.add(x)
            s["failed"].pop(str(x), None)
            if outcome:
                s["outcomes"][str(x)] = outcome
    for item in parse_list(a.failed):
        seq, _, note = item.partition(":")
        k = str(int(seq))
        if int(seq) in done:
            continue
        f = s["failed"].setdefault(k, {"attempts": 0})
        f["attempts"] += 1
        f["note"] = note[:160]
        if f["attempts"] >= MAX_ATTEMPTS:
            s["gave_up"] = sorted(set(s["gave_up"]) | {int(seq)} | set(also.get(int(seq), [])))
            s["outcomes"][k] = "gave_up: " + note[:120]
    s["done"] = sorted(done)
    advance(led, s)
    s["counts"] = counts(led, s)
    Path(a.out).write_text(json.dumps(s, ensure_ascii=False))
    print(json.dumps(s["counts"]))


def counts(led, s):
    closed_done, closed_gu = set(s["done"]), set(s["gave_up"])
    c = {"records": 0, "auto_low": 0, "auto_duplicate": 0, "to_review": 0, "reviewed": 0, "gave_up": 0, "pending": 0, "failed_retrying": 0}
    for r in led.records():
        c["records"] += 1
        d = r["disposition"]
        if d == "low":
            c["auto_low"] += 1
        elif d == "duplicate":
            c["auto_duplicate"] += 1
        else:
            c["to_review"] += 1
            if r["seq"] <= s["cursor"] and r["seq"] not in closed_gu or r["seq"] in closed_done:
                c["reviewed"] += 1
            elif r["seq"] in closed_gu:
                c["gave_up"] += 1
            else:
                c["pending"] += 1
                c["failed_retrying"] += str(r["seq"]) in s["failed"]
    c["cursor"] = s["cursor"]
    c["ledger_next_seq"] = led.index["next_seq"]
    return c


def cmd_status(a):
    led, s = Ledger(Path(a.ledger)), load_state(a.state)
    print(json.dumps(counts(led, s), indent=1))


def cmd_import_done(a):
    """One-time migration: mark records whose old match ids are listed (meta/scan feed_done) as done."""
    led, s = Ledger(Path(a.ledger)), load_state(a.state)
    ids = set(json.loads(Path(a.ids).read_text()))
    n = 0
    for r in led.records():
        if r["id"] in ids and r["disposition"] in REVIEW:
            s["done"].append(r["seq"])
            s["outcomes"][str(r["seq"])] = "handled before ledger (feed_done)"
            n += 1
    advance(led, s)
    s["counts"] = counts(led, s)
    Path(a.out).write_text(json.dumps(s, ensure_ascii=False))
    print(json.dumps({"imported": n, **s["counts"]}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["next", "commit", "status", "import-done"])
    ap.add_argument("--ledger", default=str(Path(__file__).resolve().parent.parent / "data" / "ledger"))
    ap.add_argument("--state", default="")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", default="")
    ap.add_argument("--batch", default="")
    ap.add_argument("--done", default="")
    ap.add_argument("--failed", default="")
    ap.add_argument("--ids", default="")
    a = ap.parse_args()
    {"next": cmd_next, "commit": cmd_commit, "status": cmd_status, "import-done": cmd_import_done}[a.cmd](a)


if __name__ == "__main__":
    main()
