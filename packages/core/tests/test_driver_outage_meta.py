"""Reliability-neutral driver-outage labeling (Option B).

A provider usage-exhaustion (HTTP 429) that exhausts the driver's bounded
transient-retry ladder must surface HONESTLY in the UI. The channel is the
events' documented non-semantic `meta` (events.py: "Free-form, non-semantic
metadata, UI hints, VOLATILE, never load-bearing"):

  - the provider adapter records the HTTP status on the typed error as an
    INERT attribute (`LLMTransientError.http_status` — like the existing
    unused `retry_after_s`);
  - the driver's exhaustion pause labels the landing events' meta with
    `driver_error` / `driver_error_kind` / `driver_error_provider` /
    `driver_error_http_status`;
  - NOTHING else changes: same retry counts, same statuses, same details,
    byte-identical model-visible message content. The proof here compares a
    labeled run against an unlabeled run event-by-event on every semantic
    field.
"""

from __future__ import annotations

import json

import httpx
import pytest
from disco.core import ConversationStatus, MessageEvent, StatusEvent
from disco.core.events import LLMConvertible, LLMMessage
from disco.core.llm import LLMTransientError
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole
from loop_fakes import ScriptedAgent, assert_blocked_question_landing, build_loop

CID = "conv"

_NEW_META_KEYS = (
    "driver_error",
    "driver_error_kind",
    "driver_error_provider",
    "driver_error_http_status",
)


# ---- provider classification: http_status is recorded, nothing else moves ----


def _req(text: str = "hi") -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content=text)],
        max_tokens=50,
        temperature=0.0,
    )


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


async def test_raise_typed_429_carries_http_status_and_same_safe_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "usage limit reached", "type": "GoUsageLimitError"}},
        )

    with pytest.raises(LLMTransientError) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert exc.value.http_status == 429
    # Classification + secret hygiene unchanged: safe summary only, raw body redacted.
    assert str(exc.value) == "provider fake returned HTTP 429 type=GoUsageLimitError"
    assert "usage limit reached" not in str(exc.value)


