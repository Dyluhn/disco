"""BP-08 behavioral rung — LIVE cross-cell-state build against the real agent-server.

Drives the real stack (loopback :8000 API, gVisor backend, local 27B driver) with a
data-analysis prompt engineered to REQUIRE the persistent kernel: step 1 loads a CSV
into memory, later steps analyze the IN-MEMORY variables without re-reading the file.
Under the old pickle runner this worked only by accident of serialization; under
BP-08 it is kernel RAM. Stdlib-only on purpose — the sandbox image has no pandas.

Acceptance asserted (BP-08 order, rung 4):
  * >=3 `action` events with tool_call.tool_name == "code_exec";
  * NO code_exec observation contains "NameError" (the state-loss signature) and no
    agent_error mentions one;
  * the deterministic check value (sum 1..10 = 55) appears in the event log — the
    analysis actually used the loaded data;
  * conversation reaches FINISHED.

Plan approval is WS-only: when this prints NEEDS ATTENTION: AWAITING_PLAN_APPROVAL,
approve out-of-band with  .venv/bin/python test-record/bp-06/send_frame.py <cid> approve_plan

Evidence saved next to this file: events-<cid>.json + behavioral verdict on stdout.
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
RUN_TIMEOUT_S = 50 * 60
POLL_S = 15

ATTENTION = {
    "WAITING_FOR_CONFIRMATION",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_DECISION",
    "AWAITING_USER_QUESTION",
    "STUCK",
    "PAUSED",
}

PROMPT = """Do a small data analysis in this workspace using python code execution
(code_exec) in SEPARATE steps. Python standard library only — do NOT import pandas
or numpy (they are not installed). It is essential that each numbered step below is
its OWN separate code execution, and that later steps use the variables already in
memory from earlier steps — do NOT re-read the file or redefine earlier variables.

1. In one python cell: write a file sales.csv with header `day,amount` and exactly
   ten data rows: day 1..10 with amount equal to the day number (1,2,...,10). Then
   load it back with the csv module into a list-of-dicts variable named `rows`
   (convert amount to int) and print how many rows were loaded.
2. In a SECOND python cell (using the `rows` variable from step 1, without re-reading
   the csv): compute `total` = sum of amount and `avg` = total / number of rows, and
   print them like: TOTAL=<total> AVG=<avg>
3. In a THIRD python cell (using `total` and `avg` from step 2): write a file
   summary.txt containing the line `total=<total> avg=<avg>` and print the file's
   contents after writing it.
4. Verify summary.txt exists in the workspace and finish.
"""


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


def main() -> int:
    conv = _post(
        "/conversations",
        {"surface": "build", "title": "BP-08 behavioral: cross-cell-state analysis"},
    )
    cid = conv["conversation_id"]
    print(f"conversation: {cid}", flush=True)
    print(f"approve gate if needed:  .venv/bin/python test-record/bp-06/send_frame.py {cid} approve_plan", flush=True)  # noqa: E501

    sent = _post(f"/conversations/{cid}/messages", {"content": PROMPT})
    print(f"prompt sent (seq {sent['seq']})", flush=True)

    last = None
    t0 = time.time()
    status = "?"
    while time.time() - t0 < RUN_TIMEOUT_S:
        try:
            status = _get(f"/conversations/{cid}/state").get("execution_status")
        except Exception as e:  # noqa: BLE001 — transient poll failure must not kill the run
            print(f"poll error (transient): {e}", flush=True)
            time.sleep(POLL_S)
            continue
        if status != last:
            print(f"status={status} t+{int(time.time() - t0)}s", flush=True)
            if status in ATTENTION:
                print(f"NEEDS ATTENTION: {status} — intervene via UI/API", flush=True)
            last = status
        if status in ("FINISHED", "ERROR"):
            break
        time.sleep(POLL_S)
    else:
        print(f"TIMEOUT after {RUN_TIMEOUT_S}s (last status={last})", flush=True)

    # ---- evidence (saved even on failure, for the post-mortem) -----------------
    events = fetch_events(cid)
    (HERE / f"events-{cid}.json").write_text(json.dumps(events, indent=2))
    print(f"saved {len(events)} events", flush=True)

    # ---- assertions ------------------------------------------------------------
    failures: list[str] = []
    if status != "FINISHED":
        failures.append(f"run did not FINISH (final status={status})")

    code_execs = [
        e for e in events
        if e.get("kind") == "action"
        and (e.get("tool_call") or {}).get("tool_name") == "code_exec"
    ]
    print(f"code_exec actions: {len(code_execs)}", flush=True)
    if len(code_execs) < 3:
        failures.append(f"expected >=3 code_exec actions, got {len(code_execs)}")

    blob = json.dumps(events)
    if "NameError" in blob:
        failures.append("state-loss signature: a NameError appears in the event log")
    if "TOTAL=55" not in blob and "total=55" not in blob:
        failures.append("deterministic check value 55 (sum 1..10) not found in event log")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print(
        f"BEHAVIORAL PASS: {len(code_execs)} code_exec cells, state carried across "
        "cells (no NameError), total=55 computed from in-memory rows, run FINISHED",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
