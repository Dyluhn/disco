"""Private implementation parts extracted from :mod:`disco.core.appkit.records_primitive`.

This subpackage holds the cohesive private helpers (naming/ordering, D1 schema,
Drizzle schema, Worker route/handler, and Markdown emitters) that previously
lived in the single ``records_primitive.py`` module. Nothing here is part of
the public API: ``records_primitive.py`` remains the sole public-facing module
and re-imports every name defined here. External callers must never import
from ``records_primitive_parts`` directly.
"""
