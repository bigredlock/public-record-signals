"""Completeness and restart tests for the ledger + feed consumer.

Run: python -m pytest tests -q   (uses a synthetic ledger; tools/feed.py is exercised through its CLI functions)
"""
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.ledger import Ledger  # noqa: E402

FEED = [sys.executable, str(ROOT / "tools" / "feed.py")]


def make_ledger(tmp, n=1000, seed=7):
    rnd = random.Random(seed)
    led = Ledger(tmp / "ledger")
    for i in range(n):
        page = rnd.randint(1, 3)
        url = f"https://x.org/doc{rnd.randint(1, n // 2)}.pdf"
        strong = rnd.random() < 0.3
        snip = ("classroom door locks " if strong else "general safety update ") + str(i)
        led.add({"id": f"m{i}", "district": "D", "state": "Texas", "nces_id": str(rnd.randint(1, 80)), "students": rnd.randint(500, 90000),
                 "source_type": "agenda", "platform": "site", "url": url, "page": page, "title": "t", "date": "",
                 "matched": ["lock"] if strong else ["safety"], "strong": strong, "snippet": snip, "found": "2026-11-09"}, "")
    led.save()
    return led


def feed(*args):
    out = subprocess.run(FEED + list(args), capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def drain(tmp, crash_after=None, limit=40):
    """Process every review record in batches. If crash_after=k, the k-th batch is issued but never committed."""
    st, batch = tmp / "state.json", tmp / "batch.json"
    seen_done = []
    k = 0
    while True:
        r = feed("next", "--ledger", str(tmp / "ledger"), "--state", str(st), "--limit", str(limit), "--out", str(batch))
        if r["issued"] == 0:
            break
        k += 1
        recs = json.loads(batch.read_text())["records"]
        if crash_after == k:
            crash_after = None  # the run died: nothing committed; the restart must re-issue these
            continue
        done = ",".join(str(x["seq"]) for x in recs)
        seen_done += [x["seq"] for x in recs]
        feed("commit", "--ledger", str(tmp / "ledger"), "--state", str(st), "--batch", str(batch), "--done", done, "--out", str(st))
    return json.loads(st.read_text()), seen_done, k


def test_batch_larger_than_107_processes_completely(tmp_path):
    led = make_ledger(tmp_path, n=1000)
    c = led.counts()
    to_review = c["review"] + c["changed"]
    assert to_review > 107
    s, done, batches = drain(tmp_path)
    assert s["counts"]["pending"] == 0
    assert s["counts"]["reviewed"] == to_review
    assert s["cursor"] == led.index["next_seq"] - 1


def test_restart_after_crash_loses_and_duplicates_nothing(tmp_path):
    led = make_ledger(tmp_path, n=1000)
    to_review = led.counts()["review"]
    s, done, batches = drain(tmp_path, crash_after=3)
    assert s["counts"]["pending"] == 0 and s["counts"]["reviewed"] == to_review
    assert len(done) == len(set(done)), "a record was handed out and finished twice"


def test_failed_records_retry_then_give_up(tmp_path):
    make_ledger(tmp_path, n=300)
    st, batch = tmp_path / "state.json", tmp_path / "batch.json"
    feed("next", "--ledger", str(tmp_path / "ledger"), "--state", str(st), "--limit", "5", "--out", str(batch))
    first = json.loads(batch.read_text())["records"][0]["seq"]
    for i in range(3):
        feed("commit", "--ledger", str(tmp_path / "ledger"), "--state", str(st), "--batch", str(batch), "--failed", f"{first}:403", "--out", str(st))
        s = json.loads(st.read_text())
        if i < 2:
            assert str(first) in s["failed"] and first not in s["gave_up"]
    assert first in s["gave_up"]


def test_duplicate_pages_handed_out_once(tmp_path):
    led = Ledger(tmp_path / "ledger")
    base = {"district": "D", "state": "Texas", "nces_id": "1", "students": 1, "source_type": "agenda", "platform": "site",
            "url": "https://x.org/a.pdf", "page": 4, "title": "t", "date": "", "matched": ["door lock"], "strong": True, "found": "2026-11-09"}
    led.add(dict(base, id="a", snippet="classroom door locks for all campuses"), "h1")
    led.add(dict(base, id="b", snippet="door lock replacement phase 2"), "h1")   # same page, same text -> duplicate
    led.add(dict(base, id="c", snippet="door lock replacement phase 2 revised"), "h2")  # page text changed -> changed
    led.save()
    disp = [r["disposition"] for r in led.records()]
    assert disp == ["review", "duplicate", "changed"]
    assert led.add(dict(base, id="a", snippet="x"), "h1") is None  # same match id never stored twice
