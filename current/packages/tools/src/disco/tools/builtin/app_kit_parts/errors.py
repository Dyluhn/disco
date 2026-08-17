"""Shared model-facing failure type for the AppKit tool family.

Every AppKit tool's ``run``/`` _run`` catches this at its own boundary and maps
it to a failure :class:`~disco.tools.anatomy.ToolOutcome` — see
``disco.tools.builtin.app_kit`` for the mapping.
"""

from __future__ import annotations


class _AppKitError(Exception):
    """A model-facing failure inside an app-kit tool (mapped to a failure outcome)."""
