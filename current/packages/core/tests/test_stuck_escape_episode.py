"""F1 (k6g 128k canary): recovery episodes end on progress boundaries.

The 2026-07-19 canary proved the stuck-escape episode outlived every trusted
progress signal: an approved plan revision (seq 417), sixteen mutation
receipts, passing verification, and a brand-new finish-gate proof obligation
(seq 512) all left the quarantine armed, so the model's legitimate reads at
seqs 428 and 514 were refused and the run landed STUCK. These tests pin the
repaired episode boundaries and the redundancy guard's semantic exemptions.
Nothing here names the canary's scenario, provider, or required literal —
paths and markers are arbitrary fixtures.
"""

from __future__ import annotations

import hashlib

from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.effects import (
    CoverageSpan,
    CoverageUnit,
    ObservationReceipt,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
)
from disco.core.effects import (
    EffectCapability as _EC,
)
from disco.core.events import CondensationEvent, ToolCall
from disco.core.loop import signals
from event_fakes import action
from loop_fakes import FakeExecutor, ScriptedAgent, build_loop


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _read_receipt(
    path: str,
    *,
    digest_seed: str,
    span: tuple[int, int],
    total: int,
    complete: bool = False,
) -> ObservationReceipt:
    return ObservationReceipt(
        capability=_EC.WORKSPACE_CONTENT_READ,
        revision=ResourceRevision(
            resource=ResourceKey(namespace="workspace.file", identifier=path),
            digest=_digest(digest_seed),
        ),
        coverage=ResourceCoverage(
            unit=CoverageUnit.LINES,
            spans=(CoverageSpan(start=span[0], end=span[1]),),
            total=total,
        ),
        complete=complete,
    )


def _escape_markers(seq: int) -> list:
    return [
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="stuck_escape_block:file_read",
        ).model_copy(update={"seq": seq}),
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="stuck_escape",
        ).model_copy(update={"seq": seq + 1}),
    ]


def _covered_read(seq: int, path: str, *, digest_seed: str, total: int = 300) -> list:
    """One successful full read of *path* with a receipt-proven whole coverage."""
    read = action(tool="file_read", args={"path": path}).model_copy(update={"seq": seq})
    result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=read.id,
        tool_result=ToolResult(
            call_id=read.tool_call.call_id,
            tool_name="file_read",
            success=True,
            content=f"[lines 1-{total} of {total}]\n…",
            effect_receipts=(
                _read_receipt(
                    path,
                    digest_seed=digest_seed,
                    span=(0, total),
                    total=total,
                    complete=True,
                ),
            ),
        ),
    ).model_copy(update={"seq": seq + 1})
    return [read, result]


def _driver():
    loop, _store = build_loop(ScriptedAgent([]), executor=FakeExecutor())
    return loop._driver  # noqa: SLF001 - direct pure-function contract


def _refusal(driver, events, path: str, **arguments):  # noqa: ANN001, ANN202
    call = ToolCall(tool_name="file_read", arguments={"path": path, **arguments})
    return driver.stuck_escape_redundant_read_refusal(events, call)


# ---- episode boundaries ------------------------------------------------------


def test_plan_approval_ends_the_recovery_episode():
    """k6g seq 417: `plan_approved` is a recovery boundary — detection already
    reset there while the quarantine ran on. Both must reset."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    assert signals.stuck_escape_seq(events) == 4
    assert signals.stuck_escape_blocked_tools(events) == frozenset({"file_read"})

    approved = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
    ).model_copy(update={"seq": 5})
    after = [*events, approved]
    assert signals.stuck_escape_seq(after) is None
    assert signals.stuck_escape_blocked_tools(after) == frozenset()
    driver = _driver()
    assert driver.stuck_escape_blocked_tools_for_step(after) == frozenset()
    assert _refusal(driver, after, "index.html") is None


def test_blocking_proof_obligation_ends_the_recovery_episode():
    """k6g seq 512→514: a finish-gate rejection (typed ``meta.blocking``) is a
    NEW proof obligation; the grounding read that serves it must execute."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    driver = _driver()
    # Within the episode the identical whole-file reread is provably redundant.
    assert _refusal(driver, events, "index.html") is not None

    obligation = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content="<system-reminder>a required literal is missing</system-reminder>",
        ),
        meta={"blocking": "user_literal_missing"},
    ).model_copy(update={"seq": 5})
    after = [*events, obligation]
    assert signals.stuck_escape_seq(after) is None
    assert driver.stuck_escape_blocked_tools_for_step(after) == frozenset()
    assert _refusal(driver, after, "index.html") is None


