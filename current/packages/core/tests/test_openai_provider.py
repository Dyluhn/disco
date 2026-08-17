"""OpenAI-compatible ModelProvider adapter — hermetic tests (httpx.MockTransport).

No network: a mock transport returns canned OpenAI responses so we test the
adapter's parsing, error classification, reasoning-model handling, and SSE
reassembly. (The live smoke against the real Qwen endpoint is run separately.)
"""

from __future__ import annotations

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
        assert body["model"] == "m1" and body["stream"] is False
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
