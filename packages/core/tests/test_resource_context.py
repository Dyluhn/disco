from __future__ import annotations

import asyncio
import hashlib

from disco.core import (
    ActionEvent,
    EventSource,
    LLMMessage,
    ObservationEvent,
    ToolCall,
    ToolResult,
    WorkspaceMutationEvent,
)
from disco.core.effects import (
    CoverageSpan,
    CoverageUnit,
    EffectCapability,
    ObservationReceipt,
    ResourceCoverage,
    ResourceRevision,
)
from disco.core.events import _arg_snip_marker
from disco.core.loop.context_budget import ContextCaps
from disco.core.loop.dedup import (
    _F8_TRUNCATION_MARKER_TEMPLATE,
    _REDUNDANT_READ_NOTICE,
    _STALE_READ_NOTICE,
    _SUPERSEDED_READ_NOTICE,
    collapse_superseded_reads,
)
from disco.core.loop.file_state import FileStateTracker
from disco.core.loop.messages import _workspace_paths_from_events
from disco.core.loop.resource_context import (
    canonical_workspace_identifier,
    coverage_is_subset,
    essential_read_call_ids,
    essential_read_pair_seqs,
    receipt_covered_in_current_prompt,
    select_essential_read_records,
    snapshot_receipt,
    snapshot_window_receipt,
    workspace_file_key,
)
from disco.core.loop.view_render import workspace_snapshot_message
from disco.core.view import View
from event_fakes import with_seqs


def _receipt(
    path: str,
    *,
    digest: str,
    spans: tuple[tuple[int, int], ...],
    total: int,
    complete: bool = False,
) -> ObservationReceipt:
    return ObservationReceipt(
        capability=EffectCapability.WORKSPACE_CONTENT_READ,
        revision=ResourceRevision(resource=workspace_file_key(path), digest=digest),
        coverage=ResourceCoverage(
            unit=CoverageUnit.LINES,
            spans=tuple(CoverageSpan(start=start, end=end) for start, end in spans),
            total=total,
        ),
        complete=complete,
    )


def _pair(
    call_id: str,
    receipt: ObservationReceipt,
    *,
    content: str | None = None,
) -> list:
    rendered_content = content if content is not None else f"exact body {call_id}"
    rendered_bytes = rendered_content.encode("utf-8")
    bound_receipt = receipt.model_copy(
        update={
            "rendered_size_bytes": len(rendered_bytes),
            "rendered_sha256": hashlib.sha256(rendered_bytes).hexdigest(),
        }
    )
    action = ActionEvent(
        thought="read",
        tool_call=ToolCall(
            tool_name="file_read",
            arguments={"path": receipt.revision.resource.identifier},
            call_id=call_id,
        ),
    )
    observation = ObservationEvent(
        tool_result=ToolResult(
            call_id=call_id,
            tool_name="file_read",
            success=True,
            content=rendered_content,
            effect_receipts=(bound_receipt,),
        ),
        action_id=action.id,
    )
    return [action, observation]


def test_full_read_is_not_evicted_by_a_later_partial_same_revision():
    digest = hashlib.sha256(b"same revision").hexdigest()
    events = with_seqs(
        [
            *_pair(
                "full",
                _receipt("index.html", digest=digest, spans=((0, 349),), total=349, complete=True),
            ),
            *_pair("tail", _receipt("index.html", digest=digest, spans=((300, 349),), total=349)),
        ]
    )

    assert essential_read_call_ids(events) == {"full"}
    # Both halves of the provider tool pair are protected, not just the result.
    assert essential_read_pair_seqs(events) == {1, 2}


def test_disjoint_partial_reads_are_both_essential():
    digest = "a" * 64
    events = with_seqs(
        [
            *_pair("head", _receipt("large.py", digest=digest, spans=((0, 100),), total=200)),
            *_pair("tail", _receipt("large.py", digest=digest, spans=((100, 200),), total=200)),
        ]
    )

    assert essential_read_call_ids(events) == {"head", "tail"}


