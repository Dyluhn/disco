"""BP-09 behavioral rung — LIVE driver installs a real dependency and serves with it.

Order acceptance #3: prompt a build that genuinely needs a dependency ("build an
express server on :8000 that returns JSON; install express first"), gVisor backend,
open egress. The event log must show:

  1. an install actually RUNNING in a shell session (npm/pnpm add/install express
     in a shell_exec action);
  2. a shell_view on that session (the prompt bullet's watch-the-install
     behavior — no assuming it finished);
  3. a successful local browser/curl observation whose content is the served
     JSON (the app works, on :8000);
  4. the run FINISHED.

Plan approval is WS-only: on NEEDS ATTENTION: AWAITING_PLAN_APPROVAL, approve with
  .venv/bin/python test-record/bp-06/send_frame.py <cid> approve_plan

Evidence: events-<cid>.json + install-witness.json next to this file.
Exit: 0 PASS, 1 pipeline error, 2 FAIL.
"""

from __future__ import annotations

import json
import re
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

PROMPT = (
    "Build a small Express server that listens on port 8000 and returns JSON "
    '{"service": "bp09-status", "ok": true} at the root path. Install express '
    "first — it is not preinstalled. Make sure the server actually responds, "
    "then finish."
)

INSTALL_RE = re.compile(r"\b(npm|pnpm)\b.{0,40}\b(install|add|i)\b.{0,40}express", re.IGNORECASE)


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
        {"surface": "build", "title": "BP-09 behavioral run: install express + serve JSON"},
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

    # 1. the install command in a shell session.
    installs = []
    install_sessions: set[str] = set()
    for e in events:
        if e.get("kind") != "action":
            continue
        tc = e.get("tool_call") or {}
        if tc.get("tool_name") != "shell_exec":
            continue
        args = tc.get("arguments") or {}
        if INSTALL_RE.search(str(args.get("command") or "")):
            installs.append(e["seq"])
            if args.get("session"):
                install_sessions.add(str(args["session"]))

    # 2. The install's completion must be CONFIRMED, not assumed. Two honest paths:
    #    (a) shell_view on the install session at/after the install — the order's
    #        literal wording, written for installs that outlast the runner's 15s
    #        synchronous window and come back "still running";
    #    (b) the install's shell_exec was SYNCHRONOUS: its own observation carries
    #        exit 0 + npm's completion output ("added N packages"). express installs
    #        in ~5s, inside the sync window, so there is no running session left to
    #        view — the exec result itself is a STRONGER completion proof than a
    #        shell_view glance. Requiring (a) here would test npm's speed, not the
    #        driver. Deviation recorded in the witness.
    views = [
        e["seq"] for e in events
        if e.get("kind") == "action"
        and (e.get("tool_call") or {}).get("tool_name") == "shell_view"
        and str(((e.get("tool_call") or {}).get("arguments") or {}).get("session") or "")
        in install_sessions
        and installs and e["seq"] >= installs[0]
    ]
    sync_install_proofs = []
    if installs:
        for e in events:
            if (
                e.get("kind") == "observation"
                and e["seq"] == installs[0] + 1
                and (e.get("tool_result") or {}).get("success")
            ):
                content = (e.get("tool_result") or {}).get("content") or ""
                if "exit 0" in content and re.search(r"added \d+ package", content):
                    sync_install_proofs.append(e["seq"])

    # 3. the served JSON observed from a local URL (browser or curl observation).
    served = [
        e["seq"] for e in events
        if e.get("kind") == "observation"
        and (e.get("tool_result") or {}).get("success")
        and "bp09-status" in ((e.get("tool_result") or {}).get("content") or "")
    ]

    witness = {
        "conversation": cid,
        "final_status": status,
        "install_action_seqs": installs,
        "install_sessions": sorted(install_sessions),
        "shell_view_seqs": views,
        "sync_install_proof_seqs": sync_install_proofs,
        "deviation": (
            "order says 'shell_view on it'; a synchronous exit-0 install observation "
            "with npm completion output is accepted as the equivalent (stronger) "
            "completion proof — express finishes inside the runner's 15s sync window"
            if sync_install_proofs and not views
            else None
        ),
        "served_json_observation_seqs": served,
    }
    (HERE / "install-witness.json").write_text(json.dumps(witness, indent=2))
    print(
        f"telemetry: installs={len(installs)} views={len(views)} "
        f"sync_proofs={len(sync_install_proofs)} served={len(served)}",
        flush=True,
    )

    # ---- assertions ------------------------------------------------------------
    failures: list[str] = []
    if status != "FINISHED":
        failures.append(f"run did not FINISH (final status={status})")
    if not installs:
        failures.append("no shell_exec action installs express (npm/pnpm)")
    if not views and not sync_install_proofs:
        failures.append(
            "install completion neither watched (shell_view on the install session) "
            "nor proven synchronous (exit-0 observation with npm completion output) — "
            "the don't-assume-it-finished behavior is not shown"
        )
    if not served:
        failures.append(
            "no successful observation carries the served JSON ('bp09-status') — "
            "the app was never verified working"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print("BEHAVIORAL RUN PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
