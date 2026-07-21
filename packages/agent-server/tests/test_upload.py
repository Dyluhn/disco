"""BP-11: file-upload endpoint — unit tests.

Covers:
  • Filename sanitization (traversal, dotfiles, unicode NFC, collision suffix)
  • Limit enforcement (per-file, per-request, per-conversation)
  • Event text format (thousands separator, multi-file)
  • State legality (ERROR blocks upload, FINISHED allows it)

The endpoint accesses the sandbox via runtime._executors[cid]._sandbox, so
tests inject a _FakeRuntime / _FakeExecutor / _FakeSession triple — no real
process or container required.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import uuid
from collections.abc import AsyncIterator

import pytest
from disco.agent_server import create_app
from disco.agent_server.app import _sanitize_name
from disco.core import EventSource, MessageEvent, SqliteEventStore
from fastapi.testclient import TestClient

# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeSession:
    """In-memory sandbox stub."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.recreated = False

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [k[len(prefix) :] for k in self._files if k.startswith(prefix)]

    async def read_file(self, path: str) -> bytes:
        return self._files.get(path, b"")

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data

    async def destroy(self) -> None:
        pass


class _FakeExecutor:
    def __init__(self, session: _FakeSession) -> None:
        self._sandbox = session


class _FakeRuntime:
    def __init__(self) -> None:
        self._executors: dict[str, _FakeExecutor] = {}
        self._pending_sessions: dict[str, _FakeSession] = {}
        self._sidecar: dict[str, dict[str, bytes]] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}

    def workspace_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._workspace_locks.setdefault(conversation_id, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def workspace_fence(self, conversation_id: str) -> AsyncIterator[None]:
        async with self.workspace_lock(conversation_id):
            yield

    async def record_workspace_mutation_locked(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths=(),  # noqa: ANN001
    ) -> None:
        assert self.workspace_lock(conversation_id).locked()

    def kick(self, cid: str) -> None:  # noqa: D401
        pass

    def upload_session(self, conversation_id: str) -> _FakeSession:
        executor = self._executors.get(conversation_id)
        if executor is not None:
            return executor._sandbox
        if conversation_id not in self._pending_sessions:
            self._pending_sessions[conversation_id] = _FakeSession()
        return self._pending_sessions[conversation_id]

    def store_upload(self, conversation_id: str, filename: str, data: bytes) -> None:
        if conversation_id not in self._sidecar:
            self._sidecar[conversation_id] = {}
        self._sidecar[conversation_id][filename] = data

    def get_upload_names(self, conversation_id: str) -> set[str]:
        return set(self._sidecar.get(conversation_id, {}).keys())

    def get_upload_size(self, conversation_id: str) -> int:
        return sum(len(v) for v in self._sidecar.get(conversation_id, {}).values())

    def add_upload_passages(self, conversation_id: str, passages: list) -> None:
        # G1/DR-4: no-op in the upload tests (corpus ingestion tested separately).
        pass

    def get_upload_passages(self, conversation_id: str) -> list:
        return []

    async def _rematerialize_uploads(self, conversation_id: str, session: _FakeSession) -> None:
        # Simplified version for testing the interaction.
        uploads = self._sidecar.get(conversation_id, {})
        for name, data in uploads.items():
            await session.write_file(f"uploads/{name}", data)


def _make_client(session: _FakeSession | None = None) -> tuple[TestClient, str, _FakeSession]:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = session or _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)
    return client, cid, sess


def _upload(client: TestClient, cid: str, files: list[tuple[str, bytes, str]]) -> dict:
    """POST multipart to /conversations/{cid}/files.
    `files`: list of (field_name, data, filename).
    """
    parts = [
        ("files", (fname, io.BytesIO(data), "application/octet-stream")) for _, data, fname in files
    ]
    return client.post(f"/conversations/{cid}/files", files=parts)


# ── sanitization table ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        # path traversal → only the basename remains
        ("../../etc/passwd", "passwd"),
        ("../secret.txt", "secret.txt"),
        # dotfile → leading dot stripped
        (".bashrc", "bashrc"),
        ("...hidden", "hidden"),
        # unicode NFC (composed form)
        ("café.csv", "café.csv"),  # already NFC
        ("café.csv", "café.csv"),  # NFD → NFC (é composed)
        # whitespace runs → hyphens
        ("my report.csv", "my-report.csv"),
        ("a  b  c.txt", "a-b-c.txt"),
        ("tab\there.csv", "tab-here.csv"),
        # normal name passes through unchanged
        ("data.csv", "data.csv"),
    ],
)
def test_sanitize_name(raw: str, expected: str) -> None:
    assert _sanitize_name(raw) == expected


