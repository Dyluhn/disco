"""P8 selection→scoped-edit directive tests (pure)."""

from __future__ import annotations

from disco.core.selection_edit import (
    DeckSelectionRef,
    SemanticSelectionRef,
    SourceSelectionRef,
    build_scoped_edit_directive,
    parse_selection_ref,
    selection_edit_frame_valid,
)


class TestParseSelectionRef:
    def test_source_ref(self) -> None:
        ref = parse_selection_ref(
            {"kind": "source", "oid": "index.html:12", "file": "index.html", "line": 12}
        )
        assert isinstance(ref, SourceSelectionRef)
        assert ref.file == "index.html" and ref.line == 12

    def test_deck_ref(self) -> None:
        ref = parse_selection_ref({"kind": "deck", "slide_id": "s3", "element_id": "e7"})
        assert isinstance(ref, DeckSelectionRef)
        assert ref.slide_id == "s3" and ref.element_id == "e7"

    def test_semantic_field_ref(self) -> None:
        ref = parse_selection_ref(
            {
                "kind": "semantic",
                "section_id": "hero",
                "field_id": "headline",
                "screen_label": "hero",
            }
        )
        assert isinstance(ref, SemanticSelectionRef)
        assert ref.section_id == "hero" and ref.field_id == "headline"

    def test_unknown_kind_rejected(self) -> None:
        assert parse_selection_ref({"kind": "mystery", "x": 1}) is None

    def test_missing_kind_rejected(self) -> None:
        assert parse_selection_ref({"file": "index.html", "line": 3}) is None

    def test_wrong_shape_for_kind_rejected(self) -> None:
        # a "source" without line must NOT coerce to another kind
        assert parse_selection_ref({"kind": "source", "oid": "x", "file": "index.html"}) is None

    def test_negative_line_rejected(self) -> None:
        assert (
            parse_selection_ref({"kind": "source", "oid": "x:-1", "file": "index.html", "line": -1})
            is None
        )

    def test_extra_field_rejected(self) -> None:
        # extra="forbid" — a smuggled field is rejected, not ignored
        assert (
            parse_selection_ref({"kind": "deck", "slide_id": "s", "element_id": "e", "evil": True})
            is None
        )

    def test_non_dict_rejected(self) -> None:
        assert parse_selection_ref("not a dict") is None
        assert parse_selection_ref(None) is None
        assert parse_selection_ref(42) is None


class TestBuildDirective:
    def test_source_anchored_names_file_and_line_and_targeted_tool(self) -> None:
        ref = SourceSelectionRef(oid="index.html:12", file="index.html", line=12)
        d = build_scoped_edit_directive(ref, "make it bold", human_label='h1 — "Hi"')
        assert "index.html" in d and "line 12" in d
        assert "file_edit" in d
        assert "make it bold" in d
        assert "do NOT rewrite" in d.replace("\n", " ")
        assert 'h1 — "Hi"' in d

    def test_source_unanchored_falls_back_to_label(self) -> None:
        # empty file / line 0 = resolveRef's no-anchor fallback — must NOT claim a precise anchor
        ref = SourceSelectionRef(oid="", file="", line=0)
        d = build_scoped_edit_directive(ref, "change color", human_label="the CTA button")
        assert "line 0" not in d
        assert "the CTA button" in d
        assert "targeted edit tool" in d

    def test_deck_ref_names_deck_patch(self) -> None:
        ref = DeckSelectionRef(slide_id="s3", element_id="e7")
        d = build_scoped_edit_directive(ref, "shorten this", human_label="title")
        assert "s3" in d and "e7" in d and "deck_patch" in d

    def test_semantic_field_names_app_update_content(self) -> None:
        ref = SemanticSelectionRef(section_id="hero", field_id="headline", screen_label="hero")
        d = build_scoped_edit_directive(ref, "new headline", human_label=None)
        assert "headline" in d and "hero" in d and "app_update_content" in d

    def test_semantic_collection_item(self) -> None:
        ref = SemanticSelectionRef(section_id="services", collection_id="services.cards", index=1)
        d = build_scoped_edit_directive(ref, "swap icon")
        assert "services.cards" in d and "#1" in d

    def test_semantic_section_only(self) -> None:
        ref = SemanticSelectionRef(section_id="pricing")
        d = build_scoped_edit_directive(ref, "tighten spacing")
        assert "pricing" in d and "section" in d

    def test_empty_instruction_is_flagged_not_silent(self) -> None:
        ref = SourceSelectionRef(oid="a.html:1", file="a.html", line=1)
        d = build_scoped_edit_directive(ref, "   ")
        assert "no change described" in d


class TestFrameValid:
    def test_valid_frame(self) -> None:
        assert selection_edit_frame_valid(
            {"kind": "source", "oid": "a:1", "file": "a.html", "line": 1}, "make it blue"
        )

    def test_empty_instruction_invalid(self) -> None:
        assert not selection_edit_frame_valid(
            {"kind": "source", "oid": "a:1", "file": "a.html", "line": 1}, "  "
        )

    def test_bad_ref_invalid(self) -> None:
        assert not selection_edit_frame_valid({"kind": "nope"}, "make it blue")

    def test_non_string_instruction_invalid(self) -> None:
        assert not selection_edit_frame_valid(
            {"kind": "deck", "slide_id": "s", "element_id": "e"}, None
        )
