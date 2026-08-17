"""BP-16 Phase A — the build, with a mid-run fault.

One continuous live run (27B driver, gvisor on VM-201): real UI creation +
upload + approval; once the agent's server owns a port, the harness SIGKILLs
that process from outside (docker exec — the tmux session survives and shows
the death); then every recovery + deliverable assertion from the order.
"""

from __future__ import annotations

import json
import re
import time

from common import (
    RECORD_DIR,
    SHOT_DIR,
    actions,
    browser_verifications,
    container_for,
    full_events,
    hard_denied,
    kill_port_process,
    obs_content,
    observations,
    port_owners,
    save_state,
    tool_of,
    valve_fired,
)
from drive import (
    answer_gates,
    create_submit_upload_approve,
    deliverable_battery,
    wait_finished_ui,
    wait_ports_live,
)

FAULT_PORTS = (3000, 8000)  # kill whichever the agent brings up first

# A recovery action either names the killed port or re-runs a server-shaped
# command (the later console-clean :8000 verification proves it WORKED).
_SERVER_CMD = (
    r"vite|npm run dev|npm start|http\.server|uvicorn|flask|"
    r"node .*(server|api)|python3? .*(server|api|app)"
)

# The platform's preview static server owns :8000 from container start —
# attempt-5 lesson: killing IT is not a fault the agent ever has to notice.
_PREVIEW_CMD = re.compile(r"http\.server \d+ -d /workspace")
_LAUNCH = re.compile(_SERVER_CMD, re.I)


def _agent_launched_server(events: list[dict]) -> bool:
    """True once the event log shows the AGENT starting a server-shaped
    process (server_start, or shell_exec/shell matching _SERVER_CMD)."""
    for a in actions(events):
        if tool_of(a) == "server_start":
            return True
        if tool_of(a) in ("shell_exec", "shell") and _LAUNCH.search(
            json.dumps((a.get("tool_call") or {}).get("arguments"))
        ):
            return True
    return False


