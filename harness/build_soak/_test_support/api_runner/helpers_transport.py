"""Shared helpers for the moved collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    UTC,
    Any,
    CollectedRun,
    DiscoApiClient,
    _disco_mod,
    _run_mod,
    action,
    cast,
    datetime,
    io,
    json,
    msg,
    plan,
    sqlite3,
    status,
    zipfile,
)
from .helpers_core import (
    _append_event,
    _fake_inspect_trace,
    _plant_snapshot,
    _seed_db,
    _smoke_log_with_file_write,
)


def _insert_event(db_path, cid, event):
    """Append ONE full-event dict (the _eventlog builder shape) into the durable log."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cid,
                event["seq"],
                event.get("id", f"evt_{event['seq']}"),
                event["kind"],
                event["source"],
                event.get("timestamp", ""),
                json.dumps(event),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class FakeTransport:
    """Scripts the live surfaces. `states` is the status sequence GET /state yields
    (clamped at the last). `workspace`/`preview_html` back the preview-proxy reads."""

    def __init__(
        self,
        db_path,
        *,
        states,
        workspace=None,
        preview_html="",
        health_status=200,
        health_exc=None,
        cid=_CID,
    ):
        self.db_path = db_path
        self._states = list(states)
        self._idx = 0
        self.workspace = workspace or {}
        self.preview_html = preview_html
        self.health_status = health_status
        self.health_exc = health_exc
        self.cid = cid
        self.ws_frames = []
        self.posts = []
        self._killed = False

    async def health(self):
        if self.health_exc is not None:
            raise self.health_exc
        return self.health_status, {"ok": True}

    async def post_json(self, path, body) -> tuple[int, dict[str, Any]]:
        self.posts.append((path, body))
        if path == "/conversations":
            return 200, {"conversation_id": self.cid, "surface": "build"}
        if path.endswith("/kill"):
            self._killed = True
            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE conversation_id = ?",
                        (self.cid,),
                    ).fetchone()
                seq = int(row[0] if row else 0) + 1
                killed = status(seq, "IDLE", "killed")
                killed["timestamp"] = datetime.now(UTC).isoformat()
                _insert_event(self.db_path, self.cid, killed)
            except sqlite3.OperationalError:
                # Some adapter-only tests intentionally provide no event schema.
                pass
            return 200, {
                "killed": True,
                "state": {"execution_status": "IDLE"},
                "sandbox_instance_ids": [],
            }
        if path.endswith("/resume"):
            # The REAL resume body carries a STATE string under "status" — a regression
            # guard for the http-int/state-string key collision (Bug 11): merging this
            # under "status" used to clobber the HTTP code and crash int(resp["status"]).
            return 200, {"ok": True, "status": "RUNNING"}
        return 200, {"event_id": "e", "seq": 1}

    async def get_json(self, path) -> tuple[int, dict[str, Any]]:
        if path == f"/api/debug/trace/{self.cid}":
            trace = _fake_inspect_trace()
            trace["conversation_id"] = self.cid
            return 200, trace
        if path.endswith("/state"):
            if self._killed:
                return 200, {"execution_status": "IDLE"}
            st = self._states[min(self._idx, len(self._states) - 1)]
            self._idx += 1
            return 200, {"execution_status": st}
        if path.endswith("/preview"):
            return 200, {"available": True}
        return 200, {}

    async def get_text(self, path) -> tuple[int, str, dict[str, str]]:
        if path.endswith("/preview-app/"):
            return 200, self.preview_html, {}
        for rel, html in self.workspace.items():
            if path.endswith(f"/preview-app/{rel}"):
                return 200, html, {}
        return 404, "", {}

    async def post_file(
        self,
        path,
        *,
        field,
        filename,
        content,
        content_type,
    ) -> tuple[int, dict[str, Any]]:
        self.posts.append(
            (
                path,
                {
                    "field": field,
                    "filename": filename,
                    "content_type": content_type,
                    "bytes": len(content),
                },
            )
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = [name for name in archive.namelist() if not name.endswith("/")]
            total = sum(len(archive.read(name)) for name in files)
        return 200, {
            "conversation_id": self.cid,
            "files": len(files),
            "bytes": total,
            "title": filename.removesuffix(".zip"),
        }

    async def get_bytes(self, path) -> tuple[int, bytes, dict[str, str]]:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            for rel, text in sorted(self.workspace.items()):
                bundle.writestr(rel, text.encode("utf-8") if isinstance(text, str) else text)
        return 200, archive.getvalue(), {"content-type": "application/zip"}

    async def fetch_isolated_preview(self, conversation_id) -> tuple[int, str, dict[str, str]]:
        return 200, self.preview_html, {"cache-control": "private, no-store"}

    async def ws_control(self, conversation_id, frame):
        self.ws_frames.append(frame)


def _strict_workspace_case(tmp_path, *, content: str = "<h1>sealed</h1>"):
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content))
    _plant_snapshot(projects, _CID, {"index.html": content})
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
    return db, projects, version, client


