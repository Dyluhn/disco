"""Moved snapshot readiness readback aware identity collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    SnapshotNotReadyError,
    _disco_mod,
    pytest,
)
from .helpers_01 import (
    FakeTransport,
    _edit_then_read_log,
    _FakeClock,
    _install_clock,
    _plant_snapshot,
    _seed_db,
)


@pytest.mark.asyncio
async def _impl_test_snapshot_rendered_readback_waits_for_final_bytes(tmp_path, monkeypatch):
    # The live shape: index.html's LAST mutation is a partial file_edit; a later FULL readback
    # carries the true final bytes ('Grand Opening'). The snapshot still holds the stale pre-edit
    # copy → the gate must reject it and wait until the on-disk RENDERED form equals the readback.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    stale = "<h1>Contact sales</h1>\n<p>old</p>\n"
    final = "<h1>Grand Opening</h1>\n<p>Book a Visit</p>\n"
    ws = _plant_snapshot(proj, _CID, {"index.html": stale})  # stable but STALE intermediate
    _seed_db(db, _CID, _edit_then_read_log("index.html", final))

    def flush(step):
        if step >= 2:
            (ws / "index.html").write_text(final, encoding="utf-8")

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == final  # the readback-confirmed final bytes
    assert "Contact sales" not in manifest["index.html"]["content"]  # stale never accepted
    assert clock.polls >= 2  # it genuinely waited past the stable-but-stale intermediate


@pytest.mark.asyncio
async def _impl_test_snapshot_rendered_readback_fail_fast_when_never_final(tmp_path, monkeypatch):
    # A ("rendered", …) signal is DEFINITE: if the on-disk file never matches the readback, the
    # gate FAILs FAST (WORKSPACE_SNAPSHOT_NOT_READY), never accepting the stale-but-stable copy.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE forever</h1>\n"})
    _seed_db(db, _CID, _edit_then_read_log("index.html", "<h1>Grand Opening</h1>\n"))

    clock = _FakeClock()  # no flush — disk stays stale
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=1.5,
    )

    with pytest.raises(SnapshotNotReadyError) as ei:
        await client.collect_workspace(_CID, ["index.html"])

    assert [u["path"] for u in ei.value.facts["unsatisfied"]] == ["index.html"]
    assert ei.value.facts["unsatisfied"][0]["expected"][0] == "rendered"
    assert clock.polls <= 6  # bounded — never hangs


@pytest.mark.asyncio
async def _impl_test_snapshot_stale_readback_before_later_edit_is_ignored(tmp_path, monkeypatch):
    # STALE-READBACK ordering: a FULL readback, THEN a later partial edit with NO subsequent
    # readback → the old read is seq-ignored; the path is present_unproven (extended stability),
    # NOT promoted on the stale read. We prove it does NOT fail-fast on a rendered mismatch.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>read-at-seq4</h1>\n"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>different on disk</h1>\n"})
    # readback at seq5 (content X), THEN a later file_edit at seq6 (no later readback).
    log = _edit_then_read_log("index.html", content)  # edit@2, read@4/5
    log = [e for e in log if e["seq"] != 9]  # drop FINISHED, re-add after the late edit
    log += [
        {
            "id": "a6",
            "seq": 6,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_edit",
                "arguments": {"path": "index.html"},
                "call_id": "m2",
            },
        },
        {
            "id": "o7",
            "seq": 7,
            "kind": "observation",
            "source": "environment",
            "tool_result": {"call_id": "m2", "tool_name": "file_edit", "success": True},
        },
        {"id": "s9", "seq": 9, "kind": "status", "source": "system", "status": "FINISHED"},
    ]
    _seed_db(db, _CID, log)

    clock = _FakeClock()  # disk never changes; would FAIL-FAST if the stale read were promoted
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=3.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])  # must NOT raise

    # present_unproven → accepted on extended stability (no rendered fail-fast against stale read)
    assert manifest["index.html"]["present"] is True
    assert clock.polls >= _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS - 1  # extended settle, bounded


@pytest.mark.asyncio
async def _impl_test_snapshot_paged_readback_not_promoted(tmp_path, monkeypatch):
    # A PAGED read (offset/limit, or a budget/pressure-truncated header) must NOT promote to a
    # rendered signal → present_unproven (extended stability), never a false NOT_READY.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    final = "<h1>x</h1>\n<p>y</p>\n"
    _plant_snapshot(proj, _CID, {"index.html": final})
    log = _edit_then_read_log("index.html", final)
    # Make the read PAGED: args carry offset, and the header says "read more".
    for e in log:
        if e.get("kind") == "action" and e["tool_call"]["tool_name"] == "file_read":
            e["tool_call"]["arguments"] = {"path": "index.html", "offset": 2}
        if e.get("kind") == "observation" and e["tool_result"]["tool_name"] == "file_read":
            e["tool_result"]["content"] = "[lines 2-2 of 2; read more with offset=3]\n2\t<p>y</p>"
    _seed_db(db, _CID, log)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=3.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])  # must NOT raise NOT_READY

    assert manifest["index.html"]["present"] is True  # present_unproven path, extended stability


@pytest.mark.asyncio
async def _impl_test_snapshot_synthetic_f9_readback_not_promoted(tmp_path, monkeypatch):
    # An F9 read-dedup pointer ([F9 dedup: …]) carries NO real bytes and must NOT promote to a
    # rendered signal → present_unproven (extended stability), never used as the expected identity.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    final = "<h1>real</h1>\n"
    _plant_snapshot(proj, _CID, {"index.html": final})
    log = _edit_then_read_log(
        "index.html",
        final,
        read_content="[F9 dedup: file_read(index.html) identical to a recent read this turn "
        "— see the earlier result; file_read again only if you suspect it changed]",
    )
    _seed_db(db, _CID, log)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=3.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])  # must NOT raise

    assert manifest["index.html"]["present"] is True  # present_unproven, not promoted on synthetic


@pytest.mark.asyncio
async def _impl_test_snapshot_present_unproven_extended_stability_not_bare_present(
    tmp_path, monkeypatch
):
    # No readback at all: a partial edit with no proof of final bytes → present_unproven. The gate
    # must NOT accept on bare presence — it requires EXTENDED consecutive-stable reads (more than
    # the change keeps happening), and only then accepts.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"index.html": "<h1>v0</h1>"})
    _seed_db(db, _CID, _edit_then_read_log("index.html", "irrelevant", include_read=False))

    # Keep mutating the file until the gate has polled several times — proving it does NOT accept
    # the early (changing) bytes; it only settles once the content stops changing.
    def churn(step):
        if step < _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS:
            (ws / "index.html").write_text(f"<h1>v{step}</h1>", encoding="utf-8")

    clock = _FakeClock(on_poll=churn)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["present"] is True
    assert manifest["index.html"]["proof"] == "unproven_extended_stability"
    assert manifest["index.html"]["content_stable"] is True
    # accepted only AFTER it stopped changing → needed the extended settle (not bare poll-1 present)
    assert clock.polls >= _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS - 1


@pytest.mark.asyncio
async def _impl_test_snapshot_churning_unproven_stamps_content_stable_false(tmp_path, monkeypatch):
    # At the deadline, an unproven declared file can be accepted best-effort before it reaches
    # the readiness stability threshold. The manifest must say those bytes are still unstable so
    # the content oracle reports WORKSPACE_SNAPSHOT_UNVERIFIED on a mismatch.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"index.html": "<h1>v0</h1>"})
    _seed_db(db, _CID, _edit_then_read_log("index.html", "irrelevant", include_read=False))

    def churn(step):
        (ws / "index.html").write_text(f"<h1>v{step}</h1>", encoding="utf-8")

    clock = _FakeClock(on_poll=churn)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=1.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["present"] is True
    assert manifest["index.html"]["proof"] == "unproven_extended_stability"
    assert manifest["index.html"]["content_stable"] is False
