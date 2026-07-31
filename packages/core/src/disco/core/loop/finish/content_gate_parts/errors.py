"""Shared exception for incomplete dictated-content inspection.

A leaf module: every other `content_gate_parts` module, plus `content_gates.py`
itself, imports this type from here rather than from each other, so the
package has a single acyclic dependency direction (`content_gates.py` ->
`content_gate_parts.*`, never the reverse).
"""

from __future__ import annotations


class _DictatedContentInspectionIncomplete(RuntimeError):
    """The gate could not inspect the declared app scope completely and safely."""
