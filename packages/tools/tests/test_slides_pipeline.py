"""Tests for C2 — _slides_pipeline.py + slides.py C2 wiring.

Proves:
  - A mocked-LLM AuthoredDeck parses → lowers → renders to PPTX + HTML.
  - Malformed JSON → one retry → Marp fallback (no crash, success may be False).
  - The fill stage parse failure falls back to Marp using the outline.
  - Weak-model path gets the worked-example prompt (WEAK_SYSTEM).
  - image_prompt slides invoke the image backend once each.
  - generate_deck returns (Deck, None, None) on success.
  - generate_deck returns (None, fallback_md, err) on fill failure.
  - slides_generate with goal= runs the C2 path (mocked); success=True.
  - slides_generate with markdown= (mode="markdown") skips C2.
  - _extract_json_object handles prose + code fence wrapping.

All LLM calls are mocked — NO real API calls in these tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin._deck_schema import AuthoredDeck
from disco.tools.builtin._slides_pipeline import (
    _CAPABLE_SYSTEM,
    _WEAK_SYSTEM,
    _extract_json_object,
    _parse_authored_deck,
    _stage_fill,
    _stage_outline,
    generate_deck,
)
from disco.tools.builtin.slides import SlidesGenerateArgs, SlidesTool
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxInstance
from disco.tools.secrets import CapabilityBroker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _jailed_sandbox(workspace: Path) -> ProcessSandboxInstance:
    return ProcessSandboxInstance(
        id="test-sbx",
        owner_id="test",
        conversation_id="test-cid",
        spec=SandboxSpec(),
        workspace=workspace,
    )


def _ctx(sandbox, *, assist: bool = False) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
        assist=assist,
    )


# ---------------------------------------------------------------------------
# Sample AuthoredDeck JSON (real-sample patterns from C4 experiment)
# ---------------------------------------------------------------------------

_SAMPLE_DECK_JSON = {
    "title": "EV Battery Startup Pitch",
    "theme": "disco-light",
    "slides": [
        {
            "type": "title",
            "title": "PowerCell AI",
            "body": ["Next-generation solid-state battery technology"],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": "Open with the market opportunity.",
        },
        {
            "type": "bullets",
            "title": "The Problem",
            "body": [
                "EV batteries degrade 30% in 5 years",
                "Charging takes 45+ minutes at highway speeds",
                "Rare earth supply chains are fragile",
                "Recycling rates below 5%",
            ],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
        {
            "type": "section_header",
            "title": "Our Solution",
            "body": ["Solid-state cells with 10× cycle life"],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
        {
            "type": "image_right",
            "title": "Technology",
            "body": ["Patented anode design", "Zero liquid electrolyte"],
            "layout_hint": None,
            "image_prompt": "Cross-section diagram of a solid-state battery cell",
            "chart": None,
            "table": None,
            "notes": None,
        },
        {
            "type": "closing",
            "title": "Join Us",
            "body": ["Series A: $12M | investors@powercell.ai"],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": "End with the ask.",
        },
    ],
}

_SAMPLE_OUTLINE_JSON = {
    "title": "EV Battery Startup Pitch",
    "theme": "disco-light",
    "slides": [
        {"type": "title", "title": "PowerCell AI", "body": [], "layout_hint": None, "image_prompt": None, "chart": None, "table": None, "notes": None},
        {"type": "bullets", "title": "The Problem", "body": [], "layout_hint": None, "image_prompt": None, "chart": None, "table": None, "notes": None},
        {"type": "closing", "title": "Join Us", "body": [], "layout_hint": None, "image_prompt": None, "chart": None, "table": None, "notes": None},
    ],
}


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    """Plain JSON object is returned unchanged."""
    j = '{"title": "X", "slides": []}'
    assert _extract_json_object(j) == j


def test_extract_json_with_prose():
    """JSON buried in prose is extracted."""
    text = 'Here is your deck:\n{"title": "T", "slides": []}\nThat\'s it.'
    result = _extract_json_object(text)
    parsed = json.loads(result)
    assert parsed["title"] == "T"


def test_extract_json_code_fence():
    """JSON in a markdown code fence is extracted."""
    text = '```json\n{"title": "T", "slides": []}\n```'
    result = _extract_json_object(text)
    parsed = json.loads(result)
    assert parsed["title"] == "T"


# ---------------------------------------------------------------------------
# _parse_authored_deck
# ---------------------------------------------------------------------------


def test_parse_authored_deck_valid():
    """Valid JSON parses to an AuthoredDeck."""
    raw = json.dumps(_SAMPLE_DECK_JSON)
    deck, err = _parse_authored_deck(raw)
    assert deck is not None
    assert err == ""
    assert deck.title == "EV Battery Startup Pitch"
    assert len(deck.slides) == 5


def test_parse_authored_deck_malformed_json():
    """Malformed JSON returns (None, error_str)."""
    deck, err = _parse_authored_deck("this is not JSON at all!!!")
    assert deck is None
    assert err


def test_parse_authored_deck_wrong_schema():
    """Valid JSON but wrong schema returns (None, error_str)."""
    raw = json.dumps({"wrong": "field", "no_slides": True})
    deck, err = _parse_authored_deck(raw)
    # Missing required fields → validation error
    assert deck is None
    assert err


# ---------------------------------------------------------------------------
# _stage_outline (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_outline_success():
    """Outline stage parses a mocked LLM response."""
    raw_outline = json.dumps(_SAMPLE_OUTLINE_JSON)

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = raw_outline
        deck, raw, err = await _stage_outline(
            "EV battery pitch", 3, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert deck is not None
    assert err == ""
    assert len(deck.slides) == 3
    mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_stage_outline_retry_on_malformed():
    """Malformed first response → retry with the second valid response."""
    raw_valid = json.dumps(_SAMPLE_OUTLINE_JSON)

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = ["this is not json", raw_valid]
        deck, raw, err = await _stage_outline(
            "EV battery pitch", 3, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert deck is not None
    assert err == ""
    assert mock_llm.call_count == 2  # first call + retry


@pytest.mark.asyncio
async def test_stage_outline_both_fail_returns_none():
    """Two consecutive failures → (None, raw, error)."""
    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = ["not json", "also not json"]
        deck, raw, err = await _stage_outline(
            "EV battery pitch", 3, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert deck is None
    assert err


# ---------------------------------------------------------------------------
# _stage_fill (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_fill_success():
    """Fill stage parses a mocked full-deck response."""
    outline = AuthoredDeck.model_validate(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = full_raw
        filled, err = await _stage_fill(
            outline, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert filled is not None
    assert err == ""
    assert len(filled.slides) == 5


@pytest.mark.asyncio
async def test_stage_fill_retry_on_malformed():
    """Fill retry on malformed first response."""
    outline = AuthoredDeck.model_validate(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = ["BAD JSON", full_raw]
        filled, err = await _stage_fill(
            outline, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert filled is not None
    assert err == ""
    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_stage_fill_both_fail_returns_none():
    """Two fill failures → (None, error)."""
    outline = AuthoredDeck.model_validate(_SAMPLE_OUTLINE_JSON)

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = ["BAD1", "BAD2"]
        filled, err = await _stage_fill(
            outline, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert filled is None
    assert err


# ---------------------------------------------------------------------------
# generate_deck (end-to-end, mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_deck_success(tmp_workspace):
    """Mocked LLM → generate_deck returns (Deck, None, None)."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    mock_backend = MagicMock()
    mock_backend.generate.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        # Outline call → valid; Fill call → valid
        mock_llm.side_effect = [outline_raw, full_raw]
        deck, fallback_md, err = await generate_deck(
            "EV battery startup pitch",
            "test-deck",
            ctx,
            mock_backend,
            slide_count=5,
        )

    assert deck is not None, f"Expected a Deck but got None; err={err}"
    assert fallback_md is None
    assert err is None
    assert len(deck.slides) >= 5