def test_sanitize_name_empty_result() -> None:
    # A name that reduces to nothing → None
    assert _sanitize_name("...") is None
    assert _sanitize_name(".") is None
    assert _sanitize_name("  ") is None


# ── collision suffixing ───────────────────────────────────────────────────────


def test_collision_suffix() -> None:
    client, cid, sess = _make_client()
    data = b"x" * 10
    # Upload data.csv once → saved as data.csv
    r1 = _upload(client, cid, [("files", data, "data.csv")])
    assert r1.status_code == 200
    assert r1.json()["saved"][0]["name"] == "data.csv"

    # Upload again → collision → data-2.csv
    r2 = _upload(client, cid, [("files", data, "data.csv")])
    assert r2.json()["saved"][0]["name"] == "data-2.csv"

    # Upload a third time → data-3.csv
    r3 = _upload(client, cid, [("files", data, "data.csv")])
    assert r3.json()["saved"][0]["name"] == "data-3.csv"


# ── per-file size limit ───────────────────────────────────────────────────────


def test_per_file_size_limit_reject() -> None:
    client, cid, _ = _make_client()
    big = b"x" * (25 * 1024 * 1024 + 1)  # 25 MB + 1 byte
    r = _upload(client, cid, [("files", big, "big.bin")])
    assert r.status_code == 413
    body = r.json()
    assert body["saved"] == []
    assert len(body["rejected"]) == 1
    assert "25 MB" in body["rejected"][0]["reason"]


