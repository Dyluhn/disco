"""Lazy live-desktop daemon (P2).

Manages the Xvfb + x11vnc + websockify process stack INSIDE the sandbox container.
Shipped alongside _browser_daemon.py as /workspace/.pmx/_live_view.py; imported by
the daemon on demand (try/except so unit tests don't need Xvfb installed).

Design decisions:
  - NOVNC_PORT = 6080  (websockify → the noVNC frontend)
  - VNC_PORT   = 5901  (x11vnc — bound to 127.0.0.1 inside the container, never published)
  - DISPLAY    = ":1"  (Xvfb virtual framebuffer, 1280×800×24-bit)
  - view_only  = yes   (x11vnc -viewonly flag; no input injection, enforced server-side)
  - lazy       = yes   (the stack only starts when ensure_live() is called)
  - no WM             (Chromium --kiosk fills the whole display)

Security model (hardened after the gpt-5.5 BLOCK review):
  * The SENSITIVE layer is x11vnc: it has the actual screen + (potential) input. It is
    bound to 127.0.0.1:5901 inside the container and runs -viewonly. That loopback +
    view-only pair is the real boundary, and it never leaves the container.
  * websockify binds 0.0.0.0:6080 INSIDE the container. It MUST be on the container
    interface (not loopback) or the backend's published-port mapping cannot reach it
    (Docker/Podman forward to the container's interface, never its loopback) — the same
    convention every dev-server preview follows. It is only a WebSocket↔VNC bridge to
    the already-loopback, already-view-only x11vnc; it adds no screen access of its own.
  * The host side is closed by THREE gates outside this file: the published host port is
    loopback-bound on the host; HostPreviewProxyMiddleware scopes 6080 to {cid8}; and
    PreviewService.port_upstream refuses 6080 unless live_browser.enabled. Disabling the
    feature closes the network surface, not just the button.
  * Fail-closed: we only ever reuse processes WE started (tracked Popen handles). A port
    occupied by a foreign process is treated as a hazard — we refuse rather than bridge
    to an unknown (possibly non-view-only) VNC server.

P5 live jail acceptance on the production gVisor backend (VM 201, destroyed) is still
the only piece that needs that specific hardware; the loopback-bind / view-only / teardown
invariants are provable on any container backend (rootless podman included).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any

NOVNC_PORT: int = 6080
VNC_PORT: int = 5901
DISPLAY: str = ":1"
_GEOMETRY: str = "1280x800x24"

# After this many seconds with no activity (no ensure_live / touch), the watchdog
# tears the whole stack down so an abandoned live view does not keep the VNC surface
# (and its CPU/RAM) alive for the lifetime of the sandbox.
IDLE_TIMEOUT_S: float = 600.0
_WATCHDOG_TICK_S: float = 15.0

# Per-process state: keyed by process name → Popen object (or None = not started).
# These handles are the SINGLE source of truth for "is the stack ours and alive".
_state: dict[str, Any] = {
    "xvfb": None,
    "x11vnc": None,
    "websockify": None,
}

# Idle watchdog bookkeeping.
_last_active: float = 0.0
_watchdog: threading.Thread | None = None
_watchdog_stop = threading.Event()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _proc_alive(proc: Any) -> bool:
    """True if proc is a live Popen WE started that hasn't exited."""
    return proc is not None and proc.poll() is None


def _xvfb_running() -> bool:
    """True only if OUR Xvfb Popen handle is alive. We deliberately do NOT honour a
    foreign /tmp/.X1-lock: a display socket we don't own is a hazard, not a free win."""
    return _proc_alive(_state["xvfb"])


def _port_listening(port: int) -> bool:
    """Return True if ANY process is listening on :port (best-effort, foreign-detection)."""
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


def _foreign_on_port(port: int, ours: Any) -> bool:
    """True if `port` is occupied but NOT by our tracked Popen handle `ours`.

    This is the fail-closed gate: we must never bridge websockify to, or reuse,
    a VNC/bridge process we didn't start (it might not be -viewonly / loopback)."""
    if _proc_alive(ours):
        return False  # it's ours and alive — not foreign
    return _port_listening(port)


