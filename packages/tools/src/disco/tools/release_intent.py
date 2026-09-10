"""The host-owned release-intent writer capability (WO-C1).

``release_declare`` is an ``in_process`` HOST tool: it records a project's typed,
NAMES-only release intent as a ``release-intent.json`` sidecar under the projects
root — which is HOST state the sandbox does not own. Rather than let the tool
guess that root (the old ``ProjectStore("")`` fallback wrote to the
``DISCO_DATA_DIR`` default even when the runtime selected a CUSTOM ``projects_root``),
the runtime injects a NARROW capability into ``ToolContext``: a single callable that
persists ONE conversation's intent under the ACTIVE configured store, resolved at
INVOCATION time.

The tool therefore never receives a raw root path and has no fallback store. When
the capability is absent (a standalone executor, or any run the runtime did not
wire it into), the tool fails closed — it can persist nothing.

Signature: ``(conversation_id, caller_owner_id, intent) -> None``. The
implementation resolves the active ``ProjectStore`` from settings, validates the
root, checks that the caller owns the target conversation, and writes atomically.
Every failure raises :class:`ReleaseIntentWriteError` (a typed, model-facing code)
and persists ZERO bytes; the message is safe to surface — it never embeds the
projects root, the sidecar path, or any secret value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from disco.core.release.spec import ReleaseIntent


class ReleaseIntentWriteError(Exception):
    """A typed, fail-closed failure from the host-owned release-intent writer.

    ``code`` is the model-facing tool error code (e.g. ``"invalid_projects_root"``,
    ``"owner_conversation_mismatch"``, ``"release_intent_persist_failed"``); the
    message is deliberately root/path/secret-free so surfacing it leaks nothing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# The narrow host-owned capability injected into ``ToolContext``. It persists ONE
# conversation's typed release intent under the ACTIVE configured projects root and
# raises :class:`ReleaseIntentWriteError` (persisting zero bytes) on an
# invalid/unavailable root, an owner/conversation mismatch, or a persistence
# failure. Arguments: ``(conversation_id, caller_owner_id, intent)``.
ReleaseIntentWriter = Callable[[str, str, ReleaseIntent], Awaitable[None]]


__all__ = ["ReleaseIntentWriteError", "ReleaseIntentWriter"]
