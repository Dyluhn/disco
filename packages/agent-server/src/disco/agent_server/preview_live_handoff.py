"""Explicit mutable-workspace launch transitions from a sealed Preview."""

from __future__ import annotations

import logging
from typing import Any

from .preview_manager import PreviewManager
from .preview_models import PreviewHandoffError, PreviewSession
from .preview_status import live_preview_identity

_LOG = logging.getLogger(__name__)

__all__ = ["PreviewHandoffError", "start_live_preview"]


async def start_live_preview(
    sandbox: Any,
    *,
    serve_dir: str | None = None,
    command: str | None = None,
    framework: str | None = None,
    cwd: str | None = None,
    name: str | None = None,
    supervise: bool = True,
) -> PreviewSession:
    """Health-prove an explicit host launch before replacing an isolated owner."""
    previous = getattr(sandbox, "_preview_manager", None)
    isolated = getattr(previous, "owns_sandbox", None)
    replacing = callable(isolated) and isolated() is True
    if replacing and getattr(previous, "_closed", False):
        raise PreviewHandoffError("Preview owner is closing; no replacement was launched.")
    manager = PreviewManager(sandbox) if replacing or previous is None else previous
    if previous is None:
        sandbox._preview_manager = manager
    if not replacing:
        return await manager.start(
            serve_dir=serve_dir,
            command=command,
            framework=framework,
            cwd=cwd,
            name=name,
            supervise=supervise,
        )
    assert previous is not None
    previous._retired_managers.append(manager)
    identity = live_preview_identity(sandbox)
    current = previous.canonical_lifecycle_session()
    if current is not None:
        current.update_error = "Updating Preview; showing the previous sealed app."
    try:
        launched = await manager.start(
            serve_dir=serve_dir,
            command=command,
            framework=framework,
            cwd=cwd,
            name=name,
            supervise=supervise,
        )
        _validate_replacement(sandbox, previous, manager, launched, identity)
    except BaseException as exc:
        notice = _failed_update_notice(sandbox, previous, identity, exc)
        await _close_failed_candidate(previous, manager)
        if isinstance(exc, Exception):
            raise PreviewHandoffError(notice) from exc
        raise
    # No await between freshness validation and publishing the replacement.
    # Its generation is new; existing capability checks reject the old identity.
    previous._retired_managers.remove(manager)
    manager._retired_managers.append(previous)
    sandbox._preview_manager = manager
    try:
        await previous.aclose()
    except Exception:  # noqa: BLE001 — retain ownership for teardown retry
        _LOG.warning("Retired sealed Preview cleanup failed", exc_info=True)
    else:
        if previous in manager._retired_managers:
            manager._retired_managers.remove(previous)
    return launched


def _validate_replacement(
    sandbox: Any,
    previous: PreviewManager,
    manager: PreviewManager,
    launched: PreviewSession,
    identity: tuple[str, int] | None,
) -> None:
    if manager.canonical_session() is not launched:
        raise PreviewHandoffError(launched.detail or "The replacement app is not available.")
    if (
        getattr(sandbox, "_preview_manager", None) is not previous
        or live_preview_identity(sandbox) != identity
        or previous._closed
    ):
        raise PreviewHandoffError("Preview ownership changed during the update.")


def _failed_update_notice(
    sandbox: Any,
    previous: PreviewManager,
    identity: tuple[str, int] | None,
    error: BaseException,
) -> str:
    if (
        getattr(sandbox, "_preview_manager", None) is not previous
        or live_preview_identity(sandbox) != identity
        or previous._closed
    ):
        return "Preview ownership changed; this update installed no replacement."
    notice = "Preview update failed; showing the previous sealed app. " + (
        str(error) or "The update was cancelled."
    )
    current = previous.canonical_lifecycle_session()
    if current is not None:
        current.update_error = notice
    return notice


async def _close_failed_candidate(previous: PreviewManager, candidate: PreviewManager) -> None:
    try:
        await candidate.aclose()
    except Exception:  # noqa: BLE001 — preserve the launch error and cleanup ownership
        _LOG.warning("Failed Preview candidate cleanup requires teardown retry", exc_info=True)
    else:
        if candidate in previous._retired_managers:
            previous._retired_managers.remove(candidate)
