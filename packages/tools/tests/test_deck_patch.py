"""Tests for C-EDIT-4 deck_patch tool + §4.3 DeckResolver Python side.

Covers:
  1. apply_patch: valid RFC-6902 patch mutates a copy, original untouched.
  2. apply_patch: all six operations (replace/add/remove/test/move/copy).
  3. apply_patch: bad operation raises PatchError.
  4. apply_patch: non-existent path raises PatchError.
  5. apply_patch: array index OOB raises PatchError.
  6. DeckPatchTool.run: valid patch → writes authored JSON + HTML + PPTX.
  7. DeckPatchTool.run: schema-violating patch → reverts, workspace unchanged.
  8. DeckPatchTool.run: file not found → clean failure, no writes.
  9. DeckPatchTool.run: bad JSON in deck file → clean failure.
 10. DeckResolver: element_id maps to the correct deck JSON pointer.
 11. DeckResolver: round-trip element_id → json_pointer → path segments.
 12. render_html emits data-element-id / data-slide-id on each element.
 13. DeckPatchTool registered in AGENT_TOOLS + ARTIFACT_TOOLS.
 14. deck_schema: lower_deck produces LoweredSlide with element geometry.

Note: the reconciliation onto the c1c2 schema removed two deckedit-era helpers —
`lower_to_minimal_deck` (superseded by `lower_deck`; covered in test_deck_schema.py)
and `strip_element_ids` (the export-path attribute cleaner). The latter has been
RESTORED as a tested utility in ``disco.tools.builtin._pptx_render`` (see
``TestStripElementIds`` below) but is NOT yet wired to the export path that
originally called it — ``render_html`` still stamps data-element-id /
data-slide-id and the slides.py write path emits that HTML as the downloadable
artifact with no clean variant. The export-side regression remains tracked.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.tools.builtin._deck_patch import (
    DeckPatchArgs,
    DeckPatchTool,
    PatchError,
    apply_patch,
)
from disco.tools.builtin._deck_schema import (
    AuthoredDeck,
    AuthoredSlide,
    ChartSpec,
    lower_deck_for_editor,
)
from disco.tools.builtin._pptx_render import (
    DeckSlide,
    MinimalDeck,
    render_html,
    strip_element_ids,
)
from disco.tools.registry import AGENT_TOOLS, ARTIFACT_TOOLS, agent_scope, artifact_scope

# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _sample_authored_dict() -> dict:
    """A minimal two-slide AuthoredDeck as a plain dict (the JSON round-trip form)."""
    return {
        "title": "Test Deck",
        "theme": "disco-light",
        "slides": [
            {
                "type": "title",
                "title": "Welcome",
                "body": ["A tagline"],
                "layout_hint": None,
                "image_prompt": None,
                "chart": None,
                "table": None,
                "notes": None,
            },
            {
                "type": "bullets",
                "title": "Key Points",
                "body": ["Bullet A", "Bullet B", "Bullet C"],
                "layout_hint": None,
                "image_prompt": None,
                "chart": None,
                "table": None,
                "notes": None,
            },
        ],
    }


def _make_tool_ctx(authored_dict: dict) -> tuple[DeckPatchTool, MagicMock]:
    """Return a (DeckPatchTool, fake_ctx) pair with the authored_dict pre-loaded."""
    raw = json.dumps(authored_dict, indent=2).encode()
    ctx = MagicMock()
    ctx.sandbox = MagicMock()
    ctx.sandbox.read_file = AsyncMock(return_value=raw)
    ctx.sandbox.write_file = AsyncMock()
    return DeckPatchTool(), ctx


# ─── 1–5: apply_patch unit tests ──────────────────────────────────────────────

class TestApplyPatch:
    def test_replace_title(self):
        doc = {"slides": [{"title": "Old", "body": ["Bullet"]}]}
        result = apply_patch(doc, [{"op": "replace", "path": "/slides/0/title", "value": "New"}])
        assert result["slides"][0]["title"] == "New"
        # Original must be untouched (deep copy)
        assert doc["slides"][0]["title"] == "Old"

    def test_replace_body_element(self):
        doc = {"slides": [{"body": ["A", "B", "C"]}]}
        patch = [{"op": "replace", "path": "/slides/0/body/1", "value": "Updated"}]
        result = apply_patch(doc, patch)
        assert result["slides"][0]["body"] == ["A", "Updated", "C"]

    def test_add_append(self):
        doc = {"slides": [{"body": ["A"]}]}
        result = apply_patch(doc, [{"op": "add", "path": "/slides/0/body/-", "value": "B"}])
        assert result["slides"][0]["body"] == ["A", "B"]

    def test_add_to_dict(self):
        doc = {"a": 1}
        result = apply_patch(doc, [{"op": "add", "path": "/b", "value": 2}])
        assert result["b"] == 2

    def test_remove_array_element(self):
        doc = {"body": ["A", "B", "C"]}
        result = apply_patch(doc, [{"op": "remove", "path": "/body/1"}])
        assert result["body"] == ["A", "C"]

    def test_remove_dict_key(self):
        doc = {"title": "T", "notes": "N"}
        result = apply_patch(doc, [{"op": "remove", "path": "/notes"}])
        assert "notes" not in result

    def test_test_pass(self):
        doc = {"title": "Hello"}
        result = apply_patch(doc, [{"op": "test", "path": "/title", "value": "Hello"}])
        assert result["title"] == "Hello"

    def test_test_fail_raises(self):
        doc = {"title": "Hello"}
        with pytest.raises(PatchError, match="test failed"):
            apply_patch(doc, [{"op": "test", "path": "/title", "value": "Wrong"}])

    def test_move(self):
        doc = {"a": 1, "b": 2}
        result = apply_patch(doc, [{"op": "move", "from": "/a", "path": "/c"}])
        assert result["c"] == 1
        assert "a" not in result

    def test_copy(self):
        doc = {"a": {"x": 1}}
        result = apply_patch(doc, [{"op": "copy", "from": "/a", "path": "/b"}])
        assert result["b"] == {"x": 1}
        # Must be a distinct object (deep copy)
        result["b"]["x"] = 99
        assert result["a"]["x"] == 1

    def test_bad_operation_raises(self):
        doc = {"title": "T"}
        with pytest.raises(PatchError, match="Unknown RFC-6902 operation"):
            apply_patch(doc, [{"op": "frobnicate", "path": "/title", "value": "X"}])

    def test_nonexistent_path_raises(self):
        doc = {"slides": [{"title": "T"}]}
        with pytest.raises(PatchError):
            apply_patch(doc, [{"op": "replace", "path": "/slides/99/title", "value": "X"}])

    def test_array_index_oob_raises(self):
        doc = {"arr": [1, 2, 3]}
        with pytest.raises(PatchError):
            apply_patch(doc, [{"op": "replace", "path": "/arr/5", "value": 99}])

    def test_multiple_ops_applied_in_order(self):
        doc = {"slides": [{"title": "A", "body": ["X"]}]}
        patch = [
            {"op": "replace", "path": "/slides/0/title", "value": "B"},
            {"op": "add", "path": "/slides/0/body/-", "value": "Y"},
        ]
        result = apply_patch(doc, patch)
        assert result["slides"][0]["title"] == "B"
        assert result["slides"][0]["body"] == ["X", "Y"]


# ─── 6–9: DeckPatchTool.run integration tests ─────────────────────────────────

class TestDeckPatchToolRun:
    @pytest.mark.asyncio
    async def test_valid_patch_writes_three_files(self):
        """A valid patch writes authored JSON + HTML + PPTX to the sandbox."""
        authored = _sample_authored_dict()
        tool, ctx = _make_tool_ctx(authored)

        args = DeckPatchArgs(
            deck_file="my-deck.authored.json",
            patch=[{"op": "replace", "path": "/slides/0/title", "value": "Updated Welcome"}],
        )
        outcome = await tool.run(args, ctx)

        assert outcome.success, f"Expected success, got error: {outcome.error}"
        # Three writes: authored JSON, HTML, PPTX
        assert ctx.sandbox.write_file.call_count == 3
        # The authored JSON content must contain the new title
        authored_write = ctx.sandbox.write_file.call_args_list[0]
        written_json = json.loads(authored_write[0][1].decode())
        assert written_json["slides"][0]["title"] == "Updated Welcome"

    @pytest.mark.asyncio
    async def test_patch_mutates_only_targeted_element(self):
        """Patching slide 0's title leaves slide 1 byte-stable."""
        authored = _sample_authored_dict()
        original_slide1_title = authored["slides"][1]["title"]
        tool, ctx = _make_tool_ctx(authored)

        args = DeckPatchArgs(
            deck_file="deck.authored.json",
            patch=[{"op": "replace", "path": "/slides/0/title", "value": "New Title"}],
        )
        outcome = await tool.run(args, ctx)
        assert outcome.success

        authored_write = ctx.sandbox.write_file.call_args_list[0]
        written_json = json.loads(authored_write[0][1].decode())
        # Slide 1 is unchanged
        assert written_json["slides"][1]["title"] == original_slide1_title

    @pytest.mark.asyncio
    async def test_schema_violating_patch_reverts(self):
        """Setting `slides` to a non-list fails validation; workspace NOT modified."""
        authored = _sample_authored_dict()
        tool, ctx = _make_tool_ctx(authored)

        # Remove the title field (required by AuthoredSlide) from slide 0
        args = DeckPatchArgs(
            deck_file="deck.authored.json",
            patch=[{"op": "remove", "path": "/slides/0/title"}],
        )
        outcome = await tool.run(args, ctx)

        assert not outcome.success
        assert "schema" in outcome.error.lower() or "validation" in outcome.error.lower()
        # No writes should have occurred
        ctx.sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_not_found_clean_failure(self):
        """Missing deck file → clean ToolOutcome(success=False), no write."""
        tool = DeckPatchTool()
        ctx = MagicMock()
        ctx.sandbox = MagicMock()
        ctx.sandbox.read_file = AsyncMock(side_effect=Exception("no such file"))
        ctx.sandbox.write_file = AsyncMock()

        args = DeckPatchArgs(
            deck_file="nonexistent.authored.json",
            patch=[{"op": "replace", "path": "/title", "value": "X"}],
        )
        outcome = await tool.run(args, ctx)

        assert not outcome.success
        assert "nonexistent.authored.json" in outcome.error
        ctx.sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_bad_json_clean_failure(self):
        """Malformed JSON in deck file → clean failure, no write."""
        tool = DeckPatchTool()
        ctx = MagicMock()
        ctx.sandbox = MagicMock()
        ctx.sandbox.read_file = AsyncMock(return_value=b"this is { not json")
        ctx.sandbox.write_file = AsyncMock()

        args = DeckPatchArgs(
            deck_file="deck.authored.json",
            patch=[{"op": "replace", "path": "/title", "value": "X"}],
        )
        outcome = await tool.run(args, ctx)

        assert not outcome.success
        assert "json" in outcome.error.lower()
        ctx.sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_output_filenames(self):
        """output_html / output_pptx override the default stem-based names."""
        authored = _sample_authored_dict()
        tool, ctx = _make_tool_ctx(authored)

        args = DeckPatchArgs(
            deck_file="deck.authored.json",
            patch=[{"op": "replace", "path": "/title", "value": "Renamed"}],
            output_html="custom.html",
            output_pptx="custom.pptx",
        )
        outcome = await tool.run(args, ctx)
        assert outcome.success

        written_names = [call[0][0] for call in ctx.sandbox.write_file.call_args_list]
        assert "custom.html" in written_names
        assert "custom.pptx" in written_names


