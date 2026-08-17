"""Brand CSS emitters — font-face declarations, CSS custom properties, print skeleton.

Three functions consumed by report_export.py (and the future deck renderer in tools):

  font_face_css()             — @font-face blocks embedding bundled TTFs as
                                base64 data-URIs so they work both in browsers
                                served over http (file:// is blocked by CORS)
                                and in WeasyPrint (which rejects file:// from
                                an http context and rejects weight-range syntax).

  theme_css_vars(theme)       — :root { --var: value; … } block directly from a
                                Theme object.  Same var names as the evidence CSS
                                so the mockup rules drop in unchanged.

  print_skeleton_css()        — @page cover/header/footer, numbered section
                                headers, .chip citations, follow-up page,
                                sources appendix (2-col when >30), TOC block
                                (when >10 sections), page-break hygiene.
                                Consumes only --var custom properties so the
                                same skeleton works in any registered theme.

WeasyPrint drop-cap constraint (LOCKED):
  Do NOT use ``p::first-letter { float: left }`` — WeasyPrint asserts on a
  floated ::first-letter pseudo-element.  The dropcap is an inline
  ``<span class="dropcap">`` glyph emitted in _build_pdf_html instead.
"""

from __future__ import annotations

import base64
import importlib.resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tokens import Theme


def _font_dir() -> Path:
    """Return the absolute path to the bundled fonts directory."""
    pkg = importlib.resources.files("disco.core.brand")
    # `files()` returns a Traversable; resolve to a real filesystem path.
    # The package is installed in src-layout (editable) so the path is always
    # a real directory, not a zip.  We convert to Path for url-encoding.
    return Path(str(pkg / "fonts"))


def _b64_font(name: str) -> str:
    """Return a base64-encoded data-URI for a bundled TTF file.

    Cached at module load time (populated lazily on first call to
    font_face_css()) so repeated CSS builds don't re-read the files.
    """
    return _FONT_B64_CACHE[name]


# Lazily populated on first call — avoids reading 5 TTFs at import time while
# still amortising the I/O across the process lifetime.
_FONT_B64_CACHE: dict[str, str] = {}

_FONT_FILES = [
    "Fraunces.ttf",
    "FrauncesItalic.ttf",
    "SchibstedGrotesk.ttf",
    "Newsreader.ttf",
    "NewsreaderItalic.ttf",
]


def _ensure_font_cache() -> None:
    """Populate _FONT_B64_CACHE if not already done."""
    if _FONT_B64_CACHE:
        return
    d = _font_dir()
    for fname in _FONT_FILES:
        raw = (d / fname).read_bytes()
        _FONT_B64_CACHE[fname] = "data:font/ttf;base64," + base64.b64encode(raw).decode("ascii")


def font_face_css() -> str:
    """Emit @font-face blocks embedding the bundled OFL TTFs as base64 data-URIs.

    Each font is encoded once (module-level cache) and emitted as:
        src: url('data:font/ttf;base64,<...>') format('truetype')

    This approach works for:
      - Browsers served over http/https — file:// src is blocked by browsers
        when the page is served over http (mixed-content / CORS restriction).
      - WeasyPrint — file:// paths are rejected when WeasyPrint's base_url is
        an http URL, and WeasyPrint also rejects the variable-font weight-range
        syntax ``font-weight: 100 900``.

    Single-weight values per face (not ranges) are used because WeasyPrint
    only accepts a single numeric value in @font-face font-weight.
    The font-family names match the Theme.font_* stacks.

    NOTE — PPTX: python-pptx only stores the font *name* in the slide XML;
    PPTX viewers need the font installed on the host OS to render it correctly.
    The data-URI approach here only affects HTML/PDF output.
    """
    _ensure_font_cache()

    def _uri(name: str) -> str:
        return _FONT_B64_CACHE[name]

    # font-display:swap — render the fallback font IMMEDIATELY, then swap to the brand
    # face once the (large, base64-inlined) TTF decodes. Without it the browser default
    # is `block`: the deck text is INVISIBLE for up to ~3s while the font loads (FOIT) —
    # the slide/editor/preview looks textless on every open. swap shows text instantly.
    return (
        f"@font-face{{font-family:'Fraunces';font-weight:400;font-display:swap;"
        f"src:url('{_uri('Fraunces.ttf')}') format('truetype');}}\n"
        f"@font-face{{font-family:'Fraunces';font-weight:400;font-style:italic;font-display:swap;"
        f"src:url('{_uri('FrauncesItalic.ttf')}') format('truetype');}}\n"
        f"@font-face{{font-family:'Schibsted Grotesk';font-weight:400;font-display:swap;"
        f"src:url('{_uri('SchibstedGrotesk.ttf')}') format('truetype');}}\n"
        f"@font-face{{font-family:'Newsreader';font-weight:400;font-display:swap;"
        f"src:url('{_uri('Newsreader.ttf')}') format('truetype');}}\n"
        f"@font-face{{font-family:'Newsreader';font-weight:400;font-style:italic;font-display:swap;"
        f"src:url('{_uri('NewsreaderItalic.ttf')}') format('truetype');}}\n"
    )


