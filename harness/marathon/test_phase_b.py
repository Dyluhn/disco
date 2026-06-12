"""BP-16 Phase B — restart survival.

Second run of the same scenario; when the event log shows the install step
running, SIGTERM the agent-server, restart it, resume through the UI Resume
control (BP-12). Assert: clean orphan reconciliation, workspace survival,
plan-step pointer monotonicity, and the full Phase-A deliverable battery.

The agent-server lives in tmux pane `pmx-agent-server` on this host — the
harness restarts it with the SAME env (incl. PMX_LOG_JSON=1) appending to
/tmp/pmx-marathon.log so Phase C's token series spans both lives.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

import httpx

from common import (
    API,
    RECORD_DIR,
    ROOT,
    SHOT_DIR,
    actions,
    full_events,
    grep_log,
    hard_denied,
    manifest_files,
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

_INSTALL = re.compile(r"npm (install|create|i |ci)|pnpm (install|add|create)|pip install|uv (pip|add|sync)", re.I)

# The exact command the pane runs — tee -a so Phase C's token series spans
# both server lives in one log.
# The OpenRouter key is injected the same way the operator pane does it: the
# encrypted secret store is permanently locked (PMX_SECRET_KEY lost), so the
# runtime's env fallback is the live auth path — a respawn WITHOUT it leaves
# the resumed driver 401ing (found live 2026-06-11: post-restart resume stalled
# with zero recovered ports because the agent couldn't authenticate).
_OR_KEY_SH = "PMX_OPENROUTER_API_KEY=$(python3 harness/marathon/_or_key.py)"
_SERVER_SH = (
    "cd '{root}' && " + _OR_KEY_SH + " PMX_LOG_JSON=1 PMX_DRIVER_VISION=1 PMX_SANDBOX=gvisor "
    "PMX_LOCAL_SOCKET=ssh://sandbox@100.81.82.115 PMX_LOCAL_RUNTIME=runsc "
    "PMX_DB=test-record/pmx-run.db uv run python -m disco.agent_server "
    "2>&1 | tee -a /tmp/pmx-marathon.log"
)

# Attempt-2 lesson: when the python dies, the tee pipeline exits → the pane
# dies → with remain-on-exit OFF tmux closes the window, the (only) window's
# close kills the session, and the now-session-less server EXITS — so a bare
# respawn-pane finds "no server running". Defenses, in order:
#   1. ARM_REMAIN before the kill keeps the dead pane (and thus the session
#      and server) alive — remain-on-exit is a WINDOW option, hence -w;
#   2. RESPAWN still falls back to new-session in case the server is gone
#      anyway (e.g. the operator's session predates the arm step).
ARM_REMAIN = "tmux set-option -w -t pmx-agent-server remain-on-exit on"
RESPAWN = (
    "if tmux has-session -t pmx-agent-server 2>/dev/null; then "
    'tmux respawn-pane -k -t pmx-agent-server "{cmd}"; '
    'else tmux new-session -d -s pmx-agent-server "{cmd}" '
    "&& tmux set-option -w -t pmx-agent-server remain-on-exit on; fi"
)


def _server_pid() -> int | None:
    """The PYTHON server process — not the tmux pane's bash pipeline wrapper,
    whose cmdline also matches (first Phase-B run killed the wrapper and the
    server only died via SIGPIPE, voiding the graceful-shutdown premise)."""
    out = subprocess.run(
        ["pgrep", "-f", "disco.agent_server"], capture_output=True, text=True
    ).stdout.split()
    for pid in out:
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            continue
        if comm.startswith("python"):
            return int(pid)
    return None


def _health() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def test_phase_b(firefox):
    page = firefox
    witness: dict = {}

    cid = create_submit_upload_approve(page, shot_prefix="phase-b")
    witness["cid"] = cid
    save_state(phase_b={"cid": cid})
    print(f"\n[phase-b] cid={cid} — approved, watching for the install step")

    # ── 5. kill -TERM the agent-server the moment an install action lands ────
    install_seq = None
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline and install_seq is None:
        for a in actions(full_events(cid)):
            if tool_of(a) in ("shell_exec", "shell", "server_start") and _INSTALL.search(
                json.dumps((a.get("tool_call") or {}).get("arguments"))
            ):
                install_seq = a["seq"]
                break
        if install_seq is None:
            answer_gates(page, cid)  # soft-confirms must not stall the run
            time.sleep(4)
    assert install_seq, "no install action ever appeared in the event log"

    pre_files = sorted(f["path"] for f in manifest_files(cid))
    pre_seq = max(e["seq"] for e in full_events(cid))
    pid = _server_pid()
    assert pid, "could not find the agent-server pid"
    subprocess.run(ARM_REMAIN, shell=True, check=False)  # keep the dead pane around
    subprocess.run(["kill", "-TERM", str(pid)], check=True)
    down_deadline = time.monotonic() + 60
    while time.monotonic() < down_deadline and _health():
        time.sleep(1)
    assert not _health(), "agent-server still healthy after SIGTERM"
    witness["killed"] = {"pid": pid, "install_seq": install_seq, "pre_seq": pre_seq,
                         "pre_file_count": len(pre_files)}
    print(f"[phase-b] agent-server SIGTERM'd mid-install (seq {install_seq})")

    # ── restart + reconciliation ──────────────────────────────────────────────
    subprocess.run(RESPAWN.format(cmd=_SERVER_SH.format(root=ROOT)), shell=True, check=True)
    up_deadline = time.monotonic() + 120
    while time.monotonic() < up_deadline and not _health():
        time.sleep(2)
    assert _health(), "agent-server never came back after restart"

    # 6a. orphan reconciliation ran clean (bp-13 startup sweep).
    recon_deadline = time.monotonic() + 90
    recon_lines: list[str] = []
    while time.monotonic() < recon_deadline and not recon_lines:
        recon_lines = grep_log(r"reconciled .*orphaned RUNNING|swept orphan container")
        time.sleep(3)
    assert recon_lines, "no orphan-reconciliation log lines after restart"
    tracebacks = grep_log(r"Traceback")
    witness["reconciliation"] = {"lines": recon_lines[-3:], "tracebacks": len(tracebacks)}

    # 6b. workspace survived: every pre-kill SOURCE file is still on disk.
    #     (node_modules/.pmx/cache churn is legitimate install behavior.)
    post_files = sorted(f["path"] for f in manifest_files(cid))
    survivors_expected = [
        p
        for p in pre_files
        if not p.startswith((".pmx/", "node_modules")) and "/node_modules/" not in p
    ]
    missing = [p for p in survivors_expected if p not in post_files]
    assert not missing, f"workspace lost files across restart: {missing[:10]}"
    witness["workspace"] = {
        "pre": len(pre_files),
        "post_restart": len(post_files),
        "source_files_checked": len(survivors_expected),
    }

    # ── resume through the UI (BP-12) ─────────────────────────────────────────
    page.goto(f"/build/{cid}", timeout=30_000)
    resume = page.get_by_role("button", name=re.compile("Resume the agent"))
    resume.wait_for(state="visible", timeout=120_000)
    page.screenshot(path=str(SHOT_DIR / "phase-b-resume-control.png"))
    resume.click()
    print("[phase-b] resumed via the UI — running to FINISHED")

    # 6d. same deliverable battery as Phase A — LIVE, once both ports serve
    # again post-resume (the sandbox reaps at FINISHED).
    wait_ports_live(page, cid, deadline_s=2400)
    witness["deliverables"] = deliverable_battery(page, cid, shot_prefix="phase-b")
    save_state(phase_b=witness)

    st, approvals = wait_finished_ui(page, cid, deadline_s=2700, shot_prefix="phase-b")
    witness["final_status"] = st
    witness["confirmation_approvals"] = approvals
    events = full_events(cid)
    (RECORD_DIR / f"events-{cid}.json").write_text(json.dumps(events, indent=2))
    save_state(phase_b=witness)
    assert st == "FINISHED", f"resumed build ended {st}"
    assert not hard_denied(events), "hard-denied actions present in Phase B"
    assert not valve_fired(events), "Phase B finished via the ⚠ valve"

    # 6c. completed plan steps did not re-execute (pointer monotonicity).
    done_pre: set[int] = set()
    violations: list[dict] = []
    for a in actions(events):
        if tool_of(a) != "plan_step":
            continue
        args = (a.get("tool_call") or {}).get("arguments") or {}
        idx, state = args.get("index"), args.get("state")
        if a["seq"] <= pre_seq and state == "done":
            done_pre.add(idx)
        if a["seq"] > pre_seq and state == "active" and idx in done_pre:
            violations.append({"seq": a["seq"], "index": idx})
    assert not violations, f"completed plan steps re-executed after restart: {violations}"
    witness["plan_monotonicity"] = {"done_before_restart": sorted(done_pre)}

    page.screenshot(path=str(SHOT_DIR / "phase-b-resumed-finished.png"))

    witness["assertions"] = "ALL PASS"
    save_state(phase_b=witness)
    (RECORD_DIR / "phase-b-witness.json").write_text(json.dumps(witness, indent=2))
    print("[phase-b] PASS — witness saved")
