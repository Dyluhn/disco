"""OpenAI-compatible ModelProvider adapter — hermetic tests (httpx.MockTransport).

No network: a mock transport returns canned OpenAI responses so we test the
adapter's parsing, error classification, reasoning-model handling, and SSE
reassembly. (The live smoke against the real Qwen endpoint is run separately.)
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm.errors import (
    LLMAuthError,
    LLMContextWindowExceeded,
    LLMError,
    LLMTransientError,
    is_context_window_exceeded,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.types import (
    CEILING_HIT_EMPTY_METADATA_KEY,
    EMPTY_REASONING_ONLY_METADATA_KEY,
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
)


def _req(text: str = "hi") -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content=text)],
        max_tokens=50,
        temperature=0.0,
    )


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


async def test_non_streaming_parses_content_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # `complete` delivers a buffered result over a STREAMED transport, so
        # progress (not total duration) bounds the call; usage is requested on
        # the final chunk. This handler answers with a whole JSON body anyway,
        # which the adapter must still decode rather than return empty.
        assert body["model"] == "m1" and body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        # reasoning model: content is the ANSWER, reasoning_content the thinking
                        "message": {"content": "Paris", "reasoning_content": "let me think..."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    r = await _provider(handler).complete(_req(), model="m1")
    assert r.text == "Paris"  # reasoning_content is NOT treated as the answer
    assert r.finish_reason == "stop"
    assert r.usage.input_tokens == 5 and r.usage.output_tokens == 1

    def responses_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body == {
            "model": "muse-spark-1.2-contributor",
            "input": [{"role": "user", "content": "hi"}],
            "temperature": 0.0,
            "max_output_tokens": 50,
        }
        return httpx.Response(
            200,
            json={
                "model": "muse-spark-1.2-contributor",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Muse answer"}],
                    },
                ],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 218,
                    "input_tokens_details": {"cached_tokens": 3},
                    "output_tokens_details": {"reasoning_tokens": 207},
                },
            },
        )

    muse = OpenAIProvider(
        "https://api.meta.ai/v1/responses",
        name="meta-muse",
        transport=httpx.MockTransport(responses_handler),
    )
    muse_response = await muse.complete(_req(), model="muse-spark-1.2-contributor")
    assert muse_response.text == "Muse answer"
    assert muse_response.finish_reason == "stop"
    assert muse_response.usage.input_tokens == 12
    assert muse_response.usage.cached_tokens == 3
    assert muse_response.response_metadata == {"reasoning_tokens": 207}


async def test_reasoning_only_truncation_yields_empty_content():
    # A reasoning model truncated mid-thought (small max_tokens) → empty content.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "reasoning_content": "still..."},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    r = await _provider(handler).complete(_req(), model="m")
    assert r.text == "" and r.finish_reason == "length"


async def test_context_window_error_is_classified_and_redacts_provider_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "request exceeds the available context size (131072 tokens)",
                    "type": "exceed_context_size_error",
                }
            },
        )

    with pytest.raises(LLMContextWindowExceeded) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert is_context_window_exceeded(exc.value)  # the condenser's dependency
    text = str(exc.value)
    assert text == "provider fake returned HTTP 400 type=exceed_context_size_error"
    assert "exceeds the available context size" not in text


async def test_auth_error_redacts_real_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Invalid API Key", "type": "authentication_error"}}
        )

    with pytest.raises(LLMAuthError) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert str(exc.value) == "provider fake returned HTTP 401 type=authentication_error"
    assert "Invalid API Key" not in str(exc.value)


async def test_generic_provider_error_uses_safe_summary():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "something specific broke", "type": "invalid_request"}},
        )

    with pytest.raises(LLMError) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert str(exc.value) == "provider fake returned HTTP 400 type=invalid_request"
    assert "something specific broke" not in str(exc.value)


class _SecretBearingProtocolFailure(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # A deterministic stand-in for h11's malformed-Authorization exception.
        # These are fake sentinels; a regression must not echo either one.
        first = "sk-" + "a" * 32
        second = "sk-" + "b" * 32
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {first}\\n{second}'")


async def test_nonstream_transport_error_never_reflects_header_credentials():
    provider = OpenAIProvider(
        "http://fake/v1", name="fake", transport=_SecretBearingProtocolFailure()
    )

    with pytest.raises(LLMTransientError) as exc:
        await provider.complete(_req(), model="m")

    detail = str(exc.value)
    assert detail == "connection error (LocalProtocolError)"
    assert "sk-" not in detail
    assert "Bearer" not in detail


async def test_stream_transport_error_never_reflects_header_credentials():
    provider = OpenAIProvider(
        "http://fake/v1", name="fake", transport=_SecretBearingProtocolFailure()
    )

    with pytest.raises(LLMTransientError) as exc:
        async for _chunk in provider.stream_complete(_req(), model="m"):
            pass

    detail = str(exc.value)
    assert detail == "connection error (LocalProtocolError)"
    assert "sk-" not in detail
    assert "Bearer" not in detail


async def test_streaming_reassembles_and_final_matches():
    content = _sse(
        {"model": "m", "choices": [{"delta": {"content": "Hello"}}]},
        {"model": "m", "choices": [{"delta": {"content": ", world"}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    deltas: list[str] = []
    final = None
    async for ch in _provider(handler).stream_complete(_req(), model="m"):
        if ch.done:
            final = ch.final
        elif ch.delta_text:
            deltas.append(ch.delta_text)
    assert "".join(deltas) == "Hello, world"
    assert final is not None
    assert final.text == "Hello, world"
    assert final.finish_reason == "stop"
    assert final.usage.output_tokens == 2

    def responses_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert "stream" not in body
        return httpx.Response(
            200,
            json={
                "model": "muse-spark-1.2-contributor",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "One response"}],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    responses_provider = OpenAIProvider(
        "https://api.meta.ai/v1/responses",
        name="meta-muse",
        transport=httpx.MockTransport(responses_handler),
    )
    response_chunks = [
        chunk
        async for chunk in responses_provider.stream_complete(
            _req(), model="muse-spark-1.2-contributor"
        )
    ]
    assert [chunk.delta_text for chunk in response_chunks] == ["One response", ""]
    assert response_chunks[-1].done is True
    assert response_chunks[-1].final is not None
    assert response_chunks[-1].final.text == "One response"


async def _collect(provider, model="m"):
    """Drive stream_complete to exhaustion; return (delta_text, final, arg_deltas)."""
    deltas: list[str] = []
    arg_deltas: dict[int, str] = {}
    final = None
    async for ch in provider.stream_complete(_req(), model=model):
        if ch.done:
            final = ch.final
        elif ch.tool_args_delta:
            arg_deltas[ch.tool_index] = arg_deltas.get(ch.tool_index, "") + ch.tool_args_delta
        elif ch.delta_text:
            deltas.append(ch.delta_text)
    return "".join(deltas), final, arg_deltas


def _stream_provider(content: bytes) -> OpenAIProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    return _provider(handler)


async def test_streaming_empty_reasoning_only_response_is_flagged():
    content = _sse(
        {"choices": [{"delta": {"reasoning_content": "thinking with no answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert final.response_metadata[EMPTY_REASONING_ONLY_METADATA_KEY] == {
        "finish_reason": "stop",
        "content_len": 0,
        "reasoning_len": len("thinking with no answer"),
        "tool_call_count": 0,
    }


async def test_streaming_blank_stop_response_is_flagged():
    content = _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})

    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert final.response_metadata[EMPTY_REASONING_ONLY_METADATA_KEY] == {
        "finish_reason": "stop",
        "content_len": 0,
        "reasoning_len": 0,
        "tool_call_count": 0,
    }


async def test_streaming_finish_reason_comes_from_the_last_chunk_that_carries_one():
    """The llama.cpp stream shape: nulls, then the real finish_reason on the
    SECOND-TO-LAST data chunk, then a usage-only chunk with no choices.

    Reading "the final chunk" loses the finish entirely and a truncated report
    would look complete. The last NON-NULL one is the answer, and the trailing
    usage-only chunk still has to land — its completion_tokens are what a
    reasoning model's think phase is billed against.
    """
    content = _sse(
        {"choices": [{"delta": {"content": "Report "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "body"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        },
    )

    text, final, _ = await _collect(_stream_provider(content))

    assert text == "Report body"
    assert final is not None
    assert final.text == "Report body"
    assert final.finish_reason == "length"
    # The usage-only trailer is not dropped; completion_tokens already counts
    # the provider's reasoning tokens, so nothing extra is invented here.
    assert final.usage.input_tokens == 11
    assert final.usage.output_tokens == 7


async def test_streaming_without_any_finish_reason_is_a_complete_response():
    """llama.cpp can end a stream with finish_reason null throughout. A
    successful stream that produced content is complete, not truncated."""
    content = _sse(
        {"choices": [{"delta": {"content": "Whole report"}, "finish_reason": None}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    )

    text, final, _ = await _collect(_stream_provider(content))

    assert text == "Whole report"
    assert final is not None
    assert final.finish_reason == "stop"
    assert final.usage.output_tokens == 2


@pytest.mark.parametrize(
    "content",
    [
        _sse(
            {"choices": [{"delta": {"content": "visible"}}]},
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ),
        _sse(
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
        _sse(
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ),
        _sse({"choices": [{"delta": {}, "finish_reason": "length"}]}),
    ],
)
async def test_streaming_empty_reasoning_only_neighbors_are_not_flagged(content: bytes):
    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert EMPTY_REASONING_ONLY_METADATA_KEY not in final.response_metadata


async def test_streaming_ceiling_hit_with_empty_content_is_flagged():
    """L25's class: the whole ceiling went into reasoning and NOTHING came back.

    This was recorded as a clean success — no marker, no error class — so a
    reviewer could burn three ceilings in a row and leave a trail that said
    three calls succeeded.
    """
    content = _sse(
        {"choices": [{"delta": {"reasoning_content": "thinking past the ceiling"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    )

    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert final.response_metadata[CEILING_HIT_EMPTY_METADATA_KEY] == {
        "finish_reason": "length",
        "content_len": 0,
        "reasoning_len": len("thinking past the ceiling"),
        "tool_call_count": 0,
    }
    # The `stop`-only marker keeps its exact condition: the loop's driver reads
    # it to trigger a repair, and that behaviour must not change here.
    assert EMPTY_REASONING_ONLY_METADATA_KEY not in final.response_metadata


async def test_ceiling_hit_marker_rides_the_buffered_path_too():
    """The same class, non-streamed — the shape the review call actually took."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "all of it"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4000},
            },
        )

    resp = await _provider(handler).complete(_req(), model="m1")

    assert resp.text == ""
    assert resp.response_metadata[CEILING_HIT_EMPTY_METADATA_KEY]["finish_reason"] == "length"


