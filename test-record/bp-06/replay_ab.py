"""BP-06 acceptance §3 — replay A/B on the REAL EE-Quest event log.

The order names "the EE-Quest cassette" under harness/; the only cassette there is
the 4-event loop_demo stub (no observations — nothing maskable). The faithful,
non-synthetic substitute is the real recorded EE-Quest build in test-record/pmx-run.db
(conv_7820b3..., 300 events / 112 observations, the RECORD.md prompt verbatim) —
captured live on the gVisor backend, zero fabricated content.

Method: replay the event-log prefix step by step (a step = one ActionEvent, matching
the driver's plan→act→observe cadence). At each step, materialize View.of(prefix)
twice — masking ON (shipped constants) vs OFF (window forced huge) — and estimate
prompt tokens with the engine's own heuristic (AgentLoop._estimate_tokens). Report
per-step tokens and the cumulative reduction by step 40 (required: ≥30%).
"""

from __future__ import annotations

import json
import sqlite3
import sys

from pydantic import TypeAdapter

import perpleximanus.core.view as view_mod
from perpleximanus.core.events import Event
from perpleximanus.core.loop.engine import AgentLoop
from perpleximanus.core.view import View

DB = "test-record/pmx-run.db"
CID = "conv_7820b3effc204906a88b2c92b21d00f1"

adapter = TypeAdapter(Event)


def load_events() -> list[Event]:
    db = sqlite3.connect(DB)
    rows = db.execute(
        "select payload from events where conversation_id=? order by seq", (CID,)
    ).fetchall()
    events: list[Event] = []
    for (payload,) in rows:
        try:
            events.append(adapter.validate_python(json.loads(payload)))
        except Exception as exc:  # noqa: BLE001 — skip non-core/unknown kinds, report
            print(f"  (skipped 1 event: {str(exc).splitlines()[0][:100]})", file=sys.stderr)
    return events


def estimate(events: list[Event]) -> int:
    return AgentLoop._estimate_tokens(View.of(events))


def main() -> None:
    events = load_events()
    action_idx = [i for i, e in enumerate(events) if type(e).__name__ == "ActionEvent"]
    print(f"events={len(events)} steps(actions)={len(action_idx)}")

    masked_cum = 0
    unmasked_cum = 0
    rows = []
    for step, idx in enumerate(action_idx, start=1):
        prefix = events[: idx + 1]
        t_masked = estimate(prefix)
        # masking OFF: force every observation into the "recent" window
        orig = view_mod._MASK_KEEP_RECENT
        view_mod._MASK_KEEP_RECENT = 10**9
        try:
            t_unmasked = estimate(prefix)
        finally:
            view_mod._MASK_KEEP_RECENT = orig
        masked_cum += t_masked
        unmasked_cum += t_unmasked
        rows.append((step, t_unmasked, t_masked))
        if step == 40:
            break

    print(f"\n| step | unmasked tok | masked tok | step Δ | cum unmasked | cum masked |")
    print("|-----:|------------:|-----------:|-------:|-------------:|-----------:|")
    cu = cm = 0
    for step, tu, tm in rows:
        cu += tu
        cm += tm
        if step % 5 == 0 or step == 1 or step == len(rows):
            d = (1 - tm / tu) * 100 if tu else 0.0
            print(f"| {step} | {tu:,} | {tm:,} | {d:.1f}% | {cu:,} | {cm:,} |")

    reduction = (1 - masked_cum / unmasked_cum) * 100 if unmasked_cum else 0.0
    print(
        f"\ncumulative by step {len(rows)}: unmasked={unmasked_cum:,} "
        f"masked={masked_cum:,} reduction={reduction:.1f}%"
    )
    print("PASS (>=30%)" if reduction >= 30.0 else "FAIL (<30%)")
    sys.exit(0 if reduction >= 30.0 else 1)


if __name__ == "__main__":
    main()