# ─── 10–11: DeckResolver Python side ──────────────────────────────────────────

class TestDeckResolverPython:
    """
    The Python-side 'resolver' is the lower_deck_for_editor() mapping from element_id
    to json_pointer.  These tests prove the round-trip.
    """

    def test_title_element_id_maps_to_json_pointer(self):
        deck = AuthoredDeck(
            title="D",
            slides=[AuthoredSlide(type="bullets", title="Slide One", body=["A", "B"])],
        )
        lowered = lower_deck_for_editor(deck)
        title_el = next(e for e in lowered.slides[0].elements if e.element_id == "slide-0:title")
        assert title_el.json_pointer == "/slides/0/title"

    def test_body_element_id_maps_to_json_pointer(self):
        deck = AuthoredDeck(
            title="D",
            slides=[AuthoredSlide(type="bullets", title="S", body=["A", "B", "C"])],
        )
        lowered = lower_deck_for_editor(deck)
        body1 = next(e for e in lowered.slides[0].elements if e.element_id == "slide-0:body:1")
        assert body1.json_pointer == "/slides/0/body/1"

    def test_element_id_round_trips_via_apply_patch(self):
        """element_id → json_pointer → apply_patch modifies the right field."""
        authored = AuthoredDeck(
            title="D",
            slides=[AuthoredSlide(type="bullets", title="Original", body=["A", "B"])],
        )
        lowered = lower_deck_for_editor(authored)

        # Simulate the editor: find the title element → get its pointer
        title_el = next(
            e for e in lowered.slides[0].elements if e.element_id == "slide-0:title"
        )
        pointer = title_el.json_pointer  # "/slides/0/title"

        # Apply a patch via that pointer
        authored_dict = authored.model_dump(mode="json")
        patch = [{"op": "replace", "path": pointer, "value": "Patched"}]
        patched = apply_patch(authored_dict, patch)
        assert patched["slides"][0]["title"] == "Patched"

    def test_multi_slide_element_ids_are_unique(self):
        deck = AuthoredDeck(
            title="D",
            slides=[
                AuthoredSlide(type="title", title="Slide 0", body=["Sub"]),
                AuthoredSlide(type="bullets", title="Slide 1", body=["X", "Y"]),
            ],
        )
        lowered = lower_deck_for_editor(deck)
        all_ids = [e.element_id for s in lowered.slides for e in s.elements]
        assert len(all_ids) == len(set(all_ids)), "All element_ids must be unique"

    def test_second_slide_json_pointer_uses_index_1(self):
        deck = AuthoredDeck(
            title="D",
            slides=[
                AuthoredSlide(type="bullets", title="Slide 0", body=["A"]),
                AuthoredSlide(type="bullets", title="Slide 1", body=["B"]),
            ],
        )
        lowered = lower_deck_for_editor(deck)
        slide1_title = next(
            e for e in lowered.slides[1].elements if e.element_id == "slide-1:title"
        )
        assert slide1_title.json_pointer == "/slides/1/title"


