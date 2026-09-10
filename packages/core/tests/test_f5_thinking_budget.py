"""F5 — GATED thinking-budget management.

# Contract (T3)

When the assist gate is ON, the OpenAI provider:

  (a) EMERGENCY HEAD+TAIL TRUNCATE an over-long `<think>` block in any
      message's content. Under-budget blocks and messages WITHOUT a closed
      `<think>...</think>` are passed through byte-identically. Truncation
      keeps head + tail + a recoverable marker ("…[F5 truncated N chars of
      reasoning; head (plan) above, tail (conclusion) below]…").

  (b) DISABLE thinking (`chat_template_kwargs.enable_thinking=False`) on a
      repair attempt ≥ 2. The engine threads the attempt counter via
      `CompletionRequest.attempt` (default 1 = first try); the provider
      forces `enable_thinking=False` when `req.assist=True` AND
      `req.attempt >= 2`. The override applies to BOTH the per-call
      `req.enable_thinking` and the provider default — the goal is to
      guarantee thinking is off on a repair.

Assist OFF (the capable-model default) → no F5 head+tail truncation and no
`chat_template_kwargs` override. The default `CompletionRequest.attempt=1`
keeps existing callers unaffected even if they never set the field.

ALL-TIERS think-history strip (independent of assist): a CLOSED
`<think>...</think>` block on an ASSISTANT-ROLE message is ALWAYS fully
stripped from re-fed history (reasoning-model context-poisoning fix). This
supersedes the F5 head+tail truncation for assistant messages — the block is
removed entirely. Non-assistant roles (user/tool/system) are NEVER stripped
(their literal `<think>` may be task data / tool observations). An UNCLOSED
`<think>` is preserved (the regex requires both tags).

# Prior art

SmallCode F5 (thinking_budget.js) — head+tail truncate + disable-repair
on attempt>1. SmallCode warns that injecting `enable_thinking` on some
small models (e.g. lfm2) also suppresses tool-calls — the assist-OFF gate
keeps the capable-model path free of that risk.

# Foci of this test file

  * payload shape — what the wire body looks like, not the LLM's response
  * assist-ON vs assist-OFF — both branches pinned
  * boundary conditions — under-budget, over-budget, multiple blocks,
    unclosed tag, no tag
  * streaming parity — the truncation+disable both apply to stream_complete
    (the LIVE path Disco streams)
"""

from __future__ import annotations

import json

import httpx
from disco.core.events import LLMMessage
from disco.core.llm.openai_provider import (
    _F5_THINK_BUDGET_CHARS,
    _F5_THINK_BUDGET_HEAD,
    _F5_THINK_BUDGET_TAIL,
    OpenAIProvider,
    _host_speaks_chat_template_kwargs,
    _truncate_think_block,
)
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _req(
    messages: list[LLMMessage] | None = None, *, assist: bool = False, attempt: int = 1
) -> CompletionRequest:
    """Build a request with optional over-long think content. The default
    messages are a single user turn — tests that want a long `<think>` block
    in the PRIOR assistant turn pass an explicit `messages` list."""
    if messages is None:
        messages = [LLMMessage(role="user", content="hi")]
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=messages,
        max_tokens=50,
        temperature=0.0,
        assist=assist,
        attempt=attempt,
    )


def _provider(captured: list[dict], handler=None) -> OpenAIProvider:
    """A provider whose mock transport captures the request body the provider
    would send. Tests assert on `captured[0]` (non-streaming) or the SSE
    request (streaming)."""

    def _h(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        captured.append(body)
        if handler is not None:
            return handler(request)
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(_h))


def _long_think(inner_chars: int) -> str:
    """Build a `<think>...</think>` block with exactly `inner_chars` chars
    of inner content. The opening + closing tags are not counted."""
    return f"<think>{'x' * inner_chars}</think>"


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


# ===========================================================================
# (1) Pure-function shape tests — the truncation helper in isolation.
#     These pin the exact threshold / head / tail / marker so the contract
#     is observable even without the full provider pipeline.
# ===========================================================================


def test_truncate_think_block_under_budget_passthrough():
    """A think block whose inner content is AT or BELOW the budget is left
    untouched — no false positives on normal-sized reasoning."""
    inner = "x" * _F5_THINK_BUDGET_CHARS  # exactly at the threshold
    content = f"<think>{inner}</think>"
    out = _truncate_think_block(content)
    assert out == content, "under-budget blocks must be returned unchanged"