def theme_css_vars(theme: Theme) -> str:  # noqa: F821 — imported at call site
    """Emit a ``:root { --var: value; }`` block from a Theme object.

    Var names match the evidence CSS and the frontend theme.css so mockup
    rules can be copied verbatim into the skeleton without translation.
    When ``theme.branded`` is False, ``--accent`` is set to ``--text`` colour
    (no chroma) per the neutral-parity spec.
    """

    t: Theme = theme  # type narrowing
    # Accent rationing: neutral theme uses text-muted for accent (no chroma).
    eff_accent = t.accent if t.branded else t.text_muted
    eff_link = t.link if t.branded else t.text_muted

    return f"""\
:root{{
  --bg:{t.bg}; --surface-1:{t.surface_1}; --surface-2:{t.surface_2};
  --hairline:{t.hairline}; --hairline-strong:{t.hairline_strong};
  --text:{t.text}; --text-muted:{t.text_muted}; --text-faint:{t.text_faint};
  --accent:{eff_accent}; --link:{eff_link};
  --verify-supported:{t.verify_supported};
  --verify-weak:{t.verify_weak};
  --verify-unsupported:{t.verify_unsupported};
  --warn:{t.warn};
  --display:{t.font_display};
  --ui:{t.font_ui};
  --reading:{t.font_reading};
  --mono:{t.font_mono};
}}
*{{box-sizing:border-box;}}
html,body{{margin:0;padding:0;background:var(--bg);color:var(--text);}}
"""


