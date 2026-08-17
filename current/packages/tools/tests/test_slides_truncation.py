"""H092: strict regression coverage for truncated slide-author responses."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import _slides_pipeline as slides
from disco.tools.builtin._deck_schema import AuthoredDeck
from disco.tools.builtin.slides import SlidesGenerateArgs, SlidesTool
from disco.tools.secrets import CapabilityBroker
from tool_fakes import FakeSandboxInstance


def _outline_json() -> str:
    return json.dumps(
        {
            "title": "Reliability",
            "theme": "disco-light",
            "slides": [
                {"type": "title", "title": "Reliable systems", "body": []},
                {"type": "closing", "title": "Next steps", "body": []},
            ],
        }
    )


def _context(sandbox: FakeSandboxInstance | None = None) -> ToolContext:
    return ToolContext(
        sandbox=sandbox or FakeSandboxInstance(),
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
    )


@pytest.mark.asyncio
async def test_http_adapter_preserves_length_finish_reason(monkeypatch) -> None:
    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {"content": '{"title":"cut'},
                        "finish_reason": "length",
                    }
                ]
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> _Response:
            return _Response()

    monkeypatch.setattr(slides.httpx, "AsyncClient", lambda **_kwargs: _Client())

    result = await slides._call_llm(
        [{"role": "user", "content": "build a deck"}],
        "http://provider.invalid/v1",
        "reasoning-model",
    )

    assert result.finish_reason == "length"
    assert result.content.endswith('"cut')


@pytest.mark.asyncio
async def test_outline_length_stop_fails_without_same_cap_retry() -> None:
    with patch.object(slides, "_call_llm", new_callable=AsyncMock) as call_llm:
        call_llm.return_value = slides.LLMResponse(content='{"title":"cut', finish_reason="length")

        with pytest.raises(
            slides.SlidesGenerationIncompleteError,
            match=r"slide outline response .*finish_reason='length'",
        ):
            await slides._stage_outline(
                "Reliability deck",
                2,
                slides._CAPABLE_SYSTEM,
                "http://provider.invalid/v1",
                "reasoning-model",
            )

    assert call_llm.call_count == 1


@pytest.mark.asyncio
async def test_fill_length_stop_fails_without_same_cap_retry() -> None:
    outline = AuthoredDeck.model_validate_json(_outline_json())
    with patch.object(slides, "_call_llm", new_callable=AsyncMock) as call_llm:
        call_llm.return_value = slides.LLMResponse(content='{"title":"cut', finish_reason="length")

        with pytest.raises(
            slides.SlidesGenerationIncompleteError,
            match=r"slide fill response .*finish_reason='length'",
        ):
            await slides._stage_fill(
                outline,
                slides._CAPABLE_SYSTEM,
                "http://provider.invalid/v1",
                "reasoning-model",
            )

    assert call_llm.call_count == 1


@pytest.mark.asyncio
async def test_generate_deck_classifies_fill_truncation_without_fallback_or_write() -> None:
    sandbox = FakeSandboxInstance()
    with (
        patch.object(
            slides,
            "_resolve_slides_llm",
            return_value=("http://provider.invalid/v1", "reasoning-model", None),
        ),
        patch.object(slides, "_call_llm", new_callable=AsyncMock) as call_llm,
    ):
        call_llm.side_effect = [
            slides.LLMResponse(content=_outline_json(), finish_reason="stop"),
            slides.LLMResponse(content='{"title":"cut', finish_reason="length"),
        ]

        deck, fallback, error, sidecar, image_stats = await slides.generate_deck(
            "Reliability deck",
            "must-not-exist",
            _context(sandbox),
            MagicMock(),
            slide_count=2,
        )

    assert call_llm.call_count == 2
    assert deck is None
    assert fallback is None
    assert sidecar is None
    assert image_stats is None
    assert error is not None and error.startswith("SLIDES_PROVIDER_OUTPUT_TRUNCATED:")
    assert "finish_reason='length'" in error
    assert "no plain-renderer fallback" in error
    assert await sandbox.list_dir(".") == []


@pytest.mark.asyncio
async def test_public_tool_reports_truncation_as_failure_not_degraded_renderer() -> None:
    sandbox = FakeSandboxInstance()
    with (
        patch.object(
            slides,
            "_resolve_slides_llm",
            return_value=("http://provider.invalid/v1", "reasoning-model", None),
        ),
        patch.object(slides, "_call_llm", new_callable=AsyncMock) as call_llm,
        patch(
            "disco.tools.builtin.slides.select_image_backend",
            return_value=None,
        ),
    ):
        call_llm.return_value = slides.LLMResponse(content='{"title":"cut', finish_reason="length")
        outcome = await SlidesTool().run(
            SlidesGenerateArgs(
                goal="Reliability deck",
                filename="must-not-exist",
                format="html",
                slide_count=2,
            ),
            _context(sandbox),
        )

    assert call_llm.call_count == 1
    assert not outcome.success
    assert outcome.artifacts == []
    assert outcome.structured in (None, {})
    assert outcome.error is not None
    assert outcome.error.startswith("SLIDES_PROVIDER_OUTPUT_TRUNCATED:")
    assert "finish_reason='length'" in outcome.content
    assert "DEGRADED" not in outcome.content
    assert await sandbox.list_dir(".") == []


@pytest.mark.asyncio
async def test_completed_malformed_fill_keeps_one_retry_then_plain_fallback() -> None:
    """Retain the historical inventory ID while proving diagnostics stay internal."""
    with (
        patch.object(
            slides,
            "_resolve_slides_llm",
            return_value=("http://provider.invalid/v1", "reasoning-model", None),
        ),
        patch.object(slides, "_call_llm", new_callable=AsyncMock) as call_llm,
    ):
        call_llm.side_effect = [
            slides.LLMResponse(content=_outline_json(), finish_reason="stop"),
            slides.LLMResponse(content="BAD JSON", finish_reason="stop"),
            slides.LLMResponse(content="STILL BAD", finish_reason="stop"),
        ]
        deck, fallback, error, sidecar, image_stats = await slides.generate_deck(
            "Reliability deck",
            "malformed-fallback",
            _context(),
            MagicMock(),
            slide_count=2,
        )

    assert call_llm.call_count == 3
    assert deck is None
    assert fallback is not None and "Reliable systems" in fallback
    assert error is not None and "parse failed after retry" in error
    assert sidecar is None
    assert image_stats is None
