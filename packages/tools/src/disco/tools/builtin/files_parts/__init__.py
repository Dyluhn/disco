"""Private implementation parts extracted from :mod:`disco.tools.builtin.files`.

This subpackage holds the cohesive private helpers and tool implementations that
previously lived in the single ``files.py`` module. Nothing here is part of the
public API: ``files.py`` remains the sole state-free public compatibility/export
facade and re-imports these names. External callers must never import from
``files_parts`` directly.
"""
