"""Private implementation parts extracted from :mod:`disco.tools.builtin.image_gen`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``image_gen.py`` module. Nothing here is part of the public API:
``image_gen.py`` remains the sole state-free public compatibility/export facade
and re-imports these names. External callers must never import from
``image_gen_parts`` directly.
"""
