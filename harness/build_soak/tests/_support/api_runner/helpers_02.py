"""Shared helpers for the moved collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    Any,
    DiscoApiClient,
    SnapshotNotReadyError,
    _run_mod,
    action,
    agent_error,
    json,
    msg,
    observation,
    plan,
    run_once,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _insert_event,
    _inspect_snapshot,
    _inspect_span,
    _plant_snapshot,
    _SandboxIdProgressTransport,
    _seed_db,
    _smoke_log_with_file_write,
    _smoke_scenario,
)


class _NonterminalCollectionTransport(FakeTransport):
    """RUNNING at the collection boundary; the kill emits one final trace event."""

    def __init__(self, db_path, *, kill_ack: bool = True, **kwargs):
        super().__init__(db_path, **kwargs)
        self.kill_ack = kill_ack
        self.trace_snapshot = _inspect_snapshot([_inspect_span(1), _inspect_span(2)])

    async def get_json(self, path):
        if path == f"/api/debug/trace/{self.cid}":
            return 200, json.loads(json.dumps(self.trace_snapshot))
        if path.endswith("/state") and not self._killed:
            return 200, {"execution_status": "RUNNING"}
        return await super().get_json(path)

    async def post_json(self, path, body):
        if path.endswith("/kill"):
            self.trace_snapshot = _inspect_snapshot(
                [_inspect_span(1), _inspect_span(2), _inspect_span(3)]
            )
            if not self.kill_ack:
                return 503, {"error": "kill rejected"}
        return await super().post_json(path, body)


def _nonterminal_inactive_log():
    """A genuinely inactive nonterminal prefix — no durable work terminal, so the
    late-terminal race guard cannot reroute collection onto the strict path."""
    return [
        msg(1, "user", "build a page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
        status(5, "RUNNING", "plan_approved"),
    ]


def _snapshot_not_ready_exc(cid: str) -> SnapshotNotReadyError:
    return SnapshotNotReadyError(
        "workspace snapshot never reached the agent's final state within 45s",
        {"conversation_id": cid, "snapshot_wait_s": 45.0},
    )


def _thrash_smoke_scenario() -> dict[str, Any]:
    scenario = json.loads(json.dumps(_smoke_scenario()))
    scenario["assertions"]["thrash"] = {
        "max_identical_action_repeats": 2,
        "max_same_tool_error_repeats": 1,
        "max_actionless_pauses": 0,
        "max_same_model_repair_repeats": 1,
        "max_total_model_repairs": 3,
    }
    return scenario


def _same_signature_refusals() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for seq in (11, 13):
        action_id = f"a{seq}"
        events.append(action(seq, "file_read", action_id=action_id, args={"path": "index.html"}))
        events.append(
            agent_error(
                seq + 1,
                action_id,
                error="stuck_escape_tool_quarantine:file_read",
                detail="withheld during the current loop",
            )
        )
    return events


class _LateVersionsTransport:
    """Versions publish shortly AFTER the terminal status flips — the product's
    finalization pipeline appends `workspace_version` events ~1.3s after
    FINISHED (observed live at seed 405318). A fast restore read races it and
    sees an empty list from the same 200 route."""

    def __init__(self, empty_reads: int) -> None:
        self.reads = 0
        self.empty_reads = empty_reads
        self.restores: list[str] = []

    async def get_json(self, path):
        assert path.endswith("/versions")
        self.reads += 1
        if self.reads <= self.empty_reads:
            return 200, {"versions": []}
        return 200, {"versions": [{"seq": 1}, {"seq": 2}]}

    async def post_json(self, path, body):
        self.restores.append(path)
        return 200, {"restored": 1, "new_version": 3, "tree_digest": "sha256:x"}


class _FailingRestoreTransport:
    """The versions publish, but the restore itself fails with a typed body
    (F-28, pilot seed 620108: the dossier kept only the bare 503 and the
    product's stated cause was unrecoverable)."""

    async def get_json(self, path):
        return 200, {"versions": [{"seq": 1}, {"seq": 2}]}

    async def post_json(self, path, body):
        return 503, {"detail": {"reason": "storage_error", "message": "sandbox unavailable"}}


class _ParkedIdleTransport:
    """A conversation already killed to IDLE before this poll invocation began —
    the live /state carries the durable horizon (`last_seq`) like the real route."""

    def __init__(self, last_seq: int) -> None:
        self.last_seq = last_seq

    async def get_json(self, path):
        assert path.endswith("/state")
        return 200, {"execution_status": "IDLE", "last_seq": self.last_seq}


def _killed_log() -> list[dict]:
    killed = status(6, "IDLE", "killed")
    return [
        msg(1, "user", "build it"),
        status(2, "RUNNING"),
        action(3, "file_write", action_id="a3", args={"path": "app.py"}),
        observation(4, "a3"),
        killed,
    ]


class _RotatingPreviewTransport(FakeTransport):
    """First canonical fetch lands on the by-design 409 rotation boundary (the
    sealed post-finish replay rotates the authority); the re-bootstrap succeeds."""

    def __init__(self, *args, sequence, **kwargs):
        super().__init__(*args, **kwargs)
        self.sequence = list(sequence)
        self.preview_fetches = 0

    async def fetch_isolated_preview(self, conversation_id):
        self.preview_fetches += 1
        status, body = self.sequence[min(self.preview_fetches - 1, len(self.sequence) - 1)]
        return status, body, {}


def _restart_span(seq: int, request_id: str = "req_restart") -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": "span",
        "span": "agent.step",
        "event": "start",
        "role": "agent_driver",
        "request_id": request_id,
    }


class _FreezableProgressTransport(_SandboxIdProgressTransport):
    """Progressing forever, but the cooperative pause DOES reach a step boundary.

    On the real pause control it appends the two durable events the product's
    end-gate emits when a Build run settles PAUSED: a PAUSED status, and then the
    WorkspaceVersionEvent(trigger=PAUSED) that names the immutable version.
    """

    def __init__(self, db_path, *, version_seq, **kw):
        super().__init__(db_path, **kw)
        self._version_seq = version_seq
        self.paused_at = None
        self.version_at = None

    async def ws_control(self, conversation_id, frame):
        await super().ws_control(conversation_id, frame)
        if frame.get("type") != "pause" or self.paused_at is not None:
            return
        self._seq += 1
        self.paused_at = self._seq
        _insert_event(
            self.db_path,
            self.cid,
            {"seq": self.paused_at, "kind": "status", "source": "system", "status": "PAUSED"},
        )
        self._seq += 1
        self.version_at = self._seq
        _insert_event(
            self.db_path,
            self.cid,
            {
                "seq": self.version_at,
                "kind": "workspace_version",
                "source": "system",
                "trigger": "PAUSED",
                "version_seq": self._version_seq,
            },
        )


async def _dossier_with_landed_freeze(tmp_path, monkeypatch, *, run_id="run_capsule_001"):
    """Drive the REAL hard-cap path to a landed freeze and return its dossier."""
    from disco.tools.projects.store import ProjectStore

    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>capsule bytes</h1>"
    ws = _plant_snapshot(projects, _CID, {"index.html": content})
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="PAUSED")
    assert version is not None

    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content)[:-1])
    transport = _FreezableProgressTransport(db, version_seq=version.seq, finish_after=None)
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(projects)
    )
    await run_once(
        client,
        _smoke_scenario(),
        run_id=run_id,
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.2,
        parallel_workers=10,
    )
    return tmp_path / "out" / run_id, projects, version, content, ws
