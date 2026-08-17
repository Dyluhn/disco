"""HTML/preview-rendering side -- the deck HTML document shell + legacy MinimalDeck HTML path.

Moved out of ``_pptx_render.py``. ``_render_html_c1`` was, pre-split, 180
logical lines because the entire CSS/JS/HTML document template lived inline in
its ``return`` statement (string content counts as logical source). The
template is now its own function (``_render_html_document``), whose own CSS
ruleset is further split into ``_deck_css`` / ``_deck_css_base`` /
``_deck_css_layouts`` -- every one of these is a pure extract-function move,
same characters, same escaped braces, same backslash line continuation in the
image-scrim gradient.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from disco.core.brand.tokens import Theme

from .._deck_schema import Deck, Slide
from ._geometry import _PIPELINE_GENERATOR_MARKER
from ._html_archetypes import _art_fallback_svg, _html_for_c1_slide
from ._slide_archetype_shared import _slide_archetype

if TYPE_CHECKING:
    from .._pptx_render import DeckSlide


def _brand_marks_html(slide: Slide, theme: Theme) -> str:
    """The default-template brand chrome for one HTML slide — the 'Disco.' wordmark
    (every slide) + the 'disco — Latin · verb' colophon on the cover/title slide.
    Mirrors _draw_brand_marks for PPTX. Empty when the theme is unbranded."""
    if not theme.branded:
        return ""
    wordmark = '<div class="brand-wordmark">Disco<span class="dot">.</span></div>'
    if slide.layout != "title":
        return wordmark
    colophon = (
        '<div class="brand-colophon">'
        '<div class="bc-l1"><span class="bc-hw">disco</span>'
        '<span class="bc-pos">Latin&nbsp;·&nbsp;verb</span>'
        '<span class="bc-ipa">/ˈdɪs.koː/</span></div>'
        '<div class="bc-rule"></div>'
        '<div class="bc-gloss">“I learn; I become acquainted with.”</div>'
        '<div class="bc-root">from <em>discere</em> — to learn</div>'
        "</div>"
    )
    return wordmark + colophon


def _deck_css_base() -> str:
    """First half of the <style> block's slide-deck CSS ruleset (shell/typography/
    big-number/quote styles) -- extracted verbatim, split from _deck_css only to
    stay under the callable-size cap; _deck_css_layouts holds the rest.
    """
    return """\
/* 16:9 slide deck shell */
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;display:flex;align-items:center;justify-content:center;
  min-height:100vh;overflow:hidden;}
.deck{position:relative;width:100vw;height:56.25vw;max-height:100vh;
  max-width:177.78vh;overflow:hidden;}
.slide{display:none;position:absolute;inset:0;width:100%;height:100%;
  padding:3% 4%;font-family:var(--ui);color:var(--text);}
.slide.active{display:flex;flex-direction:column;justify-content:center;}
/* layout-specific */
.slide-title-bar{width:20%;height:3px;background:var(--accent);margin-bottom:2%;}
.slide-title{font-family:var(--display);font-size:4vw;line-height:1.15;
  color:var(--text);margin-bottom:1%;}
.slide-subtitle{font-family:var(--reading);font-style:italic;font-size:2vw;
  color:var(--text-muted);}
.slide-heading{font-family:var(--ui);font-size:2.5vw;font-weight:700;
  color:var(--text);margin-bottom:1%;}
.slide-rule{width:100%;height:2px;background:var(--accent);margin-bottom:2%;}
.slide-bullets{list-style:none;padding-left:2%;}
.slide-bullets li{font-family:var(--reading);font-size:1.8vw;line-height:1.5;
  color:var(--text);margin-bottom:0.8%;padding-left:1.5%;
  border-left:3px solid var(--accent);}
.slide-section-kicker{font-family:var(--ui);font-size:0.9vw;font-weight:700;
  letter-spacing:.15em;text-transform:uppercase;color:var(--accent);
  margin-bottom:1%;}
.slide-section-title{font-family:var(--display);font-size:4.5vw;
  color:var(--text);line-height:1.1;}
.slide-kicker{font-family:var(--ui);font-size:0.95vw;font-weight:700;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:0;}
.slide-big-number-layout{height:100%;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;gap:2%;}
.slide-big-number-figure{font-family:var(--display);font-size:200px;line-height:.85;
  color:var(--accent);font-weight:800;}
.slide-big-number-support{font-family:var(--reading);font-size:2.1vw;line-height:1.25;
  color:var(--text);max-width:70%;}
.slide-quote-layout{position:relative;height:100%;display:flex;flex-direction:column;
  justify-content:center;padding:0 8% 0 12%;}
