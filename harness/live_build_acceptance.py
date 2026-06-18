"""Live build-loop acceptance — the capstone proof for the Track-A fix.

Reads a build conversation's events straight from `disco.db` and asserts the
§10.1 metric table on the FRESH (post-fix) run. Pair with a real re-run of
`build a simple macosx clone` (+ the no-monolith steer) on the merged engine:

    python3 harness/live_build_acceptance.py [conversation_id]

With no id, it picks the most recent build/agent conversation that issued a
plan + file_writes (a real build, not a trivial chat). Exit 0 ⇒ the loop met
every target (file_read < 20, max reads/path ≤ 3, re-read thoughts < 10,
browser 30s timeouts == 0). Exit 1 ⇒ it printed exactly which targets failed.

Validate-the-instrument: run it against the GOLDEN (pre-fix) conversation
`conv_6483d49d30f045699016b2946ca34523` and it must REPORT VIOLATIONS that match
the documented baseline (file_read 82, max 20, timeouts 4) — proving the DB
extractor agrees with the text-trace extractor before we trust it on a new run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_loop_metrics import (  # noqa: E402
    compute_build_metrics_from_events,
    target_violations,
)

_DB = Path(__file__).resolve().parents[1] / "disco.db"


def load_events(cid: str) -> list[tuple[str, dict]]:
    db = sqlite3.connect(str(_DB))
    try:
        rows = db.execute(
            "SELECT kind, payload FROM events WHERE conversation_id=? ORDER BY seq",
            (cid,),
        ).fetchall()
    finally:
        db.close()
    return [(k, json.loads(p)) for k, p in rows]


def terminal_status(events: list[tuple[str, dict]]) -> str | None:
    for kind, p in reversed(events):
        if kind == "status":
            return str(p.get("status") or p.get("detail") or "")
    return None


def find_latest_build(db_path: Path) -> str | None:
    """Most recent conversation that has BOTH a plan and ≥1 file_write — i.e. a
    real build run, not a trivial chat."""
    db = sqlite3.connect(str(db_path))
    try:
        cids = [
            r[0]
            for r in db.execute(
                "SELECT conversation_id, MAX(seq) FROM events GROUP BY conversation_id "
                "ORDER BY MAX(created_at) DESC"
            ).fetchall()
        ]
        for cid in cids:
            rows = db.execute(
                "SELECT kind, payload FROM events WHERE conversation_id=? ORDER BY seq",
                (cid,),
            ).fetchall()
            has_plan = any(k == "plan" for k, _ in rows)
            has_write = any(
                k == "action"
                and (json.loads(p).get("tool_call") or {}).get("tool_name") == "file_write"
                for k, p in rows
            )
            if has_plan and has_write:
                return cid
    finally:
        db.close()
    return None


def main() -> int:
    cid = sys.argv[1] if len(sys.argv) > 1 else find_latest_build(_DB)
    if not cid:
        print("no build conversation found in disco.db")
        return 2
    events = load_events(cid)
    m = compute_build_metrics_from_events(events)
    status = terminal_status(events)
    print(f"conversation: {cid}")
    print(f"  {m.as_row()}  total_events={len(events)}  terminal={status}")
    top = sorted(m.reads_by_path.items(), key=lambda kv: -kv[1])[:5]
    print(f"  top re-read paths: {top}")
    violations = target_violations(m)
    if violations:
        print("  RESULT: ✗ VIOLATES §10.1 targets:")
        for v in violations:
            print(f"    - {v}")
        return 1
    print("  RESULT: ✓ meets every §10.1 target (healthy build loop)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
