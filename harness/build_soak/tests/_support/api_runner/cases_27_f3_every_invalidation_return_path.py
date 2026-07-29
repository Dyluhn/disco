"""Moved f3 every invalidation return path collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    _run_mod,
    clean_smoke_log,
    json,
    load_manifest,
    pytest,
    run_once,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _seed_db,
    _smoke_scenario,
)
from .helpers_02 import _snapshot_not_ready_exc


@pytest.mark.asyncio
async def _impl_test_snapshot_not_ready_freezes_available_evidence(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(db, states=["FINISHED", "FINISHED"])
    client = _client(transport, tmp_path)

    async def _raise_snapshot(cid, declared):
        raise _snapshot_not_ready_exc(cid)

    monkeypatch.setattr(client, "collect_workspace", _raise_snapshot)
    out_root = tmp_path / "out"
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_f3_snapshot_001",
        out_root=out_root,
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )

    # The invalidation verdict is byte-for-byte the pre-F3 product semantics …
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "WORKSPACE_SNAPSHOT_NOT_READY"
    assert record["first_broken_link"] == "snapshot_flush -> snapshot_behind_agent_final_state"
    assert record["required_evidence_present"] is False
    # … but the exact conversation ID is preserved instead of null.
    assert record["conversation_id"] == _CID

    freeze = record["facts"]["evidence_freeze"]
    assert freeze["attempted"] is True
    assert freeze["dossier_written"] is True
    assert {"events", "state_final", "inspect_trace"} <= set(freeze["present"])
    # No relay log exists in this test: the provider slice is DISCLOSED missing.
    assert "provider_ledger" in freeze["missing"]

    base = out_root / "run_f3_snapshot_001"
    conv = base / "conversations" / _CID
    events_lines = (conv / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(events_lines) == len(clean_smoke_log())
    assert json.loads((conv / "state.final.json").read_text(encoding="utf-8"))
    assert json.loads((conv / "inspect-trace.json").read_text(encoding="utf-8"))
    assert json.loads((conv / "thrash-monitor.json").read_text(encoding="utf-8")) is not None

    # The workspace slice must NEVER look like terminal workspace truth.
    workspace_manifest = json.loads((conv / "workspace-manifest.json").read_text(encoding="utf-8"))
    assert workspace_manifest["files"] == {}
    assert workspace_manifest["_capture"]["status"] == "not_collected_invalidation"
    assert workspace_manifest["_capture"]["invalidation_code"] == "WORKSPACE_SNAPSHOT_NOT_READY"

    # The frozen dossier is the ordinary hash-locked one: manifest verifies.
    manifest = load_manifest(base)
    integrity = verify_evidence_unchanged(base, manifest)
    assert integrity.intact is True
    assert integrity.mismatches == {}
    # state.initial is an explicit capture marker, never fabricated state.
    state_initial = json.loads((conv / "state.initial.json").read_text(encoding="utf-8"))
    assert state_initial["_capture"]["status"] == "not_collected_invalidation"


@pytest.mark.asyncio
async def _impl_test_snapshot_not_ready_freeze_discloses_missing_events(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(db, states=["FINISHED", "FINISHED"])
    client = _client(transport, tmp_path)

    armed = {"on": False}
    real_collect_events = client.collect_events

    def _collect_events(cid):
        if armed["on"]:
            raise RuntimeError("event store unreachable during freeze")
        return real_collect_events(cid)

    async def _raise_snapshot(cid, declared):
        armed["on"] = True
        raise _snapshot_not_ready_exc(cid)

    monkeypatch.setattr(client, "collect_events", _collect_events)
    monkeypatch.setattr(client, "collect_workspace", _raise_snapshot)
    out_root = tmp_path / "out"
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_f3_snapshot_002",
        out_root=out_root,
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "WORKSPACE_SNAPSHOT_NOT_READY"
    assert record["conversation_id"] == _CID
    freeze = record["facts"]["evidence_freeze"]
    assert "events" in freeze["missing"]
    assert freeze["errors"]["events"] == "RuntimeError"
    # The independently collectable slices are still frozen, not discarded.
    assert "state_final" in freeze["present"]
    conv = out_root / "run_f3_snapshot_002" / "conversations" / _CID
    assert (conv / "state.final.json").exists()
    assert (conv / "events.jsonl").read_text(encoding="utf-8").strip() == ""


@pytest.mark.asyncio
async def _impl_test_snapshot_not_ready_freeze_failure_never_masks_invalidation(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(db, states=["FINISHED", "FINISHED"])
    client = _client(transport, tmp_path)

    async def _raise_snapshot(cid, declared):
        raise _snapshot_not_ready_exc(cid)

    def _dossier_boom(*_args, **_kwargs):
        raise OSError("disk full while freezing")

    monkeypatch.setattr(client, "collect_workspace", _raise_snapshot)
    monkeypatch.setattr(_run_mod, "assemble_dossier", _dossier_boom)
    out_root = tmp_path / "out"
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_f3_snapshot_003",
        out_root=out_root,
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )

    # The original invalidation survives a failed freeze, with the failure disclosed.
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "WORKSPACE_SNAPSHOT_NOT_READY"
    assert record["conversation_id"] == _CID
    freeze = record["facts"]["evidence_freeze"]
    assert freeze["dossier_written"] is False
    assert freeze["errors"]["dossier"] == "OSError"
    assert (out_root / "run_f3_snapshot_003" / "classification.json").exists()


@pytest.mark.asyncio
async def _impl_test_generic_post_create_error_freezes_evidence(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(db, states=["FINISHED", "FINISHED"])
    client = _client(transport, tmp_path)

    async def _boom(cid, declared):
        raise RuntimeError("collection transport dropped")

    monkeypatch.setattr(client, "collect_workspace", _boom)
    out_root = tmp_path / "out"
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_f3_generic_001",
        out_root=out_root,
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_INTERRUPTED"
    # The generic exception carries no facts: the client's own last-created
    # conversation is the freeze target.
    assert record["conversation_id"] == _CID
    freeze = record["facts"]["evidence_freeze"]
    assert freeze["attempted"] is True
    assert "events" in freeze["present"]
    conv = out_root / "run_f3_generic_001" / "conversations" / _CID
    assert (conv / "events.jsonl").exists()
    assert (
        json.loads((conv / "workspace-manifest.json").read_text(encoding="utf-8"))["_capture"][
            "status"
        ]
        == "not_collected_invalidation"
    )