class _CanonicalPreviewTransport(FakeTransport):
    def __init__(self, *args, preview_status=200, preview_body="", **kwargs):
        super().__init__(*args, **kwargs)
        self.preview_status = preview_status
        self.preview_body = preview_body
        self.preview_fetches = 0

    async def fetch_isolated_preview(self, conversation_id):
        self.preview_fetches += 1
        return self.preview_status, self.preview_body, {"cache-control": "private, no-store"}

    async def get_text(self, path):
        if "/preview-app/" in path:
            raise AssertionError("legacy authenticated preview-app route must not be called")
        return await super().get_text(path)


class _DecisionTransport(FakeTransport):
    """A FakeTransport whose /state ALSO surfaces `pending_alternatives_id` while the status is
    AWAITING_USER_DECISION (mirrors ConversationState) so resolve_decision can state-bind."""

    def __init__(self, *a, pending_alternatives_id=None, **kw):
        super().__init__(*a, **kw)
        self.pending_alternatives_id = pending_alternatives_id

    async def get_json(self, path):
        status, data = await super().get_json(path)
        if path.endswith("/state") and data.get("execution_status") == "AWAITING_USER_DECISION":
            data = {**data, "pending_alternatives_id": self.pending_alternatives_id}
        return status, data


class _ProgressTransport(FakeTransport):
    """Models a SLOW-BUT-PROGRESSING build: every GET /state appends a NEW event (the
    progress signal the wait resets its inactivity timer on) and reports RUNNING until
    `finish_after` polls, then FINISHED forever. `finish_after=None` → never reaches a
    terminal (always progressing — the hard-cap path)."""

    def __init__(self, db_path, *, finish_after=None, start_seq=100, **kw):
        super().__init__(db_path, states=["RUNNING"], **kw)
        self._reads = 0
        self._finish_after = finish_after
        self._seq = start_seq

    async def get_json(self, path):
        if path.endswith("/state"):
            self._reads += 1
            self._seq += 1
            _append_event(self.db_path, self.cid, self._seq)  # NEW event ⇒ progress
            if self._finish_after is not None and self._reads >= self._finish_after:
                return 200, {"execution_status": "FINISHED"}
            return 200, {"execution_status": "RUNNING"}
        return await super().get_json(path)


class _RejectedKillProgressTransport(_ProgressTransport):
    async def post_json(self, path, body):
        if path.endswith("/kill"):
            self.posts.append((path, body))
            return 500, {"killed": False}
        return await super().post_json(path, body)


class _SandboxIdProgressTransport(_ProgressTransport):
    async def post_json(self, path, body):
        status_code, data = await super().post_json(path, body)
        if path.endswith("/kill"):
            data = {**data, "sandbox_instance_ids": ["sbx_progress_timeout"]}
        return status_code, data


def _observation_for_action(
    seq: int,
    event: dict[str, Any],
    *,
    success: bool = True,
) -> dict[str, Any]:
    tc = event["tool_call"]
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "action_id": event["id"],
        "tool_result": {
            "call_id": tc["call_id"],
            "tool_name": tc["tool_name"],
            "success": success,
            "content": "ok" if success else "failed",
            "error": None if success else "boom",
        },
    }


class _ScriptStateTransport(FakeTransport):
    """GET /state yields scripted statuses (clamped at last). `on_read(n)` fires on each
    /state read (1-based) so a test can mutate the DB as the pickup wait polls — modelling
    a re-plan that lands only after several polls."""

    def __init__(self, db_path, *, states, on_read=None, **kw):
        super().__init__(db_path, states=states, **kw)
        self._on_read = on_read
        self.state_reads = 0

    async def get_json(self, path):
        if path.endswith("/state"):
            self.state_reads += 1
            if self._on_read is not None:
                self._on_read(self.state_reads)
            st = self._states[min(self.state_reads - 1, len(self._states) - 1)]
            return 200, {"execution_status": st}
        return await super().get_json(path)