def test_later_superset_makes_earlier_partial_redundant():
    digest = "b" * 64
    events = with_seqs(
        [
            *_pair("narrow", _receipt("large.py", digest=digest, spans=((20, 40),), total=100)),
            *_pair("wide", _receipt("large.py", digest=digest, spans=((0, 60),), total=100)),
        ]
    )

    assert essential_read_call_ids(events) == {"wide"}


def test_only_latest_revision_is_essential_but_revisions_are_not_equivalent():
    events = with_seqs(
        [
            *_pair(
                "r1", _receipt("app.js", digest="1" * 64, spans=((0, 5),), total=5, complete=True)
            ),
            *_pair(
                "r2", _receipt("app.js", digest="2" * 64, spans=((0, 5),), total=5, complete=True)
            ),
        ]
    )

    assert essential_read_call_ids(events) == {"r2"}


def test_new_revision_partial_survives_and_old_full_is_labeled_stale():
    events = with_seqs(
        [
            *_pair(
                "r1-full",
                _receipt(
                    "app.js",
                    digest="1" * 64,
                    spans=((0, 20),),
                    total=20,
                    complete=True,
                ),
            ),
            *_pair(
                "r2-partial",
                _receipt("app.js", digest="2" * 64, spans=((8, 12),), total=20),
            ),
        ]
    )

    out = collapse_superseded_reads(_tool_messages(events), events)

    assert out[0].content == _STALE_READ_NOTICE.format(path="app.js", digest=("1" * 64)[:12])
    assert out[1].content == "exact body r2-partial"
    assert "same resource revision" not in out[0].content


def test_stale_pointer_requires_the_new_revision_body_in_this_request():
    events = with_seqs(
        [
            *_pair(
                "r1-full",
                _receipt(
                    "app.js",
                    digest="1" * 64,
                    spans=((0, 20),),
                    total=20,
                    complete=True,
                ),
            ),
            *_pair(
                "r2-partial",
                _receipt("app.js", digest="2" * 64, spans=((8, 12),), total=20),
            ),
        ]
    )
    only_old_body = [_tool_messages(events)[0]]

    out = collapse_superseded_reads(only_old_body, events)

    assert out[0].content == "exact body r1-full"


def test_redundant_pointer_requires_covering_read_body_in_this_request():
    digest = "3" * 64
    events = with_seqs(
        [
            *_pair("narrow", _receipt("app.js", digest=digest, spans=((5, 10),), total=20)),
            *_pair("wide", _receipt("app.js", digest=digest, spans=((0, 15),), total=20)),
        ]
    )
    only_narrow_body = [_tool_messages(events)[0]]

    out = collapse_superseded_reads(only_narrow_body, events)

    assert out[0].content == "exact body narrow"


def test_redundant_pointer_requires_the_exact_covering_read_bytes():
    digest = "4" * 64
    events = with_seqs(
        [
            *_pair("narrow", _receipt("app.js", digest=digest, spans=((5, 10),), total=20)),
            *_pair("wide", _receipt("app.js", digest=digest, spans=((0, 15),), total=20)),
        ]
    )
    messages = _tool_messages(events)
    messages[1] = messages[1].model_copy(update={"content": "[masked output: exact bytes absent]"})

    out = collapse_superseded_reads(messages, events)

    assert out[0].content == "exact body narrow"
    assert out[1].content == "[masked output: exact bytes absent]"
    assert all(message.content != _REDUNDANT_READ_NOTICE for message in out)


def test_unpaired_or_mismatched_receipt_is_not_prompt_evidence():
    receipt = _receipt("x.py", digest="c" * 64, spans=((0, 1),), total=1, complete=True)
    event = ObservationEvent(
        tool_result=ToolResult(
            call_id="forged",
            tool_name="file_read",
            success=True,
            content="x",
            effect_receipts=(receipt,),
        ),
        action_id="missing",
    ).model_copy(update={"seq": 1})

    assert essential_read_call_ids([event]) == frozenset()


def test_matching_call_id_cannot_substitute_for_the_exact_action_id():
    receipt = _receipt("x.py", digest="c" * 64, spans=((0, 1),), total=1, complete=True)
    action, observation = _pair("same-call", receipt)
    forged = observation.model_copy(update={"action_id": "different-action"})

    assert essential_read_call_ids(with_seqs([action, forged])) == frozenset()


