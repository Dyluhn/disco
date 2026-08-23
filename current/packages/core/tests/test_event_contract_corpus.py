"""Canonical compatibility corpus for the complete durable Event union."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import get_args

import pytest
from disco.core.events import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    ActionEvent,
    AgentErrorEvent,
    AlternativeOption,
    AlternativesEvent,
    AppKitEjectionEvent,
    BaseEvent,
    BuildPlatformAdmissionEvent,
    ClarifyEvent,
    ClarifyQuestionItem,
    CondensationEvent,
    ContextResolvedEvent,
    ContextSummaryEvent,
    ConversationStatus,
    DatasourceEvent,
    DeliverableEvent,
    ErrorEvent,
    Event,
    EventAdapter,
    EventKind,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    QuestionsV2Event,
    QuestionsV2Item,
    ReportEvent,
    ReportSection,
    RuntimeConstraintEvent,
    ScheduleEvent,
    ScheduleRunEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
    WorkspaceMutationEvent,
    WorkspaceRestoredEvent,
    WorkspaceVersionEvent,
    event_from_json_dict,
    event_to_json_dict,
)
from disco.core.migration import migrate_event
from disco.core.state import ConversationState
from disco.core.store.sqlite import SqliteEventStore
from pydantic import ValidationError

_TIMESTAMP = datetime(2026, 7, 29, 12, 34, 56, tzinfo=UTC)
_BASE_KEYS = (
    "id",
    "source",
    "timestamp",
    "schema_version",
    "seq",
    "agent_view_id",
    "meta",
    "kind",
)
_PAYLOAD_KEYS = {
    "message": ("message", "verification_requirements"),
    "action": ("thought", "tool_call", "self_assessed_risk", "llm_response_id"),
    "observation": ("tool_result", "action_id"),
    "agent_error": (
        "error",
        "detail",
        "failure_class",
        "failure_reason",
        "action_id",
        "tool_call_id",
        "action_profile",
        "effect_receipts",
    ),
    "condensation": (
        "forgotten_start_seq",
        "forgotten_end_seq",
        "summary",
        "summary_role",
        "reason",
    ),
    "status": (
        "status",
        "detail",
        "run_intent_id",
        "host_mutation_id",
        "plan_verification_transition",
        "plan_verifier_failure",
        "plan_verifier_pass",
        "recovery_lease_transition",
    ),
    "workspace_version": ("version_seq", "tree_digest", "trigger", "final_seal"),
    "workspace_restored": ("version_seq", "tree_digest", "label"),
    "workspace_mutation": (
        "operation",
        "paths",
        "run_intent_id",
        "run_protocol_version",
    ),
    "build_platform_admission": (
        "route",
        "profile_id",
        "run_intent_id",
        "composition_authority",
        "execution_bridge",
        "composition_digest",
        "run_identity",
        "transition",
        "supersedes_admission_id",
        "verification_claims",
        "verification_contract",
    ),
    "appkit_ejection": (
        "action_id",
        "tool_call_id",
        "source_profile_id",
        "target_profile_id",
        "source_version_seq",
        "source_tree_digest",
        "ejected_version_seq",
        "ejected_tree_digest",
        "lost_guarantees",
        "appkit_verified",
    ),
    "plan": ("summary", "steps", "revision", "context"),
        "report": (
            "query",
            "summary",
            "sections",
            "passages",
            "reviewed_passages",
            "all_hits",
            "claims",
            "completed_probes",
            "pending_probes",
            "unsupported_count",
            "bounded_by",
            "depth_tier",
    ),
    "alternatives": ("failed_action_id", "summary", "options"),
    "knowledge": ("scope", "snippet"),
    "runtime_constraint": (
        "constraint_key",
        "scope",
        "guidance",
        "alternative",
        "capability_generation",
        "active",
    ),
    "datasource": ("name", "docs"),
    "deliverable": (
        "title",
        "path",
        "artifact_kind",
        "deployment_url",
        "target_id",
        "delivery_contract",
        "verification_contract_digest",
    ),
    "verifier_started": (
        "artifact_path",
        "artifact_kind",
        "verifier",
        "requested_by_event_id",
        "target_id",
        "run_intent_id",
        "run_identity",
        "delivery_shape",
        "delivery_entry_reference",
        "check_id",
        "receipt_kind",
        "issuer_id",
        "operation",
        "delegated_issuer_ids",
        "verification_contract_digest",
        "deliverable_event_id",
        "execution_identity",
        "artifact_identity",
        "preview_selection",
        "workspace_revision",
        "workspace_generation",
        "workspace_epoch",
        "observed_after_seq",
    ),
    "verifier_verdict": (
        "artifact_path",
        "artifact_kind",
        "verified",
        "verdict",
        "detail",
        "failures",
        "screenshot_path",
        "verification_result",
        "target_id",
        "check_id",
        "receipt_kind",
        "verification_contract_digest",
        "requested_by_event_id",
    ),
    "verifier_shadow": (
        "artifact_path",
        "artifact_kind",
        "inline_verdict",
        "host_verdict",
        "agreement",
        "detail",
    ),
    "error": ("code", "detail"),
    "schedule": ("action", "schedule_id", "rrule", "description"),
    "schedule_run": ("schedule_id", "coalesced"),
    "clarify": ("question", "items"),
    "questions_v2": ("question", "items"),
    "context_resolved": (
        "range_id",
        "forgotten_start_seq",
        "forgotten_end_seq",
        "reason",
        "summary_ref_path",
    ),
    "context_summary": ("range_id", "rel_path", "summary", "artifact_kind"),
}

# These hashes were generated from the accepted pre-extraction implementation.
_EXPECTED_DIGESTS = {
    "message": "8b4eaffafef7a82c230158b3087b85c8389c20b640031dba839088a986099da6",
    "action": "34ac3440ad4758d4fb4ffa3f15c9c7da3de919b866aae7f296074fabae502ca5",
    "observation": "b3c5dd8c7f142e3a05491ee2302d45e579c35cdd93c32d30f620c620f9f254ec",
    "agent_error": "cb5ae5cd5ed70bce12f07fd7211ae54e3ad8b6a49ec6eefc1473c52d79dc2f6c",
    "condensation": "cd3508bfd412fa344bf7002f99464ebc9cf1525c533712bcc8b8212a1852e744",
    "status": "c93ae7f0ff633e6c3ed65f7254b7cbb64d25266709ad5508e487e201b397bc20",
    "workspace_version": "af802a1c75769da8cf3e95b51b6adad5979fd9e3d366babdc7e3ca275afa5c94",
    "workspace_restored": "81ed33d666e3e8b1703c736c61c75cc958a6be3ecf581515c3b0c5ff8c54f610",
    "workspace_mutation": "7bee3f81c8039434905eab5d3909fabfb452c79816f943379850a7c80421ef0e",
    "build_platform_admission": "3d4e18a539a3cb2154bc2201e8f6b89e44c56e5c80d78cd247928e9955390429",
    "appkit_ejection": "0649776978b87095aff671e39377068ee60d757e5ed527d9cacdc96b4d29cd74",
    "plan": "a46005ac3cf3e54c4c6ff0840cd49d58b3ccd0d224f9bbd3d609663bf6f2c7ad",
    "report": "7f2b215b63214958df02e1f2941223fad5f9c561859e482659da44160e4b5700",
    "alternatives": "7717421de89409f5011c551da33cb6cc3a4b47428847d5626a8ab44716c0cb26",
    "knowledge": "5e817149942f8fcc99d9b0b54cc11a984cef31f9f3bd5a969f2dbb17fd603a1a",
    "runtime_constraint": "5001da3952633fa21d53bc1f4f6d40ec8908e40b516eea0b224180c26f3bfd55",
    "datasource": "7cb90fa5944aaa260e91f0a0609284a6c8fa8f6a7130225e02954356b556d0c4",
    "deliverable": "1f55d2816f81f1924cf83339e87441895a93f6797996fd319f53246eb2e056e1",
    "verifier_started": "f11b71844ee7214ec6e5024d9a7d3eca04234f24bab714faf69dc0acb0997630",
    "verifier_verdict": "a59b00ec289de32cf969a71294fbb035a3c73bd3b0ac11364d3df0c18d79ba27",
    "verifier_shadow": "fe9dabacd4d77d8d54ebf6091bb2ba6498b51a3a0b13a1a48a0852b55a4b686c",
    "error": "53c8a7983238adcad3291443b561267703f355d3f107be00f31f4f5d13879900",
    "schedule": "40ddf261ce43f6f2bde00ddbf21175887f1ff4dddb3acf1f828a985e2020062a",
    "schedule_run": "c8cfeab308673b30fb807709b73f495806fc459eb62640eda32ad232181522f9",
    "clarify": "df5619368fc2ebdc33309639180fe3b5bc7a85da8ab764402d5e7bdcaaed6480",
    "questions_v2": "aa1f2bf5e9820268d6cbbfe17b775eaed6a8f86f19bbeab0becb17c8da076ead",
    "context_resolved": "cf583c5d51c2676e1942b010712251705f35db0ef904c07daad88b37796f575c",
    "context_summary": "405fe8a026cb1a610b5ebd8d332a1b5455b414b8f797c4469a1d747c45e8decc",
}


def _envelope(index: int) -> dict[str, object]:
    return {
        "id": f"evt_corpus_{index:02d}",
        "timestamp": _TIMESTAMP,
        "seq": index,
        "agent_view_id": "view-corpus",
        "meta": {"fixture": "event-contract-v1"},
    }


def make_event_corpus() -> tuple[BaseEvent, ...]:
    """Return one deterministic, valid instance of every Event member in order."""
    return (
        MessageEvent(
            **_envelope(1),
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Build the fixture."),
        ),
        ActionEvent(
            **_envelope(2),
            thought="Inspect deterministic input.",
            tool_call=ToolCall(
                tool_name="file_read",
                arguments={"path": "README.md"},
                call_id="call_corpus",
            ),
        ),
        ObservationEvent(
            **_envelope(3),
            tool_result=ToolResult(
                call_id="call_corpus",
                tool_name="file_read",
                success=True,
                content="fixture bytes",
            ),
            action_id="evt_corpus_02",
        ),
        AgentErrorEvent(
            **_envelope(4),
            error="fixture_error",
            detail="deterministic failure",
            action_id="evt_corpus_02",
            tool_call_id="call_corpus",
        ),
        CondensationEvent(
            **_envelope(5),
            forgotten_start_seq=1,
            forgotten_end_seq=2,
            summary="Earlier fixture activity.",
        ),
        StatusEvent(**_envelope(6), status=ConversationStatus.RUNNING),
        WorkspaceVersionEvent(
            **_envelope(7),
            version_seq=7,
            tree_digest="a" * 64,
            trigger="checkpoint",
        ),
        WorkspaceRestoredEvent(
            **_envelope(8),
            version_seq=3,
            tree_digest="b" * 64,
            label="fixture restore",
        ),
        WorkspaceMutationEvent(
            **_envelope(9),
            operation="editor.write",
            paths=("README.md",),
        ),
        BuildPlatformAdmissionEvent(
            **_envelope(10),
            route="legacy",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent-corpus",
            composition_authority="legacy",
        ),
        AppKitEjectionEvent(
            **_envelope(11),
            action_id="evt_corpus_02",
            tool_call_id="call_corpus",
            source_version_seq=1,
            source_tree_digest="c" * 64,
            ejected_version_seq=2,
            ejected_tree_digest="d" * 64,
            lost_guarantees=APPKIT_EJECTION_LOST_GUARANTEES,
        ),
        PlanEvent(
            **_envelope(12),
            summary="Implement the fixture.",
            steps=[PlanStep(title="Create corpus", detail="Pin all event shapes.")],
            context="Accepted architecture contract.",
        ),
        ReportEvent(
            **_envelope(13),
            query="What is pinned?",
            summary="Every event shape.",
            sections=[
                ReportSection(
                    id="section-1",
                    title="Coverage",
                    markdown="All 28 kinds.",
                    cited_passage_ids=["passage-1"],
                )
            ],
            passages=[{"id": "passage-1", "text": "fixture"}],
            reviewed_passages=[{"id": "passage-2", "text": "reviewed fixture"}],
            all_hits=[{"url": "https://example.invalid/fixture"}],
        ),
        AlternativesEvent(
            **_envelope(14),
            failed_action_id="evt_corpus_02",
            summary="Choose a deterministic recovery.",
            options=[
                AlternativeOption(
                    id="retry",
                    title="Retry",
                    description="Retry the fixture.",
                    tool_name="file_read",
                    arguments={"path": "README.md"},
                )
            ],
        ),
        KnowledgeEvent(
            **_envelope(15),
            scope="fixtures",
            snippet="Keep event bytes deterministic.",
        ),
        RuntimeConstraintEvent(
            **_envelope(16),
            constraint_key="fixture.constraint",
            scope="corpus",
            guidance="Use fixed values.",
            alternative="Regenerate from the accepted baseline.",
            capability_generation="generation-1",
        ),
        DatasourceEvent(
            **_envelope(17),
            name="Fixture API",
            docs="GET /fixture returns stable bytes.",
        ),
        DeliverableEvent(
            **_envelope(18),
            title="Event corpus",
            path="packages/core/tests/test_event_contract_corpus.py",
            artifact_kind="files",
        ),
        VerifierStartedEvent(
            **_envelope(19),
            artifact_path="artifact.txt",
            artifact_kind="files",
            target_id="target-corpus",
            check_id="check-corpus",
            receipt_kind="fixture",
            workspace_revision=1,
            workspace_generation="generation-1",
            observed_after_seq=18,
        ),
        VerifierVerdictEvent(
            **_envelope(20),
            artifact_path="artifact.txt",
            artifact_kind="files",
            detail="Fixture verdict.",
            target_id="target-corpus",
            check_id="check-corpus",
            receipt_kind="fixture",
        ),
        VerifierShadowEvent(
            **_envelope(21),
            artifact_path="artifact.txt",
            artifact_kind="files",
            inline_verdict="pass",
            host_verdict="pass",
            agreement=True,
            detail="Fixture agreement.",
        ),
        ErrorEvent(
            **_envelope(22),
            code="fixture_error",
            detail="Deterministic conversation error.",
        ),
        ScheduleEvent(
            **_envelope(23),
            action="created",
            schedule_id="schedule-corpus",
            rrule="0 12 * * *",
            description="Run the fixture.",
        ),
        ScheduleRunEvent(
            **_envelope(24),
            schedule_id="schedule-corpus",
            coalesced=True,
        ),
        ClarifyEvent(
            **_envelope(25),
            question="Choose fixture scope.",
            items=[
                ClarifyQuestionItem(
                    id="scope",
                    question="Which scope?",
                    type="choice",
                    options=["all"],
                    answer="all",
                )
            ],
        ),
        QuestionsV2Event(
            **_envelope(26),
            question="Confirm fixture inputs.",
            items=[
                QuestionsV2Item(
                    id="inputs",
                    question="Use fixed inputs?",
                    options=["yes", "no"],
                    answer="yes",
                )
            ],
        ),
        ContextResolvedEvent(
            **_envelope(27),
            range_id="cxr_corpus",
            forgotten_start_seq=1,
            forgotten_end_seq=2,
            reason="resolved",
            summary_ref_path="summaries/corpus.md",
        ),
        ContextSummaryEvent(
            **_envelope(28),
            range_id="cxr_corpus",
            rel_path="summaries/corpus.md",
            summary="Deterministic context summary.",
            artifact_kind="summary",
        ),
    )


CORPUS = make_event_corpus()


class _OutOfUnionEvent(BaseEvent):
    """Concrete BaseEvent subclass deliberately absent from Event."""

    kind: str = "out_of_union"


def _json_digest(event: BaseEvent) -> str:
    encoded = json.dumps(
        event_to_json_dict(event), ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_corpus_exactly_matches_ordered_event_union_and_kind_enum() -> None:
    union_members = get_args(get_args(Event)[0])
    assert tuple(type(event) for event in CORPUS) == union_members
    assert {event.kind for event in CORPUS} == set(EventKind)
    assert len(CORPUS) == len(EventKind) == 28


@pytest.mark.parametrize("event", CORPUS, ids=lambda event: event.kind.value)
def test_every_event_has_stable_json_shape_bytes_and_round_trip(event: BaseEvent) -> None:
    raw = event_to_json_dict(event)
    assert tuple(raw) == _BASE_KEYS + _PAYLOAD_KEYS[event.kind.value]
    assert _json_digest(event) == _EXPECTED_DIGESTS[event.kind.value]
    restored = event_from_json_dict(migrate_event(raw))
    adapted = EventAdapter.validate_python(raw)
    assert type(restored) is type(event)
    assert type(adapted) is type(event)
    assert restored == event
    assert adapted == event


@pytest.mark.parametrize("event", CORPUS, ids=lambda event: event.kind.value)
def test_migration_is_pure_idempotent_and_defensively_copies(event: BaseEvent) -> None:
    raw = event_to_json_dict(event)
    snapshot = deepcopy(raw)
    once = migrate_event(raw)
    twice = migrate_event(once)
    assert raw == snapshot
    assert once == twice == raw
    assert once is not raw
    assert twice is not once


@pytest.mark.parametrize("event", CORPUS, ids=lambda event: event.kind.value)
def test_every_concrete_event_is_frozen(event: BaseEvent) -> None:
    with pytest.raises(ValidationError):
        event.meta = {}  # type: ignore[misc]


def test_unknown_kind_and_unexpected_known_kind_field_fail_closed() -> None:
    unknown = event_to_json_dict(CORPUS[0])
    unknown["kind"] = "future_event"
    with pytest.raises(ValidationError):
        EventAdapter.validate_python(unknown)

    extra = event_to_json_dict(CORPUS[0])
    extra["unexpected"] = True
    with pytest.raises(ValidationError):
        EventAdapter.validate_python(extra)


def test_historical_deliverable_without_kind_is_migrated_only_on_read() -> None:
    deliverable = next(event for event in CORPUS if isinstance(event, DeliverableEvent))
    raw = event_to_json_dict(deliverable)
    del raw["artifact_kind"]
    snapshot = deepcopy(raw)

    migrated = migrate_event(raw)

    assert raw == snapshot
    assert migrated["artifact_kind"] == "app"
    assert migrate_event(migrated) == migrated
    restored = event_from_json_dict(migrated)
    assert isinstance(restored, DeliverableEvent)
    assert restored.artifact_kind == "app"


def test_frozen_corpus_produces_the_accepted_canonical_fold() -> None:
    expected = {
        "conversation_id": "corpus",
        "execution_status": "ERROR",
        "iteration": 1,
        "max_iterations": 42,
        "last_seq": 28,
        "active_agent_view_id": None,
        "active_agent_view_seq": None,
        "agent_view_pending": False,
        "pending_action_id": None,
        "pending_plan_id": None,
        "pending_alternatives_id": None,
        "pending_question_id": None,
        "pending_clarify_id": None,
        "pending_questions_v2_id": None,
        "extras": {},
    }
    first = ConversationState.reconstruct("corpus", list(CORPUS), max_iterations=42)
    second = ConversationState.reconstruct("corpus", list(CORPUS), max_iterations=42)
    assert first.model_dump(mode="json") == expected
    assert second == first


async def test_store_rejects_untyped_values_before_any_persistence() -> None:
    store = SqliteEventStore(":memory:")
    valid = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="valid"),
    )
    try:
        with pytest.raises(TypeError, match="concrete Event"):
            await store.append("corpus", {"kind": "message"})  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="concrete Event"):
            await store.append("corpus", BaseEvent(source=EventSource.USER))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="concrete Event"):
            await store.append(
                "corpus",
                _OutOfUnionEvent(source=EventSource.USER),  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="concrete Event"):
            await store.append_many(
                "corpus",
                [valid, {"kind": "message"}],  # type: ignore[list-item]
            )
        assert await store.get_events("corpus") == []

        stored = await store.append("corpus", valid)
        assert stored.kind is EventKind.MESSAGE
        assert await store.get_events("corpus") == [stored]
    finally:
        store.close()