async def test_length_finish_with_content_carries_no_empty_marker():
    """A truncated report is a real answer that got cut off, not an empty one."""
    content = _sse(
        {"choices": [{"delta": {"content": "half a repor"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    )

    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert CEILING_HIT_EMPTY_METADATA_KEY not in final.response_metadata
    assert EMPTY_REASONING_ONLY_METADATA_KEY not in final.response_metadata


async def test_tool_call_only_response_carries_no_empty_marker():
    """No prose and a tool call is the normal agent turn, not an empty reply."""
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )

    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert CEILING_HIT_EMPTY_METADATA_KEY not in final.response_metadata
    assert EMPTY_REASONING_ONLY_METADATA_KEY not in final.response_metadata


async def test_streaming_tool_args_accumulate_across_many_fragments():
    # Single tool call: name+id+empty-string arg in the first delta, then the
    # JSON arguments streamed across 3 more deltas (incl. an empty-string
    # fragment). The final assembled arguments must be COMPLETE and parseable.
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "update_plan_progress", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"steps": ["Build '}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ""}}]}}]
        },  # empty fragment, no reset
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'home", "Add foo'}}]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ter"]}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    _, final, arg_deltas = await _collect(_stream_provider(content))
    assert final is not None and len(final.tool_calls) == 1
    tc = final.tool_calls[0]
    assert tc.tool_name == "update_plan_progress"
    assert tc.arguments == {"steps": ["Build home", "Add footer"]}  # COMPLETE, no truncation
    assert arg_deltas[0] == '{"steps": ["Build home", "Add footer"]}'  # watch-it-write intact