def test_duplicate_call_id_across_distinct_pairs_authenticates_neither_receipt():
    receipt = _receipt("x.py", digest="c" * 64, spans=((0, 1),), total=1, complete=True)
    events = with_seqs([*_pair("duplicate", receipt), *_pair("duplicate", receipt)])

    assert essential_read_call_ids(events) == frozenset()


def test_multiple_observations_for_one_action_authenticate_no_receipt():
    receipt = _receipt("x.py", digest="c" * 64, spans=((0, 1),), total=1, complete=True)
    action, observation = _pair("duplicate-result", receipt)
    duplicate = observation.model_copy(update={"id": "evt_duplicate_observation"})

    assert essential_read_call_ids(with_seqs([action, observation, duplicate])) == frozenset()


def test_duplicate_observation_event_id_still_fails_pair_cardinality():
    receipt = _receipt("x.py", digest="c" * 64, spans=((0, 1),), total=1, complete=True)
    action, observation = _pair("same-result-id", receipt)

    assert (
        essential_read_call_ids(with_seqs([action, observation, observation.model_copy()]))
        == frozenset()
    )


def test_wrong_source_or_reverse_event_order_cannot_authenticate_a_receipt():
    receipt = _receipt("x.py", digest="c" * 64, spans=((0, 1),), total=1, complete=True)
    action, observation = _pair("ordered", receipt)
    wrong_source_action = action.model_copy(update={"source": EventSource.USER, "seq": 1})
    ordered_observation = observation.model_copy(update={"seq": 2})
    assert essential_read_call_ids([wrong_source_action, ordered_observation]) == frozenset()

    reverse_action = action.model_copy(update={"seq": 2})
    reverse_observation = observation.model_copy(update={"seq": 1})
    assert essential_read_call_ids([reverse_action, reverse_observation]) == frozenset()


def test_pointer_coverage_requires_same_resource_revision_and_exact_range():
    wanted = _receipt("x.py", digest="d" * 64, spans=((10, 20),), total=30)
    same = _receipt("./x.py", digest="d" * 64, spans=((0, 30),), total=30, complete=True)
    wrong_revision = _receipt("x.py", digest="e" * 64, spans=((0, 30),), total=30, complete=True)

    assert receipt_covered_in_current_prompt(wanted, [same])
    assert not receipt_covered_in_current_prompt(wanted, [wrong_revision])
    assert coverage_is_subset(wanted.coverage, [same.coverage])


def test_workspace_path_aliases_share_one_resource_identity():
    aliases = ["x.py", "./x.py", "workspace/x.py", "/workspace/x.py", "a/../x.py"]
    assert {canonical_workspace_identifier(path) for path in aliases} == {"x.py"}
    assert {workspace_file_key(path) for path in aliases} == {workspace_file_key("x.py")}


def test_selection_is_deterministic_for_seqless_fixtures():
    digest = "f" * 64
    records = []
    for events in (
        _pair("first", _receipt("x", digest=digest, spans=((0, 5),), total=10)),
        _pair("second", _receipt("x", digest=digest, spans=((5, 10),), total=10)),
    ):
        # Public fold authenticates pairs before selection; use it to obtain records.
        from disco.core.loop.resource_context import read_receipt_records

        records.extend(read_receipt_records(events))

    selected = select_essential_read_records(records)
    assert [record.call_id for record in selected] == ["first", "second"]


def _tool_messages(events: list) -> list[LLMMessage]:
    return [event.to_llm_message() for event in events if isinstance(event, ObservationEvent)]


def test_same_revision_snapshot_compacts_historical_read():
    digest = hashlib.sha256(b"current bytes").hexdigest()
    receipt = _receipt("app.js", digest=digest, spans=((0, 2),), total=2, complete=True)
    events = with_seqs(_pair("read", receipt))
    prompt_receipt = snapshot_receipt(
        path="./app.js",
        sha256=digest,
        total_lines=2,
        raw_size_bytes=13,
        rendered_content="current bytes",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(prompt_receipt,),
    )

    assert out[0].content == _SUPERSEDED_READ_NOTICE.format(path="app.js")


