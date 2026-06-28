"""disco.core.appkit — the AppSpec domain (P4).

An app is a structured AppSpec (sections + design + tweaks) rendered deterministically
to a self-contained index.html. The specialized mutation tools operate on the spec, not
on raw HTML, so edits stay small + semantic. Pure value objects + renderer.
"""

from __future__ import annotations

from .models import (
    DEFAULT_DESIGN,
    SECTION_KINDS,
    AppSection,
    AppSpec,
    render_html,
)

__all__ = [
    "DEFAULT_DESIGN",
    "SECTION_KINDS",
    "AppSection",
    "AppSpec",
    "render_html",
]
