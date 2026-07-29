"""Moved bug 9 authoritative workspace snapshot 1 collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    SnapshotNotReadyError,
    action,
    hashlib,
    msg,
    observation,
    pytest,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _insert_event,
    _plant_snapshot,
    _sealed_workspace_commit,
    _seed_db,
    _smoke_log_with_file_write,
    _strict_workspace_case,
    _workspace_commit,
)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "scope",
        "terminal_seq",
        "latest_effect_seq",
        "version_seq",
        "tree_digest",
        "file_count",
        "total_bytes",
    ],
)
@pytest.mark.asyncio
async def _impl_test_strict_workspace_rejects_each_missing_final_seal_field(tmp_path, field):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    marker = _sealed_workspace_commit(11, version)
    del marker["final_seal"][field]
    _insert_event(db, _CID, marker)

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["event_evidence_valid"] is False
    assert raised.value.facts["final_seal_error"] == "final seal fields do not match schema v1"


@pytest.mark.parametrize("field", ["namespace", "identifier"])
@pytest.mark.asyncio
async def _impl_test_strict_workspace_rejects_each_missing_scope_field(tmp_path, field):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    marker = _sealed_workspace_commit(11, version)
    del marker["final_seal"]["scope"][field]
    _insert_event(db, _CID, marker)

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["event_evidence_valid"] is False
    assert raised.value.facts["final_seal_error"] == "final seal scope is malformed"


@pytest.mark.parametrize(
    "corruption",
    [
        "schema_version",
        "scope_namespace",
        "scope_identifier",
        "terminal_seq",
        "latest_effect_seq",
        "version_seq",
        "tree_digest",
        "file_count",
        "total_bytes",
    ],
)
@pytest.mark.asyncio
async def _impl_test_strict_workspace_rejects_each_corrupt_final_seal_field(tmp_path, corruption):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    marker = _sealed_workspace_commit(11, version)
    seal = marker["final_seal"]
    if corruption == "schema_version":
        seal["schema_version"] = True  # bool must not masquerade as integer schema v1
    elif corruption == "scope_namespace":
        seal["scope"]["namespace"] = "workspace.live"
    elif corruption == "scope_identifier":
        seal["scope"]["identifier"] = "another-conversation"
    elif corruption == "terminal_seq":
        seal["terminal_seq"] = 9
    elif corruption == "latest_effect_seq":
        seal["latest_effect_seq"] = 7
    elif corruption == "version_seq":
        seal["version_seq"] = version.seq + 1
    elif corruption == "tree_digest":
        seal["tree_digest"] = "b" * 64
    elif corruption == "file_count":
        seal["file_count"] = version.file_count + 1
    elif corruption == "total_bytes":
        seal["total_bytes"] = version.total_bytes + 1
    _insert_event(db, _CID, marker)

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    if corruption in {"file_count", "total_bytes"}:
        assert raised.value.facts["event_evidence_valid"] is True
        assert raised.value.facts["final_seal_error"] == (
            "final seal tree facts do not match the immutable version"
        )
    else:
        assert raised.value.facts["event_evidence_valid"] is False


@pytest.mark.asyncio
async def _impl_test_strict_workspace_rejects_boolean_latest_effect_sequence(tmp_path):
    """A bool must never masquerade as schema-v1 integer sequence 1."""
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    events = [
        action(1, "custom_mutator", action_id="effect-1"),
        status(2, "FINISHED"),
    ]
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, {"index.html": "sealed\n"})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    marker = _sealed_workspace_commit(
        3,
        version,
        terminal_seq=2,
        latest_effect_seq=True,
    )
    _insert_event(db, _CID, marker)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["event_evidence_valid"] is False
    assert raised.value.facts["final_seal_error"] == (
        "final seal latest effect sequence does not match the event log"
    )


@pytest.mark.parametrize(
    "effect_kind",
    ["action", "observation", "agent_error", "workspace_mutation"],
)
@pytest.mark.asyncio
async def _impl_test_strict_workspace_fence_includes_every_effect_kind(tmp_path, effect_kind):
    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>all effects fenced</h1>"
    events = _smoke_log_with_file_write("index.html", content)
    events[8] = {
        "id": f"{effect_kind}_9",
        "seq": 9,
        "kind": effect_kind,
        "source": "system" if effect_kind == "workspace_mutation" else "agent",
    }
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, {"index.html": content})
    from disco.tools.projects.store import ProjectStore

    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    _insert_event(db, _CID, _sealed_workspace_commit(11, version, latest_effect_seq=9))
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == content


@pytest.mark.asyncio
async def _impl_test_post_terminal_workspace_mutation_invalidates_final_seal(tmp_path):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    _insert_event(
        db,
        _CID,
        {
            "id": "workspace_mutation_11",
            "seq": 11,
            "kind": "workspace_mutation",
            "source": "system",
            "operation": "editor.write",
            "paths": ["index.html"],
        },
    )
    _insert_event(db, _CID, _sealed_workspace_commit(12, version))

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["latest_effect_seq"] == 11
    assert raised.value.facts["final_seal_error"] == (
        "effect event exists at or after terminal FINISHED"
    )


@pytest.mark.parametrize(
    ("source", "state"),
    [("agent", "FINISHED"), ("system", "VERIFIED"), ("system", "RUNNING")],
)
@pytest.mark.asyncio
async def _impl_test_strict_workspace_requires_latest_exact_system_finished(
    tmp_path, source, state
):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    _insert_event(db, _CID, _sealed_workspace_commit(11, version))
    later = status(12, state)
    later["source"] = source
    _insert_event(db, _CID, later)

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["terminal_seq"] is None
    assert raised.value.facts["final_seal_error"] == ("latest status is not exact SYSTEM FINISHED")


@pytest.mark.asyncio
async def _impl_test_strict_workspace_requires_finish_trigger(tmp_path):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    marker = _sealed_workspace_commit(11, version)
    marker["trigger"] = "turn"
    _insert_event(db, _CID, marker)

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["final_seal_error"] == ("workspace version is not finish-triggered")


@pytest.mark.parametrize("field", ["version_seq", "tree_digest", "trigger"])
@pytest.mark.asyncio
async def _impl_test_strict_workspace_rejects_each_missing_version_event_field(tmp_path, field):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    marker = _sealed_workspace_commit(11, version)
    del marker[field]
    _insert_event(db, _CID, marker)

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["event_evidence_valid"] is False


@pytest.mark.asyncio
async def _impl_test_strict_workspace_accepts_exact_no_effect_fence(tmp_path):
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>no effects</h1>"
    _seed_db(db, _CID, [msg(1, "user", "answer"), status(2, "FINISHED")])
    _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(
            3,
            version,
            terminal_seq=2,
            latest_effect_seq=None,
        ),
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    manifest = await client.collect_workspace(_CID, [])

    assert manifest["index.html"]["content"] == content


@pytest.mark.asyncio
async def _impl_test_strict_workspace_final_seal_proves_partial_final_bytes(tmp_path):
    """K6: a valid immutable final-tree seal is authoritative byte proof.

    A targeted edit does not carry its whole post-edit file in the action payload, so
    the legacy action heuristic can only call it ``present_unproven``.  Strict live
    collection has stronger host evidence: the exact immutable version was verified
    before and after its manifest was read.  The declared file must therefore carry
    ``final_tree_seal`` proof without forcing the model to rewrite or reread it.
    """
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>final targeted edit</h1>\n"
    events = [
        msg(1, "user", "revise it"),
        action(2, "file_replace_lines", args={"path": "index.html"}, action_id="edit-2"),
        {
            "id": "observation-3",
            "seq": 3,
            "kind": "observation",
            "source": "environment",
            "action_id": "edit-2",
            "tool_result": {
                "call_id": "call_2",
                "tool_name": "file_replace_lines",
                "success": True,
                "content": "edited",
            },
        },
        status(4, "FINISHED"),
    ]
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(
            5,
            version,
            terminal_seq=4,
            latest_effect_seq=3,
        ),
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == content
    assert manifest["index.html"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert manifest["index.html"]["proof"] == "final_tree_seal"


@pytest.mark.asyncio
async def _impl_test_strict_workspace_final_seal_proves_shape_agnostic_generated_files(tmp_path):
    """K6: sealed proof cannot depend on a hard-coded tool or application shape.

    AppKit is the measured example: its semantic tools already emit exact receipts,
    but the old adapter only understood generic file tool names.  The final immutable
    seal must prove every present declared file even when action-name inference has no
    entry for it.
    """
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    files = {
        ".disco/appspec.json": '{"name":"sealed"}\n',
        ".disco/designspec.json": '{"theme":"warm"}\n',
    }
    events = [
        msg(1, "user", "create the application"),
        action(2, "app_create", args={"name": "sealed"}, action_id="app-create-2"),
        {
            "id": "observation-3",
            "seq": 3,
            "kind": "observation",
            "source": "environment",
            "action_id": "app-create-2",
            "tool_result": {
                "call_id": "call_2",
                "tool_name": "app_create",
                "success": True,
                "content": "created",
            },
        },
        status(4, "FINISHED"),
    ]
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, files)
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(
            5,
            version,
            terminal_seq=4,
            latest_effect_seq=3,
        ),
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    manifest = await client.collect_workspace(_CID, list(files))

    assert {manifest[path]["proof"] for path in files} == {"final_tree_seal"}
    assert {path: manifest[path]["sha256"] for path in files} == {
        path: hashlib.sha256(content.encode()).hexdigest() for path, content in files.items()
    }


@pytest.mark.asyncio
async def _impl_test_strict_workspace_rejects_mutated_immutable_version_bytes(tmp_path):
    from disco.tools.projects.store import ProjectStore

    db, projects, version, client = _strict_workspace_case(tmp_path)
    _insert_event(db, _CID, _sealed_workspace_commit(11, version))
    immutable = ProjectStore(str(projects)).version_workspace_path(_CID, version.seq)
    (immutable / "index.html").write_text("tampered after version publication", encoding="utf-8")

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["snapshot_dir"] is None
    assert raised.value.facts["workspace_version_seq"] == 11
    assert raised.value.facts["final_seal_error"] == (
        "immutable workspace version could not be freshly verified"
    )


@pytest.mark.asyncio
async def _impl_test_later_legacy_marker_invalidates_an_older_valid_seal(tmp_path):
    db, _projects, version, client = _strict_workspace_case(tmp_path)
    _insert_event(db, _CID, _sealed_workspace_commit(11, version))
    _insert_event(db, _CID, _workspace_commit(12, version.tree_digest, version_seq=version.seq))

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["workspace_version_seq"] == 12
    assert raised.value.facts["final_seal_error"] == "latest workspace version has no final seal"


@pytest.mark.asyncio
async def _impl_test_terminal_snapshot_requires_post_terminal_workspace_commit(tmp_path):
    from disco.tools.projects.store import ProjectStore, tree_digest

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>committed</h1>"
    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content))
    workspace = _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])

    assert raised.value.facts["terminal_seq"] == 10
    assert raised.value.facts["workspace_version_seq"] is None
    assert raised.value.facts["observed_tree_digest"] is None

    # A legacy marker is never sufficient in strict mode, even when its digest is exact.
    _insert_event(db, _CID, _workspace_commit(11, version.tree_digest, version_seq=version.seq))
    with pytest.raises(SnapshotNotReadyError) as legacy:
        await client.collect_workspace(_CID, ["index.html"])
    assert legacy.value.facts["final_seal_error"] == "latest workspace version has no final seal"
    assert legacy.value.facts["observed_tree_digest"] is None

    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(12, version, digest="0" * 64),
    )
    with pytest.raises(SnapshotNotReadyError) as mismatched:
        await client.collect_workspace(_CID, ["index.html"])
    assert mismatched.value.facts["workspace_version_seq"] == 12
    assert mismatched.value.facts["workspace_version_digest"] == "0" * 64
    assert mismatched.value.facts["observed_tree_digest"] == tree_digest(workspace)

    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(13, version),
    )
    (workspace / "index.html").write_text("<h1>later uncommitted bytes</h1>")
    manifest = await client.collect_workspace(_CID, ["index.html"])
    assert manifest["index.html"]["content"] == content


@pytest.mark.asyncio
async def _impl_test_stale_preterminal_workspace_commit_cannot_bless_final_tree(tmp_path):
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>final</h1>"
    _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    events = _smoke_log_with_file_write("index.html", content)
    events[-1] = _sealed_workspace_commit(
        10,
        version,
        terminal_seq=6,
        latest_effect_seq=None,
    )
    events.append(status(11, "FINISHED"))
    _seed_db(db, _CID, events)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["terminal_seq"] == 11
    assert raised.value.facts["workspace_version_seq"] == 10
    assert raised.value.facts["final_seal_error"] == (
        "workspace version does not follow terminal FINISHED"
    )

    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(12, version, terminal_seq=11),
    )
    manifest = await client.collect_workspace(_CID, ["index.html"])
    assert manifest["index.html"]["content"] == content


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "agent"),
        ("seq", "11"),
        ("version_seq", "1"),
        ("tree_digest", "not-a-digest"),
        ("trigger", "turn"),
    ],
)
@pytest.mark.asyncio
async def _impl_test_strict_commit_rejects_forged_or_malformed_system_marker(
    tmp_path, field, value
):
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>committed</h1>"
    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content))
    _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    marker = _sealed_workspace_commit(11, version)
    marker[field] = value
    _insert_event(db, _CID, marker)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["event_evidence_valid"] is False


@pytest.mark.asyncio
async def _impl_test_commit_before_late_action_outcome_cannot_bless_workspace(tmp_path):
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>committed</h1>"
    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content))
    _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(11, version),
    )
    _insert_event(db, _CID, observation(12, "evt_7", tool="file_write"))
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["terminal_seq"] == 10
    assert raised.value.facts["workspace_version_seq"] is None


@pytest.mark.asyncio
async def _impl_test_committed_version_root_symlink_cannot_escape_projects_store(tmp_path):
    import shutil

    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>same bytes outside</h1>"
    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content))
    _plant_snapshot(projects, _CID, {"index.html": content})
    store = ProjectStore(str(projects))
    version = store.cut_version(_CID, trigger="finish")
    assert version is not None
    version_workspace = store.version_workspace_path(_CID, version.seq)
    outside = tmp_path / "outside-version"
    outside.mkdir()
    (outside / "index.html").write_text(content)
    shutil.rmtree(version_workspace)
    version_workspace.symlink_to(outside, target_is_directory=True)
    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(11, version),
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["workspace_version_seq"] == 11
    assert raised.value.facts["snapshot_dir"] is None


@pytest.mark.asyncio
async def _impl_test_old_terminal_commit_cannot_bless_later_running_mutation(tmp_path):
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>old committed bytes</h1>"
    events = _smoke_log_with_file_write("index.html", content)
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="finish")
    assert version is not None
    _insert_event(
        db,
        _CID,
        _sealed_workspace_commit(11, version),
    )
    _insert_event(db, _CID, status(12, "RUNNING"))
    _insert_event(
        db,
        _CID,
        action(13, "file_write", args={"path": "index.html", "content": "new bytes"}),
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["terminal_seq"] is None
    assert raised.value.facts["workspace_version_seq"] is None


@pytest.mark.asyncio
async def _impl_test_strict_workspace_commit_fails_closed_without_terminal_event(tmp_path):
    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    events = _smoke_log_with_file_write("index.html", "<h1>bytes</h1>")[:-1]
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, {"index.html": "<h1>bytes</h1>"})
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["terminal_seq"] is None
    assert raised.value.facts["workspace_version_seq"] is None


@pytest.mark.asyncio
async def _impl_test_strict_workspace_commit_fails_closed_on_corrupt_event_evidence(
    tmp_path, monkeypatch
):
    projects = tmp_path / "projects"
    _plant_snapshot(projects, _CID, {"index.html": "<h1>bytes</h1>"})
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )
    monkeypatch.setattr(client, "collect_events", lambda _conversation_id: [{"payload": "{"}])

    with pytest.raises(SnapshotNotReadyError) as raised:
        await client.collect_workspace(_CID, ["index.html"])
    assert raised.value.facts["terminal_seq"] is None
    assert raised.value.facts["workspace_version_seq"] is None