def test_current_run_explicit_read_stays_verbatim_beside_snapshot():
    """A requested read is answered in the recent tail, not only by a prefix pointer."""

    digest = hashlib.sha256(b"current bytes").hexdigest()
    receipt = _receipt("app.js", digest=digest, spans=((0, 2),), total=2, complete=True)
    events = with_seqs(
        [
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
            *_pair("read", receipt, content="current bytes"),
        ]
    )
    prompt_receipt = snapshot_receipt(
        path="app.js",
        sha256=digest,
        total_lines=2,
        raw_size_bytes=13,
        rendered_content="current bytes",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(prompt_receipt,),
    )

    assert out[0].content == "current bytes"


def test_new_run_intent_allows_prior_run_read_to_compact_to_snapshot():
    """The high-attention copy expires at the next durable instruction boundary."""

    digest = hashlib.sha256(b"current bytes").hexdigest()
    receipt = _receipt("app.js", digest=digest, spans=((0, 2),), total=2, complete=True)
    events = with_seqs(
        [
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
            *_pair("read", receipt, content="current bytes"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
        ]
    )
    prompt_receipt = snapshot_receipt(
        path="app.js",
        sha256=digest,
        total_lines=2,
        raw_size_bytes=13,
        rendered_content="current bytes",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(prompt_receipt,),
    )

    assert out[0].content == _SUPERSEDED_READ_NOTICE.format(path="app.js")


def test_current_run_read_never_overrides_a_newer_snapshot_revision():
    """External mutation wins over high-attention placement of an old read."""

    old = _receipt("app.js", digest="1" * 64, spans=((0, 2),), total=2, complete=True)
    events = with_seqs(
        [
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
            *_pair("read", old, content="old bytes"),
        ]
    )
    changed_snapshot = snapshot_receipt(
        path="app.js",
        sha256="2" * 64,
        total_lines=2,
        raw_size_bytes=9,
        rendered_content="new bytes",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(changed_snapshot,),
    )

    assert out[0].content == _STALE_READ_NOTICE.format(path="app.js", digest=("1" * 64)[:12])


def test_latest_current_run_partial_read_is_visible_even_beside_an_older_full_read():
    """A just-requested range is answered even when a full read is already essential."""

    digest = "3" * 64
    full = _receipt("app.js", digest=digest, spans=((0, 20),), total=20, complete=True)
    partial = _receipt("app.js", digest=digest, spans=((10, 15),), total=20)
    events = with_seqs(
        [
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
            *_pair("full", full, content="full body"),
            *_pair("partial", partial, content="targeted lines"),
        ]
    )
    current = snapshot_receipt(
        path="app.js",
        sha256=digest,
        total_lines=20,
        raw_size_bytes=9,
        rendered_content="full body",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(current,),
    )

    assert out[0].content == "full body"
    assert out[1].content == "targeted lines"


def test_changed_revision_snapshot_labels_historical_read_stale():
    receipt = _receipt("app.js", digest="1" * 64, spans=((0, 2),), total=2, complete=True)
    events = with_seqs(_pair("read", receipt))
    changed_snapshot = snapshot_receipt(
        path="app.js",
        sha256="2" * 64,
        total_lines=2,
        raw_size_bytes=13,
        rendered_content="changed bytes",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(changed_snapshot,),
    )

    assert out[0].content == _STALE_READ_NOTICE.format(path="app.js", digest=("1" * 64)[:12])


def test_full_then_partial_compaction_retains_the_full_read():
    digest = "3" * 64
    events = with_seqs(
        [
            *_pair(
                "full",
                _receipt(
                    "app.js",
                    digest=digest,
                    spans=((0, 20),),
                    total=20,
                    complete=True,
                ),
            ),
            *_pair(
                "partial",
                _receipt("app.js", digest=digest, spans=((10, 20),), total=20),
            ),
        ]
    )

    out = collapse_superseded_reads(_tool_messages(events), events)

    assert out[0].content == "exact body full"
    assert out[1].content == _REDUNDANT_READ_NOTICE.format(path="app.js")


def test_normal_view_decoration_still_allows_exact_covering_read_compaction():
    digest = "8" * 64
    events = with_seqs(
        [
            *_pair("narrow", _receipt("app.js", digest=digest, spans=((5, 10),), total=20)),
            *_pair("wide", _receipt("app.js", digest=digest, spans=((0, 15),), total=20)),
        ]
    )
    rendered = [message for message in View.of(events).messages if message.role == "tool"]

    out = collapse_superseded_reads(rendered, events)

    assert out[0].content == _REDUNDANT_READ_NOTICE.format(path="app.js")
    assert out[1].content.endswith("exact body wide")


def test_bounded_essential_large_single_line_read_bypasses_generic_snip():
    body = "BEGIN-" + ("x" * 20_000) + "-END"
    events = with_seqs(
        _pair(
            "large-line",
            _receipt("minified.js", digest="9" * 64, spans=((0, 1),), total=1, complete=True),
            content=body,
        )
    )

    rendered = next(
        message for message in View.of(events).messages if message.tool_call_id == "large-line"
    )

    assert body in rendered.content
    assert "snipped" not in rendered.content


def test_historical_exact_read_working_set_is_bounded_by_resource_count():
    events: list = []
    for index in range(30):
        events.extend(
            _pair(
                f"read-{index}",
                _receipt(
                    f"src/file-{index}.txt",
                    digest=f"{index:064x}",
                    spans=((0, 1),),
                    total=1,
                    complete=True,
                ),
                content=f"EXACT-{index}-" + ("z" * 2_000),
            )
        )
    view = View.of(with_seqs(events))
    tool_bodies = [message.content for message in view.messages if message.role == "tool"]

    assert sum("EXACT-" in content for content in tool_bodies) == 8
    assert sum(len(message.content) for message in view.messages) < 40_000
    assert sum("[masked output:" in content for content in tool_bodies) == 22


def test_legacy_reads_without_receipts_are_never_compacted_by_path_guessing():
    first = ActionEvent(
        thought="read",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": "app.js"}, call_id="r1"),
    )
    second = ActionEvent(
        thought="read",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": "app.js"}, call_id="r2"),
    )
    events = with_seqs(
        [
            first,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id="r1", tool_name="file_read", success=True, content="old"
                ),
                action_id=first.id,
            ),
            second,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id="r2", tool_name="file_read", success=True, content="new"
                ),
                action_id=second.id,
            ),
        ]
    )

    out = collapse_superseded_reads(_tool_messages(events), events)

    assert [message.content for message in out] == ["old", "new"]