def test_truncate_think_block_over_budget_keeps_head_and_tail():
    """An over-budget think block is replaced with head + marker + tail.
    The marker reports the dropped char count (so it's self-describing)
    and the head + tail come from the original positions (so the model's
    plan + conclusion survive)."""
    inner = "H" * _F5_THINK_BUDGET_HEAD + "M" * 5_000 + "T" * _F5_THINK_BUDGET_TAIL
    content = f"<think>{inner}</think>"
    out = _truncate_think_block(content)
    # The opening + closing tags are preserved.
    assert out.startswith("<think>")
    assert out.endswith("</think>")
    # The head and tail of the inner content survive.
    assert "H" * _F5_THINK_BUDGET_HEAD in out
    assert "T" * _F5_THINK_BUDGET_TAIL in out
    # The middle is dropped (not present in any contiguous form).
    assert "M" * 5_000 not in out
    # The marker is the recoverable identifier (deterministic — same input
    # → same output, so the request stays cache-stable across retries).
    assert "F5 truncated 5,000 chars of reasoning" in out
    # The total length is strictly less than the original (we dropped chars).
    assert len(out) < len(content)


def test_truncate_think_block_no_think_tags_unchanged():
    """A message with NO `<think>` block at all is passed through unchanged.
    The truncation only fires on a closed `<think>...</think>` block."""
    content = "Just a normal assistant turn with no think block at all. " * 100
    assert "<think>" not in content
    assert _truncate_think_block(content) == content


def test_truncate_think_block_unclosed_tag_passthrough():
    """An UNCLOSED `<think>` (no `</think>` — e.g. the model ran out of
    tokens mid-thought) is passed through unchanged. The provider's
    caller will see `finish_reason: length` and the condenser handles
    that via the existing path; F5 only touches CLOSED blocks because
    mid-block truncation can't be done deterministically."""
    content = "<think>" + "x" * 10_000  # no closing tag
    assert "</think>" not in content
    assert _truncate_think_block(content) == content


def test_truncate_think_block_multiple_blocks_each_handled():
    """Multiple CLOSED `<think>` blocks in the same content are each
    evaluated independently — over-budget blocks are truncated,
    under-budget blocks are preserved."""
    small = "<think>short</think>"
    big_inner = "X" * (_F5_THINK_BUDGET_CHARS + 1_000)
    big = f"<think>{big_inner}</think>"
    content = small + " and " + big
    out = _truncate_think_block(content)
    # The small block is preserved verbatim.
    assert small in out
    # The big block is truncated (marker present, full big_inner is gone).
    assert "F5 truncated" in out
    assert big_inner not in out


# ===========================================================================
# (2) End-to-end (provider) tests — the truncation + disable are applied
#     to the WIRE BODY the provider sends, gated on req.assist / req.attempt.
# ===========================================================================


# --- (a) ALL-TIERS strip of re-fed assistant `<think>` history ---
#
# The reasoning-model context-poisoning fix: an assistant turn's CLOSED
# `<think>...</think>` block is ALWAYS fully stripped from re-fed history,
# INDEPENDENT of req.assist (a reasoning model run as the CAPABLE driver
# emits its chain of thought inline in `content`; re-feeding it verbatim
# every turn poisons the growing context → tool-less turns → the actionless
# valve pauses the build). The F5 head+tail truncation (which only fired
# under assist) is therefore superseded for assistant-role messages: the
# whole block is removed, not budget-trimmed. Truncation still applies to
# NON-assistant roles under assist (see test_assist_truncation_still_fires_
# for_non_assistant_role below).