# Static print/render skeleton (cover, running header/footer, numbered sections,
# citation chips, follow-up page, sources appendix). Hoisted to a module constant
# so the emitter stays a thin accessor (a data blob is not function logic).
_PRINT_SKELETON_CSS = """\
/* ---- page geometry ---- */
@page{
  size:A4;
  margin:1in 1in 1in 1in;
  /* Paint the FULL sheet (incl. the margin box) — WeasyPrint only propagates the
     html/body background to the content area, leaving white margins. Without this
     a dark-mode PDF is a dark rectangle floating on white paper (and the
     header/footer in the margins render light-on-white = invisible). */
  background:var(--bg);
  @top-center{
    content:element(running-header);
    font-family:var(--ui);
    font-size:7.5pt;
    color:var(--text-faint);
  }
  @bottom-center{
    content:counter(page);
    font-family:var(--ui);
    font-size:7.5pt;
    color:var(--accent);
  }
}
#running-header{
  position:running(running-header);
  display:flex;
  justify-content:space-between;
  align-items:baseline;
}
.running-wordmark{
  font-family:var(--display);
  font-weight:600;
  font-size:8pt;
  letter-spacing:-.01em;
}
.running-wordmark .dot{color:var(--accent);}

/* ---- cover page ---- */
.cover-page{
  page-break-after:always;
  break-after:page;
  min-height:90vh;
  display:flex;
  flex-direction:column;
  justify-content:flex-start;
  padding-top:1.5in;
}
.cover-rule{
  width:2.5in;
  height:3px;
  background:var(--accent);
  margin-bottom:0.5in;
}
.cover-title{
  font-family:var(--display);
  font-weight:600;
  font-variation-settings:'opsz' 60,'SOFT' 0,'WONK' 0;
  font-size:26pt;
  line-height:1.1;
  color:var(--text);
  letter-spacing:-.014em;
  margin:0 0 0.25in 0;
}
.dropcap{
  font-family:var(--display);
  font-weight:700;
  font-size:3em;
  line-height:0.8;
  color:var(--accent);
  display:inline;
  margin-right:0.04em;
  vertical-align:-0.1em;
}
.cover-subtitle{
  font-family:var(--reading);
  font-style:italic;
  font-size:13pt;
  color:var(--text-muted);
  line-height:1.4;
  margin-bottom:0.5in;
}
.cover-meta{
  font-family:var(--ui);
  font-size:8pt;
  color:var(--text-faint);
  letter-spacing:.04em;
  border-top:1px solid var(--hairline);
  padding-top:0.18in;
  display:flex;
  gap:1.2em;
}
.cover-meta span{
  display:inline-flex;
  align-items:center;
  gap:.3em;
}
.cover-meta .meta-label{
  text-transform:uppercase;
  letter-spacing:.1em;
  font-weight:600;
  color:var(--accent);
  font-size:6.5pt;
}

/* ---- kicker / wordmark helpers (from _brand_common.css) ---- */
.kicker{
  font-family:var(--ui);
  font-weight:600;
  letter-spacing:.18em;
  text-transform:uppercase;
  color:var(--accent);
}
.wordmark{
  font-family:var(--display);
  font-weight:600;
  font-variation-settings:'opsz' 60,'SOFT' 0,'WONK' 0;
  letter-spacing:-.01em;
}
.wordmark .dot{color:var(--accent);}

/* ---- section headers ---- */
.section-no{
  font-family:var(--ui);
  font-weight:600;
  font-size:8.5pt;
  letter-spacing:.14em;
  text-transform:uppercase;
  color:var(--accent);
  margin-top:1.4em;
  margin-bottom:.2em;
  border-bottom:1px solid var(--hairline);
  padding-bottom:.18em;
}
h2.section-title{
  font-family:var(--display);
  font-weight:600;
  font-variation-settings:'opsz' 40,'SOFT' 0,'WONK' 0;
  font-size:16pt;
  line-height:1.15;
  color:var(--text);
  margin:.2em 0 .5em 0;
  letter-spacing:-.012em;
  break-after:avoid;
  page-break-after:avoid;
}
.section-heading{
  break-inside:avoid;
  break-after:avoid;
  page-break-after:avoid;
}
section{
  margin-bottom:1.4em;
}

/* ---- body text ---- */
p{
  font-family:var(--reading);
  font-size:11pt;
  line-height:1.55;
  color:var(--text);
  margin:.5em 0;
  widows:3;
  orphans:3;
}
strong{font-weight:700;}
em{font-style:italic;}
code{
  font-family:var(--mono);
  font-size:.88em;
  background:var(--surface-1);
  padding:.08em .28em;
  border-radius:3px;
}
pre{
  font-family:var(--mono);
  font-size:.85em;
  background:var(--surface-2);
  padding:.8em;
  border-radius:4px;
  white-space:pre-wrap;
  break-inside:avoid;
}

/* ---- citation chips (from _brand_common.css:29-33) ---- */
.chip{
  font-family:var(--ui);
  font-weight:600;
  font-size:.72em;
  color:var(--accent);
  background:rgba(64,119,163,0.11);
  border:1px solid rgba(64,119,163,0.34);
  border-radius:.32em;
  padding:.02em .34em;
  vertical-align:.06em;
  white-space:nowrap;
  break-inside:avoid;
}
.chip-unknown{color:var(--text-faint);background:transparent;border-color:var(--hairline);}

/* ---- charts (```chart fences rendered to inline SVG, table fallback) ---- */
.chart-figure{
  margin:.75em 0 .9em 0;
  padding:0;
  break-before:auto;
  break-inside:avoid;
  text-align:center;
}
.chart-figure svg{max-width:100%;height:auto;}
table.report-table{
  width:100%;
  border-collapse:collapse;
  border-spacing:0;
  margin:.55em 0 .8em 0;
  font-family:var(--ui);
  font-size:8.8pt;
  line-height:1.25;
  color:var(--text);
  break-inside:avoid;
  page-break-inside:avoid;
}
table.report-table thead{display:table-header-group;}
table.report-table th,
table.report-table td{
  padding:.18em .45em .2em 0;
  text-align:left;
  vertical-align:top;
  border:0;
  border-bottom:.5pt solid var(--hairline);
}
table.report-table th{
  font-weight:700;
  color:var(--text);
  background:transparent;
}
table.report-table th.empty{color:transparent;}
table.report-table tbody tr:last-child td{border-bottom:.5pt solid var(--hairline);}
.chart-table{
  width:100%;
  border-collapse:collapse;
  border-spacing:0;
  margin:.55em 0 .8em 0;
  font-family:var(--ui);
  font-size:8.8pt;
  line-height:1.25;
  break-inside:avoid;
}
.chart-table caption{
  font-weight:600;
  color:var(--text);
  margin-bottom:.4em;
  text-align:left;
}
.chart-table th,.chart-table td{
  border:0;
  border-bottom:.5pt solid var(--hairline);
  padding:.18em .45em .2em 0;
  text-align:left;
  vertical-align:top;
}
.chart-table th{background:transparent;color:var(--text);font-weight:700;}
.chart-raw{
  white-space:pre-wrap;
  font-family:var(--mono);
  font-size:8pt;
  color:var(--text-muted);
  background:var(--surface-1);
  padding:.6em;
  border-radius:4px;
}

/* ---- executive summary box ---- */
.exec-summary{
  font-family:var(--reading);
  font-size:11.5pt;
  line-height:1.55;
  color:var(--text);
  border-left:3px solid var(--accent);
  padding:.7em 1em;
  margin-bottom:1.2em;
  background:var(--surface-1);
}

/* ---- conflict note ---- */
.conflict-note{
  font-family:var(--ui);
  font-size:8.5pt;
  color:var(--text-muted);
  font-style:italic;
  margin:.3em 0 .5em 0;
}

/* ---- bounded-by honesty footer ---- */
.bounded-note{
  border:1px solid var(--hairline);
  border-left:3px solid var(--warn);
  padding:.5em .8em;
  font-family:var(--ui);
  font-size:8.5pt;
  color:var(--text-muted);
  margin:1.2em 0;
  break-inside:avoid;
}

/* ---- follow-up Q&A page ---- */
.followup-page{
  margin-top:1.2em;
}
.followup-head{
  break-inside:avoid;
  break-after:avoid;
  page-break-after:avoid;
}
.followup-heading{
  font-family:var(--display);
  font-weight:600;
  font-size:18pt;
  letter-spacing:-.012em;
  color:var(--text);
  margin-bottom:.4em;
  break-after:avoid;
  page-break-after:avoid;
}
.followup-rule{
  width:1.5in;
  height:2px;
  background:var(--accent);
  margin-bottom:.8em;
  break-after:avoid;
  page-break-after:avoid;
}
.followup-item{
  margin-bottom:1em;
  break-inside:auto;
}
.followup-q{
  font-family:var(--ui);
  font-weight:600;
  font-size:10pt;
  color:var(--accent);
  margin-bottom:.25em;
}
.followup-a{
  font-family:var(--reading);
  font-size:11pt;
  line-height:1.5;
  color:var(--text);
}

/* ---- TOC block (shown when sections > 10) ---- */
.toc-block{
  break-after:page;
  page-break-after:always;
}
.toc-heading{
  font-family:var(--ui);
  font-weight:600;
  letter-spacing:.14em;
  text-transform:uppercase;
  font-size:8pt;
  color:var(--accent);
  margin-bottom:.6em;
  border-bottom:1px solid var(--hairline);
  padding-bottom:.2em;
}
.toc-list{
  list-style:none;
  margin:0;
  padding:0;
}
.toc-list li{
  font-family:var(--ui);
  font-size:9.5pt;
  color:var(--text);
  padding:.25em 0;
  border-bottom:1px dotted var(--hairline);
  display:flex;
  justify-content:space-between;
}
.toc-list .toc-no{
  color:var(--accent);
  font-weight:600;
  margin-right:.5em;
  min-width:1.8em;
}

/* ---- sources appendix ---- */
.sources-appendix{
  break-before:page;
  page-break-before:always;
}
.sources-heading{
  font-family:var(--ui);
  font-weight:600;
  letter-spacing:.14em;
  text-transform:uppercase;
  font-size:8pt;
  color:var(--accent);
  margin-bottom:.6em;
  border-bottom:1px solid var(--hairline);
  padding-bottom:.2em;
  break-after:avoid;
  page-break-after:avoid;
}
.sources-list{
  list-style:none;
  margin:0;
  padding:0;
}
.sources-list li{
  font-family:var(--ui);
  font-size:8.5pt;
  color:var(--text-muted);
  padding:.3em 0;
  border-bottom:1px solid var(--surface-2);
  break-inside:avoid;
  widows:2;
  orphans:2;
}
.sources-list .src-id{
  color:var(--accent);
  font-weight:600;
  margin-right:.5em;
}
.sources-list a{
  color:var(--link);
  text-decoration:none;
}
/* 2-column layout when >30 sources */
.sources-2col .sources-list{
  columns:2;
  column-gap:1.2em;
}

/* ---- definition mark (from _definition.html:22-45) ---- */
.defmark{display:inline-block;}
.defmark .l1{display:flex;align-items:baseline;gap:.42em;flex-wrap:wrap;}
.defmark .hw{
  font-family:var(--display);
  font-weight:600;
  font-variation-settings:'opsz' 40,'SOFT' 0,'WONK' 0;
  font-size:2em;
  letter-spacing:-.014em;
  color:var(--text);
  line-height:.95;
}
.defmark .pos{
  font-family:var(--ui);
  font-weight:600;
  font-size:.62em;
  letter-spacing:.15em;
  text-transform:uppercase;
  color:var(--text-faint);
}
.defmark .pos .sep{color:var(--accent);font-weight:700;padding:0 .12em;}
.defmark .ipa{
  font-family:var(--ui);
  font-weight:400;
  font-size:.66em;
  letter-spacing:.02em;
  color:var(--text-faint);
}
.defmark .rule{
  position:relative;
  height:1px;
  background:var(--hairline);
  margin:.5em 0 .48em;
}
.defmark .rule i{
  position:absolute;
  left:0;
  top:-1px;
  width:1.5em;
  height:2px;
  background:var(--accent);
  border-radius:1px;
}
.defmark .gloss{
  font-family:var(--reading);
  font-style:italic;
  font-size:1.04em;
  line-height:1.38;
  color:var(--text-muted);
}
.defmark .root{
  font-family:var(--ui);
  font-weight:400;
  font-size:.7em;
  letter-spacing:.01em;
  color:var(--text-faint);
  margin-top:.5em;
}
.defmark .root em{
  font-family:var(--reading);
  font-style:italic;
  font-size:1.1em;
  color:var(--text-muted);
}
.s-lg{font-size:15pt;}
.s-md{font-size:10pt;}
.s-sm{font-size:7pt;}
body>*:last-child{
  break-after:auto;
  page-break-after:auto;
}
"""