class _DeadWindowTransport(FakeTransport):
    """FAITHFUL model of the after-terminal FINALIZATION DEAD-WINDOW (V2 — replaces V1's
    instant-transition fake, which bypassed the dead-window and is why the V1 unit tests
    passed while the live run failed). The build rests at FINISHED and its STATUS NEVER
    CHANGES. On each send_message follow-up:
      * the next `stale_reads` /state reads STILL report FINISHED with NO new durable event —
        the append-only dead window (status does NOT change, the exact V1 trap);
      * then a RE-PLAN event (plan, revision bumped) is appended at a higher seq while the
        status is STILL FINISHED — the ONLY pickup signal is the EVENT SEQ (V2's detector);
      * then, after `work_reads` more reads, the follow-up's OWN NEW terminal (a FINISHED
        status event) is appended at a yet-higher seq — the terminal whose seq > the baseline
        that the min_seq drive guard requires before returning.
    If the runner relied on the status string (V1) it would NEVER detect pickup here; if the
    drive returned on the stale terminal it would send follow-up 2 before follow-up 1's own
    terminal. The logs record the read counts so the test can assert strict ordering."""

    def __init__(self, db_path, *, stale_reads=2, work_reads=2, start_seq=10, **kw):
        super().__init__(db_path, states=["FINISHED"], **kw)
        self._stale_reads = stale_reads
        self._work_reads = work_reads
        self._seq = start_seq
        self._sends = 0
        self._reads_since_send = None
        self._total_reads = 0
        self.send_log = []  # (send_index, total_state_reads_at_send)
        self.pickup_log = []  # (send_index, total_state_reads_at_replan_event)
        self.terminal_log = []  # (send_index, total_state_reads_at_new_terminal)

    async def get_json(self, path):
        if path.endswith("/state"):
            self._total_reads += 1
            if self._reads_since_send is not None:
                self._reads_since_send += 1
                r = self._reads_since_send
                if r == self._stale_reads + 1:
                    # dead window over: append a RE-PLAN event (status STAYS FINISHED).
                    self._seq += 1
                    _insert_event(self.db_path, self.cid, plan(self._seq, revision=1 + self._sends))
                    self.pickup_log.append((self._sends, self._total_reads))
                elif r == self._stale_reads + 1 + self._work_reads:
                    # the follow-up's OWN new terminal at a yet-higher seq.
                    self._seq += 1
                    _insert_event(self.db_path, self.cid, status(self._seq, "FINISHED"))
                    self.terminal_log.append((self._sends, self._total_reads))
                    self._reads_since_send = None
            return 200, {"execution_status": "FINISHED"}  # status NEVER changes
        return await super().get_json(path)

    async def ws_control(self, conversation_id, frame):
        await super().ws_control(conversation_id, frame)
        if frame.get("type") == "send_message":
            self._sends += 1
            self._reads_since_send = 0
            self.send_log.append((self._sends, self._total_reads))


