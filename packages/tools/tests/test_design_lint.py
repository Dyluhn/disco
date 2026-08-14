"""EPIC D3 — design_lint slop-scanner tests.

Covers, via BOTH the pure engine and the tool interface (fake sandbox/workspace):
  * a GOLDEN SLOP sample (AI-purple + Inter + gradient hero + centered-hero/
    3-cards/CTA + pill buttons + emoji) flags the expected ranked rule_ids;
  * a JUSTIFIED non-default (purple/gradient/Inter justified in designspec.json)
    SUPPRESSES exactly those findings;
  * a CLEAN recipe-based sample passes;
  * a MISSING and an INVALID DesignSpec → the relevant rules fire (intent can't
    be claimed without a valid spec);
  * the tool returns the structured verdict through the real tool interface.
"""

from __future__ import annotations

import json
import posixpath
import shlex

import pytest
from disco.core.appkit import RECIPES
from disco.core.appkit.spec import DesignSpec, Justification, Palette, Typography
from disco.core.design import DIRECTION_BY_ID, direction_tokens_css, render_design_direction
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import design_lint as design_lint_module
from disco.tools.builtin.design_lint import DesignLintArgs, DesignLintTool, lint_design

# --- samples -----------------------------------------------------------------

SLOP_CSS = """
:root { --primary: #7c3aed; --brand: #8b5cf6; }
body { font-family: Inter, system-ui, -apple-system, sans-serif; }
.hero h1 {
  background: linear-gradient(90deg, #7c3aed, #ec4899);
  -webkit-background-clip: text;
  color: transparent;
}
.btn { border-radius: 9999px; }
.button { border-radius: 999px; }
"""

SLOP_HTML = """<!doctype html>
<html><body>
<section class="hero text-center"><h1>Welcome to the Future</h1>
  <a class="btn rounded-full" href="#">Get started</a></section>
<div class="features grid grid-cols-3">
  <div class="card">Fast ⚡</div>
  <div class="card">Secure 🔒</div>
  <div class="card">Smart 🚀</div>
</div>
<section class="cta"><h2>Ready?</h2><a class="button" href="#">Sign up</a> ✨ 🎉 💡</section>
</body></html>
"""

EXPECTED_SLOP_RULES = {
    "ai_purple",
    "generic_font",
    "gradient_hero_text",
    "centered_hero_3_cards_cta",
    "pill_button_monoculture",
    "emoji_as_icons",
}


def _justified_spec() -> DesignSpec:
    """A spec that DECLARES Inter (heading) + BOTH violets it uses (primary
    #7c3aed, accent #8b5cf6) and JUSTIFIES the deliberate off-defaults:
    typography.heading, palette.primary, palette.accent, effects.gradient_text.

    Value-aware suppression matches the FLAGGED color to a DECLARED palette role,
    so the spec must actually carry every off-default value it wants excused — it
    declares #7c3aed AND #8b5cf6 because the SLOP sample uses both."""
    return DesignSpec(
        schema_version=1,
        typography=Typography(heading_font="Inter", body_font="Georgia"),
        palette=Palette(primary="#7c3aed", surface="#ffffff", text="#111111", accent="#8b5cf6"),
        layout_family="centered",
        component_style="flat",
        density="comfortable",
        justifications=(
            Justification(
                choice="typography.heading",
                reason="Inter is the established brand display face mandated by the style guide.",
            ),
            Justification(
                choice="palette.primary",
                reason="Violet #7c3aed is the registered brand primary for this product.",
            ),
            Justification(
                choice="palette.accent",
                reason="Violet #8b5cf6 is the registered brand accent paired with the primary.",
            ),
            Justification(
                choice="effects.gradient_text",
                reason="The gradient wordmark is the brand's signature hero treatment.",
            ),
        ),
    )


# --- fake sandbox/workspace --------------------------------------------------