# ─── 12–13: data-element-id in HTML + strip ───────────────────────────────────

class TestHtmlElementIds:
    def _make_html(self) -> str:
        deck = MinimalDeck(
            title="T",
            slides=[
                DeckSlide(title="Slide Zero", bullets=["A", "B"], layout="bullets"),
                DeckSlide(title="Slide One", bullets=[], layout="title"),
            ],
        )
        return render_html(deck)

    def test_data_element_id_on_title(self):
        html_str = self._make_html()
        assert 'data-element-id="slide-0:title"' in html_str
        assert 'data-element-id="slide-1:title"' in html_str

    def test_data_element_id_on_bullets(self):
        html_str = self._make_html()
        assert 'data-element-id="slide-0:body:0"' in html_str
        assert 'data-element-id="slide-0:body:1"' in html_str

    def test_data_slide_id_on_section(self):
        html_str = self._make_html()
        assert 'data-slide-id="slide-0"' in html_str
        assert 'data-slide-id="slide-1"' in html_str

    def test_section_layout_has_element_ids(self):
        deck = MinimalDeck(
            title="T",
            slides=[DeckSlide(title="Section Title", bullets=["Sub"], layout="section")],
        )
        html_str = render_html(deck)
        assert 'data-element-id="slide-0:title"' in html_str
        # The section sub-text renders as a subtitle element (id ":subtitle"),
        # not a body bullet — verified against the reconciled c1 render path.
        assert 'data-element-id="slide-0:subtitle"' in html_str

    def test_image_right_layout_stamps_title_and_body(self):
        deck = MinimalDeck(
            title="T",
            slides=[
                DeckSlide(
                    title="Image Slide",
                    bullets=["Bullet"],
                    layout="image_right",
                    image_url=None,
                )
            ],
        )
        html_str = render_html(deck)
        assert 'data-element-id="slide-0:title"' in html_str
        assert 'data-element-id="slide-0:body:0"' in html_str
        # KNOWN GAP (tracked): render_html does NOT stamp data-element-id on the
        # image element of image_right/full_image slides — verified true for both
        # the MinimalDeck compat path and the authored image_prompt path. So images
        # are selectable in the §4.5 DeckEditor canvas (which lowers separately) but
        # NOT via the §4.1 in-preview SelectionOverlay. Stamping image ids in
        # render_html needs live-preview verification before it's wired.


