"""Crawler run loop: checkpoints, deferred carry-forward, failure diagnosis, and restart without loss."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run  # noqa: E402
from crawler import config, http  # noqa: E402
from crawler.ledger import Ledger  # noqa: E402


def setup(tmp, n=300):
    data = tmp / "data"
    data.mkdir()
    dirs = [{"id": f"{i:07d}", "n": f"District {i}", "s": "Texas", "st": 10000 - i, "icp": True} for i in range(n)]
    (data / "directory.json").write_text(json.dumps(dirs))
    (data / "platforms.json").write_text(json.dumps({d["id"]: {"platform": "site", "agenda_url": "https://x"} for d in dirs}))
    run.DATA = data
    config.WORKERS = 1
    config.CHECKPOINT_EVERY = 25
    return data, dirs


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0  # each call is one "second"; budget in minutes below
        return self.t


def fake_reader(d, p, since):
    i = int(d["id"])
    if i % 50 == 7:
        return d, [], "forbidden", "HTTP 403"
    if i % 50 == 9:
        return d, [], "timeout", "read timeout"
    m = {"id": f"m{i}", "district": d["n"], "state": d["s"], "nces_id": d["id"], "students": d["st"], "source_type": "agenda",
         "platform": "site", "url": f"https://x/{i}", "page": None, "title": "t", "date": "", "matched": ["door lock"],
         "strong": True, "snippet": "classroom door lock purchase", "found": "2026-11-09", "page_hash": f"h{i}"}
    return d, [m], None, ""


def test_budget_stop_defers_and_restart_finishes(tmp_path):
    data, dirs = setup(tmp_path, 300)
    args = argparse.Namespace(limit=300, budget_min=2.0)  # ~120 reads before the budget trips
    r1 = run.Run()
    led = Ledger(data / "ledger")
    run.cmd_agendas(args, r1, led, reader=fake_reader, clock=Clock())
    r1.save(final=True)
    st = json.loads((data / "state.json").read_text())
    assert r1.rec["status"] == "partial"
    assert 107 < len(st["walked"]) < 300
    assert len(st["deferred"]) == 300 - len(st["walked"]) - len(st["failures"])
    first_walked = set(st["walked"])

    # restart: deferred go first, nothing read twice, failures wait for their retry date
    r2 = run.Run()
    led2 = Ledger(data / "ledger")
    run.cmd_agendas(argparse.Namespace(limit=300, budget_min=0), r2, led2, reader=fake_reader, clock=Clock())
    r2.save(final=True)
    st2 = json.loads((data / "state.json").read_text())
    assert st2["deferred"] == []
    assert first_walked <= set(st2["walked"])
    assert len(st2["walked"]) + len(st2["failures"]) == 300
    assert r2.rec["counts"]["fetched"] == len(st2["walked"]) - len(first_walked)  # no district re-read
    codes = {v["code"] for v in st2["failures"].values()}
    assert codes == {"forbidden", "timeout"}
    assert all(v["retry_after"] > "2026-11-09" or v["retry_after"] > run.TODAY for v in st2["failures"].values())
    # every successful district's match is in the ledger exactly once
    ids = [r["id"] for r in Ledger(data / "ledger").records()]
    assert len(ids) == len(set(ids)) == len(st2["walked"])


def test_diagnose_codes():
    import requests
    assert http.diagnose(http.Blocked("x", "robots")) == "robots"
    assert http.diagnose(http.Blocked("x", "rate_limited")) == "rate_limited"
    assert http.diagnose(requests.Timeout()) == "timeout"
    assert http.diagnose(http.JsRequired()) == "js_required"
    assert http.looks_js_only("<html><body>Please enable JavaScript to view this site.</body></html>")


def test_hung_reads_cannot_hold_the_run_past_budget(tmp_path):
    """Nov 9 run #6: reads hung for 90+ minutes, the budget check never ran, the job was killed and nothing
    was saved. Now the run stops at the budget even when no read ever returns, and checkpoints what it has."""
    import threading
    import time as _t
    data, dirs = setup(tmp_path, 40)
    config.WORKERS = 4
    gate = threading.Event()

    def reader(d, p, since):
        if int(d["id"]) >= 10:
            gate.wait(600)  # hangs like a stuck socket
        return fake_reader(d, p, since)

    run.START = _t.monotonic() - (2.0 * 60 - 3)  # 3 real seconds of budget left (floor is 60s otherwise)
    args = argparse.Namespace(limit=40, budget_min=2.0)
    r = run.Run()
    led = Ledger(data / "ledger")
    t0 = _t.monotonic()
    import builtins
    real_max = builtins.max
    run.max = lambda *a: a[1] if len(a) == 2 and a[0] == 60.0 else real_max(*a)  # drop the 60s floor for the test
    try:
        run.cmd_agendas(args, r, led, reader=reader, clock=_t.monotonic)
    finally:
        del run.max
        gate.set()
    assert _t.monotonic() - t0 < 30
    st = json.loads((data / "state.json").read_text())
    assert len(st["walked"]) + len(st["failures"]) <= 10
    assert len(st["deferred"]) >= 30
    assert (data / "diagnostics.json").exists()


def test_quiet_small_districts_wait_14_days_big_or_productive_ones_7(tmp_path):
    """Nov 10 throughput fix: small districts with no reviewable record in 60 days are re-read every 14 days."""
    import datetime as _dt
    data, dirs = setup(tmp_path, 4)
    ten_days_ago = (_dt.date.today() - _dt.timedelta(days=10)).isoformat()
    dirs[0]["st"] = 50_000   # big: due after 7 days
    dirs[1]["st"] = 2_000    # small and quiet: not due yet
    dirs[2]["st"] = 2_000    # small but produced a review record recently: due
    dirs[3]["st"] = 2_000    # small, quiet, but deferred last run: due
    (data / "directory.json").write_text(json.dumps(dirs)) if (data / "directory.json").exists() else None
    st = {"walked": {d["id"]: ten_days_ago for d in dirs}, "failures": {}, "deferred": [dirs[3]["id"]],
          "yield": {dirs[2]["id"]: _dt.date.today().isoformat()}}
    (data / "state.json").write_text(json.dumps(st))
    seen = []

    def reader(d, p, since):
        seen.append(d["id"])
        return d, [], None, ""
    run.cmd_agendas(argparse.Namespace(limit=10, budget_min=0), run.Run(), Ledger(data / "ledger"), reader=reader, clock=Clock())
    assert set(seen) == {dirs[0]["id"], dirs[2]["id"], dirs[3]["id"]}


def test_district_soft_cap_skips_further_pdfs(monkeypatch):
    import time as _t
    from crawler.sources import boards
    calls = []
    monkeypatch.setattr(http, "get", lambda url, **kw: calls.append(url))
    http.set_deadline(_t.monotonic() - 1)
    try:
        assert boards._pdf_matches({"n": "x", "s": "y", "id": "1"}, "https://x/a.pdf", "t", None, "2026-11-10", "site") == []
    finally:
        http.set_deadline(None)
    assert calls == []