def test_plain_environment_reminder_does_not_end_the_episode():
    """Untyped environment prose (role=user, no blocking meta) is NOT a
    boundary — the k6g ration reminders must not have cleared the episode."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    reminder = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="<system-reminder>keep going</system-reminder>"),
    ).model_copy(update={"seq": 5})
    after = [*events, reminder]
    assert signals.stuck_escape_seq(after) == 4
    assert _refusal(_driver(), after, "index.html") is not None


def test_user_answer_ends_the_recovery_episode():
    """k6g seq 520: the answered question restored the full surface — by then
    the harness had already killed the run. Pin the product half."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    answer = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Use your best judgment and proceed."),
    ).model_copy(update={"seq": 5})
    after = [*events, answer]
    assert signals.stuck_escape_seq(after) is None
    assert _refusal(_driver(), after, "index.html") is None


# ---- redundancy-guard semantic exemptions -----------------------------------


def test_different_resource_read_is_never_refused():
    """k6g seq 428: the next plan step's read of a DIFFERENT file."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    assert _refusal(_driver(), events, "styles.css") is None


def test_unseen_range_of_a_partially_read_resource_is_allowed():
    """Pagination stays possible: only receipt-proven lines are withheld."""
    read = action(tool="file_read", args={"path": "notes.md", "limit": 100}).model_copy(
        update={"seq": 1}
    )
    result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=read.id,
        tool_result=ToolResult(
            call_id=read.tool_call.call_id,
            tool_name="file_read",
            success=True,
            content="[lines 1-100 of 300; read more with offset=101]\n…",
            effect_receipts=(
                _read_receipt("notes.md", digest_seed="v1", span=(0, 100), total=300),
            ),
        ),
    ).model_copy(update={"seq": 2})
    events = [read, result, *_escape_markers(3)]
    driver = _driver()
    # The already-delivered prefix is refused; the unseen tail is not.
    assert _refusal(driver, events, "notes.md", limit=50) is not None
    assert _refusal(driver, events, "notes.md", offset=101) is None
    assert _refusal(driver, events, "notes.md", offset=50, limit=100) is None


def test_condensation_forgotten_coverage_is_not_knowledge():
    """Content squeezed out of the model's view by condensation is NOT held:
    refusing the re-read would trap the model against invisible bytes."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    driver = _driver()
    assert _refusal(driver, events, "index.html") is not None

    forgotten = CondensationEvent(
        forgotten_start_seq=1,
        forgotten_end_seq=2,
        summary="early exploration condensed",
        reason="tokens",
    ).model_copy(update={"seq": 5})
    after = [*events, forgotten]
    assert _refusal(driver, after, "index.html") is None


def test_opaque_trusted_mutation_makes_coverage_unprovable():
    """A broad mutator with a changed-state receipt but no exact resource
    attribution may have changed ANY file — rereads are no longer redundant."""
    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    driver = _driver()
    assert _refusal(driver, events, "index.html") is not None

    script = action(tool="run_project_script", args={"name": "build"}).model_copy(update={"seq": 5})
    script_result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=script.id,
        tool_result=ToolResult(
            call_id=script.tool_call.call_id,
            tool_name="run_project_script",
            success=True,
            content="applied",
            structured={"applied": ["postprocess"]},
        ),
    ).model_copy(update={"seq": 6})
    after = [*events, script, script_result]
    assert _refusal(driver, after, "index.html") is None


def test_unknown_resource_and_unknown_total_are_never_refused():
    events = _escape_markers(1)
    driver = _driver()
    assert _refusal(driver, events, "never-read.md") is None
    # A read of a resource whose receipts never declared a total is unprovable.
    read = action(tool="file_read", args={"path": "stream.log"}).model_copy(update={"seq": 3})
    result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=read.id,
        tool_result=ToolResult(
            call_id=read.tool_call.call_id,
            tool_name="file_read",
            success=True,
            content="opaque",
        ),
    ).model_copy(update={"seq": 4})
    assert _refusal(driver, [*events, read, result], "stream.log") is None