async def test_streaming_continuation_fragments_omit_index():
    # llama.cpp / OpenRouter shape: only the FIRST tool_call delta carries
    # `index`; argument-continuation deltas omit it. The old `tc.get("index", 0)`
    # default happened to work for a SINGLE call (0), so this is a regression guard.
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "update_plan_progress", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"function": {"arguments": '{"steps": ["a", '}}]}}
            ]
        },  # no index
        {
            "choices": [{"delta": {"tool_calls": [{"function": {"arguments": '"b"]}'}}]}}]
        },  # no index
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    _, final, _ = await _collect(_stream_provider(content))
    assert final.tool_calls[0].arguments == {"steps": ["a", "b"]}


async def test_streaming_parallel_calls_with_index_on_every_delta():
    # Compliant interleaved parallel calls — must stay correct after the fix.
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "a",
                                "type": "function",
                                "function": {"name": "toolA", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "b",
                                "type": "function",
                                "function": {"name": "toolB", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"x": '}}]}}
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 1, "function": {"arguments": '{"y": '}}]}}
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    _, final, _ = await _collect(_stream_provider(content))
    by_name = {tc.tool_name: tc.arguments for tc in final.tool_calls}
    assert by_name == {"toolA": {"x": 1}, "toolB": {"y": 2}}


