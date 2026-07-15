"""P4 / TOOL-1 tests: AppSpec model + pure mutations + deterministic renderer."""

from __future__ import annotations

import pytest
from disco.core.appkit import AppSection, AppSpec, render_html
from pydantic import ValidationError


def _spec() -> AppSpec:
    return AppSpec(
        title="Acme Roofing",
        sections=(
            AppSection(
                id="hero", kind="hero", fields={"headline": "Roofs done right", "cta_text": "Quote"}
            ),
            AppSection(id="form", kind="lead_form", fields={"title": "Get a quote"}),
        ),
    )


def test_roundtrip_and_frozen() -> None:
    s = _spec()
    assert AppSpec.model_validate(s.model_dump(mode="json")) == s
    with pytest.raises(ValidationError):
        s.title = "x"  # type: ignore[misc]


def test_unknown_section_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSection(id="x", kind="not_a_kind")


# --- pure mutations -----------------------------------------------------------
def test_with_content_touches_only_one_field() -> None:
    s = _spec().with_content("hero", "headline", "New headline")
    assert s.section("hero").fields["headline"] == "New headline"
    assert s.section("hero").fields["cta_text"] == "Quote"  # untouched
    assert s.section("form").fields["title"] == "Get a quote"  # untouched


def test_with_content_bad_section_raises() -> None:
    with pytest.raises(ValueError):
        _spec().with_content("nope", "x", "y")


def test_add_remove_reorder_sections() -> None:
    s = _spec()
    s2 = s.with_section_added(AppSection(id="about", kind="about", fields={"body": "We roof."}))
    assert [x.id for x in s2.sections] == ["hero", "form", "about"]
    s3 = s2.with_section_reordered("about", 0)
    assert [x.id for x in s3.sections] == ["about", "hero", "form"]
    s4 = s3.with_section_removed("hero")
    assert [x.id for x in s4.sections] == ["about", "form"]


def test_add_duplicate_section_id_raises() -> None:
    with pytest.raises(ValueError):
        _spec().with_section_added(AppSection(id="hero", kind="about"))


def test_design_and_tweak() -> None:
    s = _spec().with_design("primary", "#ff0000").with_tweak("lead_form.include_phone", True)
    assert s.resolved_design()["primary"] == "#ff0000"
    assert s.resolved_design()["accent"]  # defaults still present
    assert s.tweaks["lead_form.include_phone"] is True


# --- renderer -----------------------------------------------------------------
def test_render_is_self_contained_and_deterministic() -> None:
    s = _spec()
    html1 = render_html(s)
    html2 = render_html(s)
    assert html1 == html2  # deterministic
    assert "<style>" in html1 and "<link" not in html1  # inline CSS, no late stylesheet
    assert "Roofs done right" in html1  # hero headline rendered
    assert 'data-disco-section="hero"' in html1  # semantic anchors present
    assert "<form" in html1  # lead form rendered


def test_render_escapes_content() -> None:
    s = AppSpec(
        title="x",
        sections=(AppSection(id="h", kind="hero", fields={"headline": "<script>bad</script>"}),),
    )
    out = render_html(s)
    assert "<script>bad" not in out
    assert "&lt;script&gt;" in out


def test_render_reflects_design_tokens() -> None:
    s = _spec().with_design("primary", "#123456")
    assert "--primary:#123456" in render_html(s)


def test_render_sanitizes_design_token_css_injection() -> None:
    # a malicious design value must NOT break out of the <style> declaration: the
    # injection cannot introduce a second </style> or any <script>/brace breakout.
    s = _spec().with_design("primary", "red}</style><script>alert(1)</script>")
    out = render_html(s)
    assert out.count("<style>") == 1 and out.count("</style>") == 1  # no early close
    assert "<script" not in out  # injected tag chars stripped
    assert "red" in out.split("<style>")[1].split("</style>")[0]  # benign part survives


def test_duplicate_section_ids_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        AppSpec(
            title="x",
            sections=(AppSection(id="dup", kind="hero"), AppSection(id="dup", kind="about")),
        )
