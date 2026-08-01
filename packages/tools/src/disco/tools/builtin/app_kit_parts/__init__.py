"""Private implementation parts extracted from :mod:`disco.tools.builtin.app_kit`.

This subpackage holds the cohesive private helpers and tool implementations that
previously lived in the single ``app_kit.py`` module. Nothing here is part of the
public API: ``app_kit.py`` remains the sole compatibility/export facade and
re-imports these names. External callers must never import from
``app_kit_parts`` directly.
"""