class _ExecResult:
    """Minimal stand-in for the sandbox ExecResult (exit_code/stdout/stderr/timed_out)."""

    def __init__(
        self, *, exit_code: int, stdout: str = "", stderr: str = "", timed_out: bool = False
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class _FakeSandbox:
    """In-memory workspace: maps POSIX relative paths -> bytes and synthesizes
    list_dir from the key set. Enough of the SandboxInstance surface for the
    read-only design_lint walk (exec_shell size-probe / read_file / list_dir /
    file_exists). `sizes` overrides the reported byte size for a path WITHOUT
    storing the bytes — so a multi-GB hostile file can be simulated cheaply and we
    can prove `read_file` is never called for an over-cap file."""

    def __init__(self, files: dict[str, bytes], *, sizes: dict[str, int] | None = None) -> None:
        self._files = {posixpath.normpath(k): v for k, v in files.items()}
        self._sizes = {posixpath.normpath(k): v for k, v in (sizes or {}).items()}
        self.read_paths: list[str] = []  # every read_file(key) call, in order

    @staticmethod
    def _norm(path: str) -> str:
        p = path.strip().lstrip("/")
        p = p.removeprefix("workspace/")
        return posixpath.normpath(p or ".")

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> _ExecResult:
        # design_lint's size probe is `wc -c < <shlex-quoted path>`. Parse the path
        # back out and report the file's byte size (from `sizes` override or len).
        try:
            _, _, rhs = cmd.partition("<")
            path = shlex.split(rhs.strip())[0]
        except (ValueError, IndexError):
            return _ExecResult(exit_code=1, stderr="bad probe")
        key = self._norm(path)
        if key in self._sizes:
            return _ExecResult(exit_code=0, stdout=f"{self._sizes[key]}\n")
        if key in self._files:
            return _ExecResult(exit_code=0, stdout=f"{len(self._files[key])}\n")
        return _ExecResult(exit_code=1, stderr="no such file")

    async def list_dir(self, path: str) -> list[str]:
        base = self._norm(path)
        prefix = "" if base == "." else base + "/"
        children: set[str] = set()
        matched_any = False
        for key in self._files:
            if base != "." and key == base:
                # a file, not a dir → the real backend raises NotADirectory
                raise NotADirectoryError(path)
            if base == "." or key.startswith(prefix):
                matched_any = True
                rest = key if base == "." else key[len(prefix) :]
                children.add(rest.split("/", 1)[0])
        if not matched_any and base != ".":
            raise FileNotFoundError(path)
        return sorted(children)

    async def read_file(self, path: str) -> bytes:
        key = self._norm(path)
        self.read_paths.append(key)
        if key not in self._files:
            raise FileNotFoundError(path)
        return self._files[key]

    async def file_exists(self, path: str) -> bool:
        return self._norm(path) in self._files


def _ctx(sbx: _FakeSandbox) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-design-lint",
    )


# --- pure engine: golden slop ------------------------------------------------


def test_golden_slop_flags_expected_ranked_rules():
    v = lint_design(
        {"styles.css": SLOP_CSS, "index.html": SLOP_HTML},
        None,
        spec_present=False,
        spec_valid=False,
    )
    assert v["ok"] is False
    rule_ids = [f["rule_id"] for f in v["findings"]]
    assert EXPECTED_SLOP_RULES <= set(rule_ids)
    # ranked: the error (ai_purple) sorts ahead of every warning/info
    assert rule_ids[0] == "ai_purple"
    assert v["counts"]["error"] >= 1
    # findings carry the full structured shape
    required = {"rule_id", "severity", "path", "line", "evidence", "choice_key", "message"}
    for f in v["findings"]:
        assert set(f) >= required


# --- pure engine: justified suppression --------------------------------------


def test_justified_designspec_suppresses_those_findings():
    spec = _justified_spec()
    v = lint_design({"styles.css": SLOP_CSS}, spec, spec_present=True, spec_valid=True)
    fired = {f["rule_id"] for f in v["findings"]}
    # the three justified tells are gone
    assert "ai_purple" not in fired
    assert "gradient_hero_text" not in fired
    assert "generic_font" not in fired


def test_justified_only_suppresses_the_declared_font():
    """generic_font suppression is a FIELD+VALUE match: justifying typography.heading
    for Inter does NOT excuse a DIFFERENT generic font (Roboto) appearing too."""
    spec = _justified_spec()  # declares Inter, not Roboto
    css = "body { font-family: Inter; }\nh1 { font-family: Roboto; }\n"
    v = lint_design({"s.css": css}, spec, spec_present=True, spec_valid=True)
    fired = {(f["rule_id"], f["evidence"]) for f in v["findings"]}
    assert any(r == "generic_font" and "roboto" in ev.lower() for r, ev in fired)
    assert not any(r == "generic_font" and "inter" in ev.lower() for r, ev in fired)


# --- P1-1: VALUE-AWARE suppression (justified value must match the flagged one) -


def _spec_with(
    *, primary: str, accent: str | None, heading: str, body: str, justify: tuple[str, ...]
) -> DesignSpec:
    """A DesignSpec declaring the given palette/typography, justifying exactly the
    canonical `choice` keys in `justify` (each with a substantive reason)."""
    return DesignSpec(
        schema_version=1,
        typography=Typography(heading_font=heading, body_font=body),
        palette=Palette(primary=primary, surface="#ffffff", text="#111111", accent=accent),
        layout_family="editorial",
        component_style="flat",
        density="comfortable",
        justifications=tuple(
            Justification(
                choice=c,
                reason=f"{c} is a deliberate, registered brand choice for this product.",
            )
            for c in justify
        ),
    )


def test_justified_primary_for_a_different_color_does_not_suppress_purple():
    """The CRITICAL P1: a spec that justifies palette.primary=#000000 must NOT
    suppress a #7c3aed ai_purple finding — the justified value is not the flagged one."""
    spec = _spec_with(
        primary="#000000",
        accent=None,
        heading="Fraunces",
        body="Newsreader",
        justify=("palette.primary",),
    )
    css = ".brand { color: #7c3aed; }\n"
    v = lint_design({"s.css": css}, spec, spec_present=True, spec_valid=True)
    assert any(f["rule_id"] == "ai_purple" for f in v["findings"])


