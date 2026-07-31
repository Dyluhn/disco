"""Private implementation parts extracted from :mod:`disco.core.release.local_compose`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``local_compose.py`` module. Nothing here is part of the public API:
``local_compose.py`` remains the sole state-free public compatibility/export
facade and re-imports these names. External callers must never import from
``local_compose_parts`` directly.
"""
