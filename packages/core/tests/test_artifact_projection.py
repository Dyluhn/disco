"""REL-2a artifact projection from persisted event evidence."""

from __future__ import annotations

from disco.core import (
    ObservationEvent,
    ToolResult,
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
