"""Tests for C2 — _slides_pipeline.py + slides.py C2 wiring.

Proves:
  - A mocked-LLM AuthoredDeck parses → lowers → renders to PPTX + HTML.
  - Malformed authored JSON gets one correction attempt and is not delivered as
    a successful plain substitute.
  - The internal fill stage retains diagnostic outline markdown without the public
    tool rendering it under a goal-based request.
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

# ruff: noqa: E501
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from disco.core.llm.config_approvals import ConfigOriginApprovals
from disco.tools.anatomy import ToolContext
from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide, lower_deck
from disco.tools.builtin._pptx_render import render_html
from disco.tools.builtin._slides_pipeline import (
    _CAPABLE_SYSTEM,
    _VALID_THEMES,
    _WEAK_SYSTEM,
    _coerce_known_theme_aliases,
    _extract_json_object,
    _parse_authored_deck,
    _purpose_for_model_endpoint,
    _retry_msg,
    _stage_assets,
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


@pytest.fixture(autouse=True)
def _approve_mocked_llm_origins(monkeypatch):
    monkeypatch.setattr(ConfigOriginApprovals, "origin_approved", lambda *args, **kwargs: True)


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
    "accent_palette": [
        {"name": "cerulean", "value": "#4077a3", "role": "primary emphasis"},
        {"name": "moss", "value": "#397852", "role": "supportive proof"},
        {"name": "paper", "value": "#d8b26e", "role": "warm section contrast"},
    ],
    "font_pairing": {"display": "Fraunces", "body": "Newsreader", "ui": "Schibsted Grotesk"},
    "token_pair": {
        "light_bg": "#fcfcfa",
        "light_text": "#1a1813",
        "dark_bg": "#0d1017",
        "dark_text": "#e9e6df",
    },
    "art_direction": "grainy editorial risograph, cerulean and warm paper palette, soft grain, no text",
    "slides": [
        {
            "type": "title",
            "archetype": "title",
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
            "archetype": "bullets",
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
            "archetype": "section_divider",
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
            "archetype": "diagram",
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
            "archetype": "closing",
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
    "accent_palette": [
        {"name": "cerulean", "value": "#4077a3", "role": "primary emphasis"},
        {"name": "moss", "value": "#397852", "role": "supportive proof"},
        {"name": "paper", "value": "#d8b26e", "role": "warm section contrast"},
    ],
    "font_pairing": {"display": "Fraunces", "body": "Newsreader", "ui": "Schibsted Grotesk"},
    "token_pair": {
        "light_bg": "#fcfcfa",
        "light_text": "#1a1813",
        "dark_bg": "#0d1017",
        "dark_text": "#e9e6df",
    },
    "art_direction": "grainy editorial risograph, cerulean and warm paper palette, soft grain, no text",
    "slides": [
        {
            "type": "title",
            "archetype": "title",
            "title": "PowerCell AI",
            "body": [],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
        {
            "type": "bullets",
            "archetype": "bullets",
            "title": "The Problem",
            "body": [],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
        {
            "type": "closing",
            "archetype": "closing",
            "title": "Join Us",
            "body": [],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        },
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


def test_parse_authored_deck_drops_layout_names_from_optional_chart_only():
    data = json.loads(json.dumps(_SAMPLE_DECK_JSON))
    for slide, kind in zip(data["slides"], ("flow", "two_by_two", "system_map"), strict=False):
        slide["chart"] = {
            "kind": kind,
            "title": "Not actually a chart",
            "labels": ["A"],
            "series": [{"name": "Value", "data": [1]}],
        }
    data["slides"][3]["chart"] = {
        "kind": "bar",
        "title": "Real values",
        "labels": ["A", "B"],
        "series": [{"name": "Value", "data": [1, 2]}],
    }

    deck, err = _parse_authored_deck(json.dumps(data))

    assert deck is not None, err
    assert [slide.chart for slide in deck.slides[:3]] == [None, None, None]
    assert deck.slides[3].chart is not None
    assert deck.slides[3].chart.kind == "bar"


def test_parse_authored_deck_retries_unknown_chart_typos():
    data = json.loads(json.dumps(_SAMPLE_DECK_JSON))
    data["slides"][1]["chart"] = {"kind": "banana"}

    deck, err = _parse_authored_deck(json.dumps(data))

    assert deck is None
    assert "chart.kind" in err or "chart" in err


# ---------------------------------------------------------------------------
# _stage_outline (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_outline_success():
    """Outline stage parses a mocked LLM response."""
    raw_outline = json.dumps(_SAMPLE_OUTLINE_JSON)

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.return_value = raw_outline
        deck, raw, err = await _stage_outline(
            "EV battery pitch", 3, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert deck is not None
    assert err == ""
    assert len(deck.slides) == 3
    assert deck.art_direction
    assert len(deck.accent_palette) >= 3
    assert deck.font_pairing is not None
    assert deck.token_pair is not None
    assert [s.archetype for s in deck.slides] == ["title", "bullets", "closing"]
    mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_stage_outline_retry_on_malformed():
    """Malformed first response → retry with the second valid response."""
    raw_valid = json.dumps(_SAMPLE_OUTLINE_JSON)

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
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
    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.return_value = full_raw
        filled, err = await _stage_fill(
            outline, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert filled is not None
    assert err == ""
    assert len(filled.slides) == 5
    assert filled.art_direction
    assert filled.slides[0].image_prompt
    assert "full-bleed background" in filled.slides[0].image_prompt
    assert filled.slides[2].image_prompt
    assert "divider art" in filled.slides[2].image_prompt
    assert "no words, no lettering" in filled.slides[2].image_prompt
    assert filled.slides[1].image_prompt is None


@pytest.mark.asyncio
async def test_stage_fill_enforces_density_and_visual_image_slots():
    """Fill post-processing caps overfull fake LLM output and composes required A8 slots."""
    outline = AuthoredDeck.model_validate(_SAMPLE_OUTLINE_JSON)
    overfull = {
        "title": "Deck",
        "theme": "disco-light",
        "art_direction": "grainy risograph, teal and sand, soft grain, no text",
        "slides": [
            {
                "type": "title",
                "archetype": "title",
                "title": "It's not process. It's leverage.",
                "body": ["A subtitle that should stay short"],
                "layout_hint": None,
                "image_prompt": None,
                "chart": None,
                "table": None,
                "notes": None,
            },
            {
                "type": "bullets",
                "archetype": "bullets",
                "title": "Operating Model",
                "body": [
                    "This line has far too many words for a proper slide bullet budget",
                    "Second line also carries too many words for this format",
                    "Third line also carries too many words for this format",
                    "Fourth line also carries too many words for this format",
                    "Fifth line also carries too many words for this format",
                    "Sixth line should be removed by the budget",
                ],
                "layout_hint": None,
                "image_prompt": None,
                "chart": None,
                "table": None,
                "notes": None,
            },
            {
                "type": "full_image",
                "archetype": "full_bleed_image",
                "title": "Future State",
                "body": ["One concise caption", "Second concise caption", "Third line drops"],
                "layout_hint": None,
                "image_prompt": None,
                "chart": None,
                "table": None,
                "notes": None,
            },
        ],
    }

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.return_value = json.dumps(overfull)
        filled, err = await _stage_fill(
            outline, _CAPABLE_SYSTEM, "http://localhost/v1", "test-model"
        )

    assert filled is not None
    assert err == ""
    assert filled.slides[0].title == "leverage"
    assert (
        filled.slides[0].image_prompt and "full-bleed background" in filled.slides[0].image_prompt
    )
    assert len(filled.slides[1].body) == 5
    assert all(len(line.split()) <= 9 for line in filled.slides[1].body)
    assert len(filled.slides[2].body) == 2
    assert (
        filled.slides[2].image_prompt and "no words, no lettering" in filled.slides[2].image_prompt
    )
    assert filled.slides[2].layout_hint == "full_image"


@pytest.mark.asyncio
async def test_stage_fill_retry_on_malformed():
    """Fill retry on malformed first response."""
    outline = AuthoredDeck.model_validate(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        # Outline call → valid; Fill call → valid
        mock_llm.side_effect = [outline_raw, full_raw]
        deck, fallback_md, err, _, _ = await generate_deck(
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
    """Retain the historical inventory ID while proving fallback is internal-only."""
    """The internal pipeline retains outline text, but the public tool never ships it."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    mock_backend = MagicMock()

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        # Outline succeeds; fill fails twice
        mock_llm.side_effect = [outline_raw, "BAD JSON", "STILL BAD"]
        deck, fallback_md, err, _, _ = await generate_deck(
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.side_effect = ["BAD", "STILL BAD"]
        deck, fallback_md, err, _, _ = await generate_deck(
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.side_effect = [outline_raw, full_raw]
        deck, _, _, _, _ = await generate_deck(
            "EV battery startup pitch", "test-deck", ctx, mock_backend
        )

    # Fill post-processing adds required A8 slots: cover + section divider + explicit visual.
    assert mock_backend.generate.call_count == 3
    prompts = [call.kwargs["prompt"] for call in mock_backend.generate.call_args_list]
    assert any("full-bleed background" in prompt for prompt in prompts)
    assert any("divider art" in prompt for prompt in prompts)
    assert all("no words, no lettering" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_generate_deck_degrades_image_less_when_backend_none(tmp_workspace):
    """W-50: when image-gen is unconfigured the slides tool passes backend=None.
    generate_deck must DEGRADE to a text-only deck — a real Deck is still produced,
    no images are embedded, and nothing crashes."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.side_effect = [outline_raw, full_raw]
        deck, fallback_md, err, _, _ = await generate_deck(
            "EV battery startup pitch",
            "test-deck",
            ctx,
            None,  # W-50: no image backend configured → degrade, don't crash
            slide_count=5,
        )

    assert deck is not None, f"deck must still render image-less; err={err}"
    assert fallback_md is None
    assert err is None
    # Image slots remain present, but no generated bytes are embedded.
    assert not any(
        el.image_bytes for slide in deck.slides for el in slide.elements if el.kind == "image"
    )


@pytest.mark.asyncio
async def test_stage_assets_invokes_backend_without_sandbox():
    """The asset stage returns C7 bytes even when there is no sandbox to write files."""
    ctx = ToolContext.model_construct(sandbox=None)
    deck = AuthoredDeck(
        title="Visual Deck",
        slides=[
            AuthoredSlide(
                type="full_image",
                archetype="full_bleed_image",
                title="Hero",
                body=[],
                image_prompt="grainy risograph; subject: Hero; slot: full-bleed background; no words, no lettering",
            )
        ],
    )
    backend = MagicMock()
    backend.generate.return_value = b"\x89PNG\r\n\x1a\n" + b"0" * 16

    assets, _stats = await _stage_assets(deck, ctx, backend, "visual")

    assert assets[0].startswith(b"\x89PNG")
    backend.generate.assert_called_once()


@pytest.mark.asyncio
async def test_stage_assets_reuses_cached_prompt_hash(tmp_workspace):
    """A matching sidecar hash reuses the cached raster instead of regenerating."""
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)
    deck = AuthoredDeck(
        title="Visual Deck",
        slides=[
            AuthoredSlide(
                type="full_image",
                archetype="full_bleed_image",
                title="Hero",
                body=[],
                image_prompt="grainy risograph; subject: Hero; slot: full-bleed background; no words, no lettering",
            )
        ],
    )
    backend = MagicMock()
    backend.generate.return_value = b"\x89PNG\r\n\x1a\n" + b"0" * 16

    first, _ = await _stage_assets(deck, ctx, backend, "visual")
    second, _ = await _stage_assets(deck, ctx, backend, "visual")

    assert first == second
    backend.generate.assert_called_once()
    assert (tmp_workspace / "visual_img_0.png").exists()
    assert (tmp_workspace / "visual_img_0.sha256").exists()


def test_full_image_html_uses_scrim_for_text_over_image():
    """Full-bleed image slides protect text with a gradient scrim in HTML."""
    authored = AuthoredDeck(
        title="Visual Deck",
        slides=[
            AuthoredSlide(
                type="full_image",
                archetype="full_bleed_image",
                title="Hero",
                body=["Short caption"],
                layout_hint="full_image",
                image_prompt="grainy risograph; subject: Hero; slot: full-bleed background; no words, no lettering",
            )
        ],
    )
    deck = lower_deck(authored, image_assets={0: b"\x89PNG\r\n\x1a\n" + b"0" * 16})
    html = render_html(deck)

    assert "slide-image-scrim" in html
    assert "slide-full-image-bg" in html
    assert "data:image/png;base64" in html


@pytest.mark.asyncio
async def test_slides_tool_degrades_when_image_gen_unconfigured(tmp_workspace):
    """W-50 end-to-end: SlidesTool._run_c2_pipeline catches ImageGenNotConfigured from
    select_image_backend() and still produces a deck (slides render without images)."""
    from disco.tools.builtin.image_gen import ImageGenNotConfigured

    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)
    full_raw = json.dumps(_SAMPLE_DECK_JSON)

    def _raise() -> object:
        raise ImageGenNotConfigured()

    with (
        patch("disco.tools.builtin.slides.select_image_backend", side_effect=_raise),
        patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm,
    ):
        mock_llm.side_effect = [outline_raw, full_raw]
        out = await SlidesTool().run(
            SlidesGenerateArgs(goal="EV battery startup pitch", filename="deck", format="html"),
            ctx,
        )

    assert out.success is True, f"slides must render image-less, not crash: {out.content}"


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
async def test_c2_persists_authored_json_roundtrip(tmp_workspace):
    """A2.0: a C2 slides_generate writes a valid {base}.authored.json that
    round-trips AuthoredDeck.model_validate, and structured carries editable_source."""
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
            SlidesGenerateArgs(goal="EV battery startup pitch", filename="ev-deck", format="html"),
            ctx,
        )

    assert outcome.success, f"Tool failed: {outcome.error}"
    # The sidecar must exist and round-trip the schema.
    sidecar = tmp_workspace / "ev-deck.authored.json"
    assert sidecar.exists(), "authored.json sidecar was not written"
    deck = AuthoredDeck.model_validate(json.loads(sidecar.read_text()))
    assert deck.title == "EV Battery Startup Pitch"
    assert len(deck.slides) == 5
    # structured must advertise the editable source for the editor.
    assert outcome.structured.get("editable_source") == "ev-deck.authored.json"


@pytest.mark.asyncio
async def test_c2_pptx_carries_editable_source(tmp_workspace):
    """A2.0: the PPTX C2 path also carries editable_source + writes the sidecar."""
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
                goal="EV battery startup pitch", filename="ev-deck-px", format="pptx"
            ),
            ctx,
        )

    assert outcome.success, f"Tool failed: {outcome.error}"
    assert (tmp_workspace / "ev-deck-px.authored.json").exists()
    assert outcome.structured.get("editable_source") == "ev-deck-px.authored.json"


@pytest.mark.asyncio
async def test_slides_tool_c2_fill_failure_marp_fallback(tmp_workspace):
    """Retain the historical inventory ID while proving C2 cannot emit Marp."""
    """A goal-based authored failure is a failure, never a successful plain deck."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    outline_raw = json.dumps(_SAMPLE_OUTLINE_JSON)

    mock_backend = MagicMock()

    with (
        patch("disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock) as mock_llm,
        patch("disco.tools.builtin.slides.select_image_backend", return_value=mock_backend),
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

    assert outcome.success is False
    assert outcome.artifacts == []
    assert outcome.error is not None
    assert "Authored slide generation failed before delivery" in outcome.content
    assert outcome.structured == {
        "degraded": False,
        "renderer": "authored",
        "stage": "authoring",
    }
    assert list(tmp_workspace.iterdir()) == []


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
    assert "archetype" in _CAPABLE_SYSTEM
    assert "accent_palette" in _CAPABLE_SYSTEM
    assert "art_direction" in _CAPABLE_SYSTEM
    assert "no words, no lettering" in _CAPABLE_SYSTEM


def test_weak_system_has_worked_example():
    """Weak-model prompt has a concrete worked-example JSON."""
    assert '"type": "title"' in _WEAK_SYSTEM
    assert '"archetype": "title"' in _WEAK_SYSTEM
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
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

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.side_effect = [outline_raw, full_raw]
        await generate_deck("test goal", "d", ctx, mock_backend)

    first_call_messages = mock_llm.call_args_list[0][0][0]
    system_msgs = [m for m in first_call_messages if m.get("role") == "system"]
    assert system_msgs
    assert "AuthoredDeck schema" in system_msgs[0]["content"]  # capable prompt


@pytest.mark.asyncio
async def test_render_c1_deck_gates_editable_source_on_real_sidecar(tmp_workspace):
    """#4 false-affordance fix: `_render_c1_deck` advertises `editable_source` ONLY
    when the AuthoredDeck sidecar actually exists on disk. generate_deck writes it
    best-effort; a swallowed write failure must NOT surface an "Edit Slides" tab that
    404s on load. Proven BOTH directions."""
    from disco.tools.builtin._deck_schema import lower_deck

    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)
    deck = lower_deck(AuthoredDeck.model_validate(_SAMPLE_DECK_JSON))
    tool = SlidesTool()
    args = SlidesGenerateArgs(filename="pitch", goal="x", format="html")

    # No fresh sidecar this run (write failed / Marp deck) → editor NOT advertised,
    # even if a stale pitch.authored.json is lying around on disk.
    await sbx.write_file("pitch.authored.json", b'{"stale": true}')
    outcome = await tool._render_c1_deck(deck, args, ctx, "html", editable_source=None)
    assert outcome.success
    assert "editable_source" not in (outcome.structured or {})

    # A fresh sidecar written THIS run (generate_deck returned its path) → advertised.
    outcome2 = await tool._render_c1_deck(
        deck, args, ctx, "html", editable_source="pitch.authored.json"
    )
    assert (outcome2.structured or {}).get("editable_source") == "pitch.authored.json"


# ---- remote-driver auth (deck author can use a paid/remote LLM) --------------


@pytest.mark.parametrize(("stored_ref", "runtime_ref"), [("", None), (None, "")])
def test_keyless_driver_endpoint_normalizes_empty_secret_refs(
    stored_ref: str | None, runtime_ref: str | None
) -> None:
    """S01: keyless endpoints must match whether config/runtime encode no key as
    an empty string or None. A one-sided normalization silently returned
    ``model:unknown``, failed origin approval, and degraded a deck to Marp."""

    entry = MagicMock(
        base_url="http://127.0.0.1:18080/v1",
        provider="local-driver",
        api_key_env=stored_ref,
    )
    cfg = MagicMock(models={"driver": entry})

    assert (
        _purpose_for_model_endpoint(
            cfg,
            "http://127.0.0.1:18080/v1",
            runtime_ref,
        )
        == "model:local-driver"
    )


@pytest.mark.asyncio
async def test_call_llm_sends_bearer_when_api_key_present():
    """_call_llm sends Authorization: Bearer when an api_key is given (so a remote
    driver authenticates), and omits it for a keyless local endpoint."""
    from disco.tools.builtin._slides_pipeline import _call_llm

    captured: dict = {}

    def fake_client(*a, **k):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        async def _post(url, json=None, headers=None):
            captured["headers"] = headers
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"choices": [{"message": {"content": "{}"}}]})
            return resp

        client.post = _post
        return client

    with patch("disco.tools.builtin._slides_pipeline.httpx.AsyncClient", fake_client):
        await _call_llm(
            [{"role": "user", "content": "hi"}],
            "https://openrouter.ai/api/v1",
            "m",
            api_key="sk-secret",
        )
        assert captured["headers"].get("Authorization") == "Bearer sk-secret"

        captured.clear()
        await _call_llm([{"role": "user", "content": "hi"}], "http://localhost:18080/v1", "m")
        assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_call_llm_retries_one_transient_500_then_succeeds():
    from disco.tools.builtin._slides_pipeline import _call_llm

    statuses = [500, 200]
    calls = 0

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            nonlocal calls
            status = statuses[calls]
            calls += 1
            return httpx.Response(
                status,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": "{}"}}]},
            )

    with patch("disco.tools.builtin._slides_pipeline.httpx.AsyncClient", lambda **_: _Client()):
        result = await _call_llm(
            [{"role": "user", "content": "hi"}],
            "https://provider.example/v1",
            "model",
        )

    assert result.content == "{}"
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("statuses", "expected_calls"), [([500, 500], 2), ([400], 1)])
async def test_call_llm_bounds_retries(statuses, expected_calls):
    from disco.tools.builtin._slides_pipeline import _call_llm

    calls = 0

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            nonlocal calls
            status = statuses[min(calls, len(statuses) - 1)]
            calls += 1
            return httpx.Response(status, request=httpx.Request("POST", url))

    with (
        patch("disco.tools.builtin._slides_pipeline.httpx.AsyncClient", lambda **_: _Client()),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await _call_llm(
            [{"role": "user", "content": "hi"}],
            "https://provider.example/v1",
            "model",
        )

    assert calls == expected_calls


def test_resolve_slides_llm_resolves_remote_key(monkeypatch, tmp_path):
    """_resolve_slides_llm returns the AGENT_DRIVER entry's resolved api_key so the
    deck author authenticates to a remote endpoint (the gap that 401'd paid drivers)."""
    from disco.core.llm.secret_refs import set_openrouter_key
    from disco.core.llm.secrets import SecretStore
    from disco.tools.builtin import _slides_pipeline as sp

    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_SECRET_KEY", "slides-test-secret-key-32-bytes")
    set_openrouter_key(SecretStore(), "sk-live-123")

    class _Entry:
        base_url = "https://openrouter.ai/api/v1"
        model_id = "some/model:free"
        provider = "openrouter"
        api_key_env = "openrouter"

    class _Cfg:
        models = {"driver": _Entry()}

        def model_for(self, _role):
            return "driver"

    class _Approvals:
        def origin_approved(self, *_args, **_kwargs):
            return True

    class _Store:
        approvals = _Approvals()

        def load(self):
            return _Cfg()

    monkeypatch.setattr("disco.core.llm.ConfigStore", lambda: _Store())
    url, model, key = sp._resolve_slides_llm()
    assert url == "https://openrouter.ai/api/v1"
    assert model == "some/model:free"
    assert key == "sk-live-123"


def test_resolve_llm_key_reads_reserved_openrouter_slot(monkeypatch):
    """The OpenRouter driver key lives in the RESERVED 'openrouter' SecretStore slot,
    not under its env-var name — _resolve_llm_key must find it there (the canonical
    UI path), else a UI-configured OpenRouter author silently 401s."""
    from disco.core.llm.secrets import OPENROUTER_API_KEY_ENV
    from disco.tools.builtin import _slides_pipeline as sp

    monkeypatch.delenv(OPENROUTER_API_KEY_ENV, raising=False)

    class _Store:
        def get_secret(self, name):
            # Only the reserved "openrouter" slot resolves — NOT the raw
            # env-var name — proving resolution goes through the reserved
            # slot, not a plain get_secret(api_key_env) lookup.
            return "sk-or-reserved" if name == "openrouter" else None

    monkeypatch.setattr("disco.core.llm.secrets.SecretStore", lambda: _Store())
    assert sp._resolve_llm_key(OPENROUTER_API_KEY_ENV) == "sk-or-reserved"
    # a non-OpenRouter name does NOT fall back to the reserved slot
    assert sp._resolve_llm_key("SOME_OTHER_KEY") is None


# ---------------------------------------------------------------------------
# Theme validation + coercion (#5a fix)
# ---------------------------------------------------------------------------


def test_valid_themes_derived_from_schema():
    """_VALID_THEMES is derived from AuthoredDeck — must contain exactly 8 values."""
    assert len(_VALID_THEMES) == 8
    assert "disco-light" in _VALID_THEMES
    assert "disco-dark" in _VALID_THEMES
    assert "ink-light" in _VALID_THEMES
    assert "sepia-light" in _VALID_THEMES
    assert "signal-light" in _VALID_THEMES
    assert "midnight-dark" in _VALID_THEMES
    assert "neutral" in _VALID_THEMES
    assert "neutral-light" in _VALID_THEMES


def test_parse_invalid_theme_fails_validation():
    """theme='dark-research' is not a known alias and must fail pydantic validation."""
    data = dict(_SAMPLE_DECK_JSON, theme="dark-research")
    deck, err = _parse_authored_deck(json.dumps(data))
    assert deck is None
    assert err  # schema validation error mentioning the bad value


def test_retry_msg_contains_all_valid_themes():
    """_retry_msg(err) includes all 8 valid theme strings so the model can self-correct."""
    data = dict(_SAMPLE_DECK_JSON, theme="dark-research")
    _, err = _parse_authored_deck(json.dumps(data))
    msg = _retry_msg(err)
    for theme in _VALID_THEMES:
        assert theme in msg, f"_retry_msg missing valid theme: {theme!r}"
    # The specific error must also appear so the model knows what went wrong.
    assert err in msg


def test_coerce_dark_alias():
    """theme='dark' is a known alias — coerced to 'disco-dark' before validation."""
    data = dict(_SAMPLE_DECK_JSON, theme="dark")
    deck, err = _parse_authored_deck(json.dumps(data))
    assert deck is not None, f"Expected 'dark' alias coercion to succeed but got: {err}"
    assert deck.theme == "disco-dark"


def test_coerce_light_alias():
    """theme='light' is a known alias — coerced to 'disco-light' before validation."""
    data = dict(_SAMPLE_DECK_JSON, theme="light")
    deck, err = _parse_authored_deck(json.dumps(data))
    assert deck is not None, f"Expected 'light' alias coercion to succeed but got: {err}"
    assert deck.theme == "disco-light"


def test_coerce_unknown_alias_is_not_coerced():
    """An arbitrary invalid value is left as-is (not coerced) so validation rejects it."""
    data = {"theme": "corporate"}
    result = _coerce_known_theme_aliases(data)
    assert result["theme"] == "corporate"  # unchanged — not in _THEME_ALIASES


@pytest.mark.parametrize(
    "theme",
    [
        "disco-light",
        "disco-dark",
        "ink-light",
        "sepia-light",
        "signal-light",
        "midnight-dark",
        "neutral",
        "neutral-light",
    ],
)
def test_all_8_valid_themes_parse(theme):
    """Each of the 8 Literal theme values passes _parse_authored_deck successfully."""
    data = dict(_SAMPLE_DECK_JSON, theme=theme)
    deck, err = _parse_authored_deck(json.dumps(data))
    assert deck is not None, f"Valid theme {theme!r} failed: {err}"
    assert deck.theme == theme


def test_capable_system_lists_all_valid_themes():
    """_CAPABLE_SYSTEM mentions every one of the 8 valid theme values."""
    for theme in _VALID_THEMES:
        assert theme in _CAPABLE_SYSTEM, f"_CAPABLE_SYSTEM missing theme: {theme!r}"


def test_weak_system_lists_all_valid_themes():
    """_WEAK_SYSTEM mentions every one of the 8 valid theme values."""
    for theme in _VALID_THEMES:
        assert theme in _WEAK_SYSTEM, f"_WEAK_SYSTEM missing theme: {theme!r}"


@pytest.mark.asyncio
async def test_stage_assets_reports_failures_honestly():
    """Gauntlet 2026-07-07 (flux mis-config): a configured-but-broken backend
    failed EVERY slide image and the agent had no way to know — failures died
    in a log warning. Stats must carry the failure count + first error, and
    note() must say art fallback was used."""
    ctx = ToolContext.model_construct(sandbox=None)
    deck = AuthoredDeck(
        title="Visual Deck",
        slides=[
            AuthoredSlide(
                type="full_image",
                archetype="full_bleed_image",
                title="Hero",
                body=[],
                image_prompt="art; subject: Hero; slot: cover; no words",
            ),
            AuthoredSlide(
                type="bullets",
                archetype="bullets",
                title="Points",
                body=["a"],
            ),
        ],
    )
    backend = MagicMock()
    backend.generate.side_effect = RuntimeError("openrouter-image image endpoint returned HTTP 404")

    assets, stats = await _stage_assets(deck, ctx, backend, "v")

    assert assets == {}
    assert stats.configured is True
    assert stats.wanted == 1 and stats.generated == 0 and stats.failed == [0]
    assert "HTTP 404" in (stats.sample_error or "")
    note = stats.note()
    assert "FAILED" in note and "art fallback" in note and "Test" in note


@pytest.mark.asyncio
async def test_stage_assets_unconfigured_is_named_in_note():
    """backend=None (not configured) with image slots must produce a note that
    NAMES the unconfigured state — never a silent image-less deck."""
    ctx = ToolContext.model_construct(sandbox=None)
    deck = AuthoredDeck(
        title="Visual Deck",
        slides=[
            AuthoredSlide(
                type="full_image",
                archetype="full_bleed_image",
                title="Hero",
                body=[],
                image_prompt="art; subject: Hero; slot: cover; no words",
            )
        ],
    )

    assets, stats = await _stage_assets(deck, ctx, None, "v")

    assert assets == {}
    assert stats.configured is False and stats.wanted == 1
    assert "NOT configured" in stats.note()


def test_tableless_thin_comparison_demoted_to_bullets():
    """Gauntlet s-arxiv 2026-07-07: comparison_table with table=None and a
    3-line body rendered as two column headers + one lonely bullet (mostly
    empty). Such slides are demoted to bullets at prepare time; a REAL table
    or a body thick enough for balanced columns stays a comparison."""
    from disco.tools.builtin._slides_pipeline import _prepare_filled_deck

    deck = AuthoredDeck(
        title="T",
        slides=[
            AuthoredSlide(
                type="comparison",
                archetype="comparison_table",
                title="Thin",
                body=["a vs b", "c vs d", "e vs f"],
            ),
            AuthoredSlide(
                type="comparison",
                archetype="comparison_table",
                title="Rich body",
                body=["a", "b", "c", "d"],
            ),
            AuthoredSlide(
                type="table",
                archetype="comparison_table",
                title="Real table",
                body=[],
                table={"headers": ["x", "y"], "rows": [["1", "2"]]},
            ),
        ],
    )
    out = _prepare_filled_deck(deck)
    assert out.slides[0].archetype == "bullets"  # thin + tableless → demoted
    assert out.slides[1].archetype == "comparison_table"  # 4+ body lines → kept
    assert out.slides[2].archetype == "comparison_table"  # real table → kept
