"""Moved snapshot readiness gate settle on collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    FinishUnsealableContentError,
    SnapshotNotReadyError,
    _disco_mod,
    hashlib,
    pytest,
)
from .helpers_01 import (
    FakeTransport,
    _FakeClock,
    _file_write_log,
    _install_clock,
    _plant_snapshot,
    _seal_refusal_disclosure,
    _seed_db,
)


@pytest.mark.asyncio
async def _impl_test_snapshot_waits_for_byte_change_rev1_to_rev2(tmp_path, monkeypatch):
    # (a) The agent's LAST write is rev-2; the snapshot still holds rev-1 bytes on the first
    # reads. The gate must NOT accept the stale rev-1 content — it waits until the on-disk
    # sha256 matches what the agent wrote (rev-2), then accepts rev-2.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    rev1 = "<h1>Contact sales</h1>"
    rev2 = "<h1>Book a Visit</h1>"
    ws = _plant_snapshot(proj, _CID, {"index.html": rev1})  # snapshot starts STALE (rev-1)
    _seed_db(db, _CID, _file_write_log("index.html", rev2))  # agent's final write = rev-2

    def flush(step):
        if step >= 2:  # rev-2 bytes land mid-flight
            (ws / "index.html").write_text(rev2, encoding="utf-8")

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    transport = FakeTransport(db, states=["FINISHED"], workspace={})
    client = DiscoApiClient(
        transport,
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == rev2  # rev-2 accepted, never the stale rev-1
    assert "Contact sales" not in manifest["index.html"]["content"]
    assert clock.polls >= 2  # it genuinely waited for the flush


@pytest.mark.asyncio
async def _impl_test_snapshot_waits_for_added_file_between_polls(tmp_path, monkeypatch):
    # (b) rev-2 CREATES a declared file absent from the early snapshot. Gate waits until it is
    # present AND matches the agent's written bytes.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "console.log('v2')\n"
    ws = _plant_snapshot(proj, _CID, {"index.html": "<h1>x</h1>"})  # app.js not there yet
    _seed_db(db, _CID, _file_write_log("app.js", content))

    def flush(step):
        if step >= 2:
            (ws / "app.js").write_text(content, encoding="utf-8")

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["app.js"])

    assert manifest["app.js"]["present"] is True
    assert manifest["app.js"]["content"] == content
    assert clock.polls >= 2


@pytest.mark.asyncio
async def _impl_test_snapshot_waits_for_removed_file_not_stale_present(tmp_path, monkeypatch):
    # (c) The agent file_write'd then `rm`'d a declared file (final state = ABSENT). The early
    # snapshot still HAS it; the gate must NOT accept the stale-present copy — it waits until
    # the file is gone, then OMITS it.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"old.html": "<h1>doomed</h1>"})  # still present early
    log = _file_write_log("old.html", "<h1>doomed</h1>", call="w1")
    # Append a SUCCESSFUL shell `rm old.html` AFTER the write → agent-final state is absent.
    log += [
        {
            "id": "a9",
            "seq": 9,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "shell",
                "arguments": {"command": "rm old.html"},
                "call_id": "r1",
            },
        },
        {
            "id": "o10",
            "seq": 10,
            "kind": "observation",
            "source": "environment",
            "tool_result": {"call_id": "r1", "tool_name": "shell", "success": True, "content": ""},
        },
    ]
    _seed_db(db, _CID, log)

    def flush(step):
        if step >= 2:
            (ws / "old.html").unlink()

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["old.html"])

    assert "old.html" not in manifest  # stale-present NOT accepted → absent → OMITTED
    assert clock.polls >= 2


@pytest.mark.asyncio
async def _impl_test_snapshot_non_declared_churn_does_not_block_declared_set(tmp_path, monkeypatch):
    # (d) A NON-declared snapshot file keeps changing; it must not block readiness. The declared
    # file already matches the agent's write, so the gate accepts PROMPTLY (gates only the
    # declared set, manifest-complete).
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>ready</h1>"
    ws = _plant_snapshot(proj, _CID, {"index.html": content, "scratch.log": "0"})
    _seed_db(db, _CID, _file_write_log("index.html", content))

    def churn(step):  # would never stabilize — but it is NOT declared, so it must not matter
        (ws / "scratch.log").write_text(str(step), encoding="utf-8")

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

    assert manifest["index.html"]["content"] == content
    assert manifest["index.html"]["proof"] == "raw_sha"
    assert manifest["index.html"]["content_stable"] is False
    assert manifest["scratch.log"]["present"] is True  # faithfully reflected, just not gated on
    assert "content_stable" not in manifest["scratch.log"]  # non-declared entries are unstamped
    assert clock.polls == 0  # declared signal already satisfied → no needless wait


@pytest.mark.asyncio
async def _impl_test_snapshot_timeout_fail_fast_when_never_ready(tmp_path, monkeypatch):
    # (e) The snapshot NEVER reaches the agent's final state (stays stale forever). The gate must
    # FAIL-FAST with SnapshotNotReadyError (→ INVALID_RUN WORKSPACE_SNAPSHOT_NOT_READY), bounded —
    # never a silent stale best-effort PASS, never a hang.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE rev-1</h1>"})  # never updated
    _seed_db(db, _CID, _file_write_log("index.html", "<h1>final rev-2</h1>"))

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

    assert "index.html" in [u["path"] for u in ei.value.facts["unsatisfied"]]
    assert clock.polls <= 6  # bounded by snapshot_wait_s / poll cadence — never hangs


@pytest.mark.asyncio
async def _impl_test_typed_seal_refusal_is_product_fail_not_snapshot_lag(tmp_path, monkeypatch):
    """F-27 split (590005) — same never-ready snapshot as the fail-fast case,
    but the run FINISHED and the PRODUCT disclosed (typed) that the strict seal
    refused on deterministic content. That must classify as the product failure
    it is (FinishUnsealableContentError → FAIL FINISH_UNSEALABLE_CONTENT), not
    as rerunnable WORKSPACE_SNAPSHOT_NOT_READY — §17 re-running it is exactly
    how a product defect gets laundered into an invisible retry."""
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE rev-1</h1>"})  # never updated
    blocking = [
        "dist: symlink excluded",
        "index.html: symlink excluded",
        "package.json: symlink excluded",
        "src/App.jsx: symlink excluded",
    ]
    events = _file_write_log("index.html", "<h1>final rev-2</h1>")
    events.append(_seal_refusal_disclosure(5, blocking))
    _seed_db(db, _CID, events)

    clock = _FakeClock()  # disk stays stale — the seal was refused, not slow
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=1.5,
    )

    with pytest.raises(FinishUnsealableContentError) as ei:
        await client.collect_workspace(_CID, ["index.html"])

    assert ei.value.facts["seal_refused_blocking"] == blocking
    assert ei.value.facts["finished_live"] is True


@pytest.mark.asyncio
async def _impl_test_seal_refusal_superseded_by_later_seal_stays_snapshot_lag(
    tmp_path, monkeypatch
):
    """A refused seal that the model REPAIRED (a later workspace_version event
    seals the re-finish) no longer governs: a subsequent never-ready collect is
    ordinary snapshot lag, not FINISH_UNSEALABLE_CONTENT."""
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE rev-1</h1>"})
    events = _file_write_log("index.html", "<h1>final rev-2</h1>")
    events.append(_seal_refusal_disclosure(5, ["dist: symlink excluded"]))
    events.append(
        {
            "id": "wv6",
            "seq": 6,
            "kind": "workspace_version",
            "source": "system",
            "version_seq": 1,
            "tree_digest": "a" * 64,
            "trigger": "finish",
        }
    )
    _seed_db(db, _CID, events)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=1.5,
    )

    with pytest.raises(SnapshotNotReadyError):
        await client.collect_workspace(_CID, ["index.html"])


@pytest.mark.asyncio
async def _impl_test_seal_refusal_not_superseded_by_recovery_version_cut(tmp_path, monkeypatch):
    """NEGATIVE (F-27 side door) — a post-refusal RECOVERY/PAUSE version cut is
    not a repaired finish seal. The typed content refusal still governs and the
    trial stays the product failure it is, never rerunnable snapshot lag."""
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE rev-1</h1>"})
    blocking = ["dist: symlink excluded"]
    events = _file_write_log("index.html", "<h1>final rev-2</h1>")
    events.append(_seal_refusal_disclosure(5, blocking))
    events.append(
        {
            "id": "wv6",
            "seq": 6,
            "kind": "workspace_version",
            "source": "system",
            "version_seq": 1,
            "tree_digest": "a" * 64,
            "trigger": "paused",
        }
    )
    _seed_db(db, _CID, events)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=1.5,
    )

    with pytest.raises(FinishUnsealableContentError) as ei:
        await client.collect_workspace(_CID, ["index.html"])

    assert ei.value.facts["seal_refused_blocking"] == blocking


def _impl_test_typed_content_seal_refusal_helper_directions():
    """Unit — the disclosure detector: typed meta governs, prose never does,
    and a later seal supersedes."""
    from harness.build_soak.adapters.disco_api import _typed_content_seal_refusal

    disclosure = _seal_refusal_disclosure(5, ["dist: symlink excluded"])
    assert _typed_content_seal_refusal([disclosure]) == ["dist: symlink excluded"]
    # Prose-only (no typed meta) → never governs.
    prose_only = dict(disclosure)
    prose_only["meta"] = {}
    assert _typed_content_seal_refusal([prose_only]) is None
    # A LATER workspace_version supersedes the refusal.
    version = {
        "id": "wv9",
        "seq": 9,
        "kind": "workspace_version",
        "source": "system",
        "version_seq": 1,
        "tree_digest": "b" * 64,
        "trigger": "finish",
    }
    assert _typed_content_seal_refusal([disclosure, version]) is None
    # An EARLIER version (a paused recovery cut, say) does not mask a refusal
    # that came after it.
    early_version = dict(version)
    early_version["seq"] = 2
    assert _typed_content_seal_refusal([early_version, disclosure]) == ["dist: symlink excluded"]
    # A LATER version that is NOT finish-triggered (a post-refusal recovery or
    # pause cut) has no supersession authority — only the finish seal judges a
    # refused finish seal. Anything else re-opens the F-27 laundering side door.
    recovery_version = dict(version)
    recovery_version["seq"] = 9
    recovery_version["trigger"] = "paused"
    assert _typed_content_seal_refusal([disclosure, recovery_version]) == ["dist: symlink excluded"]
    # A later "finish" version that fails the harness's single workspace_version
    # shape validator has NO supersession authority — a malformed event cannot
    # launder a typed refusal back into rerunnable snapshot lag (opus #7).
    malformed_version = dict(version)
    malformed_version["tree_digest"] = "not-a-sha"
    assert _typed_content_seal_refusal([disclosure, malformed_version]) == [
        "dist: symlink excluded"
    ]
    foreign_version = dict(version)
    foreign_version["source"] = "agent"
    assert _typed_content_seal_refusal([disclosure, foreign_version]) == ["dist: symlink excluded"]
    # An unorderable (null-seq) disclosure never governs — the harness cannot
    # place it relative to the supersession boundary.
    unordered = dict(disclosure)
    unordered["seq"] = None
    assert _typed_content_seal_refusal([unordered]) is None


def _impl_test_seal_incomplete_content_kind_pinned_to_product_constant():
    """CONTRACT PIN (F-27) — the adapter splits product-FAIL vs snapshot-lag on
    the product's typed disclosure kind. The two halves of that cross-layer
    contract are separate constants in separate packages; a rename on either
    side must fail HERE, loudly — never silently revert F-27 adjudication to
    rerunnable INVALID_RUN."""
    from disco.agent_server.workspace_persistence import (
        SEAL_INCOMPLETE_CONTENT_KIND as PRODUCT_KIND,
    )

    from harness.build_soak.adapters.disco_api import (
        SEAL_INCOMPLETE_CONTENT_KIND as HARNESS_KIND,
    )

    assert HARNESS_KIND == PRODUCT_KIND


@pytest.mark.asyncio
async def _impl_test_snapshot_already_consistent_accepts_promptly(tmp_path, monkeypatch):
    # (f) The snapshot already holds the agent's final bytes → accept on the FIRST read with no
    # wait, even with a large snapshot_wait budget.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>Book a Visit</h1>"
    _plant_snapshot(proj, _CID, {"index.html": content})
    _seed_db(db, _CID, _file_write_log("index.html", content))

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == content
    assert manifest["index.html"]["proof"] == "raw_sha"
    assert manifest["index.html"]["content_stable"] is False
    assert clock.polls == 0  # already consistent → no needless wait


@pytest.mark.asyncio
async def _impl_test_snapshot_absolute_workspace_write_uses_product_resolved_receipt(
    tmp_path, monkeypatch
):
    """H187: guest-absolute actions join the relative snapshot through the product receipt."""
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<html><h1>Build Smoke OK</h1></html>\n"
    sha = hashlib.sha256(content.encode()).hexdigest()
    _plant_snapshot(proj, _CID, {"index.html": content})
    _seed_db(
        db,
        _CID,
        _file_write_log(
            "/workspace/index.html",
            content,
            structured={"path": "index.html", "sha256": sha},
        ),
    )

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["sha256"] == sha
    assert manifest["index.html"]["proof"] == "raw_sha"
    assert manifest["index.html"]["content_stable"] is False
    assert clock.polls == 0


@pytest.mark.parametrize(
    ("receipt_case", "action_path"),
    [
        ("path-mismatch", "/workspace/index.html"),
        ("sha-mismatch", "/workspace/index.html"),
        ("missing-sha", "/workspace/index.html"),
        ("missing-structured", "/workspace/index.html"),
        ("valid", "/workspace/../../index.html"),
        ("valid", "/tmp/../index.html"),
    ],
    ids=[
        "path-mismatch",
        "sha-mismatch",
        "missing-sha",
        "missing-structured",
        "workspace-traversal",
        "external-absolute-traversal",
    ],
)
def _impl_test_absolute_write_receipt_inconsistency_fails_closed(receipt_case, action_path):
    """H187 guards: ambiguous receipts can prove mutation, never final byte identity."""
    content = "final bytes\n"
    sha = hashlib.sha256(content.encode()).hexdigest()
    receipt: dict[str, str] | None = {
        "path": "other.html" if receipt_case == "path-mismatch" else "index.html",
        "sha256": "0" * 64 if receipt_case == "sha-mismatch" else sha,
    }
    if receipt_case == "missing-sha":
        receipt.pop("sha256")
    elif receipt_case == "missing-structured":
        receipt = None
    log = _file_write_log(action_path, content, structured=receipt)

    durable_rows = [{**event, "payload": event} for event in log]
    expected = _disco_mod._agent_declared_expected(durable_rows, ["index.html"])

    assert expected == {"index.html": ("present_unproven",)}


@pytest.mark.parametrize("content_state", ["missing", "elided"])
def _impl_test_absolute_write_receipt_requires_full_action_bytes(content_state):
    """H187: a receipt alone cannot prove bytes absent from the durable action payload."""
    content = "final bytes\n"
    log = _file_write_log(
        "/workspace/index.html",
        content,
        structured={"path": "index.html", "sha256": hashlib.sha256(content.encode()).hexdigest()},
    )
    arguments = log[1]["tool_call"]["arguments"]
    if content_state == "missing":
        arguments.pop("content")
    else:
        arguments["content"] = "<1,234 chars elided; full content remains in event payload>"
    durable_rows = [{**event, "payload": event} for event in log]

    expected = _disco_mod._agent_declared_expected(durable_rows, ["index.html"])

    assert expected == {"index.html": ("present_unproven",)}


def _impl_test_bad_later_write_receipt_cannot_leave_an_earlier_sha_current():
    """H187 ordering guard: a later mutation always supersedes an older valid receipt."""
    first = "first bytes\n"
    later = "later bytes\n"
    log = _file_write_log(
        "/workspace/index.html",
        first,
        call="first",
        structured={
            "path": "index.html",
            "sha256": hashlib.sha256(first.encode()).hexdigest(),
        },
    )
    log.pop()  # append the later mutation before the terminal status
    log.extend(
        _file_write_log(
            "/workspace/index.html",
            later,
            call="later",
            first_seq=10,
            structured={"path": "index.html", "sha256": "f" * 64},
        )[1:]
    )

    durable_rows = [{**event, "payload": event} for event in log]
    expected = _disco_mod._agent_declared_expected(durable_rows, ["index.html"])

    assert expected == {"index.html": ("present_unproven",)}


@pytest.mark.asyncio
async def _impl_test_snapshot_read_only_serve_and_verify_shells_preserve_file_write_sha(
    tmp_path, monkeypatch
):
    """H182: serving/probing a file after writing it is not a later mutation.

    The live static-smoke event stream wrote index.html with a precise SHA, then used curl,
    ``python -m http.server``, test/grep, and the host-generated static verifier.  Treating every
    shell action as an opaque write erased the exact SHA and mislabeled the final capture
    ``present_unproven``.  All of these strictly read-only shapes must preserve the write proof.
    """
    from disco.core.loop.finish import _static_verify_command

    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<html><h1>Build Smoke OK</h1></html>\n"
    _plant_snapshot(proj, _CID, {"index.html": content})
    log = _file_write_log("index.html", content)
    log.pop()  # append the live post-write serve/verify sequence before FINISHED
    commands = [
        (
            "shell",
            "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/ "
            '&& echo "" && curl -s http://localhost:8000/ | head -5',
        ),
        (
            "shell",
            "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/ | grep -q 200",
        ),
        ("shell_exec", "cd /workspace && python3 -m http.server 8080"),
        (
            "shell",
            "test -f index.html && curl -s -o /dev/null -w '%{http_code}' "
            'http://localhost:8080/ | grep -q 200 && echo "PASS"',
        ),
        ("shell", _static_verify_command("index.html")),
    ]
    seq = 5
    for i, (tool, command) in enumerate(commands):
        call_id = f"shell-{i}"
        log.extend(
            [
                {
                    "id": f"a{seq}",
                    "seq": seq,
                    "kind": "action",
                    "source": "agent",
                    "tool_call": {
                        "tool_name": tool,
                        "arguments": {"command": command},
                        "call_id": call_id,
                    },
                },
                {
                    "id": f"o{seq + 1}",
                    "seq": seq + 1,
                    "kind": "observation",
                    "source": "environment",
                    "tool_result": {
                        "call_id": call_id,
                        "tool_name": tool,
                        "success": True,
                        "content": "ok",
                    },
                },
            ]
        )
        seq += 2
    log.append(
        {
            "id": f"s{seq}",
            "seq": seq,
            "kind": "status",
            "source": "system",
            "status": "FINISHED",
        }
    )
    _seed_db(db, _CID, log)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert manifest["index.html"]["proof"] == "raw_sha"
    assert clock.polls == 0


@pytest.mark.parametrize(
    "opaque_command",
    [
        'echo "$(sed -i s/present/changed/ index.html)"',
        "curl -s -w '%output{index.html}overwritten' http://localhost:8000/",
        "curl -s --write-out=%output{index.html}overwritten http://localhost:8000/",
    ],
)
@pytest.mark.asyncio
async def _impl_test_snapshot_unknown_or_shell_substitution_still_downgrades_file_write_sha(
    tmp_path, monkeypatch, opaque_command
):
    """H182 fail-closed guard: executable/redirecting shell syntax stays opaque."""
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>still present</h1>\n"
    _plant_snapshot(proj, _CID, {"index.html": content})
    log = _file_write_log("index.html", content)
    log.pop()
    log.extend(
        [
            {
                "id": "a5",
                "seq": 5,
                "kind": "action",
                "source": "agent",
                "tool_call": {
                    "tool_name": "shell",
                    "arguments": {"command": opaque_command},
                    "call_id": "opaque-5",
                },
            },
            {
                "id": "o6",
                "seq": 6,
                "kind": "observation",
                "source": "environment",
                "tool_result": {
                    "call_id": "opaque-5",
                    "tool_name": "shell",
                    "success": True,
                },
            },
            {
                "id": "s7",
                "seq": 7,
                "kind": "status",
                "source": "system",
                "status": "FINISHED",
            },
        ]
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

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["proof"] == "unproven_extended_stability"
    assert clock.polls >= _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS - 1