def test_no_active_episode_means_no_refusal_at_all():
    events = _covered_read(1, "index.html", digest_seed="v1")
    assert _refusal(_driver(), events, "index.html") is None


# ---- verifier correction pass (V1, V2, V5) ----------------------------------


def test_v1_deletion_receipt_advances_the_revision_and_unblocks_the_read():
    """A deleted file's coverage proves nothing about a future read."""
    from disco.core.effects import MutationReceipt

    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    driver = _driver()
    assert _refusal(driver, events, "index.html") is not None

    delete = action(tool="file_delete", args={"path": "index.html"}).model_copy(update={"seq": 5})
    delete_result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=delete.id,
        tool_result=ToolResult(
            call_id=delete.tool_call.call_id,
            tool_name="file_delete",
            success=True,
            content="deleted",
            effect_receipts=(
                MutationReceipt(
                    resource=ResourceKey(namespace="workspace.file", identifier="index.html"),
                    before=ResourceRevision(
                        resource=ResourceKey(namespace="workspace.file", identifier="index.html"),
                        digest=_digest("v1"),
                    ),
                ),
            ),
        ),
    ).model_copy(update={"seq": 6})
    after = [*events, delete, delete_result]
    assert _refusal(driver, after, "index.html") is None


def test_v5_mutation_receipt_wins_same_observation_ordering_ties():
    """An ObservationReceipt listed before the MutationReceipt in ONE
    observation must not shadow the new digest."""
    from disco.core.effects import MutationReceipt

    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    driver = _driver()
    assert _refusal(driver, events, "index.html") is not None

    combo = action(tool="file_edit", args={"path": "index.html"}).model_copy(update={"seq": 5})
    combo_result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=combo.id,
        tool_result=ToolResult(
            call_id=combo.tool_call.call_id,
            tool_name="file_edit",
            success=True,
            content="edited",
            effect_receipts=(
                _read_receipt("index.html", digest_seed="v1", span=(0, 300), total=300),
                MutationReceipt(
                    resource=ResourceKey(namespace="workspace.file", identifier="index.html"),
                    after=ResourceRevision(
                        resource=ResourceKey(namespace="workspace.file", identifier="index.html"),
                        digest=_digest("v2"),
                    ),
                    after_size_bytes=64,
                ),
            ),
        ),
    ).model_copy(update={"seq": 6})
    after = [*events, combo, combo_result]
    assert _refusal(driver, after, "index.html") is None


def test_v2_refusal_floor_advances_on_trusted_progress_of_the_loop_resource():
    """The halt bound counts refusals only after the resource's own mutation."""
    from disco.core.effects import MutationReceipt

    events = _covered_read(1, "index.html", digest_seed="v1") + _escape_markers(3)
    driver = _driver()
    first = _refusal(driver, events, "index.html")
    assert first is not None
    assert first["refusal_floor_seq"] == 4  # the escape marker

    write = action(tool="file_write", args={"path": "index.html"}).model_copy(update={"seq": 5})
    write_result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=write.id,
        tool_result=ToolResult(
            call_id=write.tool_call.call_id,
            tool_name="file_write",
            success=True,
            content="wrote",
            structured={"path": "index.html", "sha256": "a" * 64},
            effect_receipts=(
                MutationReceipt(
                    resource=ResourceKey(namespace="workspace.file", identifier="index.html"),
                    after=ResourceRevision(
                        resource=ResourceKey(namespace="workspace.file", identifier="index.html"),
                        digest=_digest("v2"),
                    ),
                    after_size_bytes=64,
                ),
            ),
        ),
    ).model_copy(update={"seq": 6})
    reread = _covered_read(7, "index.html", digest_seed="v2")
    after = [*events, write, write_result, *reread]
    second = _refusal(driver, after, "index.html")
    assert second is not None
    assert second["refusal_floor_seq"] == 6  # the mutation observation
