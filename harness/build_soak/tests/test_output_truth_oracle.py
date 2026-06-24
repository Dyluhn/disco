"""OutputTruthOracle unit tests (guidelines §11.5, PR S2)."""

from __future__ import annotations

from _eventlog import clean_smoke_log

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.output_truth import OutputTruthOracle

_SCN = {
    "id": "static_html_minimal",
    "assertions": {
        "workspace": {"files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}]},
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}


def _run(scenario=_SCN, workspace=None, preview=None, events=None):
    return OutputTruthOracle().check(
        normalize_events(events or clean_smoke_log()),
        scenario=scenario,
        workspace_manifest=workspace,
        preview=preview,
    )


def test_finished_with_required_file_and_content_passes():
    results = _run(workspace={"index.html": "<h1>Build Smoke OK</h1>"})
    assert results[0].passed, results[0].to_dict()


def test_finished_with_missing_file_is_false_finish():
    results = _run(workspace={"other.html": "x"})
    assert results[0].code == "FALSE_FINISH_NO_OUTPUT"
    assert results[0].facts["required_path"] == "index.html"


def test_finished_with_missing_content_is_artifact_mismatch():
    results = _run(workspace={"index.html": "<h1>nope</h1>"})
    assert results[0].code == "ARTIFACT_TRUTH_MISMATCH"
    assert results[0].facts["missing_substring"] == "Build Smoke OK"


def test_no_output_assertion_skips():
    results = _run(scenario={"id": "x", "assertions": {}})
    assert results[0].skipped


def test_preview_404_is_false_finish():
    scenario = {
        "id": "p",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Build Smoke OK"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    results = _run(scenario=scenario, preview={"health": {"status": 404}, "content": ""})
    assert results[0].code == "FALSE_FINISH_PREVIEW_BROKEN"


def test_preview_content_mismatch():
    scenario = {
        "id": "p",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Grand Opening"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    preview = {"health": {"status": 200}, "content": "<h1>old</h1>"}
    results = _run(scenario=scenario, preview=preview)
    assert results[0].code == "PREVIEW_TRUTH_MISMATCH"


def test_not_finished_skips_output_truth():
    # An unfinished run can't be a "false finish".
    unfinished = clean_smoke_log()[:-1]  # drop the FINISHED status
    results = _run(workspace={"other.html": "x"}, events=unfinished)
    assert results[0].skipped