async def test_streaming_parallel_calls_with_index_dropped_on_continuations():
    # THE ROOT-CAUSE CASE. Two sequential parallel calls; each call's header
    # delta carries `index`, but its argument-continuation deltas DROP it
    # (llama.cpp shape). The old `tc.get("index", 0)` collapsed every continuation
    # onto slot 0: toolA got both calls' JSON concatenated (invalid → repaired to
    # garbage) and toolB got "" (empty args). The fix routes index-less
    # continuations to the most-recently-touched slot.
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "a",
                                "type": "function",
                                "function": {"name": "toolA", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"x": 1}'}}]}}]
        },  # no index -> toolA
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "b",
                                "type": "function",
                                "function": {"name": "toolB", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"y": 2}'}}]}}]
        },  # no index -> toolB
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    _, final, _ = await _collect(_stream_provider(content))
    by_name = {tc.tool_name: tc.arguments for tc in final.tool_calls}
    # Both calls assemble COMPLETE & isolated — no cross-contamination, no empty args.
    assert by_name == {"toolA": {"x": 1}, "toolB": {"y": 2}}


async def test_streaming_new_call_headers_without_index_or_id_stay_separate():
    """OpenCode-compatible live shape: each call is complete but the provider
    supplies neither index nor id. Fresh function headers must start fresh slots
    instead of producing ``{"path": ...}{"query": ...}`` under one ``_raw``.
    """
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "workspace_list",
                                    "arguments": '{"path":"."}',
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query":"three.js","limit":5}',
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "file_read",
                                    "arguments": '{"path":"index.html"}',
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )

    _, final, _ = await _collect(_stream_provider(content))

    assert final is not None
    assert [(call.tool_name, call.arguments) for call in final.tool_calls] == [
        ("workspace_list", {"path": "."}),
        ("search", {"query": "three.js", "limit": 5}),
        ("file_read", {"path": "index.html"}),
    ]


async def test_streaming_interleaved_continuations_keyed_by_id_without_index():
    # Hardest shape: interleaved parallel continuations that drop `index` but
    # re-send the call `id`. Routing must follow the id, not last-touched or 0.
    content = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "a",
                                "type": "function",
                                "function": {"name": "toolA", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "b",
                                "type": "function",
                                "function": {"name": "toolB", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"id": "a", "function": {"arguments": '{"x": '}}]}}
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"id": "b", "function": {"arguments": '{"y": '}}]}}
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"id": "a", "function": {"arguments": "1}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"id": "b", "function": {"arguments": "2}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    _, final, _ = await _collect(_stream_provider(content))
    by_name = {tc.tool_name: tc.arguments for tc in final.tool_calls}
    assert by_name == {"toolA": {"x": 1}, "toolB": {"y": 2}}


