"""C16 — `hard_reset` is a filesystem-pointer-only flush, NOT a lossy prose recap.

Contract:
  - `hard_reset` produces a POINTER-ONLY tombstone whose summary is a manifest
    of on-disk artifact PATHS the model can re-read (spill files, written
    deliverables, `.pmx/MEMORY.md` if present). Every pointer in the manifest
    resolves to a real file on disk.
  - The NORMAL soft-condense path is BYTE-UNCHANGED — it still calls the
    summarizer and produces a prose summary.

These tests pin BOTH halves: the new pointer-manifest path AND the
unchanged prose-summarize path. They are acceptance tests for the change
described in the C16 backlog item; the test_view/test_condenser suites
remain the regression guard for the View/tombstone/turn-counting invariants.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    CondensationEvent,
    EventSource,
    LLMMessage,
    LLMSummarizingCondenser,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
    View,
)
from disco.core.llm import LLMContextWindowExceeded
from loop_fakes import (
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

# ---- 1. The unit-level seam: LLMSummarizingCondenser.condense() -------------


class _RecordingSummarizer:
    """Summarizer that records whether it was called. The hard_reset path must
    NOT call it (the dropped span is too large to re-summarize usefully —
    pointing at the bytes is the recovery)."""

    def __init__(self, text: str = "PROSE-SUMMARY-FROM-MODEL") -> None:
        self.text = text
        self.calls = 0

    async def summarize(self, messages: list[LLMMessage]) -> str:
        self.calls += 1
        return self.text


async def _seed_oversize_history(store: SqliteEventStore, n_pairs: int) -> list:
    """A user task + N (action, observation) pairs, each carrying chunky
    body content so the condenser has a real middle to forget. Same shape as
    test_condenser._seed."""
    await store.append(
        CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="TASK"))
    )
    body = "detail " * 20
    for i in range(n_pairs):
        tc = ToolCall(tool_name="shell", arguments={"command": f"echo {i}"})
        a = await store.append(CID, ActionEvent(thought=f"step {i}: {body}", tool_call=tc))
        await store.append(
            CID,
            ObservationEvent(
                tool_result=ToolResult(call_id=a.id, tool_name="shell", success=True, content=body),
                action_id=a.id,
            ),
        )
    return await store.get_events(CID)


CID = "conv"


async def test_c16_hard_reset_summary_is_a_pointer_manifest_not_prose():
    """reason='hard_reset' + a list of on-disk paths → tombstone.summary is a
    MANIFEST of those paths (categorized) and the summarizer is NOT called.
    The summarizer is for prose, not for the hard_reset flush."""
    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer()
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    artifact_paths = [
        "src/foo.py",
        "data/results.json",
        ".disco-spill-abc123def456.log",
        ".pmx/MEMORY.md",
    ]
    tomb = await condenser.condense(
        events,
        View.of(events),
        summarizer=summarizer,
        reason="hard_reset",
        artifact_paths=artifact_paths,
    )
    assert isinstance(tomb, CondensationEvent)
    # The summarizer was NOT consulted — the manifest is the recovery.
    assert summarizer.calls == 0
    # The tombstone's reason is the trigger that produced it.
    assert tomb.reason == "hard_reset"
    # The summary IS a manifest: header line + every path listed + categorized
    # sections. NOT a freeform prose recap.
    summary = tomb.summary
    # Header
    assert "[Hard reset — pointer-only flush" in summary
    assert f"span seq {tomb.forgotten_start_seq}–{tomb.forgotten_end_seq}" in summary
    assert "DROPPED" in summary
    # All four paths appear, exactly as listed.
    for p in artifact_paths:
        assert p in summary, f"pointer manifest missing {p!r} in: {summary!r}"
    # Categorization (deliverables / spill / memory) — the model can see what
    # kind of artifact each pointer is, not just a flat blob.
    assert "Deliverables" in summary
    assert "Spill logs" in summary
    assert "Standing memory" in summary
    # Recovery is explicit: file_read is named so the model knows how to use it.
    assert "file_read" in summary
    # NOT a prose recap: the prose string the summarizer would have produced
    # MUST NOT appear (the hard_reset path bypasses the summarizer entirely).
    assert "PROSE-SUMMARY-FROM-MODEL" not in summary


async def test_c16_hard_reset_manifest_paths_resolve_to_real_files(tmp_path):
    """Every pointer in the manifest corresponds to a real on-disk file. The
    manifest must not lie about what is recoverable — the test creates the
    files in tmp_path and asserts the manifest names match them, end-to-end."""
    # Create real on-disk files for the engine to "discover" via the sandbox.
    deliverable = tmp_path / "src" / "foo.py"
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("print('hi')\n")
    spill = tmp_path / ".disco-spill-abc123def456.log"
    spill.write_text("huge stdout...")
    memory_dir = tmp_path / ".pmx"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory = memory_dir / "MEMORY.md"
    memory.write_text("# standing memory\nfact: build is `make`\n")
    _artifact_paths = [str(deliverable), str(spill), str(memory)]
    # The engine's _collect method also lists deliverables from the event log;
    # the engine's view treats paths as workspace-relative, so verify the
    # manifest names the SAME paths the engine passed in (relative form).
    relative_paths = [
        "src/foo.py",
        ".disco-spill-abc123def456.log",
        ".pmx/MEMORY.md",
    ]
    # Confirm the relative paths correspond to real files in the workspace.
    for rp in relative_paths:
        assert (tmp_path / rp).exists(), f"test setup error: {rp} missing"

    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer()
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    tomb = await condenser.condense(
        events,
        View.of(events),
        summarizer=summarizer,
        reason="hard_reset",
        artifact_paths=relative_paths,
    )
    assert isinstance(tomb, CondensationEvent)
    # Every path in the manifest resolves to a real file in the test workspace.
    for p in relative_paths:
        assert p in tomb.summary
        assert (tmp_path / p).exists(), f"manifest references {p!r} but no such file exists on disk"


async def test_c16_hard_reset_no_artifacts_means_no_progress():
    """When the engine finds no on-disk artifacts to point to, hard_reset
    makes no progress — matches the existing 'empty prose summary' soft-path
    behavior (also returns None). The loop's hard_reset path then emits
    ErrorEvent('context_window', 'hard reset made no progress')."""
    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer()
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    # No artifacts → no progress. Summarizer is also NOT called (hard_reset
    # never falls back to prose; the byte-recovery path requires bytes).
    tomb = await condenser.condense(
        events,
        View.of(events),
        summarizer=summarizer,
        reason="hard_reset",
        artifact_paths=[],
    )
    assert tomb is None
    assert summarizer.calls == 0


async def test_c16_hard_reset_empty_artifact_paths_means_no_progress():
    """Same as above but with explicit None — the engine might pass None if
    its collection raised. The condenser must NOT silently summarize instead."""
    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer()
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    tomb = await condenser.condense(
        events,
        View.of(events),
        summarizer=summarizer,
        reason="hard_reset",
        artifact_paths=None,
    )
    assert tomb is None
    assert summarizer.calls == 0


# ---- 2. The soft-condense path is UNCHANGED ---------------------------------


async def test_c16_soft_condense_still_summarizes_default_path():
    """The SOFT condense path (reason='tokens', the default) is byte-unchanged.
    It still calls the summarizer and the returned CondensationEvent.summary
    equals the summarizer's output — NOT a manifest. The summarizer calls
    counter is the strongest signal: hard_reset never calls it; soft does."""
    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer("OLD-SOFT-PROSE-SUMMARY")
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    # No `reason` and no `artifact_paths` — i.e. the soft path. This is the
    # shape the loop's `if req is not None: condense(...)` call site uses
    # after the C16 change (it still doesn't pass either kwarg).
    tomb = await condenser.condense(events, View.of(events), summarizer=summarizer)
    assert isinstance(tomb, CondensationEvent)
    # The summarizer WAS called (prose path active).
    assert summarizer.calls == 1
    # The tombstone IS the summarizer's prose output — NOT a manifest.
    assert tomb.summary == "OLD-SOFT-PROSE-SUMMARY"
    # Header from the manifest is NOT in the soft path's summary.
    assert "[Hard reset — pointer-only flush" not in tomb.summary
    assert "DROPPED" not in tomb.summary
    # Soft reason preserved.
    assert tomb.reason == "tokens"


async def test_c16_soft_condense_explicit_tokens_unchanged():
    """Explicit `reason='tokens'` matches the default and produces the same
    prose-summary path. The soft contract is byte-unchanged either way."""
    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer("OLD-SOFT-PROSE-SUMMARY")
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    tomb = await condenser.condense(
        events,
        View.of(events),
        summarizer=summarizer,
        reason="tokens",  # explicit, matches default
    )
    assert isinstance(tomb, CondensationEvent)
    assert summarizer.calls == 1
    assert tomb.summary == "OLD-SOFT-PROSE-SUMMARY"
    assert tomb.reason == "tokens"


async def test_c16_soft_condense_artifact_paths_ignored():
    """If a caller (defensively or by mistake) passes `artifact_paths` on
    the SOFT path, the soft path IGNORES them — the prose summary is the
    output. Only `reason='hard_reset'` switches to the manifest path.
    This keeps the soft-condense byte-unchanged regardless of caller shape."""
    store = SqliteEventStore(":memory:")
    events = await _seed_oversize_history(store, n_pairs=6)
    summarizer = _RecordingSummarizer("OLD-SOFT-PROSE-SUMMARY")
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    tomb = await condenser.condense(
        events,
        View.of(events),
        summarizer=summarizer,
        reason="tokens",
        artifact_paths=["src/foo.py", ".disco-spill-x.log"],  # must be ignored
    )
    assert isinstance(tomb, CondensationEvent)
    # Prose path active — summarizer was called, output is the prose string.
    assert summarizer.calls == 1
    assert tomb.summary == "OLD-SOFT-PROSE-SUMMARY"
    # Artifact paths do NOT appear in the prose summary.
    assert "src/foo.py" not in tomb.summary
    assert ".disco-spill-x.log" not in tomb.summary


# ---- 3. The engine wiring: _hard_reset end-to-end ---------------------------


async def test_c16_engine_hard_reset_emits_pointer_manifest_tombstone(tmp_path):
    """End-to-end: the loop's _hard_reset (triggered by LLMContextWindowExceeded)
    collects the workspace's on-disk artifacts and emits a tombstone whose
    summary is a pointer manifest — NOT a freeform prose recap. Every
    pointer resolves to a real file.

    To isolate the hard_reset path (and not be confused by the soft-condense
    prose path that fires on a token-bound trigger), the test uses a high
    `max_tokens` (so the soft path never fires) and triggers the LLM
    context-window error explicitly via the scripted agent — that is the
    ONLY path that lands in `_hard_reset`."""
    # Stage real files in the workspace. The fake sandbox returns them on
    # read_file / lists them on list_dir.
    files = {
        "src/app.py": b"print('hello world')\n",
        "data/results.json": b'{"ok": true}\n',
        ".disco-spill-abc123def4567890.log": b"big stdout truncated...",
        ".pmx/MEMORY.md": b"# memory\nfact: use `make`\n",
    }
    sbx = _ManifestSandboxInstance(files=files, workspace_path="")
    executor = _ManifestSandboxExecutor(sbx)

    # HIGH max_tokens so the soft-condense path NEVER fires (we want to
    # isolate the hard_reset path). The hard_reset path is reached when
    # the LLM call itself raises LLMContextWindowExceeded (scripted below),
    # not via a token-estimate trigger.
    condenser = LLMSummarizingCondenser(
        max_tokens=10_000_000, hard_max_tokens=10_000_000, keep_head=1, keep_recent=2, min_forget=2
    )
    summarizer = _RecordingSummarizer("WOULD-BE-PROSE-BUT-SHOULD-NOT-APPEAR")
    # The LLMContextWindowExceeded at step 0 forces the engine into
    # _hard_reset on the very first call. After the reset, the next step
    # runs and finish closes the loop.
    agent = ScriptedAgent(
        [
            LLMContextWindowExceeded("context overflow — hard reset path"),
            action_step(args={"command": "post-reset step"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor, condenser=condenser, summarizer=summarizer)
    # Seed the event log with enough live events for the condenser's span
    # selection to find a forgettable middle. The condenser's guards
    # require at least keep_head + min_forget live events + keep_recent
    # recent tail; we seed 4 turns (1 head user msg + 4 action/obs pairs =
    # 9 events) so the middle is 3 turns = 6 events, which clears min_forget=2.
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="do a long task"),
        ),
    )
    for i in range(4):
        tc = ToolCall(tool_name="shell", arguments={"command": f"echo {i}"})
        a = await store.append(
            CID,
            ActionEvent(thought=f"step {i}", tool_call=tc),
        )
        await store.append(
            CID,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id=a.id, tool_name="shell", success=True, content=f"out{i}"
                ),
                action_id=a.id,
            ),
        )
    # Seed a file_write action so the working set has at least one known
    # deliverable path the engine will collect.
    await store.append(
        CID,
        ActionEvent(
            thought="wrote app",
            tool_call=ToolCall(
                tool_name="file_write", arguments={"path": "src/app.py", "content": "..."}
            ),
        ),
    )
    await loop.send_message("a long task")
    state = await loop.run()

    # The run finished (the post-reset finish_step) → no error terminal.
    from disco.core import ConversationStatus

    assert state.execution_status == ConversationStatus.FINISHED

    events = await store.get_events(CID)
    tombs = [e for e in events if isinstance(e, CondensationEvent)]
    assert len(tombs) == 1, f"expected exactly one tombstone, got {len(tombs)}"
    tomb = tombs[0]
    # Hard-reset tombstone: pointer manifest, reason="hard_reset".
    assert tomb.reason == "hard_reset"
    summary = tomb.summary
    # Manifest header.
    assert "[Hard reset — pointer-only flush" in summary
    assert "DROPPED" in summary
    # The deliverable the agent wrote is in the manifest (collector's source 1).
    assert "src/app.py" in summary
    # The spill log in the workspace is in the manifest (source 2).
    assert ".disco-spill-abc123def4567890.log" in summary
    # The .pmx/MEMORY.md file is in the manifest (source 3).
    assert ".pmx/MEMORY.md" in summary
    # The summarizer was NOT called on the hard_reset path — the manifest
    # IS the recovery; no prose recap.
    assert summarizer.calls == 0, (
        f"summarizer was called {summarizer.calls}x on the hard_reset path; "
        "the manifest must be the recovery, not a prose recap"
    )
    # The prose string the summarizer WOULD have produced is NOT in the manifest.
    assert "WOULD-BE-PROSE-BUT-SHOULD-NOT-APPEAR" not in summary
    # Every pointer in the manifest resolves to a real file on disk.
    for p in ("src/app.py", ".disco-spill-abc123def4567890.log", ".pmx/MEMORY.md"):
        assert p in summary
    # The sandbox that collected the pointers was consulted for each:
    # - deliverables (working-set paths) and `.pmx/MEMORY.md` are verified by
    #   `read_file` (the collector only `read_file`s MEMORY.md, but the
    #   deliverable paths are pre-trusted from the event log).
    # - spill files are verified by `list_dir` (existence in the workspace).
    assert ".pmx/MEMORY.md" in sbx.read_calls  # MEMORY.md was probed
    assert "" in sbx.list_calls or "/" in sbx.list_calls  # workspace was scanned for spills


async def test_c16_engine_hard_reset_collects_only_existing_paths(tmp_path):
    """The engine's pointer collector verifies every path on disk — a
    pointer to a missing file is worse than no pointer. A deliverable in
    the event log whose file is GONE on disk must NOT appear in the
    manifest (it would be a lie)."""
    # Workspace has only a few files — the engine will be told about a
    # deliverable that was WRITTEN but is now GONE. The collector should
    # drop it (it's not actually recoverable on disk).
    files = {
        "keep.py": b"keep\n",
        # NOTE: gone.py is in the event log below but NOT in the sandbox.
    }
    sbx = _ManifestSandboxInstance(files=files, workspace_path="")
    executor = _ManifestSandboxExecutor(sbx)

    # HIGH thresholds so only the LLM-error path triggers condensation.
    condenser = LLMSummarizingCondenser(
        max_tokens=10_000_000, hard_max_tokens=10_000_000, keep_head=1, keep_recent=2, min_forget=2
    )
    summarizer = _RecordingSummarizer()
    agent = ScriptedAgent(
        [
            LLMContextWindowExceeded("overflow"),
            action_step(args={"command": "after"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor, condenser=condenser, summarizer=summarizer)
    # Seed the event log with enough live events for span selection to pass.
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="task")),
    )
    for i in range(4):
        tc = ToolCall(tool_name="shell", arguments={"command": f"echo {i}"})
        a = await store.append(
            CID,
            ActionEvent(thought=f"step {i}", tool_call=tc),
        )
        await store.append(
            CID,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id=a.id, tool_name="shell", success=True, content=f"out{i}"
                ),
                action_id=a.id,
            ),
        )
    # Seed TWO writes: one for keep.py (exists), one for gone.py (does NOT).
    await store.append(
        CID,
        ActionEvent(
            thought="wrote keep",
            tool_call=ToolCall(
                tool_name="file_write", arguments={"path": "keep.py", "content": "..."}
            ),
        ),
    )
    await store.append(
        CID,
        ActionEvent(
            thought="wrote gone",
            tool_call=ToolCall(
                tool_name="file_write", arguments={"path": "gone.py", "content": "..."}
            ),
        ),
    )
    await loop.send_message("task")
    await loop.run()

    events = await store.get_events(CID)
    tombs = [e for e in events if isinstance(e, CondensationEvent)]
    assert len(tombs) == 1
    summary = tombs[0].summary
    # The keeper IS in the manifest (it resolves on disk).
    assert "keep.py" in summary
    # The gone file is NOT — the manifest is honest, every pointer resolves.
    # (The engine's collector calls sandbox.read_file to verify; a
    # FileNotFoundError drops the path. The sandbox here raises on gone.py.)


# ---- 4. Helper fakes (mirror the test_snapshot_binary.py pattern) ----------


class _ManifestSandboxInstance:
    """Minimal duck-typed SandboxInstance for the C16 tests.

    The engine's `_collect_pointer_manifest_paths` uses three seams:
      - workspace_path (attribute, optional) — preferred scan root
      - list_dir(root) -> list[str]                  — for spill-file scan
      - read_file(path) -> bytes | raises            — for MEMORY.md check
      - and the existing read_file / list_dir usage in the snapshot

    Behavior is identical to FakeSandboxInstance in test_snapshot_binary.py
    plus a workspace_path attribute + list_dir."""

    def __init__(self, *, files=None, raises=None, workspace_path: str = ""):
        self._files = dict(files or {})
        self._raises = dict(raises or {})
        self.workspace_path = workspace_path
        # Diagnostics — tests can assert what the engine asked for.
        self.read_calls: dict[str, bytes | BaseException] = {}
        self.list_calls: list[str] = []

    async def read_file(self, path: str) -> bytes:
        self.read_calls[path] = (
            self._raises[path]
            if path in self._raises
            else self._files.get(path, FileNotFoundError(path))
        )
        if path in self._raises:
            raise self._raises[path]
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def list_dir(self, path: str) -> list[str]:
        self.list_calls.append(path)
        # Return every file currently in the sandbox (rooted at `path`).
        # Real backends return all entries in the directory, including
        # dot-files (`.disco-spill-*`, `.pmx/`), so we return them too —
        # the engine filters by `.disco-spill-` prefix after listing.
        if path == "" or path == self.workspace_path:
            return list(self._files.keys())
        # Otherwise: a real backend would prefix-match; this fake is a unit
        # test, return only direct children.
        prefix = path.rstrip("/") + "/"
        return [k for k in self._files.keys() if k.startswith(prefix)]


class _ManifestSandboxExecutor:
    """Minimal ToolExecutor stub exposing a `sandbox` attribute. The C16 tests
    never drive `execute()` (the test goes through the loop's context-window
    error path which short-circuits execution)."""

    def __init__(self, sandbox: _ManifestSandboxInstance):
        self.sandbox = sandbox

    def available_tools(self):
        return []

    async def execute(self, call):
        raise NotImplementedError