# ─── 12b: strip_element_ids pure helper ───────────────────────────────────────

class TestStripElementIds:
    """Tests for the pure ``strip_element_ids`` helper.

    This helper is RESTORED here as a tested utility but is NOT yet wired to
    any export path — see module docstring.  The contract:

    * ONLY ``data-element-id="..."`` and ``data-slide-id="..."`` are removed.
    * The single space immediately before each removed attribute is consumed
      along with it (the regex's leading ``\\s*``); no other whitespace is
      collapsed or modified.
    * All other text, attributes, CSS, JS, and preformatted content is
      preserved byte-for-byte.
    * Idempotent: stripping an already-clean string returns it unchanged.
    """

    def test_strips_data_element_id(self):
        html_str = '<h2 data-element-id="slide-0:title">Hi</h2>'
        out = strip_element_ids(html_str)
        assert "data-element-id" not in out
        assert out == "<h2>Hi</h2>"

    def test_strips_data_slide_id(self):
        html_str = '<section data-slide-id="slide-0">x</section>'
        out = strip_element_ids(html_str)
        assert "data-slide-id" not in out
        assert out == "<section>x</section>"

    def test_strips_both_attrs_in_one_tag(self):
        html_str = (
            '<h2 data-element-id="slide-0:title" data-slide-id="slide-0">Hi</h2>'
        )
        out = strip_element_ids(html_str)
        assert "data-element-id" not in out
        assert "data-slide-id" not in out
        # No double-space gap left behind — each attr carried its leading space.
        assert "  " not in out
        assert out == "<h2>Hi</h2>"

    def test_preserves_plain_id_attribute(self):
        """The plain ``id="slide-N"`` attribute MUST survive (only data-* is stripped)."""
        html_str = (
            '<section class="slide active" id="slide-0" '
            'data-slide-id="slide-0" data-layout="title" '
            'data-element-id="slide-0:wrap">x</section>'
        )
        out = strip_element_ids(html_str)
        assert 'id="slide-0"' in out
        assert 'class="slide active"' in out
        assert 'data-layout="title"' in out
        assert "data-element-id" not in out
        assert "data-slide-id" not in out

    def test_preserves_text_and_titles(self):
        html_str = (
            '<h2 data-element-id="slide-0:title" data-slide-id="slide-0">'
            "My Slide Title</h2>"
            '<li data-element-id="slide-0:body:0" data-slide-id="slide-0">'
            "Bullet A</li>"
        )
        out = strip_element_ids(html_str)
        assert "My Slide Title" in out
        assert "Bullet A" in out
        # Closing tags preserved.
        assert "</h2>" in out
        assert "</li>" in out

    def test_preserves_css_js_and_preformatted_whitespace(self):
        """CSS / JS / <pre> bodies MUST NOT be collapsed — only the two attrs are touched."""
        pre = "<pre>  two  spaces   and\n\tnewline</pre>"
        css = "<style>body  { color: red; } /* keep  spaces */</style>"
        js = "<script>var x=1;  if (x)  {  go();  }</script>"
        out = strip_element_ids(pre + css + js)
        assert out == pre + css + js

    def test_preserves_attr_literals_in_content(self):
        """The blocker case: the attribute STRINGS appearing inside element text, a
        <script>, or a <pre> (i.e. NOT as start-tag attributes) MUST survive. A blind
        whole-document regex corrupts these; removal must be scoped to <...> tags."""
        html_str = (
            '<p data-element-id="slide-0:body:0" data-slide-id="slide-0">'
            'the docs note data-element-id="x" is the editor id</p>'
            "<script>const s = ' data-slide-id=\"y\" ';</script>"
            '<pre>sample: data-element-id="z"</pre>'
        )
        out = strip_element_ids(html_str)
        assert "<p>" in out                      # real start-tag attrs removed
        assert 'data-element-id="x"' in out      # literal in text preserved
        assert 'data-slide-id="y"' in out        # literal in <script> preserved
        assert 'data-element-id="z"' in out      # literal in <pre> preserved

    def test_idempotent_on_already_clean_string(self):
        html_str = "<h2>Hi</h2><p>no attrs here</p>"
        out = strip_element_ids(html_str)
        assert out == html_str

    def test_idempotent_double_application(self):
        html_str = (
            '<h2 data-element-id="slide-0:title" data-slide-id="slide-0">Hi</h2>'
            '<li data-element-id="slide-0:body:0" data-slide-id="slide-0">A</li>'
        )
        once = strip_element_ids(html_str)
        twice = strip_element_ids(once)
        assert once == twice
        # And a third pass is still a no-op.
        assert strip_element_ids(twice) == twice

    def test_works_on_real_render_html_output(self):
        """End-to-end: feed a real ``render_html`` output through the helper."""
        deck = MinimalDeck(
            title="T",
            slides=[
                DeckSlide(title="Slide Zero", bullets=["A", "B"], layout="bullets"),
                DeckSlide(title="Slide One", bullets=[], layout="title"),
            ],
        )
        html_str = render_html(deck)

        # Sanity: the rendered HTML still stamps the editor-only attrs.
        assert "data-element-id" in html_str
        assert "data-slide-id" in html_str

        out = strip_element_ids(html_str)

        # Both attrs removed.
        assert "data-element-id" not in out
        assert "data-slide-id" not in out

        # Slide structure / text preserved: plain id, layout attr, titles, bullets.
        assert 'id="slide-0"' in out
        assert 'id="slide-1"' in out
        assert "Slide Zero" in out
        assert "Slide One" in out
        assert ">A<" in out or "A</li>" in out
        assert 'data-layout="bullets"' in out
        assert 'data-layout="title"' in out

        # Idempotence holds on the real output too.
        assert strip_element_ids(out) == out

    def test_does_not_touch_unrelated_data_attributes(self):
        """Only data-element-id and data-slide-id are removed — other data-* stay."""
        html_str = (
            '<div data-foo="bar" data-element-id="slide-0:x" '
            'data-baz="qux" data-slide-id="slide-0">y</div>'
        )
        out = strip_element_ids(html_str)
        assert "data-element-id" not in out
        assert "data-slide-id" not in out
        assert 'data-foo="bar"' in out
        assert 'data-baz="qux"' in out
        assert out == '<div data-foo="bar" data-baz="qux">y</div>'