async def test_streaming_error_status_raises_typed_before_any_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Invalid API Key", "type": "authentication_error"}}
        )

    with pytest.raises(LLMAuthError):
        async for _chunk in _provider(handler).stream_complete(_req(), model="m"):
            pass


def test_tool_calls_survive_array_shaped_arguments():
    """dt5 crash root cause: a provider dialect emitted arguments as a bare JSON
    ARRAY; json.loads gave a list, _coerce_args called .items() on it, and the
    AttributeError killed the whole run task. Array args must degrade to the
    honest {"_raw": ...} shape (validation refuses with feedback), never crash."""
    from disco.core.llm.openai_provider import OpenAIProvider

    raw = [
        {
            "id": "c1",
            "function": {
                "name": "update_plan_progress",
                "arguments": '[{"index": 1, "state": "done"}]',
            },
        }
    ]
    calls = OpenAIProvider._tool_calls(raw)
    assert len(calls) == 1
    args = calls[0].arguments
    assert isinstance(args, dict)
    assert "_raw" in args


# ---------------------------------------------------------------------------
# Operator-actionable provider failures must stay diagnosable.
#
# `str(exc)` is content-free by design — the host-claim and model-verifier
# gates match that bounded form exactly, so provider text must never enter it.
# But an operator staring at "returned HTTP 403" cannot tell an unpaid plan
# from an unaccepted data policy from a dead key. The provider says which, in
# its own error envelope; `provider_detail` carries it to the operator surface
# only. These are REAL bodies captured from the live providers 2026-08-23.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected_fragment"),
    [
        (
            403,
            '{"error":{"message":"this model requires a subscription, upgrade for '
            'access: https://ollama.com/upgrade","type":"api_error"}}',
            "requires a subscription",
        ),
        (
            429,
            '{"error":{"message":"you (someone) have reached your weekly usage '
            'limit, upgrade for higher limits","type":"api_error"}}',
            "weekly usage limit",
        ),
        (
            403,
            '{"type":"error","error":{"type":"DataPolicyError","message":"This model '
            "collects data used to improve its quality and requires explicit opt in: "
            'https://opencode.ai/workspace/wrk_1/go"}}',
            "requires explicit opt in",
        ),
    ],
)
def test_account_failures_carry_the_provider_reason_for_the_operator(
    status: int, body: str, expected_fragment: str
):
    from disco.core.llm._openai_response import raise_typed, safe_provider_error

    with pytest.raises(LLMError) as caught:
        raise_typed(
            "prov",
            status,
            body,
            safe_error_fn=lambda s, t: safe_provider_error("prov", s, t),
            context_overflow_fn=lambda _t, _m: False,
        )
    exc = caught.value
    # The operator gets the fix...
    assert expected_fragment in exc.provider_detail
    # ...and the bounded form the security gates match is unchanged.
    assert str(exc) == safe_provider_error("prov", status, "api_error") or str(
        exc
    ) == safe_provider_error("prov", status, "DataPolicyError")
    assert expected_fragment not in str(exc)


def test_server_errors_carry_no_provider_content():
    """5xx is not operator-actionable and its body is not an account reason —
    it stays content-free rather than leaking an upstream stack trace."""
    from disco.core.llm._openai_response import raise_typed, safe_provider_error

    with pytest.raises(LLMError) as caught:
        raise_typed(
            "prov",
            500,
            '{"error":{"message":"Internal server error at /srv/app/x.py:41"}}',
            safe_error_fn=lambda s, t: safe_provider_error("prov", s, t),
            context_overflow_fn=lambda _t, _m: False,
        )
    assert caught.value.provider_detail == ""
    assert "srv" not in str(caught.value)


def test_provider_detail_is_one_line_and_bounded():
    from disco.core.llm._openai_response import sanitize_provider_detail

    out = sanitize_provider_detail("line one\n\tline two\x00 " + "x" * 500)
    assert "\n" not in out and "\t" not in out and "\x00" not in out
    assert len(out) <= 240