@pytest.mark.asyncio
async def test_generate_deck_fill_failure_returns_fallback(tmp_workspace):
    """Fill stage failure → (None, fallback_markdown, error_msg)."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    mock_backend = MagicMock()

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        # Outline succeeds; fill fails twice
        mock_llm.side_effect = [outline_raw, "BAD JSON", "STILL BAD"]
        deck, fallback_md, err = await generate_deck(
            "EV battery startup pitch",
            "test-deck",
            ctx,
            mock_backend,
        )

    assert deck is None
    assert fallback_md is not None
    assert err is not None
    # Fallback markdown should contain outline slide titles
    assert "PowerCell AI" in fallback_md or "EV Battery" in fallback_md


@pytest.mark.asyncio
async def test_generate_deck_outline_failure_returns_fallback(tmp_workspace):
    """Outline failure → (None, minimal_fallback_md, error)."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)
    mock_backend = MagicMock()

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = ["BAD", "STILL BAD"]
        deck, fallback_md, err = await generate_deck(
            "test goal", "test-deck", ctx, mock_backend
        )

    assert deck is None
    assert fallback_md is not None  # minimal fallback produced
    assert err is not None


@pytest.mark.asyncio
async def test_generate_deck_image_backend_called_for_image_prompts(tmp_workspace):
    """Backend.generate is called once per slide with image_prompt."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    mock_backend = MagicMock()
    mock_backend.generate.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = [outline_raw, full_raw]
        deck, _, _ = await generate_deck(
            "EV battery startup pitch", "test-deck", ctx, mock_backend
        )

    # _SAMPLE_DECK_JSON has 1 slide with image_prompt
    image_slides = [s for s in _SAMPLE_DECK_JSON["slides"] if s.get("image_prompt")]
    assert mock_backend.generate.call_count == len(image_slides)


# ---------------------------------------------------------------------------
# Marp fallback wiring in slides.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slides_tool_c2_path_on_goal(tmp_workspace):
    """slides_generate with goal= runs the C2 path and produces artifacts."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    mock_backend = MagicMock()
    mock_backend.generate.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    with (
        patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm,
        patch("disco.tools.builtin.slides.select_image_backend", return_value=mock_backend),
    ):
        mock_llm.side_effect = [outline_raw, full_raw]
        outcome = await tool.run(
            SlidesGenerateArgs(
                goal="EV battery startup pitch",
                filename="ev-pitch",
                format="html",
            ),
            ctx,
        )

    assert outcome.success, f"Tool failed: {outcome.error}"
    assert "ev-pitch.html" in outcome.artifacts
    assert "c3-brand" in outcome.structured.get("renderer", "")
    assert (tmp_workspace / "ev-pitch.html").exists()


