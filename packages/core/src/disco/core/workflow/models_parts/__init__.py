"""Private implementation parts extracted from :mod:`disco.core.workflow.models`.

This subpackage holds cohesive private helpers that previously lived in the
single ``models.py`` module. Nothing here is part of the public API: ``models.py``
remains the sole public compatibility/export facade (itself re-exported through
``disco.core.workflow``) and re-imports these names. External callers must never
import from ``models_parts`` directly.
"""

from __future__ import annotations