async def test_assist_on_overlong_assistant_think_is_stripped():
    """A prior assistant turn whose content includes an over-long `<think>`
    block (the model's prior reasoning echoed back) is FULLY STRIPPED on the
    wire when assist is ON. The block is gone entirely; the prose survives."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 3)  # well over budget
    prior = LLMMessage(
        role="assistant",
        content=f"{big_think}\n\nI'll go look up the docs now.",
    )
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=True)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")

    assert captured, "provider must have made a request"
    body = captured[0]
    msgs = body["messages"]
    prior_on_wire = msgs[0]["content"]
    # The think block is GONE entirely — no tags, no marker, no inner content.
    assert "<think>" not in prior_on_wire
    assert "</think>" not in prior_on_wire
    assert "F5 truncated" not in prior_on_wire
    assert big_think not in prior_on_wire
    # The post-think prose survives.
    assert "I'll go look up the docs now." in prior_on_wire
    # The user turn is untouched.
    assert msgs[1]["content"] == "continue"


async def test_assist_on_under_budget_assistant_think_is_stripped():
    """The strip ignores the F5 budget: ANY closed assistant `<think>` block —
    even a small one — is removed from re-fed history (it is reasoning, not an
    answer). Only the answer text survives."""
    small_think = _long_think(_F5_THINK_BUDGET_CHARS // 2)
    prior = LLMMessage(role="assistant", content=f"{small_think}\nnext step")
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=True)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")

    body = captured[0]
    prior_on_wire = body["messages"][0]["content"]
    assert "<think>" not in prior_on_wire
    assert small_think not in prior_on_wire
    assert "next step" in prior_on_wire


async def test_assist_off_overlong_assistant_think_is_stripped_all_tiers():
    """Assist OFF (the capable-model / reasoning-model default) ALSO strips
    re-fed assistant `<think>` history — this is the core of the fix. The
    capable driver no longer re-ingests its own chain of thought every turn."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 3)
    prior = LLMMessage(role="assistant", content=f"{big_think}\nnext step")
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=False)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")

    body = captured[0]
    prior_on_wire = body["messages"][0]["content"]
    # Stripped even with assist OFF.
    assert "<think>" not in prior_on_wire
    assert big_think not in prior_on_wire
    assert "F5 truncated" not in prior_on_wire
    assert "next step" in prior_on_wire


async def test_assistant_closed_think_stripped_exact():
    """Exact-output pin: an assistant "<think>plan</think>answer" yields
    content == "answer" on the wire (text before `<think>` and after
    `</think>` are both preserved; only the block is removed)."""
    prior = LLMMessage(role="assistant", content="<think>plan</think>answer")
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=False)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")
    assert captured[0]["messages"][0]["content"] == "answer"


async def test_user_and_tool_literal_think_is_NOT_stripped():
    """A USER or TOOL message that legitimately contains a literal
    `<think>...</think>` (task data, a tool observation echoing model output)
    is left UNCHANGED — stripping non-assistant content would corrupt data."""
    user = LLMMessage(role="user", content="<think>x</think>y")
    # The tool result must be PAIRED with an assistant tool_call — the adjacency
    # repair (a later, correct mechanism) drops orphaned tool results before the
    # wire, so an unpaired fixture never reaches the strip under test.
    caller = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": "c1", "name": "shell", "arguments": {}}],
    )
    tool = LLMMessage(role="tool", content="<think>x</think>y", tool_call_id="c1")
    req = _req(messages=[user, caller, tool], assist=False)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")
    msgs = captured[0]["messages"]
    assert msgs[0]["content"] == "<think>x</think>y"  # user unchanged
    tool_wire = next(m for m in msgs if m.get("role") == "tool")
    assert tool_wire["content"] == "<think>x</think>y"  # tool unchanged


async def test_no_think_assistant_is_byte_identical():
    """An assistant message with NO closed `<think>` block is byte-identical
    on the wire (the strip is a no-op)."""
    content = "Just a normal assistant answer with no reasoning block."
    prior = LLMMessage(role="assistant", content=content)
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=False)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")
    assert captured[0]["messages"][0]["content"] == content


async def test_unclosed_assistant_think_is_preserved():
    """An UNCLOSED `<think>` (W-31 truncated turn — no `</think>`) is preserved
    on an assistant message: `_F5_THINK_BLOCK_RE` requires BOTH tags, so the
    strip does not fire and the partial reasoning is kept for the condenser."""
    content = "<think>" + "x" * 50  # no closing tag
    prior = LLMMessage(role="assistant", content=content)
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=False)
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")
    assert captured[0]["messages"][0]["content"] == content


