"""BP-16 marathon harness — shared plumbing.

Test-order discipline: this package only OBSERVES and injects faults; it never
patches system code. Failures become defect reports in
test-record/marathon/DEFECTS.md, not fixes.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time
from typing import Any

import httpx

API = "http://127.0.0.1:8000"
UI = "http://localhost:5174"
SANDBOX_SSH = ["ssh", "-o", "BatchMode=yes", "sandbox@100.81.82.115"]

ROOT = pathlib.Path(__file__).resolve().parents[3]
RECORD_DIR = ROOT / "test-record" / "marathon"
SHOT_DIR = ROOT / "test-record" / "screenshots" / "marathon"
CSV_PATH = pathlib.Path(__file__).parent / "sensor_readings.csv"
STATE_PATH = RECORD_DIR / "state.json"

TASK_PROMPT = (
    "Build a small 'sensor dashboard' web app. I've uploaded sensor_readings.csv. "
    "Backend: a Python (or Node) API on port 3000 that serves the CSV as JSON at "
    "/api/readings. Frontend: a Vite + React app on port 8000 that fetches "
    "/api/readings and renders a table plus a summary card (count, min, max). "
    "Install whatever you need. Verify it in the browser before finishing."
)

VALVE_MARKER = "⚠ finished WITHOUT a clean browser verification"
HARD_DENY_MARKER = "hard-denied"


# ── tiny state file shared across the three phase test modules ───────────────


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(**updates: Any) -> dict:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    st = load_state()
    st.update(updates)
    STATE_PATH.write_text(json.dumps(st, indent=2))
    return st


# ── agent-server HTTP ─────────────────────────────────────────────────────────


def api_get(path: str, timeout: float = 30.0, retries: int = 5) -> httpx.Response:
    """bp-12 template hazard: uvicorn keep-alive recycling + long server stalls
    while in-sandbox work runs. Retry generously."""
    last: Exception | None = None
    for _ in range(retries):
        try:
            return httpx.get(f"{API}{path}", timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise last  # type: ignore[misc]


def full_events(cid: str) -> list[dict]:
    """The /events route pages at 100 — walk after_seq to the end."""
    out: list[dict] = []
    after: int | None = None
    while True:
        q = "?limit=100" + (f"&after_seq={after}" if after is not None else "")
        page = api_get(f"/conversations/{cid}/events{q}").json()
        events = page.get("events", [])
        out.extend(events)
        if len(events) < 100:
            return out
        after = events[-1]["seq"]


def exec_status(cid: str) -> str:
    return api_get(f"/conversations/{cid}/state").json().get("execution_status", "?")


def wait_status(cid: str, wanted: set[str], deadline_s: float, poll_s: float = 10.0) -> str:
    deadline = time.monotonic() + deadline_s
    st = "?"
    while time.monotonic() < deadline:
        st = exec_status(cid)
        if st in wanted:
            return st
        time.sleep(poll_s)
    return st


def manifest_files(cid: str) -> list[dict]:
    r = api_get(f"/api/projects/{cid}/manifest")
    if r.status_code != 200:
        return []
    return r.json().get("files", [])


# ── sandbox-side helpers (VM-201, gvisor backend) ─────────────────────────────


def ssh_sandbox(cmd: str, timeout: int = 30) -> str:
    return subprocess.run(
        SANDBOX_SSH + [cmd], capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


def container_for(cid: str) -> str | None:
    names = ssh_sandbox(
        f"docker ps --filter label=pmx.conversation_id={cid} --format '{{{{.Names}}}}'"
    ).splitlines()
    for n in names:
        if n.startswith("pmx-sbx"):
            return n
    return None


# /proc-based port probe + killer — zero tool deps inside the container
# (ss/fuser availability varies; python3 is guaranteed in pmx-sandbox:base).
_PORT_PY = r"""
import glob, os, sys
port = int(sys.argv[1]); action = sys.argv[2]
want = "%0.4X" % port
inodes = set()
for tcp in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(tcp).read().splitlines()[1:]
    except OSError:
        continue
    for ln in lines:
        f = ln.split()
        if f[1].rsplit(":", 1)[1] == want and f[3] == "0A":  # 0A = LISTEN
            inodes.add(f[9])
