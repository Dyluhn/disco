"""Moved export side artifact extraction on collection implementations."""

from __future__ import annotations

from ._shared import _run_mod


def _impl_test_export_artifact_paths_extract_from_db_row_events():
    """`collect_events` returns DB-row dicts (payload JSON string); the export
    step's governed-artifact extraction must normalize before the admission
    oracle — previously it saw no admission on live rows and returned None for
    every run, masked by the root-viable fallback until the first subdirectory
    build (counted seed 440026, EXPORT_DOWNLOAD_MISSING)."""
    from _eventlog import to_db_rows
    from test_governed_admission_oracle import _scenario as _gov_scenario
    from test_governed_admission_oracle import _segment as _gov_segment

    events = _gov_segment()
    flat = _run_mod._governed_artifact_paths(
        events, scenario=_gov_scenario(), conversation_id="conv_fixture"
    )
    rows = _run_mod._governed_artifact_paths(
        to_db_rows(events), scenario=_gov_scenario(), conversation_id="conv_fixture"
    )
    assert flat == ["index.html"]
    assert rows == ["index.html"], "DB-row events must extract identically to flat events"