.slide-quote-mark{position:absolute;left:4%;top:14%;font-family:var(--display);
  font-size:12vw;line-height:1;color:var(--accent);font-weight:800;opacity:.9;}
.slide-quote-text{font-family:var(--reading);font-size:3.2vw;line-height:1.18;
  color:var(--text);font-style:italic;max-width:86%;}
.slide-quote-attribution{font-family:var(--ui);font-size:1.15vw;color:var(--text-muted);
  font-weight:700;margin-top:2%;}"""


def _deck_css_layouts() -> str:
    """Second half of the <style> block's slide-deck CSS ruleset (timeline/two-by-two/
    image/nav/table/brand-mark styles) -- extracted verbatim, the remainder of
    _deck_css_base's split.
    """
    return """\
.slide-timeline-layout{height:100%;display:flex;flex-direction:column;justify-content:center;}
.slide-timeline{position:relative;height:56%;margin:3% 5% 0;}
.slide-timeline-spine{position:absolute;left:8%;right:8%;top:50%;height:3px;
  background:var(--accent);}
.slide-timeline-node{position:absolute;top:50%;width:14px;height:14px;border-radius:50%;
  background:var(--accent);transform:translate(-50%,-50%);box-shadow:0 0 0 6px var(--bg);}
.slide-timeline-label{position:absolute;width:22%;transform:translateX(-50%);
  font-family:var(--reading);font-size:1.1vw;line-height:1.25;color:var(--text);text-align:center;}
.slide-timeline-label.above{bottom:55%;}
.slide-timeline-label.below{top:58%;}
.slide-timeline-label strong{display:block;font-family:var(--ui);font-size:.9vw;
  color:var(--accent);margin-bottom:.25em;}
.slide-two-by-two-layout{height:100%;display:flex;flex-direction:column;justify-content:center;}
.slide-two-by-two-grid{position:relative;display:grid;grid-template-columns:1fr 1fr;
  grid-template-rows:1fr 1fr;gap:0;border:1px solid var(--hairline);height:66%;
  margin:2% 8% 0;background:var(--surface-1);}
.slide-two-by-two-cell{display:flex;align-items:center;justify-content:center;
  padding:6%;border:1px solid var(--hairline);font-family:var(--reading);
  font-size:1.45vw;line-height:1.25;color:var(--text);text-align:center;font-weight:700;}
.slide-two-by-two-axis{position:absolute;font-family:var(--ui);font-size:.85vw;
  color:var(--accent);font-weight:800;text-transform:uppercase;letter-spacing:0;}
.slide-two-by-two-axis.x-end{right:0;bottom:-7%;}
.slide-two-by-two-axis.y-end{left:-8%;top:0;transform:rotate(-90deg);transform-origin:left top;}
.slide-full-image-wrap{position:relative;width:100%;height:100%;overflow:hidden;}
.slide-full-image-bg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;}
.slide-full-image-placeholder{position:absolute;inset:0;background:var(--surface-2);
  border:1px solid var(--hairline);display:flex;align-items:center;justify-content:center;
  color:var(--text-faint);font-family:var(--ui);font-size:1.2vw;}
.slide-image-scrim{position:absolute;inset:0;background:
  linear-gradient(90deg,rgba(0,0,0,.72) 0%,rgba(0,0,0,.52) 38%,\
rgba(0,0,0,.12) 72%,rgba(0,0,0,0) 100%);}
.slide-full-image-copy{position:absolute;left:5%;bottom:8%;max-width:68%;z-index:1;}
.slide-full-image-copy .slide-heading,
.slide-full-image-copy .slide-title{color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.35);}
.slide-full-image-copy .slide-subtitle{color:rgba(255,255,255,.86);}
.slide-full-image-copy .slide-bullets li{color:#fff;border-left-color:rgba(255,255,255,.72);
  text-shadow:0 1px 2px rgba(0,0,0,.35);}
/* nav controls */
.nav{position:fixed;bottom:1%;right:1%;display:flex;gap:.5em;z-index:10;}
.nav button{background:var(--surface-2);border:1px solid var(--hairline);
  color:var(--text-muted);font-family:var(--ui);font-size:.9em;
  padding:.3em .8em;border-radius:4px;cursor:pointer;}
.nav button:hover{background:var(--surface-1);}
.slide-counter{position:fixed;bottom:1%;left:1%;font-family:var(--ui);
  font-size:.8em;color:var(--text-faint);}
/* chart / table layouts (C8) */
.slide-chart{width:100%;margin-top:1%;overflow:hidden;}
.slide-chart svg{max-width:100%;height:auto;}
.slide-table-wrap{width:100%;overflow-x:auto;margin-top:1%;}
.slide-table{border-collapse:collapse;width:100%;font-family:var(--reading);font-size:1.4vw;}
.slide-table thead th{background:var(--accent);color:#fff;font-weight:bold;
  padding:.4em .6em;text-align:left;border:1px solid rgba(255,255,255,0.2);}
