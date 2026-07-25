"""Live verification: the CORRECT-BUILD CORPUS for visible-text assertions.

Run it directly (needs a real Chromium, so it is not a unit test):
    TMPDIR=~/.local/state/disco-campaign-tmp uv run python \
        packages/tools/scripts/verify_visible_text_corpus.py

WHY THIS EXISTS. Three campaign defects in a row were one shape: an acceptance
check testing an incidental REPRESENTATION instead of the property it claims to
test — the filename, the whitespace between elements, the CSS paint method. Each
cost a ~2-hour counted stream to find, because the counted stream was the only
thing sampling enough model-styling diversity to hit it.

This enumerates the family directly. Every entry below is a CORRECT build: each
shows the required heading the way a competent build plausibly would (gradient
text, a heading split across a block-level span, an uppercase transform, a flex
row, a line break). Any entry the extractor cannot see is a verification artifact
that will fail a correct build in the counted stream. The cloaking negatives must
stay invisible, or a relaxation has opened a hole.

Seconds instead of hours. Exit code is 0 only when every corpus check holds.
"""

from __future__ import annotations

import base64
import importlib.resources
import sys

HEADING = "Corpus Seed 1234"

# Every entry renders the heading visibly. A human opening any of these pages
# reads "Corpus Seed 1234".
CORRECT: list[tuple[str, str]] = [
    ("plain h1", f"<h1>{HEADING}</h1>"),
    (
        "gradient text (webkit fill)",
        f'<h1 style="background:linear-gradient(135deg,#C65D3B,#E5A93C);'
        f"-webkit-background-clip:text;background-clip:text;"
        f'-webkit-text-fill-color:transparent">{HEADING}</h1>',
    ),
    (
        "gradient text (color)",
        f'<h1 style="background:linear-gradient(135deg,#C65D3B,#E5A93C);'
        f'-webkit-background-clip:text;background-clip:text;color:transparent">{HEADING}</h1>',
    ),
    ("split across a span", "<h1>Corpus Seed <span>1234</span></h1>"),
    (
        "split across a BLOCK span",
        '<h1>Corpus Seed <span style="display:block">1234</span></h1>',
    ),
    ("uppercase transform", f'<h1 style="text-transform:uppercase">{HEADING}</h1>'),
    ("nested emphasis", "<h1>Corpus <em>Seed</em> 1234</h1>"),
    ("line break inside", "<h1>Corpus Seed<br>1234</h1>"),
    (
        "flex row layout",
        '<h1 style="display:flex;gap:.5rem"><span>Corpus Seed</span><span>1234</span></h1>',
    ),
    ("aria-label sibling", f'<h1><span aria-hidden="true">✦</span> {HEADING}</h1>'),
    (
        "text-shadow / letter-spacing",
        f'<h1 style="text-shadow:0 2px 8px rgba(0,0,0,.2);letter-spacing:-.02em">{HEADING}</h1>',
    ),
    (
        "clamped font-size",
        f'<h1 style="font-size:clamp(2rem,5vw,4rem)">{HEADING}</h1>',
    ),
]

# These are genuinely invisible. They must NOT be extracted, or the anti-cloaking
# guarantee is gone.
CLOAKED: list[tuple[str, str]] = [
    ("transparent fill, no clip", '<p style="-webkit-text-fill-color:transparent">CLOAKED-A</p>'),
    ("transparent color, no clip", '<p style="color:transparent">CLOAKED-B</p>'),
    ("display none", '<p style="display:none">CLOAKED-C</p>'),
    ("visibility hidden", '<p style="visibility:hidden">CLOAKED-D</p>'),
    ("aria-hidden", '<p aria-hidden="true">CLOAKED-E</p>'),
    ("zero opacity", '<p style="opacity:0">CLOAKED-F</p>'),
]


def main() -> int:
    import disco.tools.builtin._browser_daemon as daemon_mod
    from disco.tools.builtin.browser import _installed_chromium_executable
    from playwright.sync_api import sync_playwright

    from harness.build_soak.oracles.output_truth import _content_normalized

    executable = _installed_chromium_executable()
    if executable is None:
        print("installed Chromium is required")
        return 2
    font = (
        importlib.resources.files("disco.core.brand")
        .joinpath("fonts/SchibstedGrotesk.ttf")
        .read_bytes()
    )
    font_b64 = base64.b64encode(font).decode("ascii")
    handler = daemon_mod.BrowserHandler.__new__(daemon_mod.BrowserHandler)

    load_font = """
        async b64 => {
            const bytes = atob(b64);
            const data = new Uint8Array(bytes.length);
            for (let i = 0; i < bytes.length; i++) data[i] = bytes.charCodeAt(i);
            const f = new FontFace('DiscoProbe', data.buffer);
            await f.load(); document.fonts.add(f); await document.fonts.ready;
        }
    """
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            print("--- correct builds (every one must satisfy the heading assertion) ---")
            for label, markup in CORRECT:
                page.set_content(f'<style>*{{font-family:"DiscoProbe"!important}}</style>{markup}')
                page.evaluate(load_font, font_b64)
                text = handler._visible_dom_text(page)
                ok = _content_normalized(HEADING) in _content_normalized(text)
                print(f"  {'ok    ' if ok else 'FAIL  '}{label:32s} extracted={text[:64]!r}")
                if not ok:
                    failures.append(f"correct build not satisfied: {label}")

            print("\n--- cloaked copy (every one must stay invisible) ---")
            for label, markup in CLOAKED:
                page.set_content(
                    f'<style>*{{font-family:"DiscoProbe"!important}}</style>{markup}<p>anchor</p>'
                )
                page.evaluate(load_font, font_b64)
                text = handler._visible_dom_text(page)
                leaked = "CLOAKED" in text
                print(f"  {'FAIL  ' if leaked else 'ok    '}{label:32s} extracted={text[:64]!r}")
                if leaked:
                    failures.append(f"cloaked copy leaked: {label}")
        finally:
            browser.close()

    print(
        f"\n{len(CORRECT) + len(CLOAKED) - len(failures)}/"
        f"{len(CORRECT) + len(CLOAKED)} corpus checks hold"
    )
    for f in failures:
        print("  ->", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
