"""EMU geometry constants — 16:9 canvas matching C1 spec (12 192 000 × 6 858 000 EMU).

Shared by every renderer in this package (native-pptx primitives/layouts/
archetypes, the C1 Element renderer, and the HTML shell's generator marker).
Moved verbatim out of ``_pptx_render.py`` — values are unchanged.
"""

from __future__ import annotations

_SLIDE_W = 12_192_000  # 13.33 inches
_SLIDE_H = 6_858_000  # 7.5 inches

# Margins / safe-area (0.5 in = 457 200 EMU)
_MARGIN = 457_200
_PIPELINE_GENERATOR_MARKER = "disco-slides-generate:pipeline-html"

# Content width / height (canvas minus symmetric margins)
_CW = _SLIDE_W - 2 * _MARGIN  # 11 277 600
_CH = _SLIDE_H - 2 * _MARGIN  # 5 943 600

# Title-strip height (≈15% of canvas)
_TITLE_H = 914_400  # ≈ 1 in

# Brand marks — ~0.055in accent dot on the wordmark
_BRAND_DOT = 50_000
