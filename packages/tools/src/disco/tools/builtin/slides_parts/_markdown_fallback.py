"""The dependency-free markdown-fallback render path: used when only
``markdown`` is supplied (no ``goal``), or when the Marp CLI is unavailable in
the sandbox. ``_fallback_html`` produces a self-contained HTML deck without any
external renderer; ``_basic_md_to_html`` is its minimal CommonMark-to-HTML
converter, decomposed into one small "consume this block" helper per markdown
construct (code fence, heading, blockquote, list, paragraph) so the dispatcher
itself stays a flat, low-branching scan of the line stream.

Extracted from ``slides.py`` to reduce module complexity; the public facade
re-imports every name here unchanged.
"""

from __future__ import annotations

import html
import re
from textwrap import dedent

# Simple inline markup patterns applied BEFORE the html.escape pass.
# Order: protect code spans/spans first (they may contain other tokens),
# then block-level, then inline.
_FENCE_RE = re.compile(r"```(.*?)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

_MARP_MINIMAL_THEME = dedent("""\
/* Minimal built-in theme fallback */
section {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  font-size: 1.5em;
  padding: 2em;
  background: #fff;
  color: #222;
}
h1 { font-size: 2em; margin-bottom: 0.3em; }
h2 { font-size: 1.6em; margin-bottom: 0.3em; }
h3 { font-size: 1.3em; margin-bottom: 0.2em; }
code { background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }
pre { background: #f0f0f0; padding: 1em; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
ul, ol { text-align: left; padding-left: 1.5em; }
li { margin-bottom: 0.3em; }
blockquote {
  border-left: 4px solid #ccc; margin: 0.5em 0; padding: 0.3em 1em;
  color: #555; font-style: italic;
}
table { border-collapse: collapse; margin: 0.5em auto; }
th, td { border: 1px solid #ddd; padding: 0.4em 0.8em; text-align: left; }
th { background: #f5f5f5; }
img { max-width: 100%; height: auto; }
a { color: #0366d6; }
""")


def _inline_markdown_to_html(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links, images) to HTML.
    Safe to apply on already-HTML-escaped text — the regexes match literal
    markdown tokens that survive escaping."""
    text = _IMAGE_RE.sub(r'<img src="\2" alt="\1">', text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", text)
    return text


def _consume_code_fence(lines: list[str], i: int) -> tuple[str, int]:
    """Consume a fenced code block (```) starting at ``lines[i]``."""
    lang = lines[i][3:].strip()
    block_lines: list[str] = []
    i += 1
    while i < len(lines) and not lines[i].startswith("```"):
        block_lines.append(lines[i])
        i += 1
    code_html = html.escape("\n".join(block_lines))
    cls = f' class="language-{html.escape(lang)}"' if lang else ""
    i += 1  # skip closing ```
    return f"<pre><code{cls}>{code_html}</code></pre>", i


def _consume_heading(lines: list[str], i: int, match: re.Match[str]) -> tuple[str, int]:
    """Consume a heading (already matched by the caller) at ``lines[i]``."""
    level = min(len(match.group(1)), 6)
    text = _inline_markdown_to_html(html.escape(match.group(2)))
    return f"<h{level}>{text}</h{level}>", i + 1


def _consume_blockquote(lines: list[str], i: int) -> tuple[str, int]:
    """Consume a run of consecutive ``> `` blockquote lines starting at ``lines[i]``."""
    bq_lines: list[str] = []
    while i < len(lines) and lines[i].startswith("> "):
        bq_lines.append(lines[i][2:])
        i += 1
    bq_text = "<br>".join(_inline_markdown_to_html(html.escape(ln)) for ln in bq_lines)
    return f"<blockquote>{bq_text}</blockquote>", i


def _consume_unordered_list(lines: list[str], i: int) -> tuple[str, int]:
    """Consume a run of consecutive ``-``/``*``/``+`` list items starting at ``lines[i]``."""
    items = ["<ul>"]
    while i < len(lines) and re.match(r"^[-*+]\s+", lines[i]):
        text = _inline_markdown_to_html(html.escape(re.sub(r"^[-*+]\s+", "", lines[i])))
        items.append(f"<li>{text}</li>")
        i += 1
    items.append("</ul>")
    return "\n    ".join(items), i


def _consume_ordered_list(lines: list[str], i: int) -> tuple[str, int]:
    """Consume a run of consecutive ``1.`` list items starting at ``lines[i]``."""
    items = ["<ol>"]
    while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
        text = _inline_markdown_to_html(html.escape(re.sub(r"^\d+\.\s+", "", lines[i])))
        items.append(f"<li>{text}</li>")
        i += 1
    items.append("</ol>")
    return "\n    ".join(items), i


def _consume_paragraph(lines: list[str], i: int) -> tuple[str, int]:
    """Collect consecutive non-empty, non-special lines into one paragraph starting
    at ``lines[i]``. Returns an empty first element when nothing was collected."""
    para_lines: list[str] = []
    while (
        i < len(lines)
        and lines[i].strip()
        and not any(lines[i].startswith(p) for p in ("#", "```", "> ", "- ", "* ", "+ "))
        and not re.match(r"^\d+\.\s+", lines[i])
        and not re.match(r"^[-*]{3,}\s*$", lines[i])
    ):
        para_lines.append(lines[i])
        i += 1
    if not para_lines:
        return "", i
    text = "<br>".join(_inline_markdown_to_html(html.escape(ln)) for ln in para_lines)
    return f"<p>{text}</p>", i


def _basic_md_to_html(md: str) -> str:
    """Minimal CommonMark-to-HTML converter for the fallback renderer.
    Handles headings, code fences, lists, blockquotes, paragraphs, and
    inline formatting. Deliberately simple — full Marp rendering is preferred.

    Each block construct is recognized here (in the same priority order as the
    original single-pass scanner: fence, heading, blockquote, unordered list,
    ordered list, hr, blank, paragraph) and its consumption is delegated to a
    dedicated ``_consume_*`` helper so this dispatcher itself stays a flat scan."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            piece, i = _consume_code_fence(lines, i)
            out.append(piece)
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            piece, i = _consume_heading(lines, i, heading_match)
            out.append(piece)
            continue

        if line.startswith("> "):
            piece, i = _consume_blockquote(lines, i)
            out.append(piece)
            continue

        if re.match(r"^[-*+]\s+", line):
            piece, i = _consume_unordered_list(lines, i)
            out.append(piece)
            continue

        if re.match(r"^\d+\.\s+", line):
            piece, i = _consume_ordered_list(lines, i)
            out.append(piece)
            continue

        if re.match(r"^[-*]{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        if line.strip() == "":
            i += 1
            continue

        piece, i = _consume_paragraph(lines, i)
        if piece:
            out.append(piece)

    return "\n    ".join(out)


def _split_slides(markdown: str) -> list[str]:
    """Split markdown into slides on '---' separators. Handles leading/trailing
    separators and CRLF line endings. A leading YAML/Marp frontmatter block is
    stripped first so it is never counted or rendered as a slide (Codex P10-7: it
    otherwise inflated the declared slide count → false truncation at the gate)."""
    # Normalize line endings
    md = markdown.replace("\r\n", "\n")
    # Strip a leading Marp/YAML frontmatter block: `---` on the VERY first line (no
    # leading separator) whose body is NOT a markdown heading (so a real leading
    # `---\n# Slide\n---` separator+content is left intact — that guard is what the
    # empty-filtered test pins).
    md = re.sub(r"\A---\n(?!#).*?\n---[ \t]*(?:\n|\Z)", "", md, count=1, flags=re.DOTALL)
    # Split on \n---\n (slide separator on its own line)
    parts = re.split(r"\n---\n", md)
    # Filter empty slides
    return [p.strip() for p in parts if p.strip()]


def _fallback_html(markdown: str, theme: str | None = None) -> str:
    """Minimal self-contained HTML renderer. Splits on '---', wraps each
    slide in a <section> tag. Works without Marp — HTML always ships."""
    slides_raw = _split_slides(markdown)
    theme_css = theme or _MARP_MINIMAL_THEME

    sections = ""
    for i, slide_md in enumerate(slides_raw):
        html_body = _basic_md_to_html(slide_md)
        sections += f'  <section class="slide" id="slide-{i + 1}">\n    {html_body}\n  </section>\n'

    return dedent(
        f"""\
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Slide Deck</title>
      <style>
    {theme_css}
        body {{ margin: 0; padding: 0; }}
        .slide {{
          min-height: 100vh;
          box-sizing: border-box;
          display: flex;
          flex-direction: column;
          justify-content: center;
          page-break-after: always;
        }}
      </style>
    </head>
    <body>
    {sections}
    </body>
    </html>"""
    )