pids = set()
for fd in glob.glob("/proc/[0-9]*/fd/*"):
    try:
        if os.readlink(fd).strip("socket:[]") in inodes:
            pids.add(int(fd.split("/")[2]))
    except OSError:
        pass
if action == "probe":
    print(",".join(map(str, sorted(pids))))
elif action == "list":
    for p in sorted(pids):
        cmdline = open(f"/proc/{p}/cmdline").read().replace("\0", " ").strip()
        print(f"{p}\t{cmdline}")
else:
    import signal
    for p in sorted(pids):
        cmdline = open(f"/proc/{p}/cmdline").read().replace("\0", " ").strip()
        print(f"killing pid {p}: {cmdline}")
        os.kill(p, signal.SIGKILL)
"""


def port_pids(container: str, port: int) -> list[int]:
    out = ssh_sandbox(f"docker exec {container} python3 -c '{_PORT_PY}' {port} probe 2>/dev/null")
    return [int(x) for x in out.split(",") if x.strip().isdigit()]


def port_owners(container: str, port: int) -> dict[int, str]:
    """pid → cmdline for every LISTEN owner of <port>. Attempt-5 lesson: the
    platform preview server (`python3 -m http.server <port> -d /workspace`)
    owns :8000 from container start — the fault must SEE what it is about to
    kill and refuse anything the agent didn't launch."""
    out = ssh_sandbox(f"docker exec {container} python3 -c '{_PORT_PY}' {port} list 2>/dev/null")
    owners: dict[int, str] = {}
    for ln in out.splitlines():
        pid, _, cmd = ln.partition("\t")
        if pid.strip().isdigit():
            owners[int(pid)] = cmd.strip()
    return owners


def kill_port_process(container: str, port: int) -> str:
    """The Phase-A fault: SIGKILL the process owning <port>, from OUTSIDE the
    agent (docker exec) — the tmux session survives and shows the death."""
    return ssh_sandbox(f"docker exec {container} python3 -c '{_PORT_PY}' {port} kill 2>&1")


# ── event-log analysis ────────────────────────────────────────────────────────


def actions(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "action"]


def observations(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "observation"]


def tool_of(e: dict) -> str | None:
    if e.get("kind") == "action":
        return (e.get("tool_call") or {}).get("tool_name")
    if e.get("kind") == "observation":
        return (e.get("tool_result") or {}).get("tool_name")
    return None


def obs_content(e: dict) -> str:
    tr = e.get("tool_result") or {}
    return tr.get("content") or ""


def hard_denied(events: list[dict]) -> list[dict]:
    return [
        e
        for e in events
        if e.get("kind") == "agent_error" and HARD_DENY_MARKER in (e.get("error") or "")
    ]


def valve_fired(events: list[dict]) -> bool:
    return any(
        VALVE_MARKER in (e.get("content") or e.get("error") or json.dumps(e))
        for e in events
        if e.get("kind") in ("message", "agent_error", "status")
    )


def browser_verifications(events: list[dict], port: int = 8000) -> list[dict]:
    """BP-05-valid verifications: browser observations against :<port> whose
    console carries no error-level entries."""
    out = []
    for e in observations(events):
        tr = e.get("tool_result") or {}
        if tr.get("tool_name") != "browser":
            continue
        s = tr.get("structured") or {}
        url = str(s.get("url") or "")
        if f":{port}" not in url:
            continue
        console = s.get("console") or []
        errors = [c for c in console if str(c.get("level", "")).lower() == "error"]
        if s.get("ok") and not errors:
            out.append(e)
    return out


def csv_known_values() -> dict:
    """Pull assertable truths out of the REAL csv: count/min/max for the summary
    card + three distinctive cell values for the table."""
    import csv as _csv

    rows = list(_csv.DictReader(CSV_PATH.open()))
    temps = [float(r["temperature_c"]) for r in rows]
    uniq = sorted({f"{t:g}" for t in temps})
    picks = [uniq[0], uniq[len(uniq) // 2], uniq[-1]]
    return {
        "count": len(rows),
        "min": f"{min(temps):g}",
        "max": f"{max(temps):g}",
        "cells": picks,
    }


def grep_log(pattern: str, log_path: str = "/tmp/pmx-marathon.log") -> list[str]:
    p = pathlib.Path(log_path)
    if not p.exists():
        return []
    rx = re.compile(pattern)
    return [ln for ln in p.read_text(errors="replace").splitlines() if rx.search(ln)]