async def test_raise_typed_429_stream_path_carries_http_status():
    """BOTH call paths flow through _raise_typed — the stream path too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "usage limit reached", "type": "GoUsageLimitError"}},
        )

    with pytest.raises(LLMTransientError) as exc:
        async for _chunk in _provider(handler).stream_complete(_req(), model="m"):
            pass
    assert exc.value.http_status == 429
    assert str(exc.value) == "provider fake returned HTTP 429 type=GoUsageLimitError"


async def test_raise_typed_503_carries_http_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "down", "type": "server_error"}})

    with pytest.raises(LLMTransientError) as exc:
        await _provider(handler).complete(_req(), model="m")
    assert exc.value.http_status == 503
    assert str(exc.value) == "provider fake returned HTTP 503 type=server_error"


class _TimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")


class _NetworkFailureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")


async def test_timeout_and_network_errors_have_no_http_status():
    for transport in (_TimeoutTransport(), _NetworkFailureTransport()):
        provider = OpenAIProvider("http://fake/v1", name="fake", transport=transport)
        with pytest.raises(LLMTransientError) as exc:
            await provider.complete(_req(), model="m")
        assert exc.value.http_status is None


def test_http_status_default_is_none():
    """The attribute is inert and optional — a bare construction stays None."""
    assert LLMTransientError("x").http_status is None
    assert LLMTransientError("x", http_status=429).http_status == 429


# ---- driver exhaustion: landings carry the labels, semantics byte-identical --


def _usage_limit_error() -> LLMTransientError:
    # The shape a real opencode.ai 429 produces after adapter sanitization
    # (verbatim safe summary observed in the live soak's agent-server log).
    return LLMTransientError(
        "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
        provider="opencode-go",
        http_status=429,
    )


async def _run_exhaustion(monkeypatch, error: LLMTransientError, *, autonomous: bool = False):
    import disco.core.loop.driver as driver_module

    async def mock_sleep(_d):
        pass

    monkeypatch.setattr(driver_module, "_sleep", mock_sleep)
    agent = ScriptedAgent([error])
    loop, store = build_loop(agent, autonomous=autonomous)
    await loop.send_message("go")
    state = await loop.run()
    events = await store.get_events(CID)
    return state, events, agent


def _landing_statuses(events) -> list[StatusEvent]:
    return [e for e in events if isinstance(e, StatusEvent) and e.meta.get("blocked_landing")]


async def test_transient_429_exhaustion_labels_landing_meta(monkeypatch):
    state, events, agent = await _run_exhaustion(monkeypatch, _usage_limit_error())

    # The landing shape is EXACTLY the pre-existing fixture's: explained
    # driver-unavailable ask-gate after 1 initial call + 3 backoff retries.
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert agent.calls == 4
    assert_blocked_question_landing(events, legacy_detail="driver-unavailable")

    landings = _landing_statuses(events)
    assert landings, "expected blocked-landing StatusEvents"
    for status_event in landings:
        assert status_event.meta.get("driver_error") == (
            "provider opencode-go returned HTTP 429 type=GoUsageLimitError"
        )
        assert status_event.meta.get("driver_error_kind") == "LLMTransientError"
        assert status_event.meta.get("driver_error_provider") == "opencode-go"
        assert status_event.meta.get("driver_error_http_status") == 429


async def test_transient_429_exhaustion_autonomous_terminal_carries_labels(monkeypatch):
    """Autonomous flavor: the run CONCLUDES at the legacy PAUSED status — that
    terminal StatusEvent (what the UI's PAUSED banner reads) carries the labels."""
    state, events, _agent = await _run_exhaustion(
        monkeypatch, _usage_limit_error(), autonomous=True
    )
    assert state.execution_status == ConversationStatus.PAUSED
    assert_blocked_question_landing(events, legacy_detail="driver-unavailable", flavor="terminal")
    terminal = [e for e in events if isinstance(e, StatusEvent)][-1]
    assert terminal.status == ConversationStatus.PAUSED
    assert terminal.meta.get("driver_error_http_status") == 429
    assert terminal.meta.get("driver_error") == (
        "provider opencode-go returned HTTP 429 type=GoUsageLimitError"
    )


async def test_transient_without_http_status_omits_status_key(monkeypatch):
    """Negative control (timeout/network shape): no http_status and no provider →
    those keys are ABSENT from the landing meta, so the UI keeps today's generic
    text (the banner selector requires driver_error_http_status). The cause
    string/kind labels remain truthful diagnostics."""
    state, events, agent = await _run_exhaustion(
        monkeypatch, LLMTransientError("request timed out (ReadTimeout)")
    )
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert agent.calls == 4
    assert_blocked_question_landing(events, legacy_detail="driver-unavailable")
    for status_event in _landing_statuses(events):
        assert "driver_error_http_status" not in status_event.meta
        assert "driver_error_provider" not in status_event.meta
        assert status_event.meta.get("driver_error") == "request timed out (ReadTimeout)"
        assert status_event.meta.get("driver_error_kind") == "LLMTransientError"


def _semantic_projection(events) -> list[tuple]:
    """Every reliability-bearing field of the event log — everything EXCEPT the
    volatile per-run identifiers (ids, seqs, timestamps) and the non-semantic
    meta channel the labeling is allowed to use."""
    out: list[tuple] = []
    for e in events:
        row: tuple = (type(e).__name__, getattr(e, "source", None))
        if isinstance(e, StatusEvent):
            # detail may embed the (random) question-message id — normalize it
            # to a stable marker while still asserting presence/absence.
            row += (e.status, e.detail is not None)
        if isinstance(e, MessageEvent):
            row += (e.message.role, e.message.content)
        out.append(row)
    return out


async def test_labeling_never_changes_statuses_details_or_model_visible_bytes(monkeypatch):
    """The no-behavior-change proof: a labeled (429) run and an unlabeled run
    produce IDENTICAL event sequences on every semantic field — same kinds,
    statuses, detail-presence, and byte-identical ENVIRONMENT/AGENT message
    content — and the model view (to_llm_message) never contains the labels."""
    _s1, labeled, a1 = await _run_exhaustion(monkeypatch, _usage_limit_error())
    _s2, plain, a2 = await _run_exhaustion(
        monkeypatch,
        LLMTransientError(
            "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
            provider="opencode-go",
        ),
    )
    assert a1.calls == a2.calls == 4
    assert _semantic_projection(labeled) == _semantic_projection(plain)

    # The legacy detail string itself is untouched.
    details = [e.detail for e in labeled if isinstance(e, StatusEvent)]
    assert "driver-unavailable" in details

    # Model-context invariance: meta never crosses into the LLM view.
    for e in labeled:
        if isinstance(e, LLMConvertible):
            content = json.dumps(e.to_llm_message().model_dump())
            for key in _NEW_META_KEYS:
                assert key not in content
