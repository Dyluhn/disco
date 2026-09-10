"""Private projection/context helpers for the Disco Operator.

This package holds the cohesive pure helpers extracted from
``operator.py`` (PKG-08-VERIFY) — the event-log projection logic that
``OperatorClient._gate_context`` and ``OperatorClient._derive_view`` used to
inline. It owns no state, no IO, and no network authority — it is a pure
projection over an event list the caller supplies.

The parent module re-exports nothing from here; the helpers are called
directly by the OperatorClient methods.
"""

from __future__ import annotations

from .gate_context import assemble_gate_context, scan_gate_events
from .view import fold_view_events

__all__ = [
    "assemble_gate_context",
    "fold_view_events",
    "scan_gate_events",
]