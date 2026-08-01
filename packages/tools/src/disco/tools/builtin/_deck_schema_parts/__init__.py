"""Private implementation parts extracted from :mod:`disco.tools.builtin._deck_schema`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``_deck_schema.py`` module. Nothing here is part of the public API:
``_deck_schema.py`` remains the sole state-free public compatibility/export facade
and re-imports these names. External callers must never import from
``_deck_schema_parts`` directly.
"""