def print_skeleton_css() -> str:
    """Print/render skeleton CSS — branded page layout without ::first-letter float.

    Provides:
      @page           — A4, 1in margins, running header (wordmark + page number)
                        and footer.
      Cover           — .cover-page: display title (Fraunces), subtitle
                        (Newsreader italic), accent rule, metadata row.
      Section headers — .section-no (Schibsted Grotesk + accent, ``02 — Title``).
      Chips           — .chip citation badges ported verbatim from
                        _brand_common.css:29-33.
      Dropcap         — .dropcap INLINE (NOT ::first-letter{float}) per WeasyPrint
                        constraint.
      Definition mark — .defmark * classes from _definition.html:22-45.
      Follow-up page  — .followup-page (break-before: page).
      Sources appendix— .sources-appendix; .sources-2col when >30 sources.
      TOC             — .toc-block when >10 sections.
      Page breaks     — break-inside: avoid on chips/metrics/source rows.
      Wordmark        — .wordmark / .kicker reused from _brand_common.css.
    """
    return _PRINT_SKELETON_CSS


# Avoid circular import — Theme is imported locally inside theme_css_vars
# so the module loads cleanly even before tokens.py is first imported.
if False:  # pragma: no cover
    from .tokens import Theme  # noqa: F401 — type annotation only