.slide-table tbody td{padding:.35em .6em;border:1px solid var(--hairline);
  color:var(--text);}
.slide-table tbody tr:nth-child(even){background:var(--surface-1);}
.slide-table-empty{font-family:var(--ui);color:var(--text-faint);font-size:1.2vw;margin-top:2%;}
/* default-template brand chrome (mirrors the PDF report) */
.brand-wordmark{position:absolute;top:3.5%;right:4%;font-family:var(--display);
  font-weight:600;font-size:1.15vw;color:var(--text-faint);letter-spacing:-.01em;
  pointer-events:none;}
.brand-wordmark .dot{color:var(--accent);}
.brand-colophon{position:absolute;left:4%;bottom:7%;max-width:60%;pointer-events:none;}
.brand-colophon .bc-rule{width:7%;height:2px;background:var(--accent);margin:.7vw 0;}
.bc-l1{display:flex;align-items:baseline;gap:.7vw;}
.bc-hw{font-family:var(--display);font-size:1.7vw;color:var(--text);}
.bc-pos{font-family:var(--ui);font-size:.78vw;letter-spacing:.14em;
  text-transform:uppercase;color:var(--text-faint);}
.bc-ipa{font-family:var(--mono);font-size:.78vw;color:var(--text-faint);}
.bc-gloss{font-family:var(--reading);font-style:italic;font-size:1.2vw;
  color:var(--text-muted);}
.bc-root{font-family:var(--ui);font-size:.8vw;color:var(--text-faint);margin-top:.3vw;}"""


def _deck_css() -> str:
    """The <style> block's slide-deck CSS ruleset -- extracted verbatim (same
    escaped braces, same text) out of _render_html_document's f-string so that
    function stays under the callable-size cap; split further, across
    _deck_css_base/_deck_css_layouts, since the ruleset alone still exceeded it.
    """
    return _deck_css_base() + "\n" + _deck_css_layouts()


def _render_html_document(title: str, font_css: str, vars_css: str, slides_joined: str) -> str:
    """The full self-contained 16:9 brand HTML document shell: DOCTYPE, head
    (font-face + theme CSS vars + slide-deck CSS), body (the joined per-slide
    <section> markup), and the nav/keyboard-navigation script.

    Extracted verbatim (same f-string, same escaped braces, same backslash line
    continuation in the image-scrim gradient) out of _render_html_c1 so that
    function stays under the callable-size cap -- this is pure extract-function,
    the returned bytes are unchanged. The CSS ruleset itself is further split
    into _deck_css (below) since it alone still exceeded the callable cap.
    """
    return f"""\
<!DOCTYPE html>
<!-- {_PIPELINE_GENERATOR_MARKER} v1 -->
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{font_css}
{vars_css}
{_deck_css()}
</style>
</head>
<body>
<div class="deck">
{slides_joined}
</div>
<div class="nav">
  <button id="btn-prev" aria-label="Previous slide">←</button>
  <button id="btn-next" aria-label="Next slide">→</button>
