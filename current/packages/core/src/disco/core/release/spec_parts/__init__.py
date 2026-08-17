"""Private implementation parts extracted from :mod:`disco.core.release.spec`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``spec.py`` module. Nothing here is part of the public API: ``spec.py``
remains the sole state-free public compatibility/export facade and re-imports
these names. External callers must never import from ``spec_parts`` directly.
"""
