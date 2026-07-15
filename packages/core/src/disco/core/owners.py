"""Shared owner identifiers."""

from __future__ import annotations

from .env import disco_env

DEFAULT_OWNER_ID = "local"


def install_owner_id() -> str:
    """Owner used for legacy rows that predate persisted ownership.

    Missing-owner records are install-scoped, not session-scoped: they belong to
    the operator's original install owner and are only exposed through explicit
    admin gates at the server edge.
    """
    return disco_env("INSTALL_OWNER", DEFAULT_OWNER_ID).strip() or DEFAULT_OWNER_ID