# --- progress-based timeouts (model-neutral) ---------------------------------
#
# A stall is the ABSENCE OF PROGRESS, not the presence of duration. These tests
# shrink the BUDGETS rather than lengthening the delays, so nothing waits for a
# perceptible time and no assertion depends on wall-clock: what is proved is
# which budget fires, not how long anything took.


class _ScriptedSSE(httpx.AsyncByteStream):
    """SSE bytes with scripted gaps between them.

    A ``gap`` longer than the budget under test stands in for a hung provider;
    the adapter cancels the wait, so the gap is never actually waited out.
    """

    def __init__(self, *events: tuple[float, bytes]) -> None:
        self._events = events

    async def __aiter__(self):
        for gap, payload in self._events:
            if gap:
                await asyncio.sleep(gap)
            yield payload


_HANG = 30.0  # never reached: the budget under test always fires first


def _sse_bytes(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode()


def _scripted_provider(*events: tuple[float, bytes], **env: str) -> OpenAIProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ScriptedSSE(*events),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.MonkeyPatch.context() as patcher:
        for name, value in env.items():
            patcher.setenv(name, value)
        # Budgets are resolved ONCE, at construction — a live request never
        # pays for an environment lookup.
        return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


_FINAL_CHUNK = {
    "choices": [{"delta": {}, "finish_reason": "stop"}],
    "usage": {"completion_tokens": 2},
}


def test_default_budgets_bound_progress_and_never_total_duration():
    provider = OpenAIProvider("http://fake/v1", name="fake")
    timeouts = provider._timeouts
    assert (timeouts.connect_s, timeouts.first_chunk_s, timeouts.idle_s) == (30.0, 900.0, 180.0)
    # The streamed transport has no total ceiling: a model that legitimately
    # generates for an hour is not a failure.
    assert timeouts.stream_httpx_timeout().as_dict()["connect"] == 30.0
    assert timeouts.buffered_total_s == 600.0


def test_progress_budgets_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DISCO_LLM_FIRST_TOKEN_TIMEOUT_S", "1200")
    monkeypatch.setenv("DISCO_LLM_IDLE_TIMEOUT_S", "45")
    monkeypatch.setenv("DISCO_LLM_TIMEOUT_S", "700")
    provider = OpenAIProvider("http://fake/v1", name="fake")
    assert provider._timeouts.first_chunk_s == 1200.0
    assert provider._timeouts.idle_s == 45.0
    assert provider._timeouts.buffered_total_s == 700.0

    # Read once at construction: later mutation cannot move a live provider.
    monkeypatch.setenv("DISCO_LLM_IDLE_TIMEOUT_S", "1")
    assert provider._timeouts.idle_s == 45.0

    # The legacy PMX_ prefix still resolves, and unusable values keep the default
    # rather than inventing a new failure mode.
    monkeypatch.delenv("DISCO_LLM_IDLE_TIMEOUT_S")
    monkeypatch.setenv("PMX_LLM_IDLE_TIMEOUT_S", "90")
    assert OpenAIProvider("http://fake/v1", name="fake")._timeouts.idle_s == 90.0
    monkeypatch.setenv("PMX_LLM_IDLE_TIMEOUT_S", "not-a-number")
    assert OpenAIProvider("http://fake/v1", name="fake")._timeouts.idle_s == 180.0
    monkeypatch.setenv("PMX_LLM_IDLE_TIMEOUT_S", "-5")
    assert OpenAIProvider("http://fake/v1", name="fake")._timeouts.idle_s == 180.0


async def test_slow_thinking_under_the_first_chunk_budget_succeeds():
    """The defect this closes: a model thinking longer than the old flat
    timeout is a WORKING model, and must not be turned into a transient error."""
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            stream=_ScriptedSSE(
                # First chunk arrives far later than any inter-chunk gap.
                (0.15, _sse_bytes({"choices": [{"delta": {"content": "Fi"}}]})),
                (0.0, _sse_bytes({"choices": [{"delta": {"content": "nally"}}]})),
                (0.0, _sse_bytes(_FINAL_CHUNK)),
            ),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setenv("DISCO_LLM_FIRST_TOKEN_TIMEOUT_S", "5")
        patcher.setenv("DISCO_LLM_IDLE_TIMEOUT_S", "5")
        provider = OpenAIProvider(
            "http://fake/v1", name="fake", transport=httpx.MockTransport(handler)
        )

    result = await provider.complete(_req(), model="m")
    assert result.text == "Finally"
    assert result.finish_reason == "stop"
    assert len(calls) == 1  # one attempt: nothing retried a working model


async def test_stream_going_silent_past_the_idle_budget_is_a_stall():
    provider = _scripted_provider(
        (0.0, _sse_bytes({"choices": [{"delta": {"content": "start"}}]})),
        (_HANG, _sse_bytes(_FINAL_CHUNK)),
        DISCO_LLM_FIRST_TOKEN_TIMEOUT_S="5",
        DISCO_LLM_IDLE_TIMEOUT_S="0.05",
    )
    with pytest.raises(LLMTransientError) as caught:
        await provider.complete(_req(), model="m")
    assert "stream idle" in str(caught.value)


async def test_first_chunk_budget_is_distinct_from_the_idle_budget():
    """A long prefill is not a stall: the first-chunk budget governs the wait
    for the first byte even when it dwarfs the inter-chunk idle budget."""
    generous_first_chunk = _scripted_provider(
        (0.15, _sse_bytes({"choices": [{"delta": {"content": "prefilled"}}]})),
        (0.0, _sse_bytes(_FINAL_CHUNK)),
        DISCO_LLM_FIRST_TOKEN_TIMEOUT_S="5",
        DISCO_LLM_IDLE_TIMEOUT_S="0.05",
    )
    result = await generous_first_chunk.complete(_req(), model="m")
    assert result.text == "prefilled"

    # Mirror image: the same first-chunk wait against a tight first-chunk
    # budget fails, and names the first-chunk phase rather than idle.
    tight_first_chunk = _scripted_provider(
        (_HANG, _sse_bytes({"choices": [{"delta": {"content": "never"}}]})),
        DISCO_LLM_FIRST_TOKEN_TIMEOUT_S="0.05",
        DISCO_LLM_IDLE_TIMEOUT_S="5",
    )
    with pytest.raises(LLMTransientError) as caught:
        await tight_first_chunk.complete(_req(), model="m")
    assert "no first chunk" in str(caught.value)


async def test_streamed_completion_matches_the_buffered_decode():
    """Accumulation fidelity: the streamed reassembly and the genuine buffered
    decoder produce the same CompletionResult for the same generation."""
    buffered_body = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": "Answer text",
                    "reasoning_content": "thinking",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "function": {"name": "read_state", "arguments": '{"path":"/x"}'},
                        },
                        {
                            "id": "call_b",
                            "function": {"name": "read_state", "arguments": '{"path":"/y"}'},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }

    def streamed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"model": "m", "choices": [{"delta": {"content": "Answer "}}]},
                {"model": "m", "choices": [{"delta": {"reasoning_content": "think"}}]},
                {"model": "m", "choices": [{"delta": {"content": "text"}}]},
                {"model": "m", "choices": [{"delta": {"reasoning_content": "ing"}}]},
                {
                    "model": "m",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_a",
                                        "function": {
                                            "name": "read_state",
                                            "arguments": '{"path":',
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                },
                {
                    "model": "m",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": '"/x"}'}}]
                            }
                        }
                    ],
                },
                {
                    "model": "m",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 1,
                                        "id": "call_b",
                                        "function": {
                                            "name": "read_state",
                                            "arguments": '{"path":"/y"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                },
                {
                    "model": "m",
                    "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            ),
            headers={"content-type": "text/event-stream"},
        )

    def rejecting_handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["stream"]:
            return httpx.Response(
                400,
                json={"error": {"message": "stream is not supported here", "type": "bad_request"}},
            )
        return httpx.Response(200, json=buffered_body)

    streamed = await _provider(streamed_handler).complete(_req(), model="m")
    buffered = await _provider(rejecting_handler).complete(_req(), model="m")

    assert streamed.text == buffered.text == "Answer text"
    assert streamed.finish_reason == buffered.finish_reason == "tool_calls"
    assert streamed.usage == buffered.usage
    assert streamed.tool_calls == buffered.tool_calls
    assert [tc.tool_name for tc in streamed.tool_calls] == ["read_state", "read_state"]
    assert [tc.arguments for tc in streamed.tool_calls] == [{"path": "/x"}, {"path": "/y"}]
    assert streamed.model_used == buffered.model_used == "m"


async def test_stream_rejection_falls_back_to_exactly_one_buffered_call():
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        streaming = json.loads(request.content)["stream"]
        seen.append(streaming)
        if streaming:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "response_format json_object cannot be used with stream",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "buffered"}, "finish_reason": "stop"}]},
        )

    result = await _provider(handler).complete(_req(), model="m")
    assert result.text == "buffered"
    assert seen == [True, False]  # exactly one fallback, never a loop


