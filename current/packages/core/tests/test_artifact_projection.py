"""REL-2a artifact projection from persisted event evidence."""

from __future__ import annotations

from disco.core import (
    DeliverableEvent,
    ObservationEvent,
    ToolResult,
    WorkspaceMutationEvent,
    event_from_json_dict,
    event_to_json_dict,
    migrate_event,
)
from disco.core.context.artifact_projection import (
    artifact_manifest_reader_enabled,
    artifact_paths_from_events,
    artifact_paths_from_manifest_records,
)


def _file_write_obs(path: str = "site/index.html") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id="call-write",
            tool_name="file_write",
            success=True,
            content="wrote 12 bytes",
            structured={
                "path": path,
                "sha256": "f" * 64,
            },
        ),
        action_id="action-write",
    )


def test_artifact_projection_picks_up_file_write_structured_path() -> None:
    assert artifact_paths_from_events([_file_write_obs("./site/index.html")]) == {"site/index.html"}


def test_file_write_observation_round_trip_still_projects() -> None:
    raw = event_to_json_dict(_file_write_obs("index.html"))
    rebuilt = event_from_json_dict(migrate_event(raw))

    assert artifact_paths_from_events([rebuilt]) == {"index.html"}


def test_artifact_projection_quarantines_losing_view_output() -> None:
    old_intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    new_intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    events = [
        old_intent,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=old_intent.id,
            agent_view_id="view-old",
            run_protocol_version=1,
        ),
        new_intent,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=new_intent.id,
            agent_view_id="view-new",
            run_protocol_version=1,
        ),
        DeliverableEvent(
            title="late losing output",
            path="stale.txt",
            artifact_kind="files",
            agent_view_id="view-old",
        ),
        DeliverableEvent(
            title="winning output",
            path="current.txt",
            artifact_kind="files",
            agent_view_id="view-new",
        ),
    ]

    assert artifact_paths_from_events(events) == {"current.txt"}


def test_artifact_projection_keeps_winning_view_observation() -> None:
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    observation = _file_write_obs("winning.txt").model_copy(
        update={"agent_view_id": "view-current"}
    )

    assert artifact_paths_from_events(
        [
            intent,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-current",
                run_protocol_version=1,
            ),
            observation,
        ]
    ) == {"winning.txt"}


def test_artifact_projection_keeps_legacy_unattributed_output() -> None:
    assert artifact_paths_from_events(
        [
            DeliverableEvent(
                title="legacy output",
                path="legacy.txt",
                artifact_kind="files",
            )
        ]
    ) == {"legacy.txt"}


def test_artifact_manifest_reader_flag_default_on_explicit_off(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_READER", raising=False)
    monkeypatch.delenv("PMX_ARTIFACT_MANIFEST_READER", raising=False)
    assert artifact_manifest_reader_enabled() is True

    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_READER", "on")
    assert artifact_manifest_reader_enabled() is True

    for falsy in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_READER", falsy)
        assert artifact_manifest_reader_enabled() is False, falsy


def test_artifact_paths_from_manifest_records_normalizes_duck_typed_paths() -> None:
    class Rec:
        def __init__(self, path):
            self.path = path

    assert artifact_paths_from_manifest_records(
        [Rec("./out/index.html"), Rec("report.pdf"), Rec("")]
    ) == {"out/index.html", "report.pdf"}