async def test_assist_truncation_still_fires_for_non_assistant_role():
    """The F5 head+tail truncation is unchanged for NON-assistant roles under
    assist: a tool observation echoing an over-long `<think>` block is
    budget-trimmed (not stripped — strip is assistant-only)."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 3)
    caller = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": "c1", "name": "shell", "arguments": {}}],
    )
    tool = LLMMessage(role="tool", content=f"{big_think}\nobserved", tool_call_id="c1")
    req = _req(
        messages=[caller, tool, LLMMessage(role="user", content="continue")],
        assist=True,
    )
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")
    on_wire = next(m for m in captured[0]["messages"] if m.get("role") == "tool")["content"]
    # Truncated (head+tail+marker), NOT stripped — the tags survive.
    assert "<think>" in on_wire and "</think>" in on_wire
    assert "F5 truncated" in on_wire
    assert big_think not in on_wire
    assert "observed" in on_wire


# --- (b) disable thinking on repair attempt >= 2 ---


async def test_assist_on_attempt1_keeps_thinking_per_provider_default():
    """On attempt 1 (the first try), the provider default is respected.
    The provider is constructed with `enable_thinking=True` and the request
    does NOT override it; the wire body has `enable_thinking: True`."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 2)  # also exercise (a) for free
    prior = LLMMessage(role="assistant", content=f"{big_think}\ndone")
    req = _req(
        messages=[prior, LLMMessage(role="user", content="continue")],
        assist=True,
        attempt=1,
    )
    captured: list[dict] = []
    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,
        transport=httpx.MockTransport(
            lambda r: (
                captured.append(json.loads(r.content)),
                httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            )[1]
        ),
    )
    await p.complete(req, model="m1")

    body = captured[0]
    # Thinking is ON on the first attempt.
    assert body["chat_template_kwargs"]["enable_thinking"] is True


async def test_assist_on_attempt2_disables_thinking_overriding_provider_default():
    """On a repair attempt (>= 2), thinking is FORCED OFF — even when the
    provider default is `enable_thinking=True`. The F5 gate wins over
    both the per-call override and the provider default on a repair."""
    req = _req(messages=[LLMMessage(role="user", content="hi")], assist=True, attempt=2)
    captured: list[dict] = []
    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,  # provider default = ON
        transport=httpx.MockTransport(
            lambda r: (
                captured.append(json.loads(r.content)),
                httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            )[1]
        ),
    )
    await p.complete(req, model="m1")

    body = captured[0]
    # Thinking is FORCED OFF on attempt >= 2.
    assert body["chat_template_kwargs"]["enable_thinking"] is False


async def test_assist_on_attempt2_disables_thinking_overriding_req_override():
    """On attempt 2, the F5 gate also wins over `req.enable_thinking=True`
    set by the caller. The repair-attempt disable is the LAST word."""
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hi")],
        max_tokens=50,
        temperature=0.0,
        assist=True,
        attempt=2,
        enable_thinking=True,  # caller wants ON — F5 wins
    )
    captured: list[dict] = []
    await _provider(captured).complete(req, model="m1")

    body = captured[0]
    # F5 disables thinking despite the per-call override.
    assert body["chat_template_kwargs"]["enable_thinking"] is False


async def test_assist_on_attempt3_keeps_thinking_disabled():
    """Attempt 3 (and beyond) also disable thinking. The gate is a one-way
    ratchet: once we hit attempt >= 2, thinking stays OFF for the rest
    of the repair sequence."""
    req = _req(messages=[LLMMessage(role="user", content="hi")], assist=True, attempt=3)
    captured: list[dict] = []
    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,
        transport=httpx.MockTransport(
            lambda r: (
                captured.append(json.loads(r.content)),
                httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            )[1]
        ),
    )
    await p.complete(req, model="m1")
    body = captured[0]
    assert body["chat_template_kwargs"]["enable_thinking"] is False


async def test_assist_off_attempt2_does_NOT_disable_thinking():
    """Assist OFF → the F5 disable-repair gate is closed. A repair
    attempt (>= 2) on the capable-model path leaves thinking alone —
    byte-identical to today. (Per SmallCode: injecting `enable_thinking`
    on some small models also suppresses tool-calls — the assist-OFF
    gate keeps the capable-model path free of that risk.)"""
    req = _req(messages=[LLMMessage(role="user", content="hi")], assist=False, attempt=2)
    captured: list[dict] = []
    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,
        transport=httpx.MockTransport(
            lambda r: (
                captured.append(json.loads(r.content)),
                httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            )[1]
        ),
    )
    await p.complete(req, model="m1")
    body = captured[0]
    # Thinking is still ON — the F5 gate is closed.
    assert body["chat_template_kwargs"]["enable_thinking"] is True