def test_phase_a(firefox):
    page = firefox
    witness: dict = {}

    # ── 1. create + upload + approve through the real UI ─────────────────────
    cid = create_submit_upload_approve(page, shot_prefix="phase-a")
    witness["cid"] = cid
    save_state(phase_a={"cid": cid})
    print(f"\n[phase-a] cid={cid} — approved, build running")

    # ── 2. fault injection: wait for the AGENT's server to own a port ────────
    # Attempt-1 lesson: killing at the FIRST instant of port ownership lands
    # while the launch shell_exec is still the pending action — the death shows
    # up as that command's own exit-137 observation and there is nothing for
    # instruments to discover. A MID-RUN fault needs the launch to settle and
    # the agent to have moved on: port owned ≥ SETTLE_S, AND ≥2 new events
    # since first detection (the launch obs landed + the agent acted again).
    # Attempt-5 lesson: the PLATFORM preview server owns :8000 from container
    # start — a valid target must be a non-preview owner that is demonstrably
    # the agent's (cmdline looks like a server launch, or the event log shows
    # the agent launching one). Killing the preview server tests nothing.
    SETTLE_S = 30
    fault = None
    first_seen = None  # {"port", "seq", "t"} at first valid-target detection
    deadline = time.monotonic() + 1800  # 30 min for the agent to bring a server up
    while time.monotonic() < deadline and fault is None:
        full_sessions = _sessions(cid)
        sessions = [
            s
            for s in full_sessions
            if s.get("name") != "preview" and not str(s.get("name", "")).startswith("__")
        ]
        if sessions:
            container = container_for(cid)
            if container:
                events_now = full_events(cid)
                launched = _agent_launched_server(events_now)
                for port in FAULT_PORTS:
                    owners = {
                        pid: cmd
                        for pid, cmd in port_owners(container, port).items()
                        if not _PREVIEW_CMD.search(cmd)
                    }
                    if not owners:
                        continue
                    if not (launched or any(_LAUNCH.search(c) for c in owners.values())):
                        # something non-preview is listening but nothing ties it
                        # to the agent yet — keep watching, don't kill blind
                        print(f"[phase-a] :{port} owner not provably agent-launched yet: {owners}")
                        continue
                    cur_seq = max((e["seq"] for e in events_now), default=0)
                    if first_seen is None or first_seen["port"] != port:
                        first_seen = {"port": port, "seq": cur_seq, "t": time.monotonic()}
                        print(
                            f"[phase-a] :{port} owned by agent server {owners} "
                            f"(seq {cur_seq}) — settling {SETTLE_S}s"
                        )
                        break
                    settled = time.monotonic() - first_seen["t"] >= SETTLE_S
                    moved_on = cur_seq >= first_seen["seq"] + 2
                    if settled and moved_on:
                        fault = {
                            "port": port,
                            "owners": {str(k): v for k, v in owners.items()},
                            "container": container,
                            "sessions": [s.get("name") for s in full_sessions],
                            "agent_launch_in_log": launched,
                            "pre_seq": cur_seq,
                            "first_seen_seq": first_seen["seq"],
                            "ts": time.time(),
                        }
                    break
        if fault is None:
            answer_gates(page, cid)  # rm -rf-style soft-confirms must not stall the run
            time.sleep(5)
    assert fault, "the agent's own server never owned a fault port (3000/8000)"

    kill_log = kill_port_process(fault["container"], fault["port"])
    assert not _PREVIEW_CMD.search(kill_log), (
        f"fault would have hit the platform preview server, not the agent's: {kill_log!r}"
    )
    fault["kill_log"] = kill_log
    witness["fault"] = fault
    save_state(phase_a=witness)
    print(f"[phase-a] FAULT injected on :{fault['port']} — {kill_log!r}")

    # Terminal evidence: the surviving session shows the death output.
    page.get_by_role("tab", name="Terminal").click(timeout=30_000)
    pane = page.get_by_role("tabpanel").locator("pre")
    death_deadline = time.monotonic() + 180
    death_seen = ""
    while time.monotonic() < death_deadline:
        text = pane.inner_text(timeout=10_000) if pane.count() else ""
        if re.search(r"Killed|Terminated|exited|exit code", text, re.I):
            death_seen = text[-400:]
            break
        answer_gates(page, cid)
        time.sleep(3)
    page.screenshot(path=str(SHOT_DIR / "phase-a-terminal-dead-server.png"))
    witness["terminal_death_excerpt"] = death_seen

    # ── 3. deliverables LIVE, as the user, once recovery brings both ports
    # back (attempt-4 lesson: the sandbox reaps at FINISHED, so pills/preview/
    # proxy/live-terminal are only witnessable while the run is alive) ────────
    wait_ports_live(page, cid, deadline_s=1200)
    witness["deliverables"] = deliverable_battery(page, cid, shot_prefix="phase-a")
    save_state(phase_a=witness)

    # ── 4. run to FINISHED, then assert the recovery evidence ────────────────
    # (acting as the user: confirmation gates get approved through the UI)
    st, approvals = wait_finished_ui(page, cid, deadline_s=2700, shot_prefix="phase-a")
    witness["final_status"] = st
    witness["confirmation_approvals"] = approvals
    events = full_events(cid)
    (RECORD_DIR / f"events-{cid}.json").write_text(json.dumps(events, indent=2))
    save_state(phase_a=witness)
    assert st == "FINISHED", f"build ended {st}, not FINISHED"

    after_fault = [e for e in events if e["seq"] > fault["pre_seq"]]

    # 3a. the agent NOTICED through its instruments (shell_view/server_status).
    noticed = [
        o
        for o in observations(after_fault)
        if tool_of(o) in ("shell_view", "server_status")
        and re.search(
            rf"(- {fault['port']}: FREE|Killed|Terminated|exited|exit code|not running)",
            obs_content(o),
            re.I,
        )
    ]
    assert noticed, "no shell_view/server_status observation reflects the dead server"
    witness["noticed_seq"] = noticed[0]["seq"]
    witness["noticed_excerpt"] = obs_content(noticed[0])[:600]

    # 3b. a recovery action restarts the server after noticing.
    recovery = [
        a
        for a in actions(after_fault)
        if a["seq"] > noticed[0]["seq"]
        and tool_of(a) in ("server_start", "shell_exec", "shell", "shell_kill_process")
        and re.search(
            rf"{fault['port']}|{_SERVER_CMD}",
            json.dumps((a.get("tool_call") or {}).get("arguments")),
            re.I,
        )
    ]
    assert recovery, "no recovery action references the killed port after noticing"
    witness["recovery_seq"] = recovery[0]["seq"]
    witness["recovery_excerpt"] = json.dumps((recovery[0].get("tool_call") or {}).get("arguments"))[
        :400
    ]

    # 3c. zero hard-denied actions in the ENTIRE run.
    denials = hard_denied(events)
    assert not denials, f"hard-denied actions present: {len(denials)}"

    # 3d. a BP-05-valid browser verification AFTER recovery.
    verifs = [v for v in browser_verifications(events, 8000) if v["seq"] > recovery[0]["seq"]]
    assert verifs, "no console-clean :8000 browser verification after recovery"
    witness["verification_seq"] = verifs[0]["seq"]

    # 3e. FINISHED honestly — not through the 3-refusal ⚠ valve.
    assert not valve_fired(events), "run finished via the ⚠ refusal valve"

    page.screenshot(path=str(SHOT_DIR / "phase-a-recovery-feed.png"))

    witness["assertions"] = "ALL PASS"
    save_state(phase_a=witness)
    (RECORD_DIR / "phase-a-witness.json").write_text(json.dumps(witness, indent=2))
    print("[phase-a] PASS — witness saved")


def _sessions(cid: str) -> list[dict]:
    from common import api_get

    r = api_get(f"/conversations/{cid}/sessions")
    if r.status_code != 200:
        return []
    body = r.json()
    return body if isinstance(body, list) else body.get("sessions", [])
