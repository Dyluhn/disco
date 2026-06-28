"""Internal helpers for the context package — kept dependency-free.

Mirrors the id/timestamp helpers in ``disco.core.events`` so the context value
objects stay self-contained (no import of Event/Sandbox runtime types).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _mk_id(prefix: str) -> str:
    """Generate a stable, prefixed id, e.g. ``cxr_<hex>`` (prefix includes ``_``)."""
    return f"{prefix}{uuid.uuid4().hex}"