</div>
<div class="slide-counter" id="counter"></div>
<script>
(function(){{
  var slides = document.querySelectorAll('.slide');
  var cur = 0;
  function go(n) {{
    slides[cur].classList.remove('active');
    cur = Math.max(0, Math.min(slides.length - 1, n));
    slides[cur].classList.add('active');
    document.getElementById('counter').textContent =
      (cur + 1) + ' / ' + slides.length;
  }}
  document.getElementById('btn-prev').addEventListener('click', function(){{ go(cur - 1); }});
  document.getElementById('btn-next').addEventListener('click', function(){{ go(cur + 1); }});
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') go(cur + 1);
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') go(cur - 1);
  }});
  go(0);
}})();
</script>
</body>
</html>"""


def _render_html_c1(deck: Deck) -> str:  # noqa: C901
    """Render a C1 Deck to a self-contained 16:9 brand HTML string."""
    from disco.core.brand.css import font_face_css, theme_css_vars

    theme = deck.theme
    font_css = font_face_css()
    vars_css = theme_css_vars(theme)

    sections_html: list[str] = []
    for i, slide in enumerate(deck.slides):
        active = " active" if i == 0 else ""
        bg = theme.surface_1 if slide.layout == "section_header" else theme.bg
        archetype = _slide_archetype(slide)
        archetype_attr = f'data-archetype="{html.escape(archetype)}" ' if archetype else ""

        inner = _html_for_c1_slide(slide, theme, slide_idx=i)
        brand = _brand_marks_html(slide, theme)
        sections_html.append(
            f'<section class="slide{active}" id="slide-{i}" '
            f'data-slide-id="slide-{i}" '
            f'data-layout="{html.escape(slide.layout)}" '
            f"{archetype_attr}"
            f'style="background:{html.escape(bg)}">\n{inner}\n{brand}\n</section>'
        )

    slides_joined = "\n".join(sections_html)

    return _render_html_document(deck.title, font_css, vars_css, slides_joined)


# ---------------------------------------------------------------------------
# strip_element_ids -- pure helper: remove editor-only data-* attributes
# ---------------------------------------------------------------------------
# Compiled once at import time.  Each pattern matches the attribute AND the
# single space that separates it from the previous attribute in a tag -- the
# leading `\s*` consumes that one space so no double-space gap is left
# behind.  All other whitespace, text, CSS, JS, and preformatted content is
# preserved byte-for-byte.
# Match the two attributes (with their separating leading whitespace) ONLY as
# start-tag attributes -- applied per-tag, never to free text / CSS / JS / <pre>.
_TAG_RE = re.compile(r"<[^>]+>")
_STRIP_ELEMENT_ID_RE = re.compile(r'\s+data-element-id="[^"]*"')
_STRIP_SLIDE_ID_RE = re.compile(r'\s+data-slide-id="[^"]*"')


def strip_element_ids(html_str: str) -> str:
    """Strip ``data-element-id="..."`` and ``data-slide-id="..."`` from *html_str*.

    Pure helper — removes those two attributes ONLY where they appear as start-tag
    attributes (the leading whitespace separating them from the previous attribute
    is consumed so no double space is left).  Text, CSS, ``<style>``/``<script>``
    content, and ``<pre>`` blocks are preserved byte-for-byte even if they happen
    to contain the literal attribute strings — removal is scoped to ``<...>`` tags
    only.  Idempotent: a second call is a no-op.

    Note: RESTORED here as a tested utility but NOT yet wired to any export path.
    ``render_html`` still stamps the attributes so the §4.1 in-preview
    SelectionOverlay can read them; callers needing a clean downloadable variant
    must call this explicitly.
    """

    def _clean_tag(m: re.Match[str]) -> str:
        tag = _STRIP_ELEMENT_ID_RE.sub("", m.group(0))
        return _STRIP_SLIDE_ID_RE.sub("", tag)

    return _TAG_RE.sub(_clean_tag, html_str)


def _html_for_slide(slide: DeckSlide, theme: Theme) -> str:
    """Return the inner HTML markup for one slide, keyed on layout."""
    title_esc = html.escape(slide.title)

    if slide.layout == "title":
        subtitle = ""
        if slide.bullets:
            subtitle = f'<p class="slide-subtitle">{html.escape(slide.bullets[0])}</p>'
        return (
            f'<div class="slide-title-bar"></div>\n'
            f'<h1 class="slide-title">{title_esc}</h1>\n'
            f"{subtitle}"
        )

    if slide.layout == "section":
        subtitle = ""
        if slide.bullets:
            subtitle = (
                f'<p class="slide-subtitle" style="margin-top:1.5%">'
                f"{html.escape(slide.bullets[0])}</p>"
            )
        return (
            f'<p class="slide-section-kicker">Section</p>\n'
            f'<div class="slide-title-bar"></div>\n'
            f'<h2 class="slide-section-title">{title_esc}</h2>\n'
            f"{subtitle}"
        )

    if slide.layout == "image_right":
        bullets_html = _bullets_html(slide.bullets)
        img_html = ""
        if slide.image_url:
            img_html = (
                f'<img src="{html.escape(slide.image_url)}" '
                f'alt="" style="max-width:48%;max-height:90%;object-fit:contain;">'
            )
        else:
            # Themed art fallback (not a dead '[image]' box) — side slot.
            img_html = _art_fallback_svg(
                1, style="width:46%;height:80%;border:1px solid var(--hairline);"
            )
        return (
            f'<div style="display:flex;gap:4%;width:100%;height:100%;'
            f'align-items:center;">'
            f'<div style="flex:1;display:flex;flex-direction:column;">'
            f'<h2 class="slide-heading">{title_esc}</h2>'
            f'<div class="slide-rule"></div>'
            f"{bullets_html}</div>"
            f"{img_html}</div>"
        )

    # Default: bullets layout
    bullets_html = _bullets_html(slide.bullets)
    return (
        f'<h2 class="slide-heading">{title_esc}</h2>\n'
        f'<div class="slide-rule"></div>\n'
        f"{bullets_html}"
    )


def _bullets_html(bullets: list[str]) -> str:
    """Render a bullet list as HTML; returns empty string when no bullets."""
    if not bullets:
        return ""
    items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    return f'<ul class="slide-bullets">{items}</ul>'