async def test_an_unrelated_4xx_propagates_without_a_buffered_fallback():
    """The fallback is transport mechanics, not an error-swallowing retry: a
    4xx that has nothing to do with streaming must surface exactly as before."""
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["stream"])
        return httpx.Response(
            404,
            json={"error": {"message": "model not found", "type": "invalid_request_error"}},
        )

    with pytest.raises(LLMError):
        await _provider(handler).complete(_req(), model="m")
    assert seen == [True]

    auth_seen: list[bool] = []

    def auth_handler(request: httpx.Request) -> httpx.Response:
        auth_seen.append(json.loads(request.content)["stream"])
        return httpx.Response(401, json={"error": {"message": "bad key", "type": "auth_error"}})

    with pytest.raises(LLMAuthError):
        await _provider(auth_handler).complete(_req(), model="m")
    assert auth_seen == [True]


async def test_a_server_that_ignores_streaming_is_still_decoded():
    """Some OpenAI-compatible servers accept `stream: true` and answer with one
    JSON object anyway. Returning an empty answer there would be an
    infrastructure failure wearing a model failure's clothes."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "whole body"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    result = await _provider(handler).complete(_req(), model="m")
    assert result.text == "whole body"
    assert result.finish_reason == "length"
    assert result.usage.output_tokens == 3


async def test_stream_complete_uses_the_same_progress_budgets():
    provider = _scripted_provider(
        (0.0, _sse_bytes({"choices": [{"delta": {"content": "a"}}]})),
        (_HANG, _sse_bytes(_FINAL_CHUNK)),
        DISCO_LLM_FIRST_TOKEN_TIMEOUT_S="5",
        DISCO_LLM_IDLE_TIMEOUT_S="0.05",
    )
    deltas: list[str] = []
    with pytest.raises(LLMTransientError) as caught:
        async for chunk in provider.stream_complete(_req(), model="m"):
            if chunk.delta_text:
                deltas.append(chunk.delta_text)
    assert deltas == ["a"]  # bytes that DID arrive were delivered before the stall
    assert "stream idle" in str(caught.value)


def test_a_400_the_provider_labels_server_error_is_transient():
    """opencode-go wraps its own upstream faults as HTTP 400 type=server_error
    for a few seconds at a time (2026-09-03: the first cleared on the next
    call, the second killed a run). The provider's own label is the class —
    the router retries and falls over exactly as it would for a 5xx."""
    from disco.core.llm import LLMTransientError
    from disco.core.llm._openai_response import raise_typed, safe_provider_error

    with pytest.raises(LLMTransientError) as caught:
        raise_typed(
            "prov",
            400,
            '{"error":{"message":"upstream stream failed at /srv/x.py","type":"server_error"}}',
            safe_error_fn=lambda s, t: safe_provider_error("prov", s, t),
            context_overflow_fn=lambda _t, _m: False,
        )
    assert caught.value.http_status == 400
    assert str(caught.value) == "provider prov returned HTTP 400 type=server_error"
    assert "srv" not in str(caught.value)