# --- (c) streaming parity: the LIVE path Disco streams (stream_complete)
#        must apply the same truncation + disable as the non-streaming
#        `complete` path. The provider's `_payload` is shared between
#        both, so the wire body is identical. ---


async def test_streaming_assist_on_overlong_assistant_think_is_stripped():
    """stream_complete path (the LIVE path Disco streams): re-fed assistant
    `<think>` history is FULLY STRIPPED on the wire, same as the non-streaming
    case — both call sites go through `_payload`."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 2)
    prior = LLMMessage(role="assistant", content=f"{big_think}\nok")
    req = _req(messages=[prior, LLMMessage(role="user", content="continue")], assist=True)
    captured: list[dict] = []
    # Drive the stream to its terminal chunk so the call returns cleanly.
    chunks = _sse(
        {"model": "m", "choices": [{"delta": {"content": "ok"}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content) if request.content else {})
        return httpx.Response(200, content=chunks, headers={"content-type": "text/event-stream"})

    p = OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(_handler))
    async for ch in p.stream_complete(req, model="m"):
        if ch.done:
            break
    body = captured[0]
    assert body["stream"] is True
    prior_on_wire = body["messages"][0]["content"]
    assert "<think>" not in prior_on_wire
    assert "F5 truncated" not in prior_on_wire
    assert big_think not in prior_on_wire
    assert "ok" in prior_on_wire


async def test_streaming_assist_on_attempt2_disables_thinking():
    """stream_complete path: attempt >= 2 forces thinking off on the wire,
    same as the non-streaming path."""
    req = _req(messages=[LLMMessage(role="user", content="hi")], assist=True, attempt=2)
    captured: list[dict] = []
    chunks = _sse(
        {"model": "m", "choices": [{"delta": {"content": "ok"}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content) if request.content else {})
        return httpx.Response(200, content=chunks, headers={"content-type": "text/event-stream"})

    p = OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(_handler))
    async for ch in p.stream_complete(req, model="m"):
        if ch.done:
            break
    body = captured[0]
    assert body["stream"] is True
    assert body["chat_template_kwargs"]["enable_thinking"] is False


async def test_streaming_assist_off_attempt2_strips_history_but_keeps_thinking():
    """stream_complete + assist OFF + attempt >= 2: the all-tiers strip removes
    re-fed assistant `<think>` history (the fix), but the F5 disable-thinking
    gate stays CLOSED on the capable path — `enable_thinking` follows the
    provider default (True), unchanged."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 2)
    prior = LLMMessage(role="assistant", content=f"{big_think}\nok")
    req = _req(
        messages=[prior, LLMMessage(role="user", content="continue")],
        assist=False,
        attempt=2,
    )
    captured: list[dict] = []
    chunks = _sse(
        {"model": "m", "choices": [{"delta": {"content": "ok"}}]},
        {
            "model": "m",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content) if request.content else {})
        return httpx.Response(200, content=chunks, headers={"content-type": "text/event-stream"})

    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,
        transport=httpx.MockTransport(_handler),
    )
    async for ch in p.stream_complete(req, model="m"):
        if ch.done:
            break
    body = captured[0]
    prior_on_wire = body["messages"][0]["content"]
    # History stripped (all tiers).
    assert "<think>" not in prior_on_wire
    assert big_think not in prior_on_wire
    assert "F5 truncated" not in prior_on_wire
    assert "ok" in prior_on_wire
    # Disable-thinking gate stays closed on the capable path: default (True).
    assert body["chat_template_kwargs"]["enable_thinking"] is True


# ===========================================================================
# (3) Combined assist-ON case: a long think + a repair attempt together.
#     This is the most realistic F5 scenario — a repair attempt where the
#     prior turn's reasoning is huge. Both gates must fire.
# ===========================================================================


