"""BP-11 behavioral rung — LIVE driver reads an UPLOADED file and builds from it.

Order acceptance #3: upload a small REAL csv (this one is generated from the BP
campaign's own behavioral-run records — real data, not lorem ipsum) through the
NEW multipart endpoint, then prompt a build that renders it as a table. The event
log must show:

  1. the upload accepted (HTTP 200, nothing rejected) and announced via ONE
     environment MessageEvent ("User uploaded: uploads/… (N,NNN bytes)");
  2. an agent ACTION that file_reads uploads/<name>.csv (the prompt bullet's
     read-before-guessing behavior);
  3. a written page whose content carries REAL cell values from the csv (we
     check two distinct values from different rows — defeats header-only echo);
  4. the run FINISHED.

Plan approval is WS-only: on NEEDS ATTENTION: AWAITING_PLAN_APPROVAL, approve with
  .venv/bin/python test-record/bp-06/send_frame.py <cid> approve_plan

Evidence: events-<cid>.json + upload-witness.json next to this file.
Exit: 0 PASS, 1 pipeline error, 2 FAIL.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
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

# REAL data: this campaign's own bp-00 behavioral-run record (conv ids, observed
# statuses, wall-clock and event counts from test-record/bp-00/). Two cell values
# from different rows are asserted into the built page below.
CSV_NAME = "bp00-behavioral-runs.csv"
CSV_BODY = (
    b"run,conversation,final_status,duration_s,events,screenshot_obs\n"
    b"2,conv_3b9402aa654b4a43b715db10ce7b6550,ERROR,42,29,0\n"
    b"3,conv_bc8596a9ac3f43b484aaa47ad8b83372,FINISHED,375,109,15\n"
    b"4,conv_2927ed545aa347069428766a8514c8fa,FINISHED,105,33,2\n"
    b"5,conv_c5f4f81c22624a2fac545769f6b300e9,FINISHED,135,28,2\n"
)
# Distinct cell values from different rows (not headers, not both from one row).
CELL_A = "conv_bc8596a9ac3f43b484aaa47ad8b83372"
CELL_B = "135"

PROMPT = (
    "I've uploaded a CSV of test-run records. Build a single page that renders the "
    "uploaded CSV as an HTML table — every row and column, real values, no "
    "placeholders. Serve it, check it looks right, then finish."
)


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


def _upload(cid: str, name: str, data: bytes) -> tuple[int, dict]:
    """Multipart POST to the bp-11 endpoint (field name 'files', per the order)."""
    boundary = f"----pmx{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/conversations/{cid}/files",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, json.load(r)


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
        {"surface": "build", "title": "BP-11 behavioral run: build from uploaded csv"},
    )
    cid = conv["conversation_id"]
    print(f"conversation: {cid}", flush=True)
    print(f"approve gate if needed:  .venv/bin/python test-record/bp-06/send_frame.py {cid} approve_plan", flush=True)  # noqa: E501

    http_status, up = _upload(cid, CSV_NAME, CSV_BODY)
    print(f"upload: HTTP {http_status} -> {json.dumps(up)}", flush=True)
    if http_status != 200 or up.get("rejected"):
        print(f"FAIL: upload not cleanly accepted: {up}", flush=True)
        return 2

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

    # 1. the environment announcement (exact format owned by the order: thousands
    #    separators, "User uploaded: uploads/<name> (N,NNN bytes)").
    announce = [
        e for e in events
        if e.get("kind") == "message"
        and e.get("source") == "environment"
        and "User uploaded:" in ((e.get("message") or {}).get("content") or "")
        and f"uploads/{CSV_NAME}" in ((e.get("message") or {}).get("content") or "")
    ]

    # 2. a file_read action on the uploaded path.
    reads = [
        e["seq"] for e in events
        if e.get("kind") == "action"
        and (e.get("tool_call") or {}).get("tool_name") == "file_read"
        and f"uploads/{CSV_NAME}"
        in json.dumps((e.get("tool_call") or {}).get("arguments") or {})
    ]

    # 3. real cell values reach a written page. Look at file-writing actions AND
    #    code cells — the agent may inline the rows in html or generate them.
    written = [
        e["seq"] for e in events
        if e.get("kind") == "action"
        and (e.get("tool_call") or {}).get("tool_name")
        in ("file_write", "file_append", "file_edit", "file_replace_lines",
            "file_insert_lines", "code_exec")
        and CELL_A in json.dumps((e.get("tool_call") or {}).get("arguments") or {})
        and CELL_B in json.dumps((e.get("tool_call") or {}).get("arguments") or {})
    ]
    # Accept generated-at-runtime tables too: any successful observation whose
    # content carries both cells (e.g. browser text extraction of the table).
    observed = [
        e["seq"] for e in events
        if e.get("kind") == "observation"
        and (e.get("tool_result") or {}).get("success")
        and CELL_A in ((e.get("tool_result") or {}).get("content") or "")
        and CELL_B in ((e.get("tool_result") or {}).get("content") or "")
    ]

    witness = {
        "conversation": cid,
        "final_status": status,
        "upload_response": up,
        "announce_seqs": [e["seq"] for e in announce],
        "announce_text": [
            (e.get("message") or {}).get("content") for e in announce
        ],
        "file_read_seqs": reads,
        "cells_in_written_action_seqs": written,
        "cells_in_observation_seqs": observed,
        "cells": [CELL_A, CELL_B],
    }
    (HERE / "upload-witness.json").write_text(json.dumps(witness, indent=2))
    print(
        f"telemetry: announce={len(announce)} reads={len(reads)} "
        f"written={len(written)} observed={len(observed)}",
        flush=True,
    )

    # ---- assertions ------------------------------------------------------------
    failures: list[str] = []
    if status != "FINISHED":
        failures.append(f"run did not FINISH (final status={status})")
    if len(announce) != 1:
        failures.append(
            f"expected exactly ONE environment announcement for uploads/{CSV_NAME}, "
            f"got {len(announce)}"
        )
    if not reads:
        failures.append(f"no file_read action on uploads/{CSV_NAME}")
    if not written and not observed:
        failures.append(
            f"real cell values ({CELL_A!r}, {CELL_B!r}) never appear together in a "
            "written page or a successful observation — table may be placeholder"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print("BEHAVIORAL RUN PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
