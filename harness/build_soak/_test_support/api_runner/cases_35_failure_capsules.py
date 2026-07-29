"""Moved failure capsules collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    _run_mod,
    clean_smoke_log,
    hashlib,
    json,
    pytest,
    run_once,
)
from .helpers_01 import (
    _SandboxIdProgressTransport,
    _seed_db,
    _smoke_scenario,
)
from .helpers_02 import _dossier_with_landed_freeze


@pytest.mark.asyncio
async def _impl_test_capsule_binds_the_exact_boundary_and_discloses_non_promotion(
    tmp_path, monkeypatch
):
    from harness.build_soak import capsule as cap

    dossier, _projects, version, content, _ws = await _dossier_with_landed_freeze(
        tmp_path, monkeypatch
    )
    capsule = cap.build_capsule(dossier, _CID)

    # Disclosure is not decoration: a reader must never infer non-promotion.
    assert capsule["diagnostic_replay"] is True
    assert capsule["counts_toward_promotion"] is False
    assert capsule["exact_model_hidden_state_reproduced"] is False

    # Bound to the ONE accepted boundary, not to "latest".
    assert capsule["boundary"]["kind"] == "accepted_workspace_version_event"
    assert capsule["boundary"]["workspace_version_seq"] == version.seq
    assert capsule["boundary"]["workspace_tree_digest"] == version.tree_digest
    assert isinstance(capsule["boundary"]["accepted_event_horizon_seq"], int)

    # Identity, verdict and byte manifest all travel with it.
    assert capsule["verdict"]["code"] == "RUN_TIMEOUT_WHILE_PROGRESSING"
    assert capsule["scenario"]["sha256"]
    assert (
        capsule["workspace_byte_manifest"]["index.html"]
        == hashlib.sha256(content.encode()).hexdigest()
    )

    cap.verify_capsule(capsule, dossier)


@pytest.mark.asyncio
async def _impl_test_capsule_refuses_a_run_with_no_safe_boundary(tmp_path, monkeypatch):
    """A FREEZE_TIMEOUT is not a boundary and must never be dressed up as one."""
    from harness.build_soak import capsule as cap

    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    client = DiscoApiClient(
        _SandboxIdProgressTransport(db, finish_after=None), db_path=str(db), poll_interval_s=0.0
    )
    await run_once(
        client,
        _smoke_scenario(),
        run_id="run_capsule_nb",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.2,
        parallel_workers=10,
    )

    with pytest.raises(cap.CapsuleError, match="no safe boundary"):
        cap.build_capsule(tmp_path / "out" / "run_capsule_nb", _CID)


@pytest.mark.asyncio
async def _impl_test_a_tampered_capsule_field_is_refused_before_any_replay(tmp_path, monkeypatch):
    from harness.build_soak import capsule as cap

    dossier, _projects, _version, _content, _ws = await _dossier_with_landed_freeze(
        tmp_path, monkeypatch, run_id="run_capsule_tamper"
    )
    capsule = cap.build_capsule(dossier, _CID)

    for mutate in (
        lambda c: c["boundary"].__setitem__("workspace_version_seq", 999),
        lambda c: c["scenario"].__setitem__("sha256", "sha256:deadbeef"),
        lambda c: c["binding"].__setitem__("model", "some-other-model"),
        lambda c: c["workspace_byte_manifest"].__setitem__("index.html", "0" * 64),
        lambda c: c["boundary"].__setitem__("accepted_event_horizon_seq", 10**9),
        lambda c: c.__setitem__("counts_toward_promotion", True),
    ):
        altered = json.loads(json.dumps(capsule))
        mutate(altered)
        with pytest.raises(cap.CapsuleError):
            cap.verify_capsule(altered, dossier)


@pytest.mark.asyncio
async def _impl_test_capsule_restores_the_exact_immutable_bytes(tmp_path, monkeypatch):
    from harness.build_soak import capsule as cap

    dossier, projects, _version, content, _ws = await _dossier_with_landed_freeze(
        tmp_path, monkeypatch, run_id="run_capsule_restore"
    )
    capsule = cap.build_capsule(dossier, _CID)

    dest = tmp_path / "replay-workspace"
    cap.restore_workspace(capsule, projects, dest)

    assert (dest / "index.html").read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def _impl_test_restore_fails_closed_when_the_immutable_version_was_tampered(
    tmp_path, monkeypatch
):
    """Restoration re-verifies; it never trusts the event's word for the bytes."""
    from disco.tools.projects.store import ProjectStore

    from harness.build_soak import capsule as cap

    dossier, projects, version, _content, _ws = await _dossier_with_landed_freeze(
        tmp_path, monkeypatch, run_id="run_capsule_tampered_version"
    )
    capsule = cap.build_capsule(dossier, _CID)

    immutable = ProjectStore(str(projects)).version_workspace_path(_CID, version.seq)
    (immutable / "index.html").write_text("tampered after sealing", encoding="utf-8")

    with pytest.raises(cap.CapsuleError, match="freshly verified|byte manifest"):
        cap.restore_workspace(capsule, projects, tmp_path / "replay-tampered")


@pytest.mark.asyncio
async def _impl_test_a_capsule_is_not_promotion_visible(tmp_path, monkeypatch):
    """A capsule and its replay output can never be counted."""
    from harness.build_soak import capsule as cap
    from harness.build_soak import profile as prof

    dossier, _projects, _version, _content, _ws = await _dossier_with_landed_freeze(
        tmp_path, monkeypatch, run_id="run_capsule_nonpromo"
    )
    capsule = cap.build_capsule(dossier, _CID)
    out = tmp_path / "capsules"
    cap.write_capsule(capsule, out)

    prof.assert_not_promotion_visible(out)
    from harness.reliability.run import _build_soak_result

    status, units, _detail = _build_soak_result(out, exit_code=0, units=1)
    assert status == "INFRA"
    assert units == 0
