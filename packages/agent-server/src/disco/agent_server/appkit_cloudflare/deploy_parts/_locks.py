"""Per-workspace + cross-process deploy locking.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged. ``_deploy_lock_dir`` and ``_deploy_stage_root``
(the two directory-resolving siblings of ``_deploy_lock_path`` below) stay
defined directly on the ``deploy`` facade rather than here — see ``_workspace.py``'s
docstring for why (a pre-existing regression test inspects their source via
``inspect.getsource`` on the ``deploy`` MODULE OBJECT itself).

MONKEYPATCH NOTE: ``_fcntl`` is one of the four names tests monkeypatch directly on
the ``deploy`` facade module (to simulate a non-POSIX host). The POSIX-only import
therefore lives SOLELY on the facade (``deploy.py``), not here:
:func:`_cross_process_deploy_lock` resolves it through the parent module's OWN
binding at call time, so a patch on ``deploy._fcntl`` actually changes the
behaviour observed here (mirroring the pattern in ``core/release/local_compose_parts``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path

from ...workspace_process_fence import (
    WorkspaceProcessBusy,
    try_workspace_process_fence,
    workspace_process_lock_path,
)
from ..models import DeployRefused, RefusalReason

# ---- per-workspace deploy mutex (SEC-25/CORR-11) -----------------------------

#: One asyncio mutex per resolved workspace path. A real deploy mutates a single,
#: shared blast radius — the staged dist, the remote D1, wrangler.toml, the
#: deployment record — so two concurrent deploys of the SAME workspace must NEVER
#: interleave (a half-substituted wrangler.toml, a doubled D1 create, a torn
#: record). Keyed on the resolved live-workspace path; created lazily (no ``await``
#: between the get and the set, so this is race-free on the single-threaded loop).
_DEPLOY_LOCKS: dict[str, asyncio.Lock] = {}


def _deploy_lock_for(workspace: Path) -> asyncio.Lock:
    try:
        key = str(workspace.resolve())
    except OSError:
        key = str(workspace)
    lock = _DEPLOY_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DEPLOY_LOCKS[key] = lock
    return lock


# ---- cross-process advisory deploy lock (SEC-25) -----------------------------

#: The in-process ``_DEPLOY_LOCKS`` mutex + the routes per-conversation in-flight set
#: only serialise deploys WITHIN one server process. A multi-worker server (gunicorn
#: ``-w N``, multiple uvicorn workers, a restarted process racing the old one) runs N
#: independent processes that DON'T share that state, so two of them could interleave a
#: deploy of the SAME workspace — a torn wrangler.toml substitution, a doubled D1
#: create, a half-written deployment record. This file ``flock`` is the cross-process
#: backstop: an OS-level advisory lock, keyed by the resolved workspace path, that
#: serialises across processes (and auto-releases on fd close / process death, so a
#: crashed holder never deadlocks the next deploy).


def _deploy_lock_path(workspace: Path) -> Path:
    """The lockfile path for *workspace*: ``<lock dir>/<sha256(resolved path)>.lock``.
    Keyed by a hash of the RESOLVED workspace path so two processes targeting the same
    workspace pick the same lockfile (and distinct workspaces never collide), and the
    filename never leaks a host path."""
    return workspace_process_lock_path(workspace)


@contextlib.contextmanager
def _cross_process_deploy_lock(workspace: Path) -> Iterator[None]:
    """Hold a CROSS-PROCESS advisory lock for *workspace* for the body's duration
    (SEC-25). Acquires ``flock(LOCK_EX | LOCK_NB)`` on a server-controlled lockfile; if
    ANOTHER process already holds it, FAIL FAST with :class:`DeployRefused`
    (``DEPLOY_IN_PROGRESS``) rather than block or interleave. The lock is released — and
    the fd closed — in a ``finally`` (and ``flock`` auto-releases on process death, so a
    crashed holder leaves no deadlock; the stale lockfile on disk is harmless, the next
    process re-acquires it).

    PLATFORM-SPECIFIC POSTURE (the cross-process guarantee is conditional on ``fcntl``):
      * POSIX hosts (``fcntl`` present): a real OS-level ``flock`` — the lock is held
        CROSS-PROCESS, so a multi-worker server is fully serialised for this workspace.
      * Non-POSIX hosts (no ``fcntl`` — e.g. native Windows): this DEGRADES to a no-op
        with a logged warning. Locking is then IN-PROCESS ONLY (the ``_DEPLOY_LOCKS``
        asyncio mutex + the routes in-flight set still serialise WITHIN the one process);
        the cross-process backstop is simply unavailable. The deploy is NOT crashed — a
        single-process deployment (the common case) is unaffected, and a multi-worker
        non-POSIX deployment must rely on process-local serialisation. The degradation is
        logged (below) so operators/readers understand the reduced posture."""
    # Resolved through the parent facade's OWN binding (not this module's local
    # ``_fcntl``) so a test/operator patch of ``deploy._fcntl`` is actually observed —
    # see the module docstring.
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    if _deploy._fcntl is None:  # pragma: no cover - exercised only on non-POSIX hosts
        # Non-POSIX (no fcntl): no cross-process lock is available — degrade to a logged
        # no-op (in-process serialisation only). See the PLATFORM-SPECIFIC POSTURE note above.
        _deploy._log.warning(
            "fcntl is unavailable on this platform; the AppKit deploy lock is "
            "process-local only. With multiple server processes a deploy of the same "
            "workspace could interleave (SEC-25 cross-process backstop disabled)."
        )
        yield
        return
    try:
        with try_workspace_process_fence(workspace):
            yield
    except WorkspaceProcessBusy as exc:
        raise DeployRefused(
            RefusalReason.DEPLOY_IN_PROGRESS,
            "Another workspace operation is already in progress. Wait for it "
            "to finish, then retry the deploy.",
        ) from exc
