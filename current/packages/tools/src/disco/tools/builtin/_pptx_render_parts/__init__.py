"""Private implementation parts extracted from :mod:`disco.tools.builtin._pptx_render`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``_pptx_render.py`` module. Nothing here is part of the public API:
``_pptx_render.py`` remains the sole public compatibility/export facade and
re-imports these names. External callers must never import from
``_pptx_render_parts`` directly.
"""
