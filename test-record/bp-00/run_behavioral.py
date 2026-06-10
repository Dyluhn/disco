"""BP-00 behavioral rung — LIVE vision driver catches a seeded VISUAL defect.

Order acceptance #3: a build whose task prompt seeds white-text-on-white-background
via "starter CSS" (CSS variables, both #ffffff). Console is CLEAN in this scenario —
the ONLY way to notice the page is broken is to LOOK at the screenshot. The event
log must show:

  1. >=1 successful browser observation carrying `screenshot_b64` (V3 transport on,
     PMX_DRIVER_VISION=1 in the agent-server env);
  2. ZERO console errors across all qualifying browser observations (defeats any
     console-derived explanation);
  3. an agent thought AFTER the first screenshot-bearing observation that references
     the VISUAL defect (white/invisible/blank/contrast/can't-see...), i.e. the
     identification came from the image;
  4. a fixing action after that thought (arguments touch the color/CSS), and the
     run FINISHED.

Pre-flight (manual, before running):
  - workstation driver on `switch-model.sh 27b-vision` (V2 gate PASSED);
  - agent-server respawned WITH PMX_DRIVER_VISION=1 (tmux respawn-pane -k — never
    C-c the pane).

Plan approval is WS-only: on NEEDS ATTENTION: AWAITING_PLAN_APPROVAL, approve with
  .venv/bin/python test-record/bp-06/send_frame.py <cid> approve_plan

Evidence: events-<cid>.json + vision-witness.json next to this file.
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

# The seed: starter CSS with fg==bg==white behind variables. The prompt asks the
# agent to keep the starter sheet unless checking the page reveals a problem — it
# must browse, SEE the blank-looking page, and fix the contrast.
PROMPT = (
    "Create a small single-page status site: index.html with the heading "
    "'Build Status: GREEN' and a short paragraph, plus style.css. Start style.css "
    "from this starter sheet verbatim:\n\n"
    ":root { --fg: #ffffff; --bg: #ffffff; }\n"
    "body { background: var(--bg); color: var(--fg); font-family: sans-serif; "
    "margin: 2rem; }\n\n"
    "Keep the starter sheet as-is unless checking the running page shows a problem. "
    "Serve the site, make sure it looks right to a user, then finish."
)

# Visual-defect vocabulary the identifying thought must hit (case-insensitive).
VISUAL_RE = re.compile(
    r"white.{0,40}(text|background)|invisible|unreadable|blank|"
    r"(can.?t|cannot|hard to) (see|read)|contrast|same colou?r|not visible|"
    r"text.{0,30}(does ?n.?t|not) (show|appear|visible)",
    re.IGNORECASE,
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


def browser_obs(events: list[dict]) -> list[tuple[int, dict]]:
    """(seq, tool_result) for successful browser observations on a local URL."""
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
        if url.startswith("http://127.0.0.1") or url.startswith("http://localhost"):
            out.append((e.get("seq"), tr))
    return out


def main() -> int:
    conv = _post(
        "/conversations",
        {"surface": "build", "title": "BP-00 behavioral run: vision catches white-on-white"},
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

    obs = browser_obs(events)
    shot_obs = [
        (seq, tr) for seq, tr in obs
        if (tr.get("structured") or {}).get("screenshot_b64")
    ]
    err_obs = [
        (seq, tr) for seq, tr in obs
        if any(
            c.get("level") == "error"
            for c in (tr.get("structured") or {}).get("console") or []
        )
    ]
    first_shot_seq = shot_obs[0][0] if shot_obs else None

    # thoughts after the first screenshot-bearing observation
    visual_thoughts = []
    fixing_actions_after = []
    for e in events:
        if e.get("kind") != "action" or first_shot_seq is None:
            continue
        if (e.get("seq") or 0) <= first_shot_seq:
            continue
        thought = e.get("thought") or ""
        if VISUAL_RE.search(thought):
            visual_thoughts.append({"seq": e["seq"], "thought": thought})
        args = json.dumps((e.get("tool_call") or {}).get("arguments") or {})
        if re.search(r"--fg|color|style\.css", args, re.IGNORECASE):
            fixing_actions_after.append(e["seq"])

    witness = {
        "conversation": cid,
        "final_status": status,
        "browser_obs": len(obs),
        "obs_with_screenshot_b64": len(shot_obs),
        "obs_with_console_errors": len(err_obs),
        "first_screenshot_seq": first_shot_seq,
        "visual_defect_thoughts": visual_thoughts,
        "fixing_action_seqs": fixing_actions_after,
    }
    (HERE / "vision-witness.json").write_text(json.dumps(witness, indent=2))
    print(
        f"telemetry: obs={len(obs)} with_b64={len(shot_obs)} console_err={len(err_obs)} "
        f"visual_thoughts={len(visual_thoughts)} fixing_actions={len(fixing_actions_after)}",
        flush=True,
    )

    # ---- assertions ------------------------------------------------------------
    failures: list[str] = []
    if status != "FINISHED":
        failures.append(f"run did not FINISH (final status={status})")
    if not shot_obs:
        failures.append("no browser observation carried screenshot_b64 — vision transport off?")
    if err_obs:
        failures.append(
            f"{len(err_obs)} observation(s) had console errors — scenario demands a clean "
            "console; identification can't be attributed to the screenshot"
        )
    if not visual_thoughts:
        failures.append(
            "no agent thought after the first screenshot references the visual defect "
            "(white/invisible/contrast...) — identification-from-screenshot not shown"
        )
    # >= not >: an ActionEvent carries thought AND tool_call in ONE event, so the
    # identifying thought and the fixing action legitimately share a seq when the
    # agent identifies-and-fixes in a single step (run #4 did exactly this at seq
    # 23). The thought textually precedes its own tool call, so >= is not a
    # weakening of the ordering claim.
    if visual_thoughts and not any(
        seq >= visual_thoughts[0]["seq"] for seq in fixing_actions_after
    ):
        failures.append("no fixing action (color/style.css) at/after the identifying thought")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print("BEHAVIORAL RUN PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