async def test_assist_on_overlong_assistant_think_AND_attempt2_both_gates_fire():
    """A repair attempt (assist ON, attempt >= 2) that arrives with a prior
    assistant turn's over-long think block: BOTH the all-tiers strip and the
    disable-repair gate fire on the same call. The wire body must show both:
    the think history is stripped AND thinking is forced off."""
    big_think = _long_think(_F5_THINK_BUDGET_CHARS * 3)
    prior = LLMMessage(role="assistant", content=f"{big_think}\nconclusion")
    req = _req(
        messages=[prior, LLMMessage(role="user", content="continue")],
        assist=True,
        attempt=2,
    )
    captured: list[dict] = []
    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,
        transport=httpx.MockTransport(
            lambda r: (
                captured.append(json.loads(r.content)),
                httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            )[1]
        ),
    )
    await p.complete(req, model="m1")
    body = captured[0]
    # Strip gate fired: the think block is gone, the conclusion survives.
    assert "<think>" not in body["messages"][0]["content"]
    assert big_think not in body["messages"][0]["content"]
    assert "conclusion" in body["messages"][0]["content"]
    # Disable-repair gate fired.
    assert body["chat_template_kwargs"]["enable_thinking"] is False


# ===========================================================================
# (4) Default attempt — CompletionRequest.attempt defaults to 1 so any
#     caller that does NOT set it (today's callers) sees byte-identical
#     behavior. This is the "assist-OFF byte-identical" guarantee extended
#     to the attempt field itself.
# ===========================================================================


async def test_default_attempt_is_one_thinking_unchanged():
    """A request that does NOT set `attempt` defaults to 1 → thinking is
    governed by the existing per-call / provider-default rules; F5 does
    not flip it. This is the byte-identical guarantee for any caller
    that does not thread the attempt counter."""
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hi")],
        max_tokens=50,
        temperature=0.0,
        assist=True,
        # attempt NOT set — defaults to 1
    )
    captured: list[dict] = []
    p = OpenAIProvider(
        "http://fake/v1",
        name="fake",
        enable_thinking=True,
        transport=httpx.MockTransport(
            lambda r: (
                captured.append(json.loads(r.content)),
                httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            )[1]
        ),
    )
    await p.complete(req, model="m1")
    body = captured[0]
    # attempt=1 (default) → F5 disable-repair gate does NOT fire.
    assert body["chat_template_kwargs"]["enable_thinking"] is True


# --- chat_template_kwargs host gate (glm-5.2-on-Go regression, 2026-07-08) -----
# `chat_template_kwargs` is a llama.cpp/vLLM extension; strict clouds (Fireworks
# behind OpenCode Go) 400 the whole request over it. The provider must send it
# only to self-hosted-looking hosts.


def test_ctk_gate_public_hosts_refused():
    for url in (
        "https://opencode.ai/zen/go/v1",
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
        "https://api.fireworks.ai/inference/v1",
    ):
        assert _host_speaks_chat_template_kwargs(url) is False, url


def test_ctk_gate_self_hosted_allowed():
    for url in (
        "http://127.0.0.1:8080/v1",
        "http://localhost:8085/v1",  # dot-less hostname
        "http://192.168.1.50:8085/v1",
        "http://100.81.82.115:8000/v1",  # tailscale CGNAT
        "http://blackbox:8085/v1",
        "https://blackbox.taile518f9.ts.net/v1",
    ):
        assert _host_speaks_chat_template_kwargs(url) is True, url


def test_deepseek_public_api_explicitly_disables_thinking_on_both_paths():
    """DeepSeek tool turns must not create reasoning history Disco cannot replay."""
    provider = OpenAIProvider(
        "https://api.deepseek.com/v1",
        name="deepseek-direct",
        enable_thinking=True,
    )
    req = _req()

    for stream in (False, True):
        body = provider._payload(req, "deepseek-v4-flash", stream=stream)
        assert body["thinking"] == {"type": "disabled"}
        assert "chat_template_kwargs" not in body


def test_deepseek_nonthinking_policy_is_exactly_host_scoped():
    """No similarly named or unrelated OpenAI-compatible host inherits the policy."""
    req = _req()
    public_hosts = (
        "https://api.openai.com/v1",
        "https://deepseek.example/v1",
        "https://api.deepseek.com.example/v1",
    )
    for base_url in public_hosts:
        body = OpenAIProvider(base_url, enable_thinking=True)._payload(req, "m1", stream=False)
        assert "thinking" not in body
        assert "chat_template_kwargs" not in body

    self_hosted = OpenAIProvider("http://127.0.0.1:8080/v1", enable_thinking=True)
    body = self_hosted._payload(req, "m1", stream=False)
    assert "thinking" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
