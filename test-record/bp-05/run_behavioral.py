"""BP-05 behavioral rung — LIVE browser-verify finish gate against the real agent-server.

Two runs (order acceptance #2), selected by argv[1]:

  A — the task prompt SEEDS a deliberate console.error and forbids removing it.
      The gate must FIRE: the log shows >=1 refusal nudge; the run ends either via
      the 3-refusal ⚠ release valve or (if the agent disobeyed the seed and fixed
      the error) a clean verification. Either way: gate fired, run FINISHED.
      If any browser observation carried the seeded error, at least one nudge must
      QUOTE it ("The last load had errors: ...").

  B — clean app. The prompt bullet (prompts.py, BP-05) should drive the agent to
      browse BEFORE finishing: assert a clean qualifying observation, ZERO refusal
      nudges, zero ⚠, run FINISHED.

Plan approval is WS-only: when this prints NEEDS ATTENTION: AWAITING_PLAN_APPROVAL,
approve out-of-band with  .venv/bin/python test-record/bp-06/send_frame.py <cid> approve_plan

Evidence: events-<cid>.json next to this file. Exit: 0 PASS, 1 pipeline error, 2 FAIL.
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

NUDGE_PREFIX = "Before finishing: verify your app the way a user would."
QUOTE_MARK = "The last load had errors:"
VALVE_PREFIX = "⚠ finished WITHOUT a clean browser verification"

PROMPTS = {
    # Order: "seed it via the task prompt: 'the page must call console.error(...) on load'"
    "A": (
        "create index.html with a visible heading. The page must call "
        "console.error('BP05-SEEDED-ERROR') in a script tag on load — this error is an "
        "intentional, required part of the spec and must NOT be removed. Serve the page "
        "and finish."
    ),
    "B": (
        "create index.html with a visible heading, serve it, and finish when it works"
    ),
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


def env_messages(events: list[dict]) -> list[str]:
    out = []
    for e in events:
        if e.get("kind") != "message":
            continue
        if e.get("source") != "environment":
            continue
        msg = e.get("message") or {}
        out.append(msg.get("content") or "")
    return out


def browser_obs(events: list[dict]) -> list[dict]:
    """Successful browser observations (tool_result dicts) on :8000."""
    call_ids = {
        (e.get("tool_call") or {}).get("call_id")
        for e in events
        if e.get("kind") == "action"
        and (e.get("tool_call") or {}).get("tool_name") == "browser"
    }
    out = []
    for e in events:
        if e.get("kind") != "observation":
            continue
        tr = e.get("tool_result") or {}
        if tr.get("call_id") not in call_ids or not tr.get("success"):
            continue
        url = (tr.get("structured") or {}).get("url") or ""
        if url.startswith("http://127.0.0.1:8000") or url.startswith("http://localhost:8000"):
            out.append(tr)
    return out


def main() -> int:
    run = (sys.argv[1] if len(sys.argv) > 1 else "").upper()
    if run not in PROMPTS:
        print("usage: run_behavioral.py <A|B>", flush=True)
        return 1

    conv = _post(
        "/conversations",
        {"surface": "build", "title": f"BP-05 behavioral run {run}: browser-verify gate"},
    )
    cid = conv["conversation_id"]
    print(f"conversation: {cid}", flush=True)
    print(f"approve gate if needed:  .venv/bin/python test-record/bp-06/send_frame.py {cid} approve_plan", flush=True)  # noqa: E501

    sent = _post(f"/conversations/{cid}/messages", {"content": PROMPTS[run]})
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

    env = env_messages(events)
    nudges = [m for m in env if m.startswith(NUDGE_PREFIX)]
    quoted = [m for m in nudges if QUOTE_MARK in m]
    valves = [m for m in env if m.startswith(VALVE_PREFIX)]
    obs = browser_obs(events)
    obs_with_err = [
        tr for tr in obs
        if any(
            c.get("level") == "error"
            for c in (tr.get("structured") or {}).get("console") or []
        )
    ]
    clean_obs = [tr for tr in obs if tr not in obs_with_err]
    print(
        f"gate telemetry: nudges={len(nudges)} (quoting={len(quoted)}) valves={len(valves)} "
        f"browser_obs={len(obs)} (with_error={len(obs_with_err)}, clean={len(clean_obs)})",
        flush=True,
    )

    # ---- assertions ------------------------------------------------------------
    failures: list[str] = []
    if status != "FINISHED":
        failures.append(f"run did not FINISH (final status={status})")

    if run == "A":
        if not nudges:
            failures.append("gate never fired: zero refusal nudges in the event log")
        if not valves and not clean_obs:
            failures.append(
                "run ended with neither the ⚠ release valve nor a clean verification"
            )
        if obs_with_err and not quoted:
            failures.append(
                "an erroring observation existed but no nudge quoted the error line"
            )
        if valves and "BP05-SEEDED-ERROR" not in valves[0] and obs_with_err:
            failures.append(f"⚠ valve does not carry the seeded error: {valves[0]!r}")
    else:  # B
        if not obs:
            failures.append("no qualifying browser observation on :8000")
        if not clean_obs:
            failures.append("no CLEAN browser observation (console errors present?)")
        if nudges:
            failures.append(f"clean run was refused {len(nudges)}x — expected zero refusals")
        if valves:
            failures.append("⚠ valve fired on a clean run")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print(f"BEHAVIORAL RUN {run} PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