class _CancelAtRecoveryTransport(FakeTransport):
    """A deterministic cancel_at recovery fake.

    The first drive starts RUNNING, exposes a file_write on the second state read,
    and stays live until the runner posts /kill. The kill appends the product's
    post-kill IDLE status. A later after-terminal send_message appends a user turn,
    then the fake emits a re-plan pickup and a new FINISHED terminal.
    """

    def __init__(self, db_path, **kw):
        super().__init__(
            db_path,
            states=["RUNNING"],
            workspace={
                "index.html": "<h1>Beacon Status</h1><table><td>All systems nominal</td></table>"
            },
            **kw,
        )
        self._seq = 5
        self._state_reads = 0
        self._file_write_inserted = False
        self._killed = False
        self._recovery_reads: int | None = None
        self._recovery_progress_inserted = False
        self._recovery_terminal_inserted = False
        self.kill_log: list[int] = []
        self.send_log: list[int] = []
        self.pickup_log: list[int] = []
        self.terminal_log: list[int] = []
        self.idle_reads_before_followup = 0

    def _append(self, event):
        _insert_event(self.db_path, self.cid, event)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def post_json(self, path, body):
        self.posts.append((path, body))
        if path.endswith("/kill"):
            self._killed = True
            self.kill_log.append(self._state_reads)
            self._append(status(self._next_seq(), "IDLE", "killed"))
            return 200, {"killed": True, "state": {"execution_status": "IDLE"}}
        if path == "/conversations":
            return 200, {"conversation_id": self.cid, "surface": "build"}
        if path.endswith("/resume"):
            return 200, {"ok": True, "status": "RUNNING"}
        return 200, {"event_id": "e", "seq": 1}

    async def get_json(self, path):
        if path.endswith("/state"):
            self._state_reads += 1

            if not self._file_write_inserted and not self._killed and self._state_reads >= 2:
                seq = self._next_seq()
                write = action(
                    seq,
                    "file_write",
                    args={"path": "index.html", "content": "<h1>partial</h1>"},
                    action_id=f"act{seq}",
                )
                self._append(write)
                self._append(_observation_for_action(self._next_seq(), write))
                self._file_write_inserted = True

            if self._recovery_reads is not None:
                self._recovery_reads += 1
                if self._recovery_reads == 2 and not self._recovery_progress_inserted:
                    self._append(status(self._next_seq(), "RUNNING", "planning"))
                    self._append(plan(self._next_seq(), revision=2))
                    self._recovery_progress_inserted = True
                    self.pickup_log.append(self._state_reads)
                elif self._recovery_reads == 4 and not self._recovery_terminal_inserted:
                    seq = self._next_seq()
                    write = action(
                        seq,
                        "file_write",
                        args={
                            "path": "index.html",
                            "content": (
                                "<h1>Beacon Status</h1><table><td>All systems nominal</td></table>"
                            ),
                        },
                        action_id=f"act{seq}",
                    )
                    self._append(write)
                    self._append(_observation_for_action(self._next_seq(), write))
                    self._append(status(self._next_seq(), "FINISHED"))
                    self._recovery_terminal_inserted = True
                    self.terminal_log.append(self._state_reads)

                if self._recovery_terminal_inserted:
                    return 200, {"execution_status": "FINISHED"}
                if self._recovery_progress_inserted:
                    return 200, {"execution_status": "RUNNING"}
                return 200, {"execution_status": "IDLE"}

            if self._killed:
                self.idle_reads_before_followup += 1
                return 200, {"execution_status": "IDLE"}
            return 200, {"execution_status": "RUNNING"}
        return await super().get_json(path)

    async def ws_control(self, conversation_id, frame):
        await super().ws_control(conversation_id, frame)
        if frame.get("type") == "send_message":
            self.send_log.append(self._state_reads)
            self._append(msg(self._next_seq(), "user", str(frame.get("content") or "")))
            self._recovery_reads = 0


class _CancelMissedWindowTransport(FakeTransport):
    def __init__(self, db_path, **kw):
        super().__init__(
            db_path,
            states=["RUNNING"],
            workspace={"index.html": "<h1>finished too fast</h1>"},
            preview_html="<h1>finished too fast</h1>",
            **kw,
        )
        self._seq = 5
        self._state_reads = 0
        self._terminal_inserted = False

    def _append(self, event):
        _insert_event(self.db_path, self.cid, event)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def get_json(self, path):
        if path.endswith("/state"):
            self._state_reads += 1
            if not self._terminal_inserted and self._state_reads >= 2:
                write = action(
                    self._next_seq(),
                    "file_write",
                    args={"path": "index.html", "content": "<h1>finished too fast</h1>"},
                    action_id="act_fast_write",
                )
                self._append(write)
                self._append(_observation_for_action(self._next_seq(), write))
                self._append(status(self._next_seq(), "FINISHED"))
                self._terminal_inserted = True
            return 200, {"execution_status": "FINISHED" if self._terminal_inserted else "RUNNING"}
        return await super().get_json(path)


class _CleanupKillClient:
    def __init__(self, response=None) -> None:
        self.response = response or {
            "http_status": 200,
            "killed": True,
            "state": {"execution_status": "IDLE"},
        }
        self.killed: list[str] = []

    async def kill(self, cid: str) -> dict:
        self.killed.append(cid)
        return dict(self.response)


