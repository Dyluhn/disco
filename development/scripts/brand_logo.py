#!/usr/bin/env python3
"""Generate the Disco logo assets in docs/assets/ from the brand fonts and tokens.

Everything is outlined to paths, so the SVGs render identically without the
fonts installed. Run from the repository root:

    uv run python development/scripts/brand_logo.py

Outputs: docs/assets/logo.svg, logo-dark.svg (the "Disco." wordmark) and
definition-mark.svg, definition-mark-dark.svg (the Latin definition lockup).
"""

from __future__ import annotations

import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
from disco.core.brand.tokens import THEMES  # noqa: E402

FONTS = ROOT / "packages" / "core" / "src" / "disco" / "core" / "brand" / "fonts"
OUT = ROOT / "docs" / "assets"


class Face:
    """One static instance of a brand font, ready to outline text."""

    def __init__(self, file: str, **axes: float) -> None:
        font = TTFont(FONTS / file)
        self.font = instantiateVariableFont(font, axes) if "fvar" in font else font
        self.upem = self.font["head"].unitsPerEm
        self.cmap = self.font.getBestCmap()
        self.glyphs = self.font.getGlyphSet()

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        scale = size / self.upem
        return sum(self.glyphs[self.cmap[ord(c)]].width * scale + tracking * size for c in text)

    def paths(
        self, text: str, x: float, y: float, size: float, fill: str, tracking: float = 0.0
    ) -> str:
        """SVG <path> elements for `text` with its baseline at (x, y)."""
        scale = size / self.upem
        out = []
        for c in text:
            name = self.cmap[ord(c)]
            pen = SVGPathPen(self.glyphs)
            self.glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x, y)))
            d = pen.getCommands()
            if d:
                out.append(f'<path fill="{fill}" d="{d}"/>')
            x += self.glyphs[name].width * scale + tracking * size
        return "\n".join(out)


def wordmark(theme, size: float = 96.0) -> str:
    """'Disco' in Fraunces 600 / opsz 60 with the accent dot — the in-app Wordmark."""
    face = Face("Fraunces.ttf", wght=600, opsz=60, SOFT=0, WONK=0)
    tracking = -0.01
    pad = size * 0.25
    w_text = face.width("Disco", size, tracking)
    w_dot = face.width(".", size)
    width, height = w_text + w_dot + 2 * pad, size * 1.3
    baseline = size * 0.95
    body = face.paths("Disco", pad, baseline, size, theme.text, tracking)
    body += "\n" + face.paths(".", pad + w_text, baseline, size, theme.accent)
    return svg(width, height, body, theme.bg, "Disco")


def definition_mark(theme, size: float = 40.0) -> str:
    """The Latin definition lockup: headword, part of speech, IPA, rule, gloss, root."""
    display = Face("Fraunces.ttf", wght=600, opsz=40, SOFT=0, WONK=0)
    ui = Face("SchibstedGrotesk.ttf", wght=600)
    ui_book = Face("SchibstedGrotesk.ttf", wght=400)
    reading_italic = Face("NewsreaderItalic.ttf", wght=400, opsz=18)
    reading = Face("Newsreader.ttf", wght=400, opsz=18)
    pad = size * 0.8
    x, y = pad, pad + size * 2 * 0.72
    parts = []
    hw = size * 2
    parts.append(display.paths("disco", x, y, hw, theme.text, -0.014))
    cx = x + display.width("disco", hw, -0.014) + size * 0.42
    pos = size * 0.62
    parts.append(ui.paths("LATIN", cx, y, pos, theme.text_faint, 0.15))
    cx += ui.width("LATIN", pos, 0.15) + pos * 0.2
    parts.append(ui.paths("·", cx, y, pos, theme.accent))
    cx += ui.width("·", pos) + pos * 0.35
    parts.append(ui.paths("VERB", cx, y, pos, theme.text_faint, 0.15))
    cx += ui.width("VERB", pos, 0.15) + size * 0.5
    ipa = size * 0.72
    ipa_w = ipa_paths(ui_book, parts, cx, y, ipa, theme.text_muted)
    width = (
        max(
            cx + ipa_w,
            x + reading_italic.width("“I learn; I become acquainted with.”", size * 0.95),
        )
        + pad
    )
    ry = y + size * 0.55
    parts.append(
        f'<rect x="{x}" y="{ry}" width="{width - x - pad}" height="1" fill="{theme.hairline}"/>'
    )
    parts.append(
        f'<rect x="{x}" y="{ry - 3}" width="{size * 0.9}" height="4" fill="{theme.accent}"/>'
    )
    gy = ry + size * 1.05
    parts.append(
        reading_italic.paths("“I learn; I become acquainted with.”", x, gy, size * 0.95, theme.text)
    )
    rooty = gy + size * 0.9
    root = size * 0.62
    parts.append(reading.paths("from ", x, rooty, root, theme.text_muted))
    rx = x + reading.width("from ", root)
    parts.append(reading_italic.paths("discere", rx, rooty, root, theme.text_muted))
    rx += reading_italic.width("discere", root)
    parts.append(reading.paths(" — to learn", rx, rooty, root, theme.text_muted))
    height = rooty + pad * 0.6
    return svg(
        width,
        height,
        "\n".join(parts),
        theme.bg,
        "disco — Latin, verb: I learn; I become acquainted with.",
    )


def ipa_paths(face: Face, parts: list[str], x: float, y: float, size: float, fill: str) -> float:
    """/ˈdɪs.koː/ from glyphs the brand fonts have: the stress mark is a drawn
    tick, the small-capital I is a reduced capital, the length mark a colon.
    Returns the advance."""
    x0 = x
    parts.append(face.paths("/", x, y, size, fill))
    x += face.width("/", size)
    parts.append(
        f'<rect x="{x + size * 0.05:.1f}" y="{y - size * 0.72:.1f}" '
        f'width="{size * 0.07:.1f}" height="{size * 0.24:.1f}" fill="{fill}"/>'
    )
    x += size * 0.22
    parts.append(face.paths("d", x, y, size, fill))
    x += face.width("d", size)
    parts.append(face.paths("I", x, y, size * 0.72, fill))
    x += face.width("I", size * 0.72) + size * 0.04
    parts.append(face.paths("s.ko", x, y, size, fill))
    x += face.width("s.ko", size)
    parts.append(face.paths(":/", x, y, size, fill))
    x += face.width(":/", size)
    return x - x0


def svg(width: float, height: float, body: str, bg: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.1f} {height:.1f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" aria-label="{title}">\n'
        f"<title>{title}</title>\n"
        f'<rect width="100%" height="100%" rx="{min(width, height) * 0.06:.1f}" fill="{bg}"/>\n'
        f"{body}\n</svg>\n"
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    light, dark = THEMES[("disco", "light")], THEMES[("disco", "dark")]
    files = {
        "logo.svg": wordmark(light),
        "logo-dark.svg": wordmark(dark),
        "definition-mark.svg": definition_mark(light),
        "definition-mark-dark.svg": definition_mark(dark),
    }
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT) / name} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
