"""Lane L26 — a terminal deep-research failure travels as fields, not prose.

Every terminal deep-research error used to reach the UI and the acceptance
harness as one English sentence in ``ErrorEvent.detail`` /
``StatusEvent.detail``. Anything that wanted to ACT on the failure — an error
panel that renders the next action on its own line, a batch tally that counts
outages apart from research results — had to read that sentence back, and a
message rewording silently re-bucketed it.

These tests pin the two halves of the fix: the class comes from the code path
that raised (never from the message), and the prose is unchanged.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.agent_server._deep_research_service_parts.failures import (
    preflight_failure,
    run_failure_for,
)
from disco.core import (
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    RunFailure,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import LLMAuthError
from disco.retrieval.deep_research._exhaustion import ExtractionOutcomes, exhaustion_failure
from disco.retrieval.deep_research.agent import ResearchAgentError


class _FakeNLI:
    def entail(self, premise, hypothesis):
        return "entail"

    def score(self, premise, hypothesis):
        return 0.9


def _dr_runtime(store: SqliteEventStore) -> ConversationRuntime:
    rt = ConversationRuntime(
        store,
        research_providers={
            "search": object(),
            "extraction": object(),
            "reranker": _FakeNLI(),
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )
    rt.settings._set_surface("c1", "deep_research")
    return rt


# ---- the class comes from the code path that raised -------------------------


def test_a_research_wall_carries_its_own_class_verbatim() -> None:
    """`_exhaustion` decided which provider had failed; nothing re-decides it."""
    failure = exhaustion_failure(
        budget="turn",
        extraction=ExtractionOutcomes(attempted=60, failures={"upstream_http_500": 59}),
        trail=[],
        sources_retained=0,
        turns_used=8,
        turns_total=8,
    )
    exc = ResearchAgentError(failure)

    assert run_failure_for(exc) is failure
    assert failure.failure_class == "extraction_infrastructure"
    # The sentence an operator reads is rendered FROM the fields.
    assert str(exc) == failure.detail
    assert "59/60 attempts failed" in failure.why


def test_a_run_that_genuinely_found_nothing_is_not_classed_as_an_outage() -> None:
    """A research RESULT and an infrastructure failure must stay tellable apart."""
    failure = exhaustion_failure(
        budget="source",
        extraction=ExtractionOutcomes(attempted=20, failures={}),
        trail=[],
        sources_retained=0,
        turns_used=4,
        turns_total=8,
    )
    assert failure.failure_class == "no_usable_evidence"
    assert "this is a result, not an outage" in failure.next


def test_the_host_circuit_breaker_names_itself() -> None:
    failure = exhaustion_failure(
        budget="infrastructure",
        extraction=ExtractionOutcomes(attempted=0, failures={}),
        trail=[],
        sources_retained=0,
        turns_used=0,
        turns_total=8,
        streak=8,
    )
    assert failure.failure_class == "host_circuit_breaker"
    assert "never reached the world" in failure.why


def test_a_provider_error_is_classed_by_its_type_not_its_message() -> None:
    failure = run_failure_for(
        LLMAuthError("invalid api key", provider="ollama-cloud", model="deepseek")
    )
    assert failure.failure_class == "model_provider"
    assert "LLMAuthError [ollama-cloud / deepseek]" in failure.why
    assert "invalid api key" in failure.why
    assert "quota" in failure.next


def test_an_unmapped_exception_is_admitted_rather_than_guessed_at() -> None:
    failure = run_failure_for(RuntimeError("something nobody classified"))
    assert failure.failure_class == "internal_error"
    assert "no failing boundary named itself" in failure.state
    assert "report this run" in failure.next


def test_every_failure_radiates_the_four_parts() -> None:
    failures = [
        preflight_failure("Driver 'm' rejected the API key: invalid api key"),
        run_failure_for(RuntimeError("x")),
        run_failure_for(LLMAuthError("y", provider="p", model="m")),
    ]
    for failure in failures:
        assert isinstance(failure, RunFailure)
        for part in (failure.why, failure.state, failure.next, failure.allowed):
            assert part.strip()
            assert part in failure.detail


# ---- and it really lands on the wire ----------------------------------------


async def test_the_preflight_error_event_carries_the_structured_failure() -> None:
    """End to end: a dead driver's ErrorEvent has fields as well as a sentence."""
    store = SqliteEventStore(":memory:")
    rt = _dr_runtime(store)
    reason = "Driver 'm' rejected the API key: invalid api key"

    async def _dead(cid, **kw):
        return reason

    rt._driver_preflight.check = _dead  # type: ignore[method-assign]

    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER, message=LLMMessage(role="user", content="research X")
        ),
    )
    await rt.deep_research._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    error = next(
        e for e in events if isinstance(e, ErrorEvent) and e.code == "deep_research_preflight"
    )
    statuses = [e for e in events if isinstance(e, StatusEvent)]

    # The prose is unchanged...
    assert error.detail == reason
    assert statuses[-1].status == ConversationStatus.ERROR
    # ...and the same failure is now readable as fields.
    assert error.failure is not None
    assert error.failure.failure_class == "preflight"
    assert error.failure.why == reason
    assert "nothing ran" in error.failure.state
    assert "ask this question again" in error.failure.next


def test_the_error_event_round_trips_its_failure_through_the_store_schema() -> None:
    """The field is on the durable event, not only on the in-memory object."""
    from disco.core.events import event_from_json_dict, event_to_json_dict

    failure = preflight_failure("no reranker is configured")
    event = ErrorEvent(code="deep_research_preflight", detail="x", failure=failure)
    restored = event_from_json_dict(event_to_json_dict(event))

    assert isinstance(restored, ErrorEvent)
    assert restored.failure == failure


def test_an_error_event_written_before_the_field_existed_still_loads() -> None:
    from disco.core.events import event_from_json_dict, event_to_json_dict

    raw = event_to_json_dict(ErrorEvent(code="max_iterations", detail="reached 500"))
    del raw["failure"]
    restored = event_from_json_dict(raw)

    assert isinstance(restored, ErrorEvent)
    assert restored.failure is None
