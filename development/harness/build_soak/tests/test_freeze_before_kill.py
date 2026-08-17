"""`freeze_progressing_workspace` — the pre-kill freeze, and its honest failures.

Counted context seeds 460004/460005 lost every byte a progressing run had written
because the hard-cap stop killed first and read the durable store afterwards.
These pin the corrected sequence and, just as importantly, that a freeze which does
NOT land reports FREEZE_TIMEOUT with an empty manifest rather than claiming
preservation.
"""

from __future__ import annotations

from typing import Any

from harness.build_soak.adapters.disco_api import DiscoApiClient


class _FakeClient:
    """Only the surface freeze_progressing_workspace touches."""

    freeze_progressing_workspace = DiscoApiClient.freeze_progressing_workspace
    _verified_workspace_version = DiscoApiClient._verified_workspace_version
    _read_snapshot_manifest = DiscoApiClient._read_snapshot_manifest

    def __init__(self, timelines: list[list[dict[str, Any]]]) -> None:
        self._timelines = timelines
        self.paused = False
        self._projects_root = None
        self.verified: Any = None
        self.manifest: dict[str, Any] = {}
        # The real client always has this (set in DiscoApiClient.__init__); a
        # successful freeze registers the verified immutable version here so
        # browser collection cannot fall back to the mutable ProjectStore head.
        self._collected_workspace_dirs: dict[str, Any] = {}

    def collect_events(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._timelines[0] if len(self._timelines) == 1 else self._timelines.pop(0)

    async def pause(self, conversation_id: str) -> None:
        self.paused = True

    # stub the two reads so the sequence logic is what is under test
    def _verified_workspace_version(self, conversation_id, version_seq):  # type: ignore[no-redef]
        return self.verified

    def _read_snapshot_manifest(self, conversation_id, declared, snapshot_dir):  # type: ignore[no-redef]
        return self.manifest


class _Verified:
    def __init__(self) -> None:
        self.workspace = "/frozen"
        self.tree_digest = "d" * 64
        self.file_count = 3
        self.total_bytes = 167


def _status(seq: int, value: str) -> dict[str, Any]:
    return {"seq": seq, "kind": "status", "status": value}


def _version(seq: int, trigger: str, version_seq: int = 7) -> dict[str, Any]:
    return {"seq": seq, "kind": "workspace_version", "trigger": trigger, "version_seq": version_seq}


def _user_msg(seq: int) -> dict[str, Any]:
    return {"seq": seq, "kind": "message", "source": "user", "message": {"role": "user"}}


def _intent(seq: int, run_intent_id: str) -> dict[str, Any]:
    return {"seq": seq, "kind": "workspace_mutation", "run_intent_id": run_intent_id}


def _view(seq: int, agent_view_id: str) -> dict[str, Any]:
    return {"seq": seq, "kind": "workspace_mutation", "agent_view_id": agent_view_id}


async def test_freeze_reads_the_immutable_version_the_paused_event_names() -> None:
    """POSITIVE: pause -> PAUSED -> version event AFTER it -> that exact version."""
    before = [_status(10, "RUNNING")]
    after = [*before, _status(11, "PAUSED"), _version(12, "PAUSED", version_seq=7)]
    client = _FakeClient([before, after])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", ["REPORT.md"], deadline_s=5)

    assert client.paused is True
    assert result["status"] == "frozen"
    assert result["version_seq"] == 7
    assert result["paused_seq"] == 11
    assert result["horizon_seq"] == 12
    assert result["manifest"] == {"REPORT.md": {"present": True}}


async def test_a_version_event_before_the_paused_status_does_not_count() -> None:
    """NEGATIVE: the freeze proof must FOLLOW the pause, not precede it.

    An earlier PAUSED version from a prior pause/resume cycle would otherwise be
    accepted as this stop's evidence.
    """
    before = [_status(10, "RUNNING"), _version(11, "PAUSED", version_seq=3)]
    after = [*before, _status(12, "PAUSED")]
    client = _FakeClient([before, after])
    client.verified = _Verified()

    result = await client.freeze_progressing_workspace("c", [], deadline_s=2)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert result["manifest"] == {}
    assert "WorkspaceVersionEvent" in (result["reason"] or "")


async def test_no_paused_status_is_freeze_timeout_with_empty_manifest() -> None:
    """NEGATIVE: pause never reaches a step boundary — claim nothing."""
    events = [_status(10, "RUNNING")]
    client = _FakeClient([events])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", [], deadline_s=2)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert result["manifest"] == {}, "a failed freeze must never report workspace truth"
    assert "PAUSED status" in (result["reason"] or "")


async def test_a_user_turn_across_the_horizon_invalidates_the_freeze() -> None:
    """NEGATIVE: run authority must not move underneath the freeze horizon."""
    before = [_status(10, "RUNNING")]
    after = [*before, _user_msg(11), _status(12, "PAUSED"), _version(13, "PAUSED")]
    client = _FakeClient([before, after])
    client.verified = _Verified()

    result = await client.freeze_progressing_workspace("c", [], deadline_s=5)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert "user turn" in (result["reason"] or "")


async def test_unverifiable_version_is_not_reported_as_frozen() -> None:
    """NEGATIVE: if the immutable version cannot be freshly verified, claim nothing."""
    before = [_status(10, "RUNNING")]
    after = [*before, _status(11, "PAUSED"), _version(12, "PAUSED")]
    client = _FakeClient([before, after])
    client.verified = None  # verification fails
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", [], deadline_s=5)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert result["manifest"] == {}
    assert "freshly verified" in (result["reason"] or "")


async def test_deduplicated_version_seq_is_still_a_valid_freeze() -> None:
    """An unchanged tree may reuse an existing immutable version.

    The EVENT is the freeze proof, not a numerically new version_seq — requiring
    novelty would fail a run whose workspace simply did not change.
    """
    before = [_status(10, "RUNNING"), _version(9, "FINISHED", version_seq=4)]
    after = [*before, _status(11, "PAUSED"), _version(12, "PAUSED", version_seq=4)]
    client = _FakeClient([before, after])
    client.verified = _Verified()
    client.manifest = {"index.html": {"present": True}}

    result = await client.freeze_progressing_workspace("c", [], deadline_s=5)

    assert result["status"] == "frozen"
    assert result["version_seq"] == 4


async def test_a_resume_across_the_horizon_invalidates_the_freeze() -> None:
    """NEGATIVE: the run left PAUSED before the snapshot event landed.

    A version event that follows a resume does not describe a quiesced workspace,
    so it must not be accepted as this stop's freeze proof.
    """
    before = [_status(10, "RUNNING")]
    after = [
        *before,
        _status(11, "PAUSED"),
        _status(12, "RUNNING"),  # <- the resume
        _version(13, "PAUSED"),
    ]
    client = _FakeClient([before, after])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", ["REPORT.md"], deadline_s=5)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert result["manifest"] == {}
    assert "left PAUSED" in (result["reason"] or "")


async def test_a_new_run_intent_across_the_horizon_invalidates_the_freeze() -> None:
    """NEGATIVE: run authority moved to a newer intent underneath the freeze."""
    before = [_intent(9, "intent-1"), _status(10, "RUNNING")]
    after = [
        *before,
        _intent(11, "intent-2"),  # <- authority moved
        _status(12, "PAUSED"),
        _version(13, "PAUSED"),
    ]
    client = _FakeClient([before, after])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", ["REPORT.md"], deadline_s=5)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert result["manifest"] == {}
    assert "run intent changed" in (result["reason"] or "")


async def test_an_agent_view_change_across_the_horizon_invalidates_the_freeze() -> None:
    """NEGATIVE: the model-view generation was superseded during the freeze.

    The agent view is a separate authority axis from the run intent: a view can be
    superseded while the intent id is unchanged, and evidence bound to a superseded
    view is not current.
    """
    before = [_view(9, "view-1"), _status(10, "RUNNING")]
    after = [
        *before,
        _view(11, "view-2"),  # <- same intent, superseded view
        _status(12, "PAUSED"),
        _version(13, "PAUSED"),
    ]
    client = _FakeClient([before, after])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", ["REPORT.md"], deadline_s=5)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert result["manifest"] == {}
    assert "agent view" in (result["reason"] or "")


async def test_a_landed_freeze_registers_the_immutable_version_as_workspace_source() -> None:
    """The frozen version becomes the conversation's authoritative workspace dir.

    Browser collection resolves its source as
    ``_collected_workspace_dirs.get(cid) or _snapshot_workspace_dir(cid)``.  Without
    this registration the freeze path leaves that dict empty and the ``or`` silently
    falls back to the MUTABLE store head, which after the kill holds only the
    pre-run import snapshot.
    """
    before = [_status(10, "RUNNING")]
    after = [*before, _status(11, "PAUSED"), _version(12, "PAUSED", version_seq=7)]
    client = _FakeClient([before, after])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", ["REPORT.md"], deadline_s=5)

    assert result["status"] == "frozen"
    assert result["workspace_dir"] == "/frozen"
    assert client._collected_workspace_dirs["c"] == "/frozen"


async def test_a_failed_freeze_registers_no_workspace_source() -> None:
    """NEGATIVE: a freeze that did not land must not point evidence anywhere.

    Registering on failure would be worse than not registering at all -- it would
    hand browser collection a directory the freeze never verified.
    """
    events = [_status(10, "RUNNING")]
    client = _FakeClient([events])
    client.verified = _Verified()
    client.manifest = {"REPORT.md": {"present": True}}

    result = await client.freeze_progressing_workspace("c", ["REPORT.md"], deadline_s=2)

    assert result["status"] == "FREEZE_TIMEOUT"
    assert client._collected_workspace_dirs == {}
