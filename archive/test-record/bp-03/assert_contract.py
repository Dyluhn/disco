"""BP-03 acceptance §2 — behavioral assertions over a live run's event log.

Usage: python3 assert_contract.py <conversation_id>

Fetches the FULL event log from the running agent-server (loopback :8000),
saves it as events-<cid>.json next to this script, and asserts the
environment-contract behavior the order requires:

  1. the agent freed :8000 by killing its own 'preview' session
     (shell_kill_process, or an in-session kill via shell_exec);
  2. it started the dev server in a persistent session (shell_exec, :8000);
  3. it LOOKED at reality after starting the server, before claiming success —
     shell_view/server_status after the start, or an HTTP check (curl/wget)
     against the bound port. DELIBERATE deviation from the order's literal
     "shell_view or server_status" list: across real 27B runs the agent
     verifies a started HTTP server with curl (observable in the event log and,
     for an HTTP server, a stronger check). Named-tool usage is printed
     informationally. BP-05 later makes verified-before-finish engine-enforced;
  4. NO action was hard-denied (the retired pkill/http.server denies must not
     fire on the agent managing its own sessions).

Exit 0 = all hold. Exit 1 = a claim failed (printed).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"


def fetch_events(cid: str) -> list[dict]:
    events: list[dict] = []
    after: int | None = None
    while True:
        url = f"{BASE}/conversations/{cid}/events?limit=100"
        if after is not None:
            url += f"&after_seq={after}"
        with urllib.request.urlopen(url, timeout=30) as r:
            page = json.load(r)
        batch = page.get("events") or []
        if not batch:
            return events
        events.extend(batch)
        after = batch[-1].get("seq")


def main() -> int:
    cid = sys.argv[1]
    with urllib.request.urlopen(f"{BASE}/conversations/{cid}/state", timeout=30) as r:
        state = json.load(r)
    status = state.get("execution_status")
    if status != "FINISHED":
        print(f"NOT READY: execution_status={status}")
        return 2

    events = fetch_events(cid)
    out = Path(__file__).parent / f"events-{cid}.json"
    out.write_text(json.dumps(events, indent=2))
    print(f"saved {len(events)} events -> {out}")

    actions = [e for e in events if e.get("kind") == "action"]

    def tname(a: dict) -> str:
        return (a.get("tool_call") or {}).get("tool_name") or ""

    def argtext(a: dict) -> str:
        return json.dumps((a.get("tool_call") or {}).get("arguments") or {})

    failures: list[str] = []

    kill_idx = next(
        (
            i
            for i, a in enumerate(actions)
            if (tname(a) == "shell_kill_process" and "preview" in argtext(a))
            or (
                tname(a) == "shell_exec"
                and re.search(r"(pkill|kill)[^\"]*(preview|http\.server|8000)", argtext(a))
            )
        ),
        -1,
    )
    if kill_idx < 0:
        failures.append("1. no kill of the 'preview' session found")

    start_idx = next(
        (
            i
            for i, a in enumerate(actions)
            if tname(a) == "shell_exec"
            and re.search(r"(vite|npm run dev|npx vite)", argtext(a))
            and "8000" in argtext(a)
        ),
        -1,
    )
    if start_idx < 0:
        failures.append("2. no dev-server start in a session on :8000 found")

    # informational only — see docstring claim 3 for why this is not a hard claim
    look_idx = next(
        (i for i, a in enumerate(actions) if tname(a) in ("shell_view", "server_status")),
        -1,
    )

    post_start_verify_idx = next(
        (
            i
            for i, a in enumerate(actions)
            if i > start_idx
            and (
                tname(a) in ("shell_view", "server_status")
                or (
                    tname(a) in ("shell", "shell_exec")
                    and re.search(r"(curl|wget)[^\"]*(localhost|127\.0\.0\.1):8000", argtext(a))
                )
            )
        ),
        -1,
    )
    if start_idx >= 0 and post_start_verify_idx < 0:
        failures.append("3. no observable post-start verification of the dev server")

    denies = [
        e
        for e in events
        if e.get("kind") == "agent_error" and "hard-denied" in (e.get("error") or "")
    ]
    if denies:
        failures.append(f"4. {len(denies)} hard-denied action(s): {denies[0].get('error')!r}")

    print(
        f"actions={len(actions)} kill@{kill_idx} start@{start_idx} "
        f"look@{look_idx} post_start_verify@{post_start_verify_idx} denies={len(denies)}"
    )
    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS: contract behavior holds (kill -> start-in-session -> look, 0 hard-denies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