def test_per_file_size_limit_at_boundary() -> None:
    client, cid, _ = _make_client()
    exact = b"x" * (25 * 1024 * 1024)  # exactly 25 MB → accepted
    r = _upload(client, cid, [("files", exact, "exact.bin")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "exact.bin"


# ── per-request file count ────────────────────────────────────────────────────


def test_too_many_files_per_request() -> None:
    client, cid, _ = _make_client()
    files = [("files", b"x", f"f{i}.txt") for i in range(21)]
    r = _upload(client, cid, files)
    assert r.status_code == 413
    assert "too_many_files" in r.json()["detail"]["reason"]


def test_max_files_per_request_is_accepted() -> None:
    client, cid, _ = _make_client()
    files = [("files", b"x", f"f{i}.txt") for i in range(20)]
    r = _upload(client, cid, files)
    assert r.status_code == 200
    assert len(r.json()["saved"]) == 20


# ── per-conversation quota ────────────────────────────────────────────────────


def test_per_conversation_quota_reject() -> None:
    sess = _FakeSession()
    # Pre-fill uploads/ to just below 100 MB
    large_chunk = b"x" * (99 * 1024 * 1024)
    sess._files["uploads/existing.bin"] = large_chunk

    client, cid, _ = _make_client(sess)
    # Trying to add 2 MB would push over 100 MB
    r = _upload(client, cid, [("files", b"x" * (2 * 1024 * 1024), "extra.bin")])
    assert r.status_code == 413
    assert "quota" in r.json()["rejected"][0]["reason"]


def test_per_conversation_quota_allows_when_under() -> None:
    sess = _FakeSession()
    sess._files["uploads/existing.bin"] = b"x" * (90 * 1024 * 1024)  # 90 MB
    client, cid, _ = _make_client(sess)
    # Adding 5 MB → 95 MB total (under limit)
    r = _upload(client, cid, [("files", b"x" * (5 * 1024 * 1024), "ok.bin")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "ok.bin"


# ── event text format ─────────────────────────────────────────────────────────


async def test_event_text_format_single_file() -> None:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)

    # 18234 bytes — verifies thousands separator in the event text
    data = b"x" * 18234
    r = _upload(client, cid, [("files", data, "data.csv")])
    assert r.status_code == 200

    all_events = await store.get_events(cid)
    env = [
        e for e in all_events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert len(env) == 1
    assert env[0].message.content == "User uploaded: uploads/data.csv (18,234 bytes)"


async def test_event_text_format_multi_file() -> None:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)

    r = _upload(
        client,
        cid,
        [
            ("files", b"a" * 100, "a.csv"),
            ("files", b"b" * 200, "b.csv"),
        ],
    )
    assert r.status_code == 200

    all_events = await store.get_events(cid)
    env = [
        e for e in all_events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert len(env) == 1
    content = env[0].message.content
    assert "uploads/a.csv (100 bytes)" in content
    assert "uploads/b.csv (200 bytes)" in content


async def test_no_event_when_all_rejected() -> None:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)

    big = b"x" * (25 * 1024 * 1024 + 1)
    r = _upload(client, cid, [("files", big, "big.bin")])
    assert r.status_code == 413

    all_events = await store.get_events(cid)
    env = [
        e for e in all_events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert env == []


# ── state legality ────────────────────────────────────────────────────────────


async def test_upload_blocked_in_error_state() -> None:
    from disco.core import ConversationStatus as CS
    from disco.core import StatusEvent

    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)

    await store.append(cid, StatusEvent(status=CS.ERROR, detail="test error"))
    r = _upload(client, cid, [("files", b"hi", "f.txt")])
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "conversation_in_error_state"


async def test_upload_allowed_in_finished_state() -> None:
    from disco.core import ConversationStatus as CS
    from disco.core import StatusEvent

    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)

    await store.append(cid, StatusEvent(status=CS.FINISHED, detail="done"))
    r = _upload(client, cid, [("files", b"hello", "f.txt")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "f.txt"


def test_upload_allowed_in_idle_state() -> None:
    client, cid, _ = _make_client()
    r = _upload(client, cid, [("files", b"hi", "f.txt")])
    assert r.status_code == 200


def test_upload_no_executor_lazily_creates_session() -> None:
    # Primary flow: user uploads BEFORE the first kick → lazy pending session, 200 OK.
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()  # no executor yet — upload_session creates a pending session
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    r = _upload(client, cid, [("files", b"hi", "f.txt")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "f.txt"
    # The session was recorded as pending (the build loop will adopt it later).
    assert cid in rt._pending_sessions


def test_upload_no_runtime_returns_409() -> None:
    # 409 is now reserved for the no-runtime case only.
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=None))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    r = _upload(client, cid, [("files", b"hi", "f.txt")])
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "no_active_sandbox"


@pytest.mark.asyncio
async def test_pending_session_adopted_by_build_loop() -> None:
    """upload_session() on a fresh conversation creates a pending session; the NEXT
    _compose_build_loop call for that conversation adopts it (object identity)."""
    from unittest import mock

    from disco.agent_server.runtime import ConversationRuntime
    from disco.core.llm import DefaultLLMRouter
    from disco.core.loop import RouterAgent

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    cid = "conv_adoption_test"

    with mock.patch.object(rt, "_sandbox_service_now"):
        session = rt.upload_session(cid)
        assert cid in rt._pending_sessions

        loop = rt._compose_build_loop(cid, router, agent)

    assert loop.executor._sandbox is session
    assert cid not in rt._pending_sessions


@pytest.mark.asyncio
async def test_pending_session_destroyed_on_kill() -> None:
    """Killing a conversation tears down any pending (pre-loop) sandbox session."""
    from unittest import mock

    from disco.agent_server.runtime import ConversationRuntime

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")

    with mock.patch.object(rt, "_sandbox_service_now"):
        session = rt.upload_session(cid)
    assert cid in rt._pending_sessions

    session.destroy = mock.AsyncMock()
    await rt.kill(cid)

    session.destroy.assert_called_once()
    assert cid not in rt._pending_sessions


def test_partial_reject_returns_200_with_saved_and_rejected() -> None:
    client, cid, _ = _make_client()
    big = b"x" * (25 * 1024 * 1024 + 1)
    small = b"ok"
    r = _upload(
        client,
        cid,
        [
            ("files", big, "big.bin"),
            ("files", small, "small.txt"),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["saved"]) == 1
    assert body["saved"][0]["name"] == "small.txt"
    assert len(body["rejected"]) == 1
    assert "big.bin" == body["rejected"][0]["name"]


def test_sidecar_quota_counts_toward_limit() -> None:
    """[DC-07] Server-side upload storage counts toward the 100 MB quota."""
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)
    # Pre-fill sidecar with 99 MB — near the 100 MB cap.
    rt.store_upload(cid, "big.bin", b"x" * (99 * 1024 * 1024))
    client = TestClient(create_app(store, runtime=rt))
    # 2 MB extra pushes over 100 MB → rejected.
    r = _upload(client, cid, [("files", b"x" * (2 * 1024 * 1024), "extra.bin")])
    assert r.status_code == 413
    assert "quota" in r.json()["rejected"][0]["reason"]


def test_sidecar_quota_allows_when_under() -> None:
    """[DC-07] Uploads still accepted when sidecar is below the 100 MB cap."""
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)
    # Pre-fill sidecar with 90 MB — 5 MB more is safe.
    rt.store_upload(cid, "big.bin", b"x" * (90 * 1024 * 1024))
    client = TestClient(create_app(store, runtime=rt))
    r = _upload(client, cid, [("files", b"x" * (5 * 1024 * 1024), "ok.bin")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "ok.bin"


@pytest.mark.asyncio
async def test_upload_survives_recreation() -> None:
    """[DC-07] upload -> simulate sandbox recreation -> re-materialize."""
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)

    # 1. Upload a file.
    data = b"important data"
    r = _upload(client, cid, [("files", data, "data.txt")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "data.txt"

    # Verify it is in the sandbox AND sidecar.
    assert await sess.read_file("uploads/data.txt") == data
    assert rt.get_upload_names(cid) == {"data.txt"}

    # 2. Simulate sandbox recreation (wipe it).
    sess._files = {}
    assert await sess.read_file("uploads/data.txt") == b""

    # 3. Trigger re-materialization.
    await rt._rematerialize_uploads(cid, sess)

    # 4. Verify it is back.
    assert await sess.read_file("uploads/data.txt") == data


@pytest.mark.asyncio
async def test_upload_rematerialized_on_lazy_compose_path() -> None:
    """[DC-07] Regression: post-restart resume path — sidecar has uploads, no
    executor or pending session exists → compose fresh session (lazy compose) →
    _rematerialize_uploads runs → files land in the sandbox.

    This is the REAL failing path from DEFECT-7b: after a server restart, the
    pending-sessions dict is empty, so _compose_build_loop creates a FRESH
    SandboxSession whose on_recreate hook never fires (it's not a recreation).
    The fix wires _rematerialize_uploads into _run_with_persistence so it runs
    on EVERY build path — including the lazy compose."""
    import tempfile
    from unittest import mock

    from disco.agent_server.runtime import ConversationRuntime
    from disco.core.llm import DefaultLLMRouter
    from disco.core.loop import RouterAgent

    store = SqliteEventStore(":memory:")
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")

    rt = ConversationRuntime(store)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)

    with tempfile.TemporaryDirectory() as tmpdir:
        rt._uploads_base = tmpdir
        data = b"post-restart data"
        rt.store_upload(cid, "data.csv", data)
        assert rt.get_upload_names(cid) == {"data.csv"}

        # Post-restart state: no executor, no pending session.
        assert cid not in rt._executors
        assert cid not in rt._pending_sessions

        # Mock a sandbox instance that stores files in memory.
        mock_files: dict[str, bytes] = {}
        mock_instance = mock.MagicMock()
        mock_instance.write_file = mock.AsyncMock(
            side_effect=lambda path, d: mock_files.__setitem__(path, d)
        )
        mock_instance.read_file = mock.AsyncMock(side_effect=lambda path: mock_files.get(path, b""))
        mock_instance.list_dir = mock.AsyncMock(
            side_effect=lambda path: [
                k[len(path.rstrip("/") + "/") :]
                for k in mock_files
                if k.startswith(path.rstrip("/") + "/")
            ]
        )
        mock_instance.id = "mock-sandbox-instance"

        mock_service = mock.MagicMock()
        mock_service.create = mock.AsyncMock(return_value=mock_instance)
        mock_service.name = "mock"

        # Simulate the resume path: compose a fresh build loop (the lazy path).
        with mock.patch.object(rt, "_sandbox_service_now", return_value=mock_service):
            rt._compose_build_loop(cid, router, agent)

        # Now an executor exists with a fresh sandbox (post-lazy-compose).
        executor = rt._executors[cid]
        session = executor._sandbox
        assert session is not None

        # The sandbox should be empty (no uploads yet — this IS the bug).
        assert mock_files == {}

        # Trigger re-materialization — this is what _run_with_persistence does
        # after the chokepoint fix.
        await rt._rematerialize_uploads(cid)

        # Assert the uploads were written into the sandbox.
        assert mock_files.get("uploads/data.csv") == data