def _spawn(argv: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[bytes] | None:
    """Start a long-lived child in its OWN session/process group so teardown() can
    kill the whole group (the child + anything it forks). Foreground — NO self-daemonising
    flags (-bg / --daemon) — so the Popen handle tracks the REAL server, not a parent
    that exits immediately (the bug that made the old teardown a no-op)."""
    try:
        return subprocess.Popen(
            argv,
            env=env if env is not None else dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group → killpg in teardown
        )
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_xvfb() -> bool:
    """Ensure the Xvfb virtual framebuffer is running on DISPLAY :1.

    Idempotent — reuses OUR live handle. Returns True on success."""
    if _xvfb_running():
        return True
    proc = _spawn(["Xvfb", DISPLAY, "-screen", "0", _GEOMETRY, "-nolisten", "tcp"])
    if proc is None:
        return False
    _state["xvfb"] = proc
    # Give Xvfb a moment to bind the socket before x11vnc tries to connect.
    time.sleep(0.5)
    return _proc_alive(proc)


def ensure_x11vnc() -> bool:
    """Ensure x11vnc is running on loopback VNC_PORT (5901), view-only, attached to :1.

    Fail-closed: if 5901 is occupied by a process we did NOT start, refuse (return False)
    rather than risk bridging to a non-view-only server. Idempotent for OUR handle."""
    if _proc_alive(_state["x11vnc"]):
        return True
    if _foreign_on_port(VNC_PORT, _state["x11vnc"]):
        return False  # unknown VNC already on 5901 — fail closed, never reuse
    proc = _spawn(
        [
            "x11vnc",
            "-display", DISPLAY,
            "-localhost",       # bind 127.0.0.1 only — the screen never leaves the container
            "-rfbport", str(VNC_PORT),
            "-nopw",
            "-forever",
            "-noxdamage",
            "-quiet",
            "-viewonly",        # no input injection — server-side enforced
        ],
        env={**os.environ, "DISPLAY": DISPLAY},
    )
    if proc is None:
        return False
    _state["x11vnc"] = proc
    time.sleep(0.3)
    return _proc_alive(proc)


def ensure_websockify() -> bool:
    """Ensure websockify bridges 0.0.0.0:NOVNC_PORT → 127.0.0.1:VNC_PORT.

    Binds the CONTAINER interface (0.0.0.0) so the backend's published-port mapping can
    reach it (loopback would be unreachable through Docker/Podman publish). It only
    bridges to the already-loopback, already-view-only x11vnc. Fail-closed on a foreign
    listener; idempotent for OUR handle."""
    if _proc_alive(_state["websockify"]):
        return True
    if _foreign_on_port(NOVNC_PORT, _state["websockify"]):
        return False  # something else already on 6080 — fail closed
    proc = _spawn(
        [
            "websockify",
            f"0.0.0.0:{NOVNC_PORT}",
            f"127.0.0.1:{VNC_PORT}",
            "--web", "/usr/share/novnc",
        ]
    )
    if proc is None:
        return False
    _state["websockify"] = proc
    time.sleep(0.3)
    return _proc_alive(proc)


def _any_alive() -> bool:
    """True if ANY tracked child is still alive (vs is_live() which needs ALL three)."""
    return any(_proc_alive(_state.get(k)) for k in ("xvfb", "x11vnc", "websockify"))


def _watchdog_loop() -> None:
    """Reap the stack when it is fully idle OR partially dead.

    Two exit conditions, both of which TEAR DOWN before returning so no surviving
    child is ever orphaned:
      * not is_live() — one or more children died; teardown() reaps the survivors
        (and any zombies) instead of leaving a half-open stack/surface around.
      * idle longer than IDLE_TIMEOUT_S with no touch() heartbeat — abandoned view."""
    while not _watchdog_stop.wait(_WATCHDOG_TICK_S):
        if not is_live():
            if _any_alive():
                teardown()  # partial death — never leave a survivor (e.g. a live x11vnc)
            return
        if (time.monotonic() - _last_active) >= IDLE_TIMEOUT_S:
            teardown()
            return


def _start_watchdog() -> None:
    """Start (or restart) the single idle/partial-death watchdog for the CURRENT stack.

    Restart-safe: a prior teardown() leaves `_watchdog_stop` SET, and an old watchdog
    thread may still be winding down. We must signal + join that stale thread and then
    CLEAR the stop flag before starting a fresh one — otherwise a quick close→reopen
    would early-return on the dying thread, never clear the flag, and leave the new
    stack with no reaper at all (the race the round-4 review caught). Only ever called
    from the main path (ensure_live), never from the watchdog thread itself."""
    global _watchdog
    old = _watchdog
    if old is not None and old.is_alive() and old is not threading.current_thread():
        _watchdog_stop.set()       # wake the old thread out of its wait() so it exits now
        old.join(timeout=2)
    _watchdog_stop.clear()         # fresh run: the new loop must NOT see a set stop flag
    _watchdog = threading.Thread(target=_watchdog_loop, name="live-view-watchdog", daemon=True)
    _watchdog.start()


def touch() -> None:
    """Reset the idle timer — call when the user (re)opens / heartbeats the live view."""
    global _last_active
    _last_active = time.monotonic()


def ensure_live() -> bool:
    """Idempotent: ensure Xvfb + x11vnc + websockify are all running, then arm the idle
    watchdog. Returns True only if the WHOLE stack came up. A failure (missing binary or
    a foreign listener) returns False without crashing — and tears down any partial start
    so we never leave a half-open surface."""
    touch()
    ok = ensure_xvfb() and ensure_x11vnc() and ensure_websockify()
    if not ok:
        teardown()  # fail-closed: don't leave Xvfb-only / half-bridged state up
        return False
    _start_watchdog()
    return True


def is_live() -> bool:
    """True only if all three processes WE started are still alive."""
    return (
        _proc_alive(_state["xvfb"])
        and _proc_alive(_state["x11vnc"])
        and _proc_alive(_state["websockify"])
    )


def teardown() -> None:
    """Stop the whole stack. Because every child runs in its OWN process group
    (start_new_session=True) and WITHOUT self-daemonising flags, killing the group
    reliably reaps the real server (the old -bg/--daemon path left orphans that
    terminate() could never reach). Best-effort; ignores already-dead processes."""
    _watchdog_stop.set()
    for key in ("websockify", "x11vnc", "xvfb"):
        proc = _state.get(key)
        if proc is None:
            continue
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            # Group already gone, or pid reused — fall back to a direct terminate.
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        # REAP the child: we are its parent, so without wait() a SIGTERM'd process
        # lingers as a <defunct> zombie until the daemon exits. wait() collects it.
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Didn't die on SIGTERM in time — escalate to SIGKILL, then reap.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — already reaped / not our child
            pass
        _state[key] = None
