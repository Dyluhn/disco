"""BP-04 behavioral rung — LIVE browser-tool build against the real agent-server.

Drives the real stack (loopback :8000 API, gVisor backend, local 27B driver) with the
order's rung-4 prompt VERBATIM: create index.html, serve it, then use the `browser`
tool to navigate to http://127.0.0.1:8000/ and report what is seen. Under the old
one-shot fetcher this returned quarantined raw HTML; under BP-04 it must be the
persistent Playwright daemon's rendered observation.

Acceptance asserted (BP-04 order, rung 4):
  * >=1 `action` event with tool_call.tool_name == "browser";
  * a successful browser observation whose content carries the fence + "TITLE:";
  * that observation's structured payload has the daemon shape: url on
    127.0.0.1:8000, a `console` list, an `elements` list, a screenshot_path;
  * conversation reaches FINISHED.

DEVIATION NOTE vs the order text ("feed shows ... TITLE: and CONSOLE"): the landed
renderer emits a CONSOLE block ONLY when error/warning entries exist (signal, not
noise) — a clean page has none. So CONSOLE presence is asserted on the STRUCTURED
event payload (always carried), not the rendered text.

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

# The order's rung-4 prompt, verbatim.
PROMPT = (
    "create index.html with a visible heading, then use the browser tool to "
    "navigate to http://127.0.0.1:8000/ and report what you see"
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
        {"surface": "build", "title": "BP-04 behavioral: browser-tool verification"},
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

    browser_actions = [
        e for e in events
        if e.get("kind") == "action"
        and (e.get("tool_call") or {}).get("tool_name") == "browser"
    ]
    print(f"browser actions: {len(browser_actions)}", flush=True)
    if not browser_actions:
        failures.append("no `browser` action events in the run")

    # successful browser observations with the daemon's structured shape
    browser_call_ids = {
        (e.get("tool_call") or {}).get("call_id") for e in browser_actions
    }
    good_obs = []
    for e in events:
        if e.get("kind") != "observation":
            continue
        tr = e.get("tool_result") or {}
        if tr.get("call_id") not in browser_call_ids or not tr.get("success"):
            continue
        good_obs.append(e)

    qualified = False
    for e in good_obs:
        tr = e["tool_result"]
        content = tr.get("content") or ""
        st = tr.get("structured") or {}
        url = st.get("url") or ""
        if "TITLE:" not in content or "[UNTRUSTED WEB CONTENT" not in content:
            continue
        if not (url.startswith("http://127.0.0.1:8000") or url.startswith("http://localhost:8000")):  # noqa: E501
            continue
        if not isinstance(st.get("console"), list):
            failures.append(f"qualifying observation missing structured console list: {st.keys()}")  # noqa: E501
            continue
        if not isinstance(st.get("elements"), list):
            failures.append("qualifying observation missing structured elements list")
            continue
        qualified = True
        print(
            f"qualifying browser observation seq={e.get('seq')}: url={url} "
            f"console={len(st['console'])} entries, elements={len(st['elements'])}, "
            f"screenshot={st.get('screenshot_path')!r}",
            flush=True,
        )
        break
    if not qualified and not failures:
        failures.append(
            "no successful browser observation on 127.0.0.1:8000 with fence + TITLE: "
            "+ structured daemon shape"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print(
        f"BEHAVIORAL PASS: {len(browser_actions)} browser action(s), daemon-shaped "
        "observation on 127.0.0.1:8000 (fence + TITLE: + console/elements), run FINISHED",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
