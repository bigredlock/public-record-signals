"""Durable feed ledger: every match the crawler finds gets a permanent sequence number and a disposition.

Files (all under data/ledger/, committed by the GitHub Action):
  index.json        {"version", "next_seq", "ids": {match_id: seq}, "groups": {group_key: {...}}, "chunks": [...]}
  chunk-000001.jsonl, chunk-000002.jsonl, ...   one record per line, CHUNK_SIZE records per file, append-only

Why: the old feed was one big matches.json re-sorted every night, so a reader that could only see the first
part saw the same ~100 records forever. Here a record never moves or changes once written; consumers keep a
cursor (see tools/feed.py) and can always resume exactly where they stopped.

Record fields (on top of the filter.scan fields):
  seq          int, permanent, increasing in discovery order
  group_key    sha1(nces_id|url|page)[:16]: one document page = one group
  text_hash    sha1 of the normalized page text the snippet came from (change detection)
  disposition  "review"    worth a human/Claude look (specific door, lock, access, funding or competitor terms)
               "low"       only generic words (safety, security, SRO, threat assessment): counted, not reviewed
               "duplicate" another snippet on a page already in the ledger with the same text
               "changed"   the page was seen before but its text changed (re-review it)
  reason       why that disposition
  competitor   list of competitor brands named in the snippet (from config.COMPETITOR_PATTERNS), if any
"""
import hashlib
import json
import re
from pathlib import Path

from . import config

CHUNK_SIZE = 500
LEDGER_VERSION = 1

# Specific terms that make a match worth review even when STRONG_RE did not fire.
REVIEW_RE = re.compile(
    r"classroom\s+door|door\s+lock|door\s+hardware|lock(?:ing)?\s+hardware|locks?\b|lockdown|intruder|barricade|"
    r"access\s+control|vestibule|harden|safe\s+room|STOP\s+School\s+Violence|SVPP|COPS|SAFE\s+grant|"
    r"school\s+safety\s+grant|security\s+(?:upgrades?|improvements?|projects?)|safety\s+and\s+security\s+(?:upgrades?|improvements?|projects?)",
    re.I,
)


def group_key(nces_id: str, url: str, page) -> str:
    return hashlib.sha1(f"{nces_id}|{url}|{page}".encode()).hexdigest()[:16]


def text_hash(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").lower().split()).encode()).hexdigest()[:12]


def competitors_in(text: str) -> list[str]:
    found = []
    for brand, rx in config.COMPETITOR_RES.items():
        if rx.search(text or ""):
            found.append(brand)
    return found


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / "index.json"
        if p.exists():
            self.index = json.loads(p.read_text())
        else:
            self.index = {"version": LEDGER_VERSION, "next_seq": 1, "ids": {}, "groups": {}, "chunks": []}
        self._pending: list[dict] = []

    # ---------------------------------------------------------------- writing
    def add(self, m: dict, page_text_hash: str = "") -> dict | None:
        """Add one match. Returns the stored record, or None when the exact match id is already in the ledger."""
        ids = self.index["ids"]
        if m["id"] in ids:
            return None
        gk = group_key(m.get("nces_id", ""), m.get("url", ""), m.get("page"))
        g = self.index["groups"].get(gk)
        th = page_text_hash  # "" for legacy records without a page hash: no change detection for those
        comp = competitors_in(m.get("snippet", "") + " " + m.get("title", ""))
        specific = bool(m.get("strong")) or bool(REVIEW_RE.search(m.get("snippet", ""))) or m.get("source_type") in ("award", "grant_window") or bool(comp)
        if g is None:
            disposition = "review" if specific else "low"
            reason = "specific term" if specific else "generic words only (" + ", ".join(m.get("matched", [])[:4]) + ")"
        elif th and g.get("text_hash") and th != g["text_hash"]:
            disposition = "changed" if (specific or g.get("review")) else "low"
            reason = f"page text changed since seq {g['first_seq']}"
        else:
            # Same page, same text, another snippet. Promote to review only if it adds a specific term the group lacked.
            if specific and not g.get("review"):
                disposition, reason = "review", f"new specific term on page first seen at seq {g['first_seq']}"
            else:
                disposition, reason = "duplicate", f"same page as seq {g['first_seq']}"
        seq = self.index["next_seq"]
        rec = dict(m, seq=seq, group_key=gk, text_hash=th, disposition=disposition, reason=reason)
        if comp:
            rec["competitor"] = comp
        self.index["next_seq"] = seq + 1
        ids[m["id"]] = seq
        if g is None:
            self.index["groups"][gk] = {"first_seq": seq, "last_seq": seq, "count": 1, "text_hash": th,
                                        "review": disposition == "review", "nces_id": m.get("nces_id", "")}
        else:
            g["last_seq"] = seq
            g["count"] += 1
            if disposition in ("review", "changed"):
                g["review"] = True
            if disposition == "changed" or (th and not g.get("text_hash")):
                g["text_hash"] = th
        self._pending.append(rec)
        return rec

    def save(self):
        """Append pending records to chunk files and write the index. Safe to call often (checkpoints)."""
        chunks = self.index["chunks"]
        while self._pending:
            if not chunks or chunks[-1]["count"] >= CHUNK_SIZE:
                n = len(chunks) + 1
                chunks.append({"file": f"chunk-{n:06d}.jsonl", "first_seq": self._pending[0]["seq"], "last_seq": 0, "count": 0})
            c = chunks[-1]
            room = CHUNK_SIZE - c["count"]
            take, self._pending = self._pending[:room], self._pending[room:]
            with open(self.root / c["file"], "a", encoding="utf-8") as f:
                for r in take:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            c["count"] += len(take)
            c["last_seq"] = take[-1]["seq"]
        tmp = self.root / "index.json.tmp"
        tmp.write_text(json.dumps(self.index, ensure_ascii=False))
        tmp.replace(self.root / "index.json")

    # ---------------------------------------------------------------- reading
    def records(self, after_seq: int = 0):
        for c in self.index["chunks"]:
            if c["last_seq"] <= after_seq:
                continue
            with open(self.root / c["file"], encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    if r["seq"] > after_seq:
                        yield r

    def counts(self) -> dict:
        out = {"records": 0, "review": 0, "changed": 0, "low": 0, "duplicate": 0}
        for r in self.records():
            out["records"] += 1
            out[r["disposition"]] = out.get(r["disposition"], 0) + 1
        out["groups"] = len(self.index["groups"])
        out["next_seq"] = self.index["next_seq"]
        return out