def test_bp06_cannot_mask_an_essential_read_older_than_eight_outputs():
    digest = "4" * 64
    events: list = [
        *_pair(
            "essential",
            _receipt("source.py", digest=digest, spans=((0, 1),), total=1, complete=True),
            content="ESSENTIAL-EXACT-BODY-" + "z" * 700,
        )
    ]
    for index in range(9):
        action = ActionEvent(
            thought="diagnostic",
            tool_call=ToolCall(
                tool_name="shell", arguments={"command": "status"}, call_id=f"f{index}"
            ),
        )
        events.extend(
            [
                action,
                ObservationEvent(
                    tool_result=ToolResult(
                        call_id=f"f{index}",
                        tool_name="shell",
                        success=True,
                        content=f"filler-{index}-" + "x" * 700,
                    ),
                    action_id=action.id,
                ),
            ]
        )
    events = with_seqs(events)
    view = View.of(events)
    rendered = next(message for message in view.messages if message.tool_call_id == "essential")

    assert "ESSENTIAL-EXACT-BODY" in rendered.content
    assert "[masked output:" not in rendered.content


class _Sandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[str] = []

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        return self.files[path]


def _write(path: str) -> ActionEvent:
    return ActionEvent(
        thought="write",
        tool_call=ToolCall(
            tool_name="file_write",
            arguments={"path": path, "content": ""},
        ),
    )


def _read(path: str) -> ActionEvent:
    return ActionEvent(
        thought="read",
        tool_call=ToolCall(
            tool_name="file_read",
            arguments={"path": path},
        ),
    )