def test_matching_primary_value_suppresses_purple():
    """The flip side: when the spec's palette.primary IS the flagged purple and it
    is justified, the ai_purple finding is suppressed."""
    spec = _spec_with(
        primary="#7c3aed",
        accent=None,
        heading="Fraunces",
        body="Newsreader",
        justify=("palette.primary",),
    )
    css = ".brand { color: #7c3aed; }\n"
    v = lint_design({"s.css": css}, spec, spec_present=True, spec_valid=True)
    assert not any(f["rule_id"] == "ai_purple" for f in v["findings"])


def test_matching_primary_value_suppresses_purple_across_hex_rgb_spelling():
    """Value match resolves color, so a hex spec primary suppresses an rgb()-spelled
    occurrence of the same violet (and not a different one)."""
    spec = _spec_with(
        primary="#7c3aed",
        accent=None,
        heading="Fraunces",
        body="Newsreader",
        justify=("palette.primary",),
    )
    css = ".a { color: rgb(124, 58, 237); }\n.b { color: rgb(139, 92, 246); }\n"
    v = lint_design({"s.css": css}, spec, spec_present=True, spec_valid=True)
    fired = {f["evidence"] for f in v["findings"] if f["rule_id"] == "ai_purple"}
    assert not any("124" in ev for ev in fired)  # the declared primary: suppressed
    assert any("139" in ev for ev in fired)  # a different violet: still fires


def test_justified_roboto_heading_does_not_suppress_inter():
    """Symmetric font case: justifying a Roboto heading must NOT suppress a
    DIFFERENT generic font (Inter) the implementation uses."""
    spec = _spec_with(
        primary="#1f2a44",
        accent=None,
        heading="Roboto",
        body="Georgia",
        justify=("typography.heading",),
    )
    css = "h1 { font-family: Inter; }\n"
    v = lint_design({"s.css": css}, spec, spec_present=True, spec_valid=True)
    assert any(
        f["rule_id"] == "generic_font" and "inter" in f["evidence"].lower() for f in v["findings"]
    )


# --- C3 deck artifact rule pack ----------------------------------------------


def _deck_slide(
    n: int,
    body: str,
    *,
    layout: str = "image_right",
    style: str = "background:linear-gradient(135deg,#0f766e,#f59e0b)",
) -> str:
    return (
        f'<section class="slide" id="slide-{n}" data-slide-id="slide-{n}" '
        f'data-layout="{layout}" style="{style}">{body}</section>'
    )


def _deck_html(*slides: str, head_style: str = "") -> str:
    return (
        "<!doctype html><html><head>"
        f"<style>{head_style}</style>"
        '</head><body><div class="deck">' + "\n".join(slides) + "</div></body></html>"
    )


def _fired(verdict: dict[str, object]) -> set[str]:
    return {str(f["rule_id"]) for f in verdict["findings"]}  # type: ignore[index]


