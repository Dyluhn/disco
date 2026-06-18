"""Lazy live-desktop daemon (P2).

Manages the Xvfb + x11vnc + websockify process stack INSIDE the sandbox container.
Shipped alongside _browser_daemon.py as /workspace/.pmx/_live_view.py; imported by
the daemon on demand (try/except so unit tests don't need Xvfb installed).

Design decisions:
  - NOVNC_PORT = 6080  (websockify → the noVNC frontend)
  - VNC_PORT   = 5901  (x11vnc loopback-only — never in USER_PORTS)
  - DISPLAY    = ":1"  (Xvfb virtual framebuffer, 1280×800×24-bit)
  - view_only  = yes   (x11vnc -viewonly flag; no input injection by default)
  - lazy       = yes   (the stack only starts when ensure_live() is called)
  - no WM             (Chromium --kiosk fills the whole display)

Security: VNC is bound to 127.0.0.1 inside the sandbox only. The noVNC iframe
goes through the existing per-conversation preview proxy ({cid8}-6080.localhost),
the same auth/jail as the dev-server preview. Host network is never reachable.

P5 live jail acceptance (loopback-bind, per-conv jail, view-only, idle teardown)
is HARDWARE-DEFERRED — VM 201 (the gVisor sandbox host) was destroyed. These
invariants must be verified on a real sandbox backend before shipping to production.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

NOVNC_PORT: int = 6080
VNC_PORT: int = 5901
DISPLAY: str = ":1"
_GEOMETRY: str = "1280x800x24"

# Per-process state: keyed by process name → Popen object (or None = not started).
_state: dict[str, Any] = {
    "xvfb": None,
    "x11vnc": None,
    "websockify": None,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _proc_alive(proc: Any) -> bool:
    """True if proc is a live Popen that hasn't exited."""
    return proc is not None and proc.poll() is None


def _xvfb_running() -> bool:
    """Check via lock file + Popen handle whether Xvfb :1 is up."""
    if _proc_alive(_state["xvfb"]):
        return True
    # Also check the lock file in case a prior process owns it.
    lock = "/tmp/.X1-lock"
    if os.path.exists(lock):
        try:
            pid = int(open(lock).read().strip())
            # Check if that pid is alive.
            os.kill(pid, 0)
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale lock — Xvfb is gone; we'll start a fresh one.
            try:
                os.remove(lock)
            except OSError:
                pass
    return False


def _port_listening(port: int) -> bool:
    """Return True if a process is listening on loopback:port (best-effort)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_xvfb() -> bool:
    """Ensure the Xvfb virtual framebuffer is running on DISPLAY :1.

    Idempotent — does nothing if already up. Returns True on success."""
    if _xvfb_running():
        return True
    env = dict(os.environ)
    try:
        proc = subprocess.Popen(
            [
                "Xvfb",
                DISPLAY,
                "-screen", "0", _GEOMETRY,
                "-nolisten", "tcp",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _state["xvfb"] = proc
        # Give Xvfb a moment to bind the socket before x11vnc tries to connect.
        time.sleep(0.5)
        return _proc_alive(proc)
    except FileNotFoundError:
        return False


def ensure_x11vnc() -> bool:
    """Ensure x11vnc is running on loopback VNC_PORT (5901), attached to DISPLAY :1.

    View-only by default (-viewonly). Idempotent — no-ops if already up.
    Returns True on success."""
    if _proc_alive(_state["x11vnc"]):
        return True
    if _port_listening(VNC_PORT):
        return True  # something is already on 5901 (e.g. prior process)
    try:
        proc = subprocess.Popen(
            [
                "x11vnc",
                "-display", DISPLAY,
                "-localhost",
                "-rfbport", str(VNC_PORT),
                "-nopw",
                "-forever",
                "-bg",
                "-noxdamage",
                "-quiet",
                "-viewonly",
            ],
            env={**os.environ, "DISPLAY": DISPLAY},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _state["x11vnc"] = proc
        time.sleep(0.3)
        return _proc_alive(proc) or _port_listening(VNC_PORT)
    except FileNotFoundError:
        return False


def ensure_websockify() -> bool:
    """Ensure websockify is bridging 127.0.0.1:NOVNC_PORT → 127.0.0.1:VNC_PORT.

    Idempotent — no-ops if already up. Returns True on success."""
    if _proc_alive(_state["websockify"]):
        return True
    if _port_listening(NOVNC_PORT):
        return True
    try:
        proc = subprocess.Popen(
            [
                "websockify",
                "--daemon",
                f"127.0.0.1:{NOVNC_PORT}",
                f"127.0.0.1:{VNC_PORT}",
                "--web", "/usr/share/novnc",
            ],
            env=dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _state["websockify"] = proc
        time.sleep(0.3)
        return _proc_alive(proc) or _port_listening(NOVNC_PORT)
    except FileNotFoundError:
        return False


def ensure_live() -> bool:
    """Idempotent: ensure Xvfb + x11vnc + websockify are all running.

    Returns True if the whole stack came up. Each sub-call is best-effort;
    a missing binary returns False without crashing."""
    ok_xvfb = ensure_xvfb()
    ok_vnc = ensure_x11vnc()
    ok_ws = ensure_websockify()
    return ok_xvfb and ok_vnc and ok_ws


def is_live() -> bool:
    """True if all three processes appear to be running."""
    return (
        _xvfb_running()
        and (_proc_alive(_state["x11vnc"]) or _port_listening(VNC_PORT))
        and (_proc_alive(_state["websockify"]) or _port_listening(NOVNC_PORT))
    )


def teardown() -> None:
    """Best-effort shutdown of the Xvfb + x11vnc + websockify stack.

    Sends SIGTERM to each tracked process; ignores errors (processes may
    have already exited). Does NOT remove the Xvfb lock file — the OS
    cleans it on process death."""
    for key in ("websockify", "x11vnc", "xvfb"):
        proc = _state.get(key)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            _state[key] = None