def test_rereads_do_not_reorder_the_workspace_state_working_set():
    events = [_write("a.py"), _write("b.py")]
    assert _workspace_paths_from_events(events) == (["b.py", "a.py"], [])

    events.append(_read("a.py"))
    assert _workspace_paths_from_events(events) == (["b.py", "a.py"], [])

    read_only = [_read("r1.py"), _read("r2.py"), _read("r1.py")]
    assert _workspace_paths_from_events(read_only) == ([], ["r2.py", "r1.py"])


def test_reference_packs_and_context_files_are_read_inputs_not_working_set():
    """Pharmacy run 7: the snapshot re-injected 142 KB of reference-pack text every turn
    (207 KB total) because the agent had *read* the packs. Inputs the host placed for
    reading stay one read away; only files the agent writes there count."""
    events = [
        _read("/workspace/references/socket-io-v4/PACK.md"),
        _read("references/email/nodemailer-smtp-transport.md"),
        _read(".disco/context/direction_tokens.css"),
        _read("src/server.js"),
        _write("src/public/app.js"),
    ]
    assert _workspace_paths_from_events(events) == (["src/public/app.js"], ["src/server.js"])
    # a write into an input directory is the agent's own file and stays in the snapshot
    assert _workspace_paths_from_events([_write("references/notes.md")]) == (
        ["references/notes.md"],
        [],
    )


def test_reread_keeps_cacheable_workspace_snapshot_byte_stable():
    sandbox = _Sandbox({"a.py": b"a = 1\n", "b.py": b"b = 2\n"})
    before_events = [_write("a.py"), _write("b.py")]
    after_events = [*before_events, _read("a.py")]

    before = asyncio.run(workspace_snapshot_message(sandbox, before_events))
    after = asyncio.run(workspace_snapshot_message(sandbox, after_events))

    assert before is not None and after is not None
    assert before.content == after.content


def test_reread_does_not_change_the_single_file_snapshot_slot():
    sandbox = _Sandbox({"a.py": b"a = 1\n", "b.py": b"b = 2\n"})
    caps = ContextCaps(
        max_files=1,
        per_file_chars=100,
        total_chars=2_000,
        read_char_budget=100,
        obs_snip_chars=100,
    )
    before_events = [_write("a.py"), _write("b.py")]
    after_events = [*before_events, _read("a.py")]

    before = asyncio.run(workspace_snapshot_message(sandbox, before_events, caps=caps))
    after = asyncio.run(workspace_snapshot_message(sandbox, after_events, caps=caps))

    assert before is not None and after is not None
    assert "BEGIN FILE b.py" in before.content
    assert "BEGIN FILE a.py" not in before.content
    assert before.content == after.content


def test_snapshot_emits_exact_receipt_and_never_uses_prior_turn_pointer():
    raw = b"const answer = 42;\n"
    sandbox = _Sandbox({"app.js": raw})
    tracker = FileStateTracker()
    events = [_write("app.js")]

    first_receipts: list[ObservationReceipt] = []
    first = asyncio.run(
        workspace_snapshot_message(
            sandbox,
            events,
            tracker=tracker,
            out_prompt_receipts=first_receipts,
        )
    )
    second_receipts: list[ObservationReceipt] = []
    second = asyncio.run(
        workspace_snapshot_message(
            sandbox,
            events,
            tracker=tracker,
            out_prompt_receipts=second_receipts,
        )
    )

    assert first is not None and second is not None
    assert "BEGIN FILE app.js" in second.content
    assert "current, shown earlier" not in second.content
    assert first.content == second.content
    assert len(first_receipts) == len(second_receipts) == 1
    receipt = second_receipts[0]
    assert receipt.revision.digest == hashlib.sha256(raw).hexdigest()
    assert receipt.complete
    assert receipt.coverage.covers_total()
    assert receipt.revision.digest in second.content
    assert "Host-observed exact resource ranges physically present" in second.content


