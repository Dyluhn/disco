"""Private projection/render helpers for the AppKit regression dashboard.

This package holds the cohesive pure helpers extracted from
``appkit_dashboard.py`` (PKG-08-VERIFY). It owns no state, no IO beyond what
the caller passes in, and no product/database authority — it is a pure
projection/render layer over already-written evidence dossiers.

The parent module re-exports the public surface; nothing here is part of the
public API.
"""

from __future__ import annotations

from .collect import RowProjectionPorts, build_row_for_dossier
from .render import render_row, render_rows_table, render_summary_line

__all__ = [
    "build_row_for_dossier",
    "RowProjectionPorts",
    "render_row",
    "render_rows_table",
    "render_summary_line",
]