def test_deck_type_floor_flags_inline_font_size_under_24px():
    bad = _deck_html(_deck_slide(0, '<p style="font-size:23px">Too small</p><img src="cover.png">'))
    clean = _deck_html(
        _deck_slide(0, '<p style="font-size:24px">Large enough</p><img src="cover.png">')
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    assert any(
        f["rule_id"] == "deck_type_floor" and f["severity"] == "error" for f in bad_v["findings"]
    )
    assert "deck_type_floor" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_text_budget_flags_overlong_slide_and_clean_slide_passes():
    long_text = " ".join(f"word{i}" for i in range(91))
    bad = _deck_html(_deck_slide(0, f'<p>{long_text}</p><img src="cover.png">'))
    clean = _deck_html(
        _deck_slide(
            0,
            "<ul><li>One</li><li>Two</li><li>Three</li><li>Four</li>"
            '<li>Five</li><li>Six</li></ul><img src="cover.png">',
        )
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    budget = [f for f in bad_v["findings"] if f["rule_id"] == "deck_text_budget"]
    assert budget and "too much text on slide 1" in budget[0]["message"]
    assert "deck_text_budget" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_text_budget_flags_more_than_six_bullet_lines():
    bullets = "".join(f"<li>Point {i}</li>" for i in range(7))
    bad = _deck_html(_deck_slide(0, f'<ul>{bullets}</ul><img src="cover.png">'))
    clean = _deck_html(
        _deck_slide(
            0,
            "<ul><li>One</li><li>Two</li><li>Three</li><li>Four</li>"
            '<li>Five</li><li>Six</li></ul><img src="cover.png">',
        )
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    assert "deck_text_budget" in _fired(bad_v)
    assert "deck_text_budget" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_bullet_monotony_flags_three_slide_run_and_clean_mix_passes():
    bullet_body = "<h2>Topic</h2><ul><li>A</li><li>B</li></ul>"
    bad = _deck_html(
        _deck_slide(0, bullet_body, layout="bullets"),
        _deck_slide(1, bullet_body, layout="bullets"),
        _deck_slide(2, bullet_body, layout="bullets"),
        _deck_slide(3, '<h2>Visual break</h2><img src="cover.png">', layout="image_right"),
    )
    clean = _deck_html(
        _deck_slide(0, bullet_body, layout="bullets"),
        _deck_slide(1, '<h2>Visual break</h2><img src="cover.png">', layout="image_right"),
        _deck_slide(2, bullet_body, layout="bullets"),
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    monotony = [f for f in bad_v["findings"] if f["rule_id"] == "deck_bullet_monotony"]
    assert monotony and "slides 1-3" in monotony[0]["message"]
    assert "deck_bullet_monotony" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_missing_imagery_flags_zero_images_and_image_deck_passes():
    bad = _deck_html(_deck_slide(0, "<h1>Cover</h1><p>A designed opening.</p>"))
    clean = _deck_html(_deck_slide(0, '<h1>Cover</h1><img src="cover.png">'))

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    imagery = [f for f in bad_v["findings"] if f["rule_id"] == "deck_missing_imagery"]
    assert (
        imagery
        and imagery[0]["message"] == "no imagery: cover/divider slides should carry generated art"
    )
    assert "deck_missing_imagery" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_glass_flags_backdrop_filter_without_saturate_and_clean_glass_passes():
    bad = _deck_html(
        _deck_slide(
            0,
            '<div style="backdrop-filter:blur(14px);background:rgba(255,255,255,.16)">Glass</div>'
            '<img src="cover.png">',
        )
    )
    clean = _deck_html(
        _deck_slide(
            0,
            '<div style="backdrop-filter:blur(14px) saturate(160%);'
            'background:rgba(255,255,255,.16)">Glass</div><img src="cover.png">',
        )
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    assert "deck_glass_missing_saturate" in _fired(bad_v)
    assert "deck_glass_missing_saturate" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_glass_flags_flat_single_color_parent_and_gradient_parent_passes():
    bad = _deck_html(
        _deck_slide(
            0,
            '<div style="backdrop-filter:blur(14px) saturate(160%);'
            'background:rgba(255,255,255,.16)">Glass</div>',
            style="background:#f8fafc",
        ),
        _deck_slide(1, '<h2>Image slide</h2><img src="cover.png">'),
    )
    clean = _deck_html(
        _deck_slide(
            0,
            '<div style="backdrop-filter:blur(14px) saturate(160%);'
            'background:rgba(255,255,255,.16)">Glass</div><img src="cover.png">',
        )
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    assert "deck_glass_flat_backdrop" in _fired(bad_v)
    assert "deck_glass_flat_backdrop" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_orphan_slide_flags_title_plus_one_short_line_and_clean_passes():
    bad = _deck_html(_deck_slide(0, "<h2>Thin Slide</h2><p>One short line.</p>", layout="bullets"))
    clean = _deck_html(
        _deck_slide(
            0,
            "<h2>Substantive Slide</h2><p>First supporting line.</p>"
            '<p>Second supporting line.</p><img src="cover.png">',
            layout="bullets",
        )
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    assert "deck_orphan_slide" in _fired(bad_v)
    assert "deck_orphan_slide" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_deck_handwritten_html_flags_data_label_nav_without_marker_and_clean_passes():
    body = """
<div class="deck">
  <section data-label="One"><h2>One</h2><p>First line.</p><img src="a.png"></section>
  <section data-label="Two"><h2>Two</h2><p>Second line.</p><img src="b.png"></section>
</div>
<nav class="slide-nav"><button id="btn-next">Next</button></nav>
"""
    bad = f"<!doctype html><html><body>{body}</body></html>"
    clean = (
        "<!doctype html><!-- disco-slides-generate:pipeline-html v1 -->"
        f"<html><body>{body}</body></html>"
    )

    bad_v = lint_design({"deck.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"deck.html": clean}, None, spec_present=False, spec_valid=False)

    assert "deck_handwritten_html" in _fired(bad_v)
    assert "deck_handwritten_html" not in _fired(clean_v)
    assert clean_v["ok"] is True


# --- W2 static-site rule pack -------------------------------------------------


def test_web_banned_default_font_flags_first_family_and_clean_pairing_passes():
    bad = """
body { font-family: Inter, ui-sans-serif, system-ui; }
h1 { font-family: Roboto, sans-serif; }
"""
    clean = """
body { font-family: "Source Serif 4", Georgia, serif; }
h1 { font-family: "Fraunces", Arial, sans-serif; }
"""

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    assert "web_banned_default_font" in _fired(bad_v)
    assert "web_banned_default_font" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_web_reflexive_hover_scale_flags_more_than_three_selectors():
    bad = """
.card:hover { transform: scale(1.05); }
.tile:hover { transform: scale(1.03); }
.plan:hover { transform: scale(1.04); }
.quote:hover { transform: scale(1.02); }
"""
    clean = """
.card:hover { transform: scale(1.03); }
.tile:hover { transform: scale(1.02); }
.plan:hover { transform: translateY(-2px); }
"""

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    assert "web_reflexive_hover_scale" in _fired(bad_v)
    assert "web_reflexive_hover_scale" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_web_hover_only_interactivity_flags_missing_focus_visible_and_clean_passes():
    bad = "button:hover { color: #123456; }\n"
    clean = """
button:hover { color: #123456; }
button:focus-visible { outline: 2px solid #123456; outline-offset: 3px; }
"""

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    assert "web_hover_only_interactivity" in _fired(bad_v)
    assert "web_hover_only_interactivity" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_web_text_over_image_no_scrim_flags_background_media_and_clean_passes():
    bad = """
<section class="hero" style="background-image:url(hero.jpg)">
  <h1>Build with intent</h1>
</section>
"""
    clean = """
<section class="hero"
  style="background-image:linear-gradient(rgba(0,0,0,.48),rgba(0,0,0,.28)),url(hero.jpg)">
  <h1>Build with intent</h1>
</section>
"""

    bad_v = lint_design({"index.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"index.html": clean}, None, spec_present=False, spec_valid=False)

    assert "web_text_over_image_no_scrim" in _fired(bad_v)
    assert "web_text_over_image_no_scrim" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_web_glass_reuses_saturate_and_backdrop_rules_for_sites():
    bad = """
<main style="background:#f8fafc">
  <div style="backdrop-filter:blur(14px);background:rgba(255,255,255,.16)">Glass</div>
</main>
"""
    clean = """
<main style="background:linear-gradient(135deg,#0f766e,#f59e0b)">
  <div style="backdrop-filter:blur(14px) saturate(160%);
  background:rgba(255,255,255,.16)">Glass</div>
</main>
"""

    bad_v = lint_design({"index.html": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"index.html": clean}, None, spec_present=False, spec_valid=False)

    assert "web_glass_missing_saturate" in _fired(bad_v)
    assert "web_glass_flat_backdrop" in _fired(bad_v)
    assert "web_glass_missing_saturate" not in _fired(clean_v)
    assert "web_glass_flat_backdrop" not in _fired(clean_v)
    assert clean_v["ok"] is True


def test_web_default_hidden_content_flags_fail_hidden_and_clean_gates_pass():
    bad = """
.reveal { opacity: 0; transform: translateY(16px); }
section.fade-up { visibility: hidden; }
"""
    clean = """
html.js .reveal { opacity: 0; transform: translateY(16px); }
@media (prefers-reduced-motion: no-preference) {
  .fade-up { opacity: 0; }
}
"""

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    assert "web_default_hidden_content" in _fired(bad_v)
    assert "web_default_hidden_content" not in _fired(clean_v)
    assert clean_v["ok"] is True


# --- T1 numeric design-constraint rules --------------------------------------


def test_numeric_too_many_fonts_flags_four_real_families_and_allows_three():
    bad = """
h1 { font-family: Fraunces, Georgia, serif; }
body { font-family: "Source Sans 3", system-ui, sans-serif; }
code { font-family: "JetBrains Mono", ui-monospace, monospace; }
blockquote { font-family: Newsreader, Georgia, serif; }
"""
    clean = """
h1 { font-family: Fraunces, Georgia, serif; }
body { font-family: "Source Sans 3", system-ui, -apple-system, sans-serif; }
code { font-family: "JetBrains Mono", ui-monospace, monospace; }
.system { font-family: system-ui, -apple-system, sans-serif; }
"""

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    bad_findings = [f for f in bad_v["findings"] if f["rule_id"] == "too_many_fonts"]
    assert bad_findings and bad_findings[0]["severity"] == "warning"
    assert "too_many_fonts" not in _fired(clean_v)


def test_numeric_font_size_too_small_flags_resolved_values_under_floor():
    bad = ".caption { font-size: 11px; }\n"
    clean = (
        ":root { --font-size-small: 11px; }\n"
        ".body { font-size: 16px; }\n"
        ".note { font-size: 1rem; }\n"
        ".eyebrow { font-size: 0.8rem; }\n"
    )

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    bad_findings = [f for f in bad_v["findings"] if f["rule_id"] == "font_size_too_small"]
    assert bad_findings and bad_findings[0]["severity"] == "info"
    assert "font_size_too_small" not in _fired(clean_v)


def test_numeric_too_many_colors_counts_only_distinct_saturated_hues():
    bad = """
:root {
  --rose:#e11d48; --orange:#f97316; --yellow:#eab308; --lime:#84cc16;
  --green:#22c55e; --cyan:#06b6d4; --blue:#2563eb; --magenta:#db2777;
}
"""
    clean = """
:root {
  --primary:#2e7d66; --accent:#3b6ea8; --warn:#d9902f;
  --support:#0ea5a4; --danger:#b14e49; --success:#2f855a;
  --bg:#f8fafc; --text:#111827; --muted:#9ca3af;
  --accent-strong:#338a70; --accent-soft:#296f5b;
}
"""

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    bad_findings = [f for f in bad_v["findings"] if f["rule_id"] == "too_many_colors"]
    assert bad_findings and bad_findings[0]["severity"] == "info"
    assert "too_many_colors" not in _fired(clean_v)


def test_numeric_tight_line_height_flags_only_unitless_cramped_values():
    bad = ".copy { line-height: 1.1; }\n"
    clean = (
        ":root { --line-height-tight: 1.1; }\n"
        "h1, h2 { line-height: 1.1; }\n"
        ".copy { line-height: 1.5; }\n"
        ".caption { line-height: 24px; }\n"
    )

    bad_v = lint_design({"site.css": bad}, None, spec_present=False, spec_valid=False)
    clean_v = lint_design({"site.css": clean}, None, spec_present=False, spec_valid=False)

    bad_findings = [f for f in bad_v["findings"] if f["rule_id"] == "tight_line_height"]
    assert bad_findings and bad_findings[0]["severity"] == "info"
    assert "tight_line_height" not in _fired(clean_v)


def test_numeric_rules_do_not_flag_soft_depth_direction_tokens():
    css = (
        direction_tokens_css(DIRECTION_BY_ID["soft-depth-saas"])
        + """
body {
  font-family: var(--ui);
  font-size: 16px;
  line-height: 1.5;
  background: var(--bg);
  color: var(--text);
}
h1 {
  font-family: var(--display);
  font-size: 42px;
  line-height: 1.2;
  color: var(--accent);
}
p {
  font-family: var(--reading);
  font-size: 18px;
  line-height: 1.6;
}
code {
  font-family: var(--mono);
  font-size: 14px;
  line-height: 1.45;
}
.button {
  background: var(--accent);
  color: var(--bg);
  border-radius: 8px;
}
"""
    )
    v = lint_design({"tokens.css": css}, None, spec_present=False, spec_valid=False)
    numeric_rule_ids = {
        "too_many_fonts",
        "font_size_too_small",
        "too_many_colors",
        "tight_line_height",
    }

    assert not (numeric_rule_ids & _fired(v)), v["findings"]


def test_numeric_rule_meta_severities_are_registered():
    rule_meta = design_lint_module._RULE_META

    assert rule_meta["too_many_fonts"][0] == "warning"
    assert rule_meta["font_size_too_small"][0] == "info"
    assert rule_meta["too_many_colors"][0] == "info"
    assert rule_meta["tight_line_height"][0] == "info"


# --- P1-2: single source of truth for canonical keys --------------------------


def test_linter_suppressible_keys_subset_of_core_canonical():
    """The linter owns NO local copy of its canonical keys — every key it can
    suppress is part of core's exported CANONICAL_CHOICE_KEYS (no drift)."""
    from disco.core.appkit import CANONICAL_CHOICE_KEYS
    from disco.tools.builtin.design_lint import SUPPRESSIBLE_CHOICE_KEYS

    assert SUPPRESSIBLE_CHOICE_KEYS <= CANONICAL_CHOICE_KEYS


def test_every_recipe_lowers_to_a_clean_passing_site():
    """Every seeded recipe's declared fonts + palette, rendered into CSS, pass the
    linter — its non-defaults are all justified with MATCHING values."""
    for r in RECIPES:
        ds = r.to_design_spec()
        css = (
            f'body {{ font-family: "{r.body_font}", serif; }}\n'
            f'h1 {{ font-family: "{r.heading_font}", serif; }}\n'
            f".brand {{ color: {r.primary}; }}\n"
            ".btn { border-radius: 6px; }\n"
        )
        v = lint_design({"site.css": css}, ds, spec_present=True, spec_valid=True)
        assert v["ok"] is True, (r.id, v["findings"])


# --- P1-3: the designspec read is BOUNDED -------------------------------------


@pytest.mark.asyncio
async def test_oversized_designspec_is_bounded_not_a_bomb():
    """An oversized .disco/designspec.json is treated as present-but-invalid (rules
    fire, nothing suppressed) — never parsed into a memory/time bomb, never a crash."""
    huge = b'{"x":"' + b"a" * (300 * 1024) + b'"}'  # > the 256 KiB spec cap
    sbx = _FakeSandbox(
        {
            "styles.css": SLOP_CSS.encode(),
            ".disco/designspec.json": huge,
        }
    )
    out = await DesignLintTool().run(DesignLintArgs(), _ctx(sbx))
    assert out.success is True
    assert out.structured is not None
    assert out.structured["design_spec_present"] is True
    assert out.structured["design_spec_valid"] is False
    fired = {f["rule_id"] for f in out.structured["findings"]}
    assert "ai_purple" in fired  # nothing suppressed


@pytest.mark.asyncio
async def test_oversized_designspec_is_never_read_into_memory():
    """P1-3: the size is probed IN the sandbox and an over-cap spec is refused BEFORE
    read_file — a multi-GB hostile spec never lands in the Python process. The spec is
    treated present-but-invalid (ai_purple still fires) and read_file is NOT called."""
    spec_path = posixpath.normpath(".disco/designspec.json")
    sbx = _FakeSandbox(
        # only a tiny placeholder is stored; the size probe REPORTS multi-GB
        {"styles.css": SLOP_CSS.encode(), ".disco/designspec.json": b"{}"},
        sizes={spec_path: 4 * 1024 * 1024 * 1024},  # 4 GiB > the 256 KiB cap
    )
    out = await DesignLintTool().run(DesignLintArgs(), _ctx(sbx))
    assert out.structured is not None
    assert out.structured["design_spec_present"] is True
    assert out.structured["design_spec_valid"] is False
    fired = {f["rule_id"] for f in out.structured["findings"]}
    assert "ai_purple" in fired  # nothing suppressed
    # the over-cap spec was NEVER pulled into memory
    assert spec_path not in sbx.read_paths


@pytest.mark.asyncio
async def test_oversized_source_file_is_skipped_without_reading():
    """P1-3: an over-cap SOURCE file is size-probed and skipped BEFORE read_file — its
    bytes never enter the process — while normal-size siblings are read + linted."""
    big = posixpath.normpath("src/huge.css")
    sbx = _FakeSandbox(
        {
            "src/huge.css": b"/* placeholder */",  # tiny store; probe reports huge
            "src/styles.css": SLOP_CSS.encode(),  # normal-size, must be read + linted
        },
        sizes={big: 3 * 1024 * 1024 * 1024},  # 3 GiB > _MAX_FILE_BYTES
    )
    out = await DesignLintTool().run(DesignLintArgs(root="."), _ctx(sbx))
    assert out.success is True
    assert out.structured is not None
    # the over-cap file was never read; the normal-size one was
    assert big not in sbx.read_paths
    assert posixpath.normpath("src/styles.css") in sbx.read_paths
    # the normal-size slop file still produced findings (no crash, scan ran)
    fired = {f["rule_id"] for f in out.structured["findings"]}
    assert "ai_purple" in fired
    assert all("huge.css" not in f["path"] for f in out.structured["findings"])


# --- pure engine: clean recipe-based sample ----------------------------------


def test_clean_recipe_sample_passes():
    r = RECIPES[0]
    ds = r.to_design_spec()
    css = (
        f'body {{ font-family: "{r.body_font}", serif; }}\n'
        f'h1 {{ font-family: "{r.heading_font}", serif; }}\n'
        f".brand {{ color: {r.primary}; }} .accent {{ color: {r.accent}; }}\n"
        ".btn { border-radius: 4px; }\n"
    )
    html = (
        '<section class="masthead"><h1>An Essay</h1></section>'
        "<main><article>Considered long-form prose, set in real type.</article></main>"
        '<footer class="sitemap">links</footer>'
    )
    v = lint_design({"site.css": css, "index.html": html}, ds, spec_present=True, spec_valid=True)
    assert v["ok"] is True, v["findings"]
    rendered = design_lint_module._render(v)
    assert "covers the current artifact bytes" in rendered
    assert "do not re-run unless a relevant file changes" in rendered


# --- pure engine: missing / invalid spec -------------------------------------


def test_missing_spec_fires_relevant_rules():
    v = lint_design({"styles.css": SLOP_CSS}, None, spec_present=False, spec_valid=False)
    fired = {f["rule_id"] for f in v["findings"]}
    assert {"ai_purple", "generic_font", "gradient_hero_text"} <= fired
    assert v["design_spec_present"] is False


def test_invalid_spec_suppresses_nothing():
    # spec_present True but parsed to None (invalid) → no suppression
    v = lint_design({"styles.css": SLOP_CSS}, None, spec_present=True, spec_valid=False)
    fired = {f["rule_id"] for f in v["findings"]}
    assert "ai_purple" in fired
    assert v["design_spec_valid"] is False


# --- the tool interface (fake sandbox) ---------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_structured_verdict_over_workspace():
    sbx = _FakeSandbox(
        {
            "src/styles.css": SLOP_CSS.encode(),
            "index.html": SLOP_HTML.encode(),
            "node_modules/junk.css": b"body{font-family:Inter}",  # must be skipped
        }
    )
    out = await DesignLintTool().run(DesignLintArgs(root="."), _ctx(sbx))
    assert out.success is True
    assert out.structured is not None
    assert out.structured["ok"] is False
    rule_ids = {f["rule_id"] for f in out.structured["findings"]}
    assert EXPECTED_SLOP_RULES <= rule_ids
    # node_modules was skipped — every finding path is a real source file
    assert all("node_modules" not in f["path"] for f in out.structured["findings"])
    assert "DESIGN_LINT" in out.content
    assert "this result is stale after mutation" in out.content
    assert "Otherwise proceed without claiming a clean lint result" in out.content


@pytest.mark.asyncio
async def test_tool_suppresses_via_disco_designspec_json():
    spec = _justified_spec()
    sbx = _FakeSandbox(
        {
            "styles.css": SLOP_CSS.encode(),
            ".disco/designspec.json": json.dumps(spec.model_dump(mode="json")).encode(),
        }
    )
    out = await DesignLintTool().run(DesignLintArgs(), _ctx(sbx))
    assert out.success is True
    assert out.structured is not None
    assert out.structured["design_spec_present"] is True
    assert out.structured["design_spec_valid"] is True
    fired = {f["rule_id"] for f in out.structured["findings"]}
    assert "ai_purple" not in fired
    assert "gradient_hero_text" not in fired


@pytest.mark.asyncio
async def test_tool_with_invalid_designspec_json_fires_rules():
    sbx = _FakeSandbox(
        {
            "styles.css": SLOP_CSS.encode(),
            ".disco/designspec.json": b"{ this is not valid json",
        }
    )
    out = await DesignLintTool().run(DesignLintArgs(), _ctx(sbx))
    assert out.structured is not None
    assert out.structured["design_spec_present"] is True
    assert out.structured["design_spec_valid"] is False
    fired = {f["rule_id"] for f in out.structured["findings"]}
    assert "ai_purple" in fired


@pytest.mark.asyncio
async def test_tool_is_read_only_and_registered():
    from disco.tools.builtin import build_default_registry

    assert DesignLintTool.definition.read_only is True
    assert "not semantic proof or completion authority" in DesignLintTool.definition.description
    assert "TWO debug calls shared" in DesignLintTool.definition.description
    assert build_default_registry().names().issuperset({"design_lint"})


# --- H2 direction-conformance (advisory) -------------------------------------


def test_direction_font_mismatch_fires_and_committed_fonts_pass():
    direction = DIRECTION_BY_ID[
        "editorial-magazine"
    ]  # Newsreader / Schibsted Grotesk / IBM Plex Mono
    off = "h1 { font-family: 'Comic Sans MS', cursive; }\n"
    on = (
        "h1 { font-family: 'Newsreader', Georgia, serif; }\n"
        "body { font-family: 'Schibsted Grotesk', system-ui, sans-serif; }\n"
    )
    off_v = lint_design(
        {"s.css": off}, None, spec_present=False, spec_valid=False, direction=direction
    )
    on_v = lint_design(
        {"s.css": on}, None, spec_present=False, spec_valid=False, direction=direction
    )
    assert any(
        f["rule_id"] == "direction_font_mismatch"
        and f["severity"] == "warning"
        and "comic sans" in f["evidence"].lower()
        for f in off_v["findings"]
    )
    assert not any(f["rule_id"] == "direction_font_mismatch" for f in on_v["findings"])


def test_direction_generic_fallbacks_and_no_direction_stay_quiet():
    direction = DIRECTION_BY_ID["swiss-international"]
    generic = (
        "body { font-family: system-ui, sans-serif; }\n"
        "code { font-family: monospace; }\n"
        ".x { font-family: serif; }\n"
    )
    gv = lint_design(
        {"s.css": generic}, None, spec_present=False, spec_valid=False, direction=direction
    )
    assert not any(f["rule_id"] == "direction_font_mismatch" for f in gv["findings"])

    # NO direction committed → the conformance rule no-ops (no crash), even on a
    # font/color that WOULD contradict a direction.
    off = "h1 { font-family: 'Comic Sans MS'; color: #1e90ff; }\n"
    nv = lint_design({"s.css": off}, None, spec_present=False, spec_valid=False)
    assert not any(
        f["rule_id"] in {"direction_font_mismatch", "direction_palette_drift"}
        for f in nv["findings"]
    )


def test_direction_palette_drift_fires_for_far_color_and_near_accent_passes():
    direction = DIRECTION_BY_ID["editorial-magazine"]  # seed #4B3F72; accents inc. #B54A3A
    far = ".x { color: #1e90ff; }\n"  # saturated blue, far from every committed color
    near = ".y { color: #b54a3a; }\n"  # exactly a committed accent
    far_v = lint_design(
        {"s.css": far}, None, spec_present=False, spec_valid=False, direction=direction
    )
    near_v = lint_design(
        {"s.css": near}, None, spec_present=False, spec_valid=False, direction=direction
    )
    assert any(
        f["rule_id"] == "direction_palette_drift" and f["severity"] == "info"
        for f in far_v["findings"]
    )
    assert not any(f["rule_id"] == "direction_palette_drift" for f in near_v["findings"])


@pytest.mark.asyncio
async def test_direction_conformance_is_advisory_via_tool():
    """The committed direction is read from durable context and drives conformance
    findings, but the tool merely RETURNS a verdict (never raises/blocks) and stays
    read-only — conformance is advisory."""
    direction = DIRECTION_BY_ID["editorial-magazine"]
    sbx = _FakeSandbox(
        {
            "styles.css": b"h1 { font-family: 'Comic Sans MS'; }\n",
            ".disco/context/design_direction.md": render_design_direction(direction).encode(),
        }
    )
    out = await DesignLintTool().run(DesignLintArgs(), _ctx(sbx))
    assert out.success is True  # scan ran; conformance findings are just findings
    assert out.structured is not None
    fired = {f["rule_id"] for f in out.structured["findings"]}
    assert "direction_font_mismatch" in fired
    assert DesignLintTool.definition.read_only is True