def test_snapshot_omits_invalid_utf8_instead_of_silently_replacing_bytes():
    sandbox = _Sandbox({"invalid.txt": b"valid\n\xff\xfe\n"})
    receipts: list[ObservationReceipt] = []

    snapshot = asyncio.run(
        workspace_snapshot_message(
            sandbox,
            [_write("invalid.txt")],
            out_prompt_receipts=receipts,
        )
    )

    assert snapshot is not None
    assert "\ufffd" not in snapshot.content
    assert "invalid UTF-8 text omitted without lossy replacement: invalid.txt" in snapshot.content
    assert receipts == []
    assert "CURRENT PROMPT FILE COVERAGE" not in snapshot.content


def test_truncated_snapshot_emits_exact_current_revision_byte_ranges():
    raw = ("A" * 80 + "B" * 80).encode()
    sandbox = _Sandbox({"large.txt": raw})
    receipts: list[ObservationReceipt] = []
    caps = ContextCaps(
        max_files=1,
        per_file_chars=40,
        total_chars=2_000,
        read_char_budget=40,
        obs_snip_chars=100,
    )

    snapshot = asyncio.run(
        workspace_snapshot_message(
            sandbox,
            [_write("large.txt")],
            caps=caps,
            out_prompt_receipts=receipts,
        )
    )

    assert snapshot is not None
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.revision.digest == hashlib.sha256(raw).hexdigest()
    assert receipt.coverage.unit == CoverageUnit.BYTES
    assert len(receipt.coverage.spans) == 2
    assert not receipt.complete
    assert "exact byte ranges" in snapshot.content


def test_truncated_current_snapshot_invalidates_older_read_revision():
    old = _receipt("large.txt", digest="1" * 64, spans=((0, 1),), total=1, complete=True)
    events = with_seqs(_pair("old", old))
    current = snapshot_window_receipt(
        path="large.txt",
        sha256="2" * 64,
        raw_size_bytes=100,
        byte_spans=((0, 20), (80, 100)),
        rendered_content="head … tail",
    )

    out = collapse_superseded_reads(
        _tool_messages(events),
        events,
        prompt_receipts=(current,),
    )

    assert out[0].content == _STALE_READ_NOTICE.format(path="large.txt", digest=("1" * 64)[:12])


def test_snapshot_canonicalizes_aliases_before_spending_file_slots():
    sandbox = _Sandbox({"x.py": b"print('one')\n"})
    events = [_write("x.py"), _write("./x.py"), _write("workspace/x.py")]

    snapshot = asyncio.run(workspace_snapshot_message(sandbox, events))

    assert snapshot is not None
    assert sandbox.reads == ["x.py"]
    assert snapshot.content.count("BEGIN FILE x.py") == 1


def test_snapshot_names_budget_omitted_paths_for_targeted_recovery():
    sandbox = _Sandbox({"a.py": b"a = 1\n", "b.py": b"b = 2\n"})
    caps = ContextCaps(
        max_files=1,
        per_file_chars=100,
        total_chars=2_000,
        read_char_budget=100,
        obs_snip_chars=100,
    )

    snapshot = asyncio.run(
        workspace_snapshot_message(sandbox, [_write("a.py"), _write("b.py")], caps=caps)
    )

    assert snapshot is not None
    assert "BEGIN FILE b.py" in snapshot.content
    assert "not present in this request because of the snapshot budget: a.py" in snapshot.content


def test_snapshot_with_zero_file_slots_still_names_recoverable_paths():
    sandbox = _Sandbox({"only.py": b"value = 1\n"})
    caps = ContextCaps(
        max_files=0,
        per_file_chars=100,
        total_chars=2_000,
        read_char_budget=100,
        obs_snip_chars=100,
    )

    snapshot = asyncio.run(workspace_snapshot_message(sandbox, [_write("only.py")], caps=caps))

    assert snapshot is not None
    assert sandbox.reads == []
    assert "not present in this request because of the snapshot budget: only.py" in snapshot.content


def test_elision_markers_are_metadata_not_unconditional_reread_commands():
    markers = (
        _arg_snip_marker(9_001),
        _F8_TRUNCATION_MARKER_TEMPLATE.format(path="src/app.ts"),
    )

    for marker in markers:
        lowered = marker.lower()
        assert "not file content" in lowered
        assert "do not copy or re-send" in lowered
        assert "file_read" not in lowered
        assert "full content" not in lowered
        assert "only the" in lowered and "range" in lowered
