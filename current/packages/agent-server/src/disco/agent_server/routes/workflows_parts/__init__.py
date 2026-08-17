"""Private implementation parts extracted from :mod:`disco.agent_server.routes.workflows`.

This subpackage holds cohesive private helpers that previously lived in the
single ``workflows.py`` module. Nothing here is part of the public API:
``workflows.py`` remains the sole route-factory facade (``make_workflows_router``)
and re-imports the entry point it needs. External callers must never import
from ``workflows_parts`` directly.
"""

from __future__ import annotations
