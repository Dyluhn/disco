"""PKG-03 red-before-green contracts for the harness transport split.

The established deterministic cases remain the transcript authority for start,
follow-up, pause/resume, progressing timeout/freeze, cleanup, and pre-create
infrastructure handling.  These tests additionally prove that the old facade
and new owners execute equivalent fresh fakes, retain identical dossier bytes,
and keep every outcome's ``<out>/<run_id>`` path.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import test_api_runner as legacy
from _eventlog import clean_smoke_log

from harness.build_soak import run as old_run
from harness.build_soak._test_support.api_runner.helpers_core import (
    _client,
    _seed_db,
    _smoke_scenario,
)
from harness.build_soak._test_support.api_runner.helpers_transport import (
    FakeTransport,
)
from harness.build_soak.adapters.disco_api import (
    CollectedRun,
    DiscoApiClient,
    InfraProbeError,
)
from harness.build_soak.ports import (
    ConversationClient,
    EvidenceSink,
    EvidenceSourceClient,
    ObservabilityClient,
    ProductClient,
    ProgressClient,
    ScenarioDriver,
)

_CID = "conv_fake123"
_FIXED_TIME = "2026-07-29T00:00:00+00:00"


def _fresh_smoke(root: Path) -> tuple[DiscoApiClient, Any, dict[str, Any]]:
    root.mkdir(parents=True)
    db = root / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=[
            "RUNNING",
            "AWAITING_PLAN_APPROVAL",
            "FINISHED",
            "FINISHED",
            "FINISHED",
        ],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<html><h1>Build Smoke OK</h1></html>",
    )
    return _client(transport, root), transport, _smoke_scenario()


def _normalized_tree(root: Path) -> dict[str, bytes]:
    tree: dict[str, bytes] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["finished_at"] = "<variable>"
            tree[rel] = json.dumps(manifest, indent=2, sort_keys=True).encode()
        else:
            tree[rel] = path.read_bytes()
    return tree


def _minimal_run() -> CollectedRun:
    return CollectedRun(
        conversation_id="conv-retained",
        events=clean_smoke_log(),
        state_initial={},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
    )


def test_typed_ports_are_bounded_and_match_the_existing_client() -> None:
    assert ProductClient.__bases__ == (
        ConversationClient,
        ProgressClient,
        ObservabilityClient,
        EvidenceSourceClient,
        ScenarioDriver.__bases__[0],
    )
    assert not [
        name
        for name, value in ProductClient.__dict__.items()
        if not name.startswith("_") and callable(value)
    ]
    for port in (
        ConversationClient,
        ProgressClient,
        ObservabilityClient,
        EvidenceSourceClient,
    ):
        for name, value in port.__dict__.items():
            if not name.startswith("_") and callable(value):
                assert hasattr(DiscoApiClient, name), name
    assert (
        len(
            [
                name
                for name, value in EvidenceSink.__dict__.items()
                if not name.startswith("_") and callable(value)
            ]
        )
        == 5
    )


@pytest.mark.asyncio
async def test_frozen_transcript_authorities_still_execute_model_free(
    tmp_path: Path,
) -> None:
    """Run the six frozen behavior authorities from fresh deterministic state."""

    await legacy.test_bf2a_collection_starts_before_message_kick()

    followup = tmp_path / "followup"
    followup.mkdir()
    await legacy.test_after_terminal_followups_are_serialized(followup)

    paused = tmp_path / "paused"
    paused.mkdir()
    await legacy.test_paused_then_finished_resumes_to_terminal(paused)

    cleanup = tmp_path / "cleanup"
    cleanup.mkdir()
    await legacy.test_cleanly_terminal_run_is_released(cleanup)

    infra = tmp_path / "infra"
    infra.mkdir()
    await legacy.test_infra_gate_catches_httpx_connect_error(infra)

    freeze = tmp_path / "freeze"
    freeze.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        await legacy.test_progressing_hardcap_freeze_preserves_artifact_and_png_across_kill(
            freeze,
            patch,
        )


@pytest.mark.asyncio
async def test_old_and_new_drivers_have_the_same_fresh_smoke_transcript(
    tmp_path: Path,
) -> None:
    from harness.build_soak.scenario_driver import DefaultScenarioDriver

    old_client, old_transport, scenario = _fresh_smoke(tmp_path / "old")
    new_client, new_transport, fresh_scenario = _fresh_smoke(tmp_path / "new")

    old_result = await old_run.drive_scenario(
        old_client,
        scenario,
        model="fake-model",
        autonomous=False,
        timeout_s=5.0,
        hard_cap_s=10.0,
        seed=7,
    )
    new_result = await DefaultScenarioDriver().drive(
        new_client,
        fresh_scenario,
        model="fake-model",
        autonomous=False,
        timeout_s=5.0,
        hard_cap_s=10.0,
        seed=7,
    )

    assert old_transport.posts == new_transport.posts
    assert old_transport.ws_frames == new_transport.ws_frames
    assert old_transport.posts[:2] == [
        (
            "/conversations",
            {
                "surface": "build",
                "autonomous": False,
                "model_override": "fake-model",
            },
        ),
        (
            f"/conversations/{_CID}/messages",
            {"content": scenario["prompt"]},
        ),
    ]
    assert asdict(old_result) == asdict(new_result)


@pytest.mark.asyncio
async def test_old_facade_delegates_once_with_exact_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.build_soak.evidence_sink import FilesystemEvidenceSink
    from harness.build_soak.run_coordinator import RunCoordinator
    from harness.build_soak.scenario_driver import DefaultScenarioDriver

    client = object()
    scenario = {"id": "delegation", "prompt": "x"}
    collected = _minimal_run()
    driver_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    coordinator_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    sink_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def drive(
        _self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> CollectedRun:
        driver_calls.append((args, kwargs))
        return collected

    async def run_once(
        _self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        coordinator_calls.append((args, kwargs))
        return {"status": "PASS", "run_id": "run-1"}

    def assemble(
        _self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Path:
        sink_calls.append((args, kwargs))
        return tmp_path / "out" / "run-1"

    monkeypatch.setattr(DefaultScenarioDriver, "drive", drive)
    monkeypatch.setattr(RunCoordinator, "run_once", run_once)
    monkeypatch.setattr(FilesystemEvidenceSink, "assemble_dossier", assemble)

    driven = await old_run.drive_scenario(
        client,  # type: ignore[arg-type]
        scenario,
        model="m",
        autonomous=True,
        timeout_s=3.0,
        hard_cap_s=4.0,
        seed=5,
    )
    retained = old_run.assemble_dossier(
        tmp_path / "out",
        "run-1",
        scenario,
        collected,
        model="m",
        autonomous=True,
        commit="abc",
        started_at=_FIXED_TIME,
    )
    outcome = await old_run.run_once(
        client,  # type: ignore[arg-type]
        scenario,
        run_id="run-1",
        out_root=tmp_path / "out",
        model="m",
        autonomous=True,
        commit="abc",
        timeout_s=3.0,
        hard_cap_s=4.0,
        seed=5,
    )

    assert driven is collected
    assert retained == tmp_path / "out" / "run-1"
    assert outcome["status"] == "PASS"
    assert driver_calls == [
        (
            (client, scenario),
            {
                "model": "m",
                "autonomous": True,
                "timeout_s": 3.0,
                "hard_cap_s": 4.0,
                "seed": 5,
            },
        )
    ]
    assert len(sink_calls) == 1
    assert sink_calls[0][0][:4] == (
        tmp_path / "out",
        "run-1",
        scenario,
        collected,
    )
    assert len(coordinator_calls) == 1
    assert coordinator_calls[0][0] == (client, scenario)


@pytest.mark.asyncio
async def test_sink_preserves_dossier_bytes_and_all_outcome_paths(
    tmp_path: Path,
) -> None:
    from harness.build_soak.evidence_sink import FilesystemEvidenceSink

    sink = FilesystemEvidenceSink()
    run = _minimal_run()
    scenario = {"id": "retention", "prompt": "retain facts"}
    old_base = old_run.assemble_dossier(
        tmp_path / "old",
        "run-pass",
        scenario,
        run,
        model="m",
        autonomous=False,
        commit="abc",
        started_at=_FIXED_TIME,
    )
    new_base = sink.assemble_dossier(
        tmp_path / "new",
        "run-pass",
        scenario,
        run,
        model="m",
        autonomous=False,
        commit="abc",
        started_at=_FIXED_TIME,
    )
    assert old_base.relative_to(tmp_path / "old") == Path("run-pass")
    assert new_base.relative_to(tmp_path / "new") == Path("run-pass")
    assert _normalized_tree(old_base) == _normalized_tree(new_base)
    messages_path = new_base / "conversations/conv-retained/model-messages.jsonl"
    reconstructed = [json.loads(line) for line in messages_path.read_text().splitlines()]
    assert reconstructed[0]["status"] == "ok"
    assert reconstructed[0]["exact_provider_prompt"] is False
    assert any(
        row.get("message", {}).get("role") == "user"
        for row in reconstructed[1:]
    )
    manifest = json.loads((new_base / "manifest.json").read_text())
    assert "model-messages.jsonl" in manifest["evidence_hashes"]

    infra = sink.record_infra_failure(
        tmp_path / "out",
        "run-infra",
        scenario,
        InfraProbeError("probe-down", {"component": "agent_server"}),
    )
    invalid = sink.record_invalid_run(
        tmp_path / "out",
        "run-invalid",
        scenario,
        "connection lost",
    )
    failed = sink.record_finish_unsealable(
        tmp_path / "out",
        "run-fail",
        scenario,
        "seal refused",
    )
    assert [infra["status"], invalid["status"], failed["status"]] == [
        "INFRA_FAILURE",
        "INVALID_RUN",
        "FAIL",
    ]
    for run_id in ("run-infra", "run-invalid", "run-fail"):
        base = tmp_path / "out" / run_id
        assert (base / "classification.json").is_file()
        assert (base / "timeline.md").is_file()


def test_new_owner_signatures_keep_the_frozen_facade_contract() -> None:
    from harness.build_soak.evidence_sink import FilesystemEvidenceSink
    from harness.build_soak.run_coordinator import RunCoordinator
    from harness.build_soak.scenario_driver import DefaultScenarioDriver

    assert tuple(inspect.signature(DefaultScenarioDriver.drive).parameters) == (
        "self",
        *inspect.signature(old_run.drive_scenario).parameters.keys(),
    )
    assert tuple(inspect.signature(RunCoordinator.run_once).parameters) == (
        "self",
        *inspect.signature(old_run.run_once).parameters.keys(),
    )
    assert tuple(inspect.signature(FilesystemEvidenceSink.assemble_dossier).parameters) == (
        "self",
        *inspect.signature(old_run.assemble_dossier).parameters.keys(),
    )
