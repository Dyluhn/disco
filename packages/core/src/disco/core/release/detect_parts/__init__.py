"""Private implementation parts extracted from :mod:`disco.core.release.detect`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``detect.py`` module.  Nothing here is part of the public API:
``detect.py`` remains the sole state-free public compatibility/export facade
and re-imports these names.  External callers must never import from
``detect_parts`` directly.
"""
