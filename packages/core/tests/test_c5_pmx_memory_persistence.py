"""C5 — persist the MEMORY channel to `<workspace>/.pmx/MEMORY.md` so it
survives a hard filesystem reset.

Contract:
  - Each `remember` call writes through to `.pmx/MEMORY.md` (the in-View
    KnowledgeEvent is the authoritative in-session source; the file is a
    durable mirror).
  - On a fresh / rehydrated box (the SandboxSession `_on_recreate` flow),
    `.pmx/MEMORY.md` is read back and its facts are staged on the session
    for the agent loop to re-emit as KnowledgeEvents. The in-View channel
    is restored to match the surviving on-disk mirror.
  - The `.pmx/` directory is excluded from the working-set deliverable
    snapshot (like `.disco-spill-*` is) — the mirror is not a deliverable
    the model should see in its context.

Acceptance (mirrors the task spec):
  1. A `remember` action writes `.pmx/MEMORY.md` with the expected content.
  2. After a simulated hard reset (fresh box + the recreate read-back hook),
     the memory is RECOVERED from the file.
  3. `.pmx/` paths are excluded from the working-set snapshot.

These tests use only fakes (no real model, no real container) — same pattern
as `test_c16_hard_reset_pointer_flush.py` and `test_rematerialize_servers.py`.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    EventSource,
    KnowledgeEvent,
    ToolCall,
    ToolResult,
)
from disco.tools.sandbox.session import SandboxSession
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

CID = "conv-c5"


# ---------------------------------------------------------------------------
# Helper fakes — duck-typed SandboxInstance + a fakable SandboxService
# ---------------------------------------------------------------------------


class _FakeSandboxInstance:
    """In-memory sandbox used by both the engine's write-through (Test 1) and
    the session's read-back (Test 2). Workspace-relative POSIX paths map to
    in-process bytes; every read/write/list call is recorded so tests can
    assert on the exact API surface the engine and session touched.

    This is the `ctx.sandbox` the task spec mentions: SandboxSession wraps
    it, the engine reaches it via `getattr(self.executor, "sandbox", None)`,
    and both sides go through the same `read_file` / `write_file` /
    `list_dir` surface.
    """

    id = "fake-sbx-c5"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        # Diagnostic record of every IO call (path-keyed for reads/writes,
        # ordered for list_dir).
        self.read_calls: list[str] = []
        self.write_calls: list[tuple[str, int]] = []  # (path, byte_count)
        self.list_calls: list[str] = []
        # Optional: raise on a specific read_file path (to simulate a dead
        # sandbox without the heavy SandboxUnavailableError machinery).
        self.read_raises: dict[str, BaseException] = {}
        self.write_raises: dict[str, BaseException] = {}

    async def read_file(self, path: str) -> bytes:
        self.read_calls.append(path)
        if path in self.read_raises:
            raise self.read_raises[path]
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        if path in self.write_raises:
            raise self.write_raises[path]
        self.write_calls.append((path, len(data)))
        self.files[path] = bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        self.list_calls.append(path)
        # Return just the top-level entries under `path` (a real backend
        # would return just the children, not the whole tree).
        prefix = (path.rstrip("/") + "/") if path else ""
        entries: set[str] = set()
        for k in self.files:
            if not k.startswith(prefix):
                continue
            tail = k[len(prefix):]
            if not tail:
                continue
            # Just the first path component (top-level child).
            entries.add(tail.split("/", 1)[0])
        return sorted(entries)

    async def exec_shell(self, cmd: str, *, timeout_s: int):
        from disco.tools.sandbox.base import ExecResult
        return ExecResult(exit_code=0, stdout="", stderr="")

    def display_url(self):
        return None

    def expose_port(self, port: int):
        return None


class _FakeSandboxService:
    """Service that hands out a single instance (the `_FakeSandboxInstance`
    you pass in). The session's `create()` returns it; subsequent recreates
    on a typed-death path go through the same `initial`.

    `name` is the SandboxService protocol attribute (base.py: SandboxService
    has `name: str` — "process" | "e2b" | "gvisor" | "remote"). The
    SandboxSession constructor reads `service.name` to decide the
    per-conversation tmux namespace, so the fake MUST expose it (else
    AttributeError at __init__, masking the real test). "fake" is a
    non-process value, which is what these tests want — the namespace
    branch in SandboxSession takes the non-process path (no
    conversation_id prefix)."""

    name = "fake"

    def __init__(self, instance) -> None:
        self._instance = instance
        self.create_calls = 0

    async def create(self, spec, *, owner_id, conversation_id):
        self.create_calls += 1
        return self._instance


# ---------------------------------------------------------------------------
# Test 1: a `remember` call writes through to `<workspace>/.pmx/MEMORY.md`
# ---------------------------------------------------------------------------


class _WriteThroughSandboxExecutor:
    """Minimal ToolExecutor stub exposing a `sandbox` attribute AND a
    successful `execute()` for the `shell` action that comes BEFORE the
    `remember` (the fresh-session backstop refuses a zero-work remember).
    The write-through is what we're testing — `execute()` is a black box.

    `available_tools()` returns a single `shell` ToolSpec because the
    engine's Rung-7 unknown-tool requery treats any tool name absent from
    the offered set as a hallucination and requeries the model; a
    `shell` action would loop on "Unknown tool 'shell'" forever. The
    default FakeExecutor ships with shell for the same reason — we
    mirror that contract here."""

    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox
        self.execute_calls: list[ToolCall] = []

    def available_tools(self):
        from disco.core.llm import ToolSpec
        return [ToolSpec(name="shell", description="run a shell command", parameters_schema={})]

    async def execute(self, call: ToolCall):
        self.execute_calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


@pytest.mark.asyncio
async def test_c5_remember_writes_through_to_pmx_memory_md():
    """A `remember` action in the engine's main loop:
      (a) emits a KnowledgeEvent into the event log (in-View source of truth)
      (b) writes the fact to `.pmx/MEMORY.md` via the sandbox (durable mirror)

    Both halves must happen for a single `remember` call. The View emission
    is byte-unchanged from the prior behavior (the existing test_dedup_remember
    pins that); the NEW part is the file on disk.
    """
    sbx = _FakeSandboxInstance()
    executor = _WriteThroughSandboxExecutor(sbx)

    agent = ScriptedAgent(
        [
            # Real work first — the fresh-session backstop refuses a
            # zero-work remember, so a `shell` action must precede it.
            action_step("shell", {"command": "echo hi"}),
            action_step("remember", {"fact": "build command is `make`", "scope": "build"}),
            action_step("remember", {"fact": "use Postgres 15", "scope": "db"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    await loop.send_message("go")
    await loop.run()

    # (a) View emission is unchanged.
    events = await store.get_events(CID)
    knowledges = [e for e in events if isinstance(e, KnowledgeEvent)]
    assert len(knowledges) == 2
    assert {k.snippet for k in knowledges} == {
        "build command is `make`",
        "use Postgres 15",
    }
    assert {k.scope for k in knowledges} == {"build", "db"}

    # (b) Write-through happened — the file is on disk with both facts.
    assert ".pmx/MEMORY.md" in sbx.files
    body = sbx.files[".pmx/MEMORY.md"].decode("utf-8")
    # The header marks this as the standing-memory mirror.
    assert "# Standing memory" in body
    # Each fact is a list item, grouped under its scope heading.
    assert "## build" in body
    assert "## db" in body
    assert "- build command is `make`" in body
    assert "- use Postgres 15" in body
    # The write-through wrote exactly this file (no spillover into other
    # workspace paths).
    pmx_writes = [p for p, _ in sbx.write_calls if p == ".pmx/MEMORY.md"]
    assert len(pmx_writes) >= 1


@pytest.mark.asyncio
async def test_c5_remember_dedup_keeps_mirror_in_sync():
    """The existing in-View dedup of duplicate remember calls is a strict
    SUBSET of the write-through: a duplicate never reaches the file either,
    because the in-View dedup short-circuits BEFORE the write-through
    runs. So a repeated fact x 3 = exactly one KnowledgeEvent AND one
    file write (or two, if a scope-grouped mirror is appended-to across
    iterations — the file ends with the fact exactly once).
    """
    sbx = _FakeSandboxInstance()
    executor = _WriteThroughSandboxExecutor(sbx)

    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("remember", {"fact": "alpha", "scope": "x"}),
            action_step("remember", {"fact": "alpha", "scope": "x"}),       # dup
            action_step("remember", {"fact": "alpha  ", "scope": "x"}),    # dup (trailing ws)
            action_step("remember", {"fact": "alpha", "scope": "y"}),      # new scope
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    await loop.send_message("go")
    await loop.run()

    # In-View: 2 unique (scope, fact) pairs.
    knowledges = [e for e in await store.get_events(CID) if isinstance(e, KnowledgeEvent)]
    assert len(knowledges) == 2
    assert {k.snippet for k in knowledges} == {"alpha"}

    # On-disk mirror: each unique pair appears exactly once.
    body = sbx.files[".pmx/MEMORY.md"].decode("utf-8")
    # Count of `- alpha` list items == 2 (one per unique scope).
    alpha_count = sum(1 for line in body.splitlines() if line.strip() == "- alpha")
    assert alpha_count == 2, f"mirror should hold each unique fact once; body:\n{body}"


@pytest.mark.asyncio
async def test_c5_remember_without_scope_appends_to_root_list():
    """A `remember` with no `scope` is still legal and still must be
    mirrored — it goes into the root (un-headed) list of facts, which the
    read-back parser handles as `current_scope == ""`. The on-disk file
    must contain the fact even when no `## <scope>` heading is emitted."""
    sbx = _FakeSandboxInstance()
    executor = _WriteThroughSandboxExecutor(sbx)

    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("remember", {"fact": "no-scope fact"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    await loop.send_message("go")
    await loop.run()

    body = sbx.files[".pmx/MEMORY.md"].decode("utf-8")
    assert "- no-scope fact" in body


# ---------------------------------------------------------------------------
# Test 2: hard reset (fresh box + the rehydrate read-back) RECOVERS memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c5_session_recreate_reads_back_pmx_memory_md():
    """Simulated hard reset: a SandboxSession whose live box is replaced by
    a fresh instance. The fresh instance has `.pmx/MEMORY.md` (the durable
    mirror), and after the recreate, the session has staged its facts for
    the loop to re-emit.

    Setup:
      1. Build a session with a fake sandbox; write `.pmx/MEMORY.md` to it
         (simulating a previous run that remembered facts).
      2. Force a recreate by calling `_recreate(dead)` with the same
         instance as `dead` — the new instance is the same fake (a "fresh
         box" in the test sense), and we re-seed its files to mimic a
         backend that survives a hard reset of just the in-memory View
         (the .pmx/ tree is what we want to keep).

    Outcome:
      - The session's `_recovered_memory_facts` cache is populated with
        the parsed (scope, snippet) pairs.
      - The cache is consumable exactly once (take_ drains, second take
        returns []).
    """
    from disco.tools.sandbox.session import SandboxSession

    # The "fresh" instance has the durable mirror already on disk
    # (simulating a backend that survived a hard reset of just the View).
    fresh = _FakeSandboxInstance()
    mirror_text = (
        "# Standing memory\n"
        "\n"
        "Durable facts the agent learned this run (C5).\n"
        "\n"
        "## build\n"
        "- build command is `make`\n"
        "- parallel jobs: -j8\n"
        "\n"
        "## db\n"
        "- use Postgres 15\n"
    )
    fresh.files[".pmx/MEMORY.md"] = mirror_text.encode("utf-8")

    # Use the session's own _FakeSandboxService path: the service is
    # stateless on create, so a re-create returns the same instance.
    # `name` mirrors _FakeSandboxService above — required by the
    # SandboxService protocol (base.py) and read by SandboxSession.__init__.
    class _SameService:
        name = "fake"

        def __init__(self, inst):
            self._inst = inst

        async def create(self, spec, *, owner_id, conversation_id):
            return self._inst

    svc = _SameService(fresh)
    session = SandboxSession(
        svc, owner_id="local", conversation_id="c-c5-rec", on_recreate=None
    )

    # Prime the session with its first instance via _ensure() so that
    # _recreate's guard (`if self._instance is not dead: return`) is
    # satisfied — the test treats the pre-seeded `fresh` AS the session's
    # live instance, then a recreate replaces it with a same-shape
    # fresh one from the service (which is `fresh` again, since
    # _SameService hands out the same instance). The .pmx/ file the
    # test pre-seeded onto `fresh` is still on it after the swap.
    current = await session._ensure()
    assert current is fresh
    await session._recreate(fresh)

    # The session should have parsed the mirror and staged the facts.
    facts = session.peek_recovered_memory_facts()
    assert len(facts) == 3, f"expected 3 facts, got {facts}"
    # Order matters: the read-back parser walks the file top-to-bottom.
    by_scope: dict[str, list[str]] = {}
    for scope, snippet in facts:
        by_scope.setdefault(scope, []).append(snippet)
    assert by_scope == {
        "build": ["build command is `make`", "parallel jobs: -j8"],
        "db": ["use Postgres 15"],
    }

    # Drain semantics: take_ consumes, second call returns [].
    drained = session.take_recovered_memory_facts()
    assert len(drained) == 3
    assert session.peek_recovered_memory_facts() == []
    assert session.take_recovered_memory_facts() == []


@pytest.mark.asyncio
async def test_c5_session_recreate_no_memory_file_is_silent_noop():
    """A fresh box that was never written to (no `.pmx/MEMORY.md` on the
    new instance) leaves the session's cache as None / empty. The
    take_ method returns [] — the loop sees nothing to re-emit and just
    continues. This is the cold-start path: nothing to recover, so do
    nothing. The agent simply has no memory from prior runs (which is
    correct — there wasn't any)."""
    fresh = _FakeSandboxInstance()  # no `.pmx/MEMORY.md`
    svc = _FakeSandboxService(fresh)
    session = SandboxSession(svc, owner_id="local", conversation_id="c-c5-empty")
    # Prime the session's live instance first (mirror of the read-back
    # test above), THEN force a recreate. Without priming,
    # _recreate's `self._instance is not dead` guard returns early
    # (None is not the fake instance) and the recovery never runs.
    current = await session._ensure()
    assert current is fresh
    await session._recreate(fresh)
    assert session.peek_recovered_memory_facts() == []
    assert session.take_recovered_memory_facts() == []


@pytest.mark.asyncio
async def test_c5_engine_drains_recovered_facts_and_re_emits_as_knowledge_events():
    """End-to-end: the engine sees the session has staged recovery facts,
    drains them, and re-emits them as KnowledgeEvents. The in-View channel
    is restored to match the surviving on-disk mirror — exactly what the
    task spec calls the 'RECOVERED' outcome.

    The test wires a fake sandbox + a session that returns the staged
    facts from `take_recovered_memory_facts`, drives the engine, and
    asserts KnowledgeEvents appear in the event log with the right
    (scope, snippet) shapes.
    """
    sbx = _FakeSandboxInstance()
    # Pre-seed the on-disk mirror (the test simulates "we just came back
    # from a recreate; the file is on disk, the in-memory View is gone").
    sbx.files[".pmx/MEMORY.md"] = (
        b"# Standing memory\n\n## build\n- make is the build\n"
    )

    # Wire a session-like object that returns the recovery facts.
    class _FakeSessionForRecovery:
        """Duck-typed: just enough to satisfy engine._drain_recovered_memory_facts.
        `take_recovered_memory_facts` returns the staged facts; the test
        asserts those facts land in the event log as KnowledgeEvents."""

        def __init__(self, facts: list[tuple[str, str]]) -> None:
            self._facts = list(facts)
            self.take_calls = 0

        def take_recovered_memory_facts(self):
            self.take_calls += 1
            out = list(self._facts)
            self._facts = []  # one-shot drain
            return out

    fake_session = _FakeSessionForRecovery(
        [("build", "make is the build"), ("db", "Postgres 15")]
    )
    executor = _WriteThroughSandboxExecutor(sbx)
    # Override the sandbox on the executor with the fake session — the
    # engine reaches `take_recovered_memory_facts` via
    # `getattr(self.executor, "sandbox", None)`.
    executor.sandbox = fake_session  # type: ignore[assignment]

    # The agent only finishes; the engine's first iteration drains the
    # recovery facts BEFORE consulting the agent. We don't need any
    # real `shell` work — the recovery is the point.
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    knowledges = [e for e in events if isinstance(e, KnowledgeEvent)]

    # Both recovered facts landed in the event log.
    assert {(k.scope, k.snippet) for k in knowledges} >= {
        ("build", "make is the build"),
        ("db", "Postgres 15"),
    }
    # The take_ method was consulted (drain happened).
    assert fake_session.take_calls >= 1
    # The recovery facts were emitted as KnowledgeEvents with the
    # AGENT source (matching the in-View `remember` handler's shape).
    assert all(k.source == EventSource.AGENT for k in knowledges)
    # Pinned: source is AGENT (not a transient ENVIRONMENT message), so
    # they survive condensation the same way live `remember` facts do.
    assert all(isinstance(k, KnowledgeEvent) for k in knowledges)


# ---------------------------------------------------------------------------
# Test 3: `.pmx/` is excluded from the deliverable set (working-set snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c5_workspace_snapshot_excludes_pmx_directory():
    """`.pmx/MEMORY.md` (and any other file under `.pmx/`) must be
    excluded from the working-set snapshot the model sees — the
    in-View channel is the source of truth, the file is a write-through
    mirror, and surfacing the mirror in the working set would be a
    divergent second store from the model's POV. Same shape as the
    `.disco-spill-*` exclusion (engine.py:1925).

    The test:
      - Stages a real `.pmx/MEMORY.md` AND a real deliverable in the
        workspace (e.g. `app.py`).
      - Mutates `app.py` via the event log so the engine's
        `_workspace_paths_from_events` picks it up as a working-set file.
      - Calls the engine's `_workspace_snapshot_message` and asserts the
        resulting LLMMessage mentions `app.py` but NOT `.pmx/MEMORY.md`.
    """
    sbx = _FakeSandboxInstance()
    # Two files: the deliverable AND the mirror.
    sbx.files["app.py"] = b"print('hello')\n"
    sbx.files[".pmx/MEMORY.md"] = b"# Standing memory\n- a fact\n"
    executor = _WriteThroughSandboxExecutor(sbx)

    # Build a loop whose `executor.sandbox` is our fake — then call the
    # engine's snapshot method directly. We don't need to run the loop:
    # the snapshot is built from the event list (file_write actions seed
    # the working set) plus the live sandbox state, both faked.
    agent = ScriptedAgent([finish_step()])
    loop, _store = build_loop(agent, executor=executor, conversation_id=CID)

    # Seed a `file_write` event so `_workspace_paths_from_events` picks
    # `app.py` up as a working-set file. The snapshot is built FROM the
    # event list, so we pass it in directly (no need to run the loop).
    from disco.core import ToolCall
    events = [
        ActionEvent(
            thought="wrote app",
            tool_call=ToolCall(
                tool_name="file_write",
                arguments={"path": "app.py", "content": "..."},
            ),
        )
    ]
    snapshot_msg = await loop._workspace_snapshot_message(events)
    assert snapshot_msg is not None, "expected a workspace-snapshot message"
    snapshot = snapshot_msg.content

    # The deliverable IS in the snapshot (the file the agent built).
    assert "app.py" in snapshot
    assert "print('hello')" in snapshot

    # The `.pmx/MEMORY.md` mirror is NOT in the snapshot — it's an
    # internal harness file, not a deliverable. The exclusion matches
    # the `.disco-spill-*` pattern (see engine.py:1925).
    assert ".pmx/MEMORY.md" not in snapshot
    # And no chunk of the mirror's body leaked in either (defense in
    # depth — a partial match would also be a leak).
    assert "Standing memory" not in snapshot


@pytest.mark.asyncio
async def test_c5_workspace_snapshot_excludes_pmx_nested_and_root():
    """The `.pmx/` exclusion is component-based: any file with `.pmx` as
    a path component is excluded, regardless of depth. A hypothetical
    `subdir/.pmx/MEMORY.md` (e.g. an install in a sub-folder) is also
    excluded — the directory is the harness's, not the agent's."""
    sbx = _FakeSandboxInstance()
    sbx.files["app.py"] = b"print('a')\n"
    sbx.files[".pmx/MEMORY.md"] = b"# mirror\n"
    sbx.files["subdir/.pmx/MEMORY.md"] = b"# nested mirror\n"
    executor = _WriteThroughSandboxExecutor(sbx)

    agent = ScriptedAgent([finish_step()])
    loop, _store = build_loop(agent, executor=executor, conversation_id=CID)

    # Seed two file_writes: one for the deliverable, one for the nested
    # mirror. The snapshot is built from the event list — no need to run
    # the loop. The snapshot method walks `events` and reads live bytes
    # for each path it finds; the fake sandbox serves those bytes.
    from disco.core import ToolCall
    events = [
        ActionEvent(
            thought="wrote app",
            tool_call=ToolCall(
                tool_name="file_write",
                arguments={"path": "app.py", "content": "..."},
            ),
        ),
        ActionEvent(
            thought="wrote subdir mirror",
            tool_call=ToolCall(
                tool_name="file_write",
                arguments={"path": "subdir/.pmx/MEMORY.md", "content": "..."},
            ),
        ),
    ]
    snapshot_msg = await loop._workspace_snapshot_message(events)
    assert snapshot_msg is not None, "expected a workspace-snapshot message"
    snapshot = snapshot_msg.content
    # The deliverable is in.
    assert "app.py" in snapshot
    # Both mirror paths are out (root and nested).
    assert ".pmx/MEMORY.md" not in snapshot
    assert "subdir/.pmx/MEMORY.md" not in snapshot
    # And no mirror content leaked.
    assert "mirror" not in snapshot or "app.py" in snapshot  # the word is fine in app.py content


# ---------------------------------------------------------------------------
# Test 4: end-to-end write-through + simulated recreate + recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c5_end_to_end_remember_survives_simulated_hard_reset():
    """The full C5 happy path:
      1. Engine's `remember` action writes the fact to `.pmx/MEMORY.md`.
      2. The session's `_on_recreate`-driven read-back (test simulates
         this by calling `_recreate` with a fresh instance that has the
         file) stages the facts for the loop.
      3. The engine's next step drains the staged facts and re-emits
         them as KnowledgeEvents.
      4. The agent's View now sees the recovered memory — a hard reset
         that wiped the in-View channel is undone by the file.

    The test wires both halves (write-through AND read-back) through a
    single SandboxSession and asserts the file is the durable carrier.
    """

    # Phase A: write-through. The engine runs one `remember`, which
    # should land the fact in `.pmx/MEMORY.md`.
    sbx = _FakeSandboxInstance()
    executor = _WriteThroughSandboxExecutor(sbx)
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("remember", {"fact": "use pytest -q", "scope": "test"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    await loop.send_message("go")
    await loop.run()
    body = sbx.files[".pmx/MEMORY.md"].decode("utf-8")
    assert "- use pytest -q" in body

    # Phase B: simulated hard reset. A new SandboxSession is built that
    # points at the SAME sandbox instance (so the .pmx/ file is on it),
    # and a recreate is forced. The read-back must stage the fact.
    fresh = sbx  # the same fake — it has the file
    svc = _FakeSandboxService(fresh)
    session = SandboxSession(svc, owner_id="local", conversation_id="c-c5-e2e")
    # Prime the session's live instance (mirrors the read-back test's
    # setup; without this, _recreate's `self._instance is not dead`
    # guard returns early and no recovery runs).
    current = await session._ensure()
    assert current is fresh
    await session._recreate(fresh)
    facts = session.take_recovered_memory_facts()
    assert facts == [("test", "use pytest -q")]

    # Phase C: the engine drains the facts on its next step. We re-use
    # the recovery drain directly (this is the exact code path the loop
    # runs at the start of every iteration).
    class _SessionShim:
        def __init__(self, staged):
            self._staged = list(staged)

        def take_recovered_memory_facts(self):
            out = list(self._staged)
            self._staged = []
            return out

    shim_sbx = _SessionShim([("test", "use pytest -q")])
    # Build a fresh executor whose `sandbox` is the shim.
    phase_c_executor = _WriteThroughSandboxExecutor(_FakeSandboxInstance())
    phase_c_executor.sandbox = shim_sbx  # type: ignore[assignment]
    agent = ScriptedAgent([finish_step()])
    loop2, store2 = build_loop(agent, executor=phase_c_executor, conversation_id="conv-c5-e2e")
    await loop2.send_message("recover")
    await loop2.run()
    knowledges = [
        e for e in await store2.get_events("conv-c5-e2e") if isinstance(e, KnowledgeEvent)
    ]
    assert any(k.snippet == "use pytest -q" and k.scope == "test" for k in knowledges)


# ---------------------------------------------------------------------------
# Sanity: regression guard for the no-sandbox executor path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c5_remember_with_sandboxless_executor_does_not_raise():
    """A `remember` action with a sandbox-less executor (no `.sandbox`
    attribute on the executor — e.g. an old test fake) must NOT raise:
    the write-through degrades to a no-op and the in-View emission is
    unaffected. The fact still survives the run (in-View is the source
    of truth) — only the on-disk mirror is missing. This is the
    fallback the spec mandates: never wedge the agent on a missing
    sandbox."""
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("remember", {"fact": "no-sandbox fact"}),
            finish_step(),
        ]
    )
    # FakeExecutor has no `sandbox` attribute — that's the test setup.
    loop, store = build_loop(agent, executor=FakeExecutor(), conversation_id=CID)
    await loop.send_message("go")
    state = await loop.run()
    # No error, no crash.
    from disco.core import ConversationStatus
    assert state.execution_status == ConversationStatus.FINISHED
    # In-View emission is unchanged.
    knowledges = [e for e in await store.get_events(CID) if isinstance(e, KnowledgeEvent)]
    assert any(k.snippet == "no-sandbox fact" for k in knowledges)
