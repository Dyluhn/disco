import os
from unittest.mock import MagicMock, patch

import pytest
from disco.core.llm import Difficulty, OperatingMode, OverflowSignal
from disco.core.loop.agent import RouterAgent
from disco.core.view import View


@pytest.mark.asyncio
async def test_prefill_masking_behavior():
    """B9: Assistant prefill is set correctly based on mode and env var."""
    router = MagicMock()
    
    # We want to capture the CompletionRequest sent to the router
    captured_req = None
    async def mock_stream(req, **kwargs):
        nonlocal captured_req
        captured_req = req
        from disco.core.llm import CompletionResponse, StreamChunk, TokenUsage
        yield StreamChunk(
            done=True,
            final=CompletionResponse(
                text=" doing something.",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                finish_reason="stop",
                model_used="test-model"
            )
        )
    router.stream_complete = mock_stream
    
    agent = RouterAgent(router)
    view = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    
    # Case 1: PLANNING mode, enabled
    with patch.dict(os.environ, {"PMX_PLAN_PREFILL": "1"}):
        await agent.step(
            view,
            tools=[],
            mode=OperatingMode.PLANNING,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE)
        )
        assert captured_req.assistant_prefill is not None
        assert "I've analyzed" in captured_req.assistant_prefill

    # Case 2: PLANNING mode, disabled
    with patch.dict(os.environ, {"PMX_PLAN_PREFILL": "0"}):
        await agent.step(
            view,
            tools=[],
            mode=OperatingMode.PLANNING,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE)
        )
        assert captured_req.assistant_prefill is None

    # Case 3: LONG_HORIZON mode, enabled (should still be None)
    with patch.dict(os.environ, {"PMX_PLAN_PREFILL": "1"}):
        await agent.step(
            view,
            tools=[],
            mode=OperatingMode.LONG_HORIZON,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE)
        )
        assert captured_req.assistant_prefill is None