# ─── 14: Registry membership ──────────────────────────────────────────────────

class TestRegistryMembership:
    def test_deck_patch_in_agent_tools(self):
        assert "deck_patch" in AGENT_TOOLS

    def test_deck_patch_in_artifact_tools(self):
        assert "deck_patch" in ARTIFACT_TOOLS

    def test_deck_patch_in_agent_scope(self):
        scope = agent_scope(model_policy=ModelExecutionPolicy.standard())
        assert "deck_patch" in scope.allowed_tools

    def test_deck_patch_in_artifact_scope(self):
        scope = artifact_scope()
        assert "deck_patch" in scope.allowed_tools

    def test_deck_patch_registered_in_default_registry(self):
        from disco.tools.builtin import build_default_registry
        reg = build_default_registry()
        scope = agent_scope(model_policy=ModelExecutionPolicy.standard())
        tool = reg.get("deck_patch", scope=scope)
        assert tool is not None
        assert tool.definition.name == "deck_patch"


# ─── 15–16: deck_schema lower functions ───────────────────────────────────────

class TestDeckSchema:
    def test_lower_deck_element_geometry(self):
        deck = AuthoredDeck(
            title="D",
            slides=[AuthoredSlide(type="bullets", title="S", body=["A"])],
        )
        lowered = lower_deck_for_editor(deck)
        assert len(lowered.slides) == 1
        title_el = next(e for e in lowered.slides[0].elements if e.kind == "title")
        # Title element must have a non-zero width
        assert title_el.geometry.w > 0
        assert title_el.geometry.h > 0

    def test_lower_deck_chart_element(self):
        deck = AuthoredDeck(
            title="D",
            slides=[
                AuthoredSlide(
                    type="metrics",
                    title="Q1",
                    chart=ChartSpec(
                        kind="bar",
                        title="Revenue",
                        labels=["Jan", "Feb"],
                        series=[{"name": "Sales", "data": [1.0, 2.0]}],
                    ),
                )
            ],
        )
        lowered = lower_deck_for_editor(deck)
        chart_el = next(
            (e for e in lowered.slides[0].elements if e.kind == "chart"), None
        )
        assert chart_el is not None
        assert "Revenue" in chart_el.content or "chart" in chart_el.content.lower()

    def test_lower_deck_image_full(self):
        deck = AuthoredDeck(
            title="D",
            slides=[
                AuthoredSlide(
                    type="full_image",
                    title="Visual",
                    body=[],
                    image_prompt="A futuristic city",
                )
            ],
        )
        lowered = lower_deck_for_editor(deck)
        img_el = next(
            (e for e in lowered.slides[0].elements if e.kind == "image_prompt"), None
        )
        assert img_el is not None
        assert "futuristic" in img_el.content

    def test_theme_mapping_disco_dark(self):
        deck = AuthoredDeck(
            title="D",
            theme="disco-dark",
            slides=[AuthoredSlide(type="title", title="T")],
        )
        lowered = lower_deck_for_editor(deck)
        assert lowered.theme_mode == "dark"

    def test_unknown_type_defaults_to_bullets(self):
        """Custom/unknown slide types must not crash — they fall back to bullets layout."""
        deck = AuthoredDeck(
            title="D",
            slides=[AuthoredSlide(type="custom_era_slide", title="T", body=["X"])],
        )
        lowered = lower_deck_for_editor(deck)
        assert lowered.slides[0].layout == "bullets"


