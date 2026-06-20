"""disco.core.brand — Shared brand theme engine.

One token-based engine consumed by BOTH the report exporter (agent_server.
report_export) and the future deck renderer (tools.slides).  Lives in ``core``
(the lowest shared layer) so both consumers can import downward.

Public surface:

  Tokens
  ------
  Theme               — frozen dataclass of all brand tokens
  THEMES              — dict[(name, mode), Theme]
  resolve_theme(name, mode) → Theme   (raises ValueError on unknown name)

  CSS emitters
  ------------
  font_face_css()                 → str  @font-face blocks (file:// abs URLs)
  theme_css_vars(theme)           → str  :root { --var: value; … } block
  print_skeleton_css()            → str  Full print layout skeleton

  Mark partials
  -------------
  definition_mark_html(scale)     → str  HTML for the disco Latin mark
  wordmark_html()                 → str  HTML for the Disco. wordmark
"""

from .css import font_face_css, print_skeleton_css, theme_css_vars
from .mark import definition_mark_html, wordmark_html
from .tokens import (
    TEMPLATE_CATALOG,
    THEMES,
    TemplateInfo,
    Theme,
    is_valid_template,
    list_templates,
    parse_template_id,
    resolve_theme,
)

__all__ = [
    "Theme",
    "THEMES",
    "resolve_theme",
    "TemplateInfo",
    "TEMPLATE_CATALOG",
    "list_templates",
    "parse_template_id",
    "is_valid_template",
    "font_face_css",
    "theme_css_vars",
    "print_skeleton_css",
    "definition_mark_html",
    "wordmark_html",
]
