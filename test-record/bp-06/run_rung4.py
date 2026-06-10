"""BP-06 rung 4 — LIVE EE-Quest v1→v2 re-run against the real agent-server.

Drives the real stack (loopback :8000, gVisor backend, local 27B driver) with the
VERBATIM prompts captured from the original EE-Quest cassette
(eequest-prompts.json, conv_7820b3ef): v1 (build the app), then v2 (iterate:
3rd game + initials leaderboard) in the SAME conversation — the phase that
historically blew the context window.

Acceptance asserted afterwards (BP-06 order, rung 4):
  * conversation reaches FINISHED after BOTH phases;
  * ZERO ErrorEvent(code="context_window")  — the hard-reset-made-no-progress marker;
  * ZERO CondensationEvent of any kind      — NOTE: a *successful* hard reset emits a
    tombstone whose reason field is "tokens" (view.py:494 — the "hard_reset" literal
    exists in the union but nothing stamps it), so the only sound mechanical assertion
    is "no tombstone at all". With BP-06 masking loaded, the view must stay under the
    soft bound for a run of this size; ANY tombstone here means masking failed rung 4.

Saves the full event log to events-<cid>.json next to this file.
Exit codes: 0 PASS, 1 pipeline error, 2 assertion FAIL.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
HERE = Path(__file__).parent
PHASE_TIMEOUT_S = 50 * 60  # generous: v1 took ~20 min historically
POLL_S = 15

# States that mean "blocked on a human" — surfaced loudly (the monitor picks the
# line up) but NOT treated as terminal: an out-of-band approve/steer resumes the
# run and this driver keeps waiting.
ATTENTION = {
    "WAITING_FOR_CONFIRMATION",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_DECISION",
    "AWAITING_USER_QUESTION",
    "STUCK",
    "PAUSED",
}


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.load(r)


def fetch_events(cid: str) -> list[dict]:
    events: list[dict] = []
    after: int | None = None
    while True:
        url = f"/conversations/{cid}/events?limit=100"
        if after is not None:
            url += f"&after_seq={after}"
        batch = _get(url).get("events") or []
        if not batch:
            return events
        events.extend(batch)
        after = batch[-1].get("seq")


def wait_finished(cid: str, phase: str) -> str:
    """Poll until FINISHED/ERROR. Attention states are reported, not terminal."""
    t0 = time.time()
    last = None
    while time.time() - t0 < PHASE_TIMEOUT_S:
        try:
            status = _get(f"/conversations/{cid}/state").get("execution_status")
        except Exception as e:  # noqa: BLE001 — transient poll failure must not kill the run
            print(f"[{phase}] poll error (transient): {e}", flush=True)
            time.sleep(POLL_S)
            continue
        if status != last:
            print(f"[{phase}] status={status} t+{int(time.time() - t0)}s", flush=True)
            if status in ATTENTION:
                print(f"[{phase}] NEEDS ATTENTION: {status} — intervene via UI/API", flush=True)
            last = status
        if status in ("FINISHED", "ERROR"):
            return status
        time.sleep(POLL_S)
    print(f"[{phase}] TIMEOUT after {PHASE_TIMEOUT_S}s (last status={last})", flush=True)
    return f"TIMEOUT:{last}"


def main() -> int:
    prompts = json.loads((HERE / "eequest-prompts.json").read_text())

    conv = _post(
        "/conversations",
        {"surface": "build", "title": "BP-06 rung4: EE-Quest live re-run (v1→v2)"},
    )
    cid = conv["conversation_id"]
    print(f"conversation: {cid}", flush=True)

    boundaries: dict[str, int] = {}
    for phase in ("v1", "v2"):
        sent = _post(f"/conversations/{cid}/messages", {"content": prompts[phase]["prompt"]})
        boundaries[phase] = sent["seq"]
        print(f"[{phase}] prompt sent (seq {sent['seq']})", flush=True)
        status = wait_finished(cid, phase)
        if status != "FINISHED":
            print(f"[{phase}] RUN DID NOT FINISH: {status}", flush=True)
            # still save what we have for the post-mortem
            (HERE / f"events-{cid}.json").write_text(json.dumps(fetch_events(cid), indent=2))
            return 2

    events = fetch_events(cid)
    out = HERE / f"events-{cid}.json"
    out.write_text(json.dumps(events, indent=2))
    print(f"saved {len(events)} events -> {out}", flush=True)

    # ---- assertions ----------------------------------------------------------
    failures: list[str] = []
    ctx_errors = [
        e for e in events if e.get("kind") == "error" and e.get("code") == "context_window"
    ]
    if ctx_errors:
        failures.append(
            f"{len(ctx_errors)} ErrorEvent(code=context_window) at seq "
            f"{[e.get('seq') for e in ctx_errors]}"
        )
    tombstones = [e for e in events if e.get("kind") == "condensation"]
    if tombstones:
        failures.append(
            "CondensationEvent present (any tombstone = masking failed to keep the view "
            "under the bound; a successful hard reset is indistinguishable by reason): "
            + json.dumps(
                [
                    {
                        "seq": t.get("seq"),
                        "reason": t.get("reason"),
                        "span": [t.get("forgotten_start_seq"), t.get("forgotten_end_seq")],
                    }
                    for t in tombstones
                ]
            )
        )

    actions = [e for e in events if e.get("kind") == "action"]
    print(
        f"summary: {len(events)} events, {len(actions)} actions; "
        f"v1 from seq {boundaries['v1']}, v2 from seq {boundaries['v2']}; "
        f"tombstones={len(tombstones)}, context_window_errors={len(ctx_errors)}",
        flush=True,
    )
    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print("RUNG4 PASS: v1→v2 FINISHED with no hard reset and no condensation", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