@pytest.mark.asyncio
async def test_deck_patch_preserves_generated_images_on_edit(tmp_path):
    """Editing an image deck must NOT drop its images to placeholders: deck_patch
    reloads {stem}_img_{i}.png from the sandbox and re-embeds (C7)."""
    import io as _io
    import zipfile

    from disco.tools.anatomy import ToolContext
    from disco.tools.builtin._deck_patch import DeckPatchTool
    from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide
    from disco.tools.sandbox import SandboxSession
    from disco.tools.sandbox.process import ProcessSandboxService
    from PIL import Image

    # a deck with one image slide + its on-disk generated image
    authored = AuthoredDeck(title="T", theme="disco-light", slides=[
        AuthoredSlide(type="full_image", title="Cover", body=[], image_prompt="a tree"),
    ])
    buf = _io.BytesIO()
    Image.new("RGB", (64, 36), (10, 160, 60)).save(buf, format="PNG")
    png = buf.getvalue()

    svc = ProcessSandboxService(root=str(tmp_path))
    session = SandboxSession(svc, owner_id="o", conversation_id="c")
    await session.write_file("deck.authored.json", authored.model_dump_json().encode())
    await session.write_file("deck_img_0.png", png)  # the generated asset, by convention

    ctx = ToolContext(sandbox=session, workspace_path=".", timeout_s=60,
                      capabilities=None, owner_id="o", conversation_id="c")
    tool = DeckPatchTool()
    # a no-op-ish patch (change the title) to trigger a re-render
    from disco.tools.builtin._deck_patch import DeckPatchArgs
    args = DeckPatchArgs(deck_file="deck.authored.json",
                         patch=[{"op": "replace", "path": "/title", "value": "T2"}])
    out = await tool.run(args, ctx)
    assert out.success, out.error

    pptx = await session.read_file("deck.pptx")
    with zipfile.ZipFile(_io.BytesIO(pptx)) as z:
        media = [n for n in z.namelist() if n.startswith("ppt/media/")]
    assert media, "edit dropped the image — re-render did not re-embed the generated asset"
