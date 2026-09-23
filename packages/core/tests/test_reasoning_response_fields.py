"""Reasoning wire fields are protocol data, never a model-name capability."""

import asyncio
import json

import httpx
import pytest
from disco.core.llm._openai_timeouts import iter_with_progress_timeout
from disco.core.llm._provider_retry import meaningful_sse_line
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.stream_progress import observe_stream_progress
from test_stream_progress import _collector, _req, _sse


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize("delivery", ["complete", "stream", "json", "buffered"])
async def test_reasoning_fields_preserve_progress_and_empty_answer_metadata(field, delivery):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if delivery == "buffered" and payload.get("stream"):
            return httpx.Response(501, text="stream is unsupported")
        if delivery in ("json", "buffered"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "", field: "considering evidence"},
                            "finish_reason": "length",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {field: "considering evidence", "content": ""}}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAIProvider(
        "https://arbitrary.test/v1", name="unknown-service", transport=httpx.MockTransport(handler)
    )
    seen, observer = _collector()
    with observe_stream_progress(observer):
        if delivery == "stream":
            chunks = [c async for c in provider.stream_complete(_req(), model="new-model")]
            assert not any(c.delta_text for c in chunks)
            response = chunks[-1].final
        else:
            response = await provider.complete(_req(), model="new-model")
    assert response.text == ""  # reasoning is not promoted into answer prose
    assert response.response_metadata["ceiling_hit_empty"]["reasoning_len"] == len(
        "considering evidence"
    )
    if delivery != "buffered":
        assert seen[-1].reasoning_tokens == 1
        assert seen[-1].tokens_streamed == 1
        assert seen[-1].final
    else:
        assert [r["stream"] for r in requests] == [True, False]


@pytest.mark.parametrize(
    "delta",
    [
        {"reasoning": "same", "reasoning_content": "same"},
        {"reasoning": "active", "reasoning_content": ""},
        {"reasoning": "active", "reasoning_content": None},
    ],
)
async def test_reasoning_aliases_neither_duplicate_nor_hide_activity(delta):
    def handler(request):
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": delta}]},
                {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAIProvider("https://arbitrary.test/v1", transport=httpx.MockTransport(handler))
    seen, observer = _collector()
    with observe_stream_progress(observer):
        response = await provider.complete(_req(), model="unknown")
    assert response.text == "answer"
    assert seen[-1].reasoning_tokens == 1
    assert seen[-1].tokens_streamed == 2


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_reasoning_deltas_extend_the_meaningful_progress_deadline(field):
    line = "data: " + json.dumps({"choices": [{"delta": {field: "still thinking"}}]})

    async def source():
        for _ in range(12):
            await asyncio.sleep(0.005)
            yield line

    output = [
        line
        async for line in iter_with_progress_timeout(
            source(),
            provider_name="arbitrary",
            first_chunk_s=0.03,
            idle_s=0.03,
            is_progress=meaningful_sse_line,
        )
    ]
    assert len(output) == 12


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize("value", [None, "", [], {}, 42, True])
def test_empty_or_nontext_reasoning_is_not_generation(field, value):
    assert not meaningful_sse_line("data: " + json.dumps({"choices": [{"delta": {field: value}}]}))
