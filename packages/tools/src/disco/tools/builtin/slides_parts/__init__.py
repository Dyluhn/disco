"""Private implementation parts extracted from :mod:`disco.tools.builtin.slides`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``slides.py`` module, split by the real render-path seams:
  ``_c1_render``        -- the C1-deck-render path (renders an already-built C1
     Deck from the structured C2 pipeline via the C3 renderer).
  ``_marp_path``        -- the Marp-CLI-in-sandbox helpers.
  ``_markdown_fallback`` -- the dependency-free markdown→HTML renderer used
     when Marp is unavailable.
  ``_export_stamp``     -- reads a produced deck artifact back and derives
     ExportRenderFacts for the finish-gate truncation check.

Nothing here is part of the public API: ``slides.py`` remains the sole public
compatibility/export facade and re-imports these names. External callers must
never import from ``slides_parts`` directly.
"""