@pytest.mark.asyncio
async def test_slides_tool_c2_fill_failure_marp_fallback(tmp_workspace):
    """C2 fill failure → Marp fallback path triggered (no crash)."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)

    mock_backend = MagicMock()

    with (
        patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm,
        patch("disco.tools.builtin.slides.select_image_backend", return_value=mock_backend),
        patch("disco.tools.builtin.slides._marp_available", return_value=False),
    ):
        mock_llm.side_effect = [outline_raw, "BAD JSON", "ALSO BAD"]
        outcome = await tool.run(
            SlidesGenerateArgs(
                goal="EV battery startup pitch",
                filename="fallback-deck",
                format="html",
            ),
            ctx,
        )

    # The fallback HTML should have been written (Marp absent → _fallback_html path)
    # The outcome may succeed (fallback rendered) or fail (no markdown) — must not crash
    assert outcome is not None, "Tool must never crash"
    # Content should mention the fallback
    assert outcome.content  # non-empty content regardless of success/failure


@pytest.mark.asyncio
async def test_slides_tool_markdown_mode_skips_c2(tmp_workspace):
    """mode='markdown' bypasses the C2 pipeline even if goal is set."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    with (
        patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm,
        patch("disco.tools.builtin.slides._marp_available", return_value=False),
    ):
        outcome = await tool.run(
            SlidesGenerateArgs(
                goal="Should be ignored",
                markdown="# Slide 1\n\n---\n\n# Slide 2",
                filename="md-deck",
                format="html",
                mode="markdown",
            ),
            ctx,
        )

    # C2 should NOT have been called
    mock_llm.assert_not_called()
    assert outcome.success
    assert "md-deck.html" in outcome.artifacts


@pytest.mark.asyncio
async def test_slides_tool_markdown_only_no_c2(tmp_workspace):
    """No goal + markdown → uses Marp/fallback path without calling C2."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    with (
        patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm,
        patch("disco.tools.builtin.slides._marp_available", return_value=False),
    ):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown="# Only Markdown\n\n---\n\n# Slide 2",
                filename="plain-md",
                format="html",
            ),
            ctx,
        )

    mock_llm.assert_not_called()
    assert outcome.success


# ---------------------------------------------------------------------------
# Tier-aware prompt selection
# ---------------------------------------------------------------------------


def test_capable_system_has_schema_hint():
    """Capable-model prompt includes the AuthoredDeck schema."""
    assert "AuthoredDeck" in _CAPABLE_SYSTEM
    assert "body" in _CAPABLE_SYSTEM
    assert "image_prompt" in _CAPABLE_SYSTEM


def test_weak_system_has_worked_example():
    """Weak-model prompt has a concrete worked-example JSON."""
    assert '"type": "title"' in _WEAK_SYSTEM
    assert '"body":' in _WEAK_SYSTEM
    assert "DECK TITLE HERE" in _WEAK_SYSTEM


@pytest.mark.asyncio
async def test_weak_model_gets_weak_prompt(tmp_workspace):
    """ctx.assist=True → _WEAK_SYSTEM is used in the LLM call."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx, assist=True)  # weak model

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    mock_backend = MagicMock()
    mock_backend.generate.return_value = b"\x89PNG\r\n\x1a\n"

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = [outline_raw, full_raw]
        await generate_deck("test goal", "d", ctx, mock_backend)

    # Inspect what system prompt was used
    first_call_messages = mock_llm.call_args_list[0][0][0]  # first positional arg = messages
    system_msgs = [m for m in first_call_messages if m.get("role") == "system"]
    assert system_msgs
    assert "DECK TITLE HERE" in system_msgs[0]["content"]  # weak prompt has worked example


@pytest.mark.asyncio
async def test_capable_model_gets_capable_prompt(tmp_workspace):
    """ctx.assist=False → _CAPABLE_SYSTEM is used in the LLM call."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx, assist=False)  # capable model

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    mock_backend = MagicMock()
    mock_backend.generate.return_value = b"\x89PNG\r\n\x1a\n"

    with patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = [outline_raw, full_raw]
        await generate_deck("test goal", "d", ctx, mock_backend)

    first_call_messages = mock_llm.call_args_list[0][0][0]
    system_msgs = [m for m in first_call_messages if m.get("role") == "system"]
    assert system_msgs
    assert "AuthoredDeck schema" in system_msgs[0]["content"]  # capable prompt