def _cleanup_run(state_final: dict[str, Any] | None = None) -> CollectedRun:
    return CollectedRun(
        conversation_id="conv_terminal",
        events=[],
        state_initial={},
        state_final=state_final or {"status": "FINISHED"},
        workspace_manifest={},
        preview=None,
        timeline=["RUNNING", "FINISHED"],
    )


def _fake_podman(
    monkeypatch,
    *,
    ps_stdout: str = "",
    volume_stdout: str = "",
    dangling_stdout: str = "",
) -> None:
    class _Result:
        returncode = 0

        def __init__(self, text: str) -> None:
            self.stdout = text

    def fake_run(*args, **kwargs):
        argv = args[0]
        if argv == ["podman", "ps", "--format", "{{.Names}}"]:
            return _Result(ps_stdout)
        if argv == ["podman", "volume", "ls", "--format", "{{.Name}}"]:
            return _Result(volume_stdout)
        if argv == [
            "podman",
            "volume",
            "ls",
            "--filter",
            "dangling=true",
            "--format",
            "{{.Name}}",
        ]:
            return _Result(dangling_stdout)
        raise AssertionError(f"unexpected podman command: {argv!r}")

    monkeypatch.setattr(_run_mod.subprocess, "run", fake_run)


class _H347StateTransport(FakeTransport):
    def __init__(self, db_path, *, on_state_read=None, **kwargs):
        super().__init__(db_path, **kwargs)
        self.state_reads = 0
        self.on_state_read = on_state_read

    async def get_json(self, path):
        if path.endswith("/state"):
            self.state_reads += 1
            if self.on_state_read is not None:
                self.on_state_read(self.state_reads)
        return await super().get_json(path)


def _h347_seed(db, *, tool="verify_appkit_app"):
    pending = action(3, tool, action_id="act_h347")
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING"), pending])
    return pending


def _h348_span(seq: int, phase: str, request_id: str = "req_h348") -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": "span",
        "span": "agent.step",
        "event": phase,
        "role": "agent_driver",
        "request_id": request_id,
    }


def _inspect_span(seq: object, *, label: str | None = None) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": "span",
        "span": "agent.step",
        "event": "end",
        "request_id": label or f"request-{seq}",
    }


def _inspect_snapshot(
    events: list[dict[str, Any]],
    *,
    dropped: object = 0,
    conversation_id: str = _CID,
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "event_count": len(events),
        "dropped_event_count": dropped,
        # Deliberately untrusted/stale: the aggregate must derive projections
        # exclusively from canonical events.
        "routing_decisions": [{"stale": True}],
        "spans": [{"stale": True}],
        "tool_scopes": [{"stale": True}],
        "progress_shadows": [{"stale": True}],
        "events": events,
    }


def _h348_attach(client: DiscoApiClient, *events: dict[str, Any]) -> None:
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot(list(events)))
    client._inspect_aggregations[_CID] = aggregation


class _InspectSnapshotTransport:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.raise_on_trace = False

    async def get_json(self, _path) -> tuple[int, object]:
        if self.raise_on_trace:
            raise ConnectionError("transient inspect failure")
        return 200, self.snapshot


def _inspect_client(transport: _InspectSnapshotTransport) -> DiscoApiClient:
    return DiscoApiClient(cast(Any, transport), db_path=":memory:", poll_interval_s=100.0)


class _ActiveReleaseTransport:
    """Conversation in a configurable state whose kill emits one final trace event."""

    def __init__(self, *, state: str = "RUNNING", kill_ack: bool = True) -> None:
        self.snapshot = _inspect_snapshot([_inspect_span(1), _inspect_span(2)])
        self.state = state
        self.kill_ack = kill_ack
        self.calls: list[str] = []

    async def get_json(self, path):
        if path.endswith("/state"):
            self.calls.append("state")
            return 200, {"execution_status": self.state}
        self.calls.append("trace")
        return 200, self.snapshot

    async def post_json(self, path, _body):
        if path.endswith("/kill"):
            self.calls.append("kill")
            # The still-active model flushes one final trace event as the stop lands.
            self.snapshot = _inspect_snapshot(
                [_inspect_span(1), _inspect_span(2), _inspect_span(3)]
            )
            if not self.kill_ack:
                return 503, {"error": "kill rejected"}
            return 200, {"killed": True, "state": {"execution_status": "IDLE"}}
        return 200, {}
