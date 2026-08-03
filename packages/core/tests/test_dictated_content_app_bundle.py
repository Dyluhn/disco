"""REL-RC-O — dictated quoted content: selected-app entry/bundle inspection and
the finish-gate refusal/cap behavior.

Split out of the former single test_dictated_content_finish_gate.py. Covers
multi-file selected-app scope (absolute plan paths, bounded bundle expansion,
entry proof, truncation and read/wall-clock deadlines, binary/internal/alias
exclusion), the deliverable path jail, the inspection-cause error taxonomy, and
the finish gate's refuse-then-release-at-cap discipline. See
test_dictated_content_literals.py and test_dictated_content_scope.py for the
literal-extraction/classification and scoping tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _dictated_content_support import (
    _env_messages,
    _finish,
    _FSBuildExecutor,
    _scoped_edit_directive,
    _submit_absolute_multifile_plan,
    _submit_mixed_plan,
    _submit_plan,
    _write,
)
from disco.core import ConversationStatus, DeliverableEvent, EventSource, StatusEvent
from disco.core.llm import OperatingMode
from disco.core.loop.finish import _DICTATED_CONTENT_REFUSAL_CAP, _safe_deliverable_file_path
from disco.core.loop.finish import content_gates as content_gates_module
from loop_fakes import ScriptedAgent, action_step, build_loop


def test_inspection_cause_taxonomy_never_retains_custom_exception_secrets():
    secret = "Bearer_REAL_SECRET_" + ("X" * 5000)
    adversarial_type = type(secret, (OSError,), {})
    cause = adversarial_type(10**5000, secret)
    outer = content_gates_module._DictatedContentInspectionIncomplete("inspection failed")
    outer.__cause__ = cause

    detail = content_gates_module._dictated_content_inspection_cause(outer)

    assert detail == {"category": "os_error"}
    assert secret not in repr(detail)


def test_inspection_cause_taxonomy_does_not_execute_custom_errno_hooks():
    secret = "SECRET_FROM_ERRNO_PROPERTY"

    class RaisingErrno(OSError):
        @property
        def errno(self):  # noqa: ANN201
            raise RuntimeError(secret)

    class ExplosiveInt(int):
        def __le__(self, other):  # noqa: ANN001, ANN201
            raise RuntimeError(secret)

    class AdversarialErrno(OSError):
        @property
        def errno(self):  # noqa: ANN201
            return ExplosiveInt(20)

    for cause in (RaisingErrno(secret), AdversarialErrno(secret)):
        outer = content_gates_module._DictatedContentInspectionIncomplete("inspection failed")
        outer.__cause__ = cause

        detail = content_gates_module._dictated_content_inspection_cause(outer)

        assert detail == {"category": "os_error"}
        assert secret not in repr(detail)


def test_deliverable_path_jail_accepts_only_the_canonical_workspace_root():
    assert _safe_deliverable_file_path("/workspace/release/index.html") == ("release/index.html")
    assert _safe_deliverable_file_path("/workspace", app_root=True) == "index.html"
    assert _safe_deliverable_file_path("server.py") == "server.py"
    assert _safe_deliverable_file_path("/workspace/../secret.txt") is None
    assert _safe_deliverable_file_path("/workspace/../../etc/passwd") is None
    assert _safe_deliverable_file_path("/workspaces/index.html") is None
    assert _safe_deliverable_file_path("/etc/passwd") is None


@pytest.mark.asyncio
async def test_absolute_workspace_plan_paths_preserve_multifile_literal_scope(
    tmp_path: Path,
):
    """H076: selected-app literals may live in approved sibling deliverables."""

    files = {
        "index.html": "STALE ROOT MUST NEVER OPEN",
        "release/index.html": "<h1>SELECTED RELEASE ONE</h1>",
        "release/assets/theme.css": "src:url(proof.woff2) format('woff2')",
        "release/scripts/app.js": (
            "document.body.dataset.scriptLoaded='true'; // SCRIPT ASSET LOADED"
        ),
    }
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_absolute_multifile_plan()]),
        conversation_id="dictated-absolute-multifile",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message(
        "Create root index.html with 'STALE ROOT MUST NEVER OPEN', selected release "
        "HTML with 'SELECTED RELEASE ONE', CSS format 'woff2', and JS values 'true' "
        "and 'SCRIPT ASSET LOADED'."
    )
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-absolute-multifile",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="selected release",
            path="release/index.html",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent(
        [
            action_step(
                "file_write",
                {
                    "path": "/workspace/release/index.html",
                    "content": "<h1>SELECTED RELEASE ONE</h1>",
                },
            ),
            _finish(),
        ]
    )
    state = await loop.run()
    events = await store.get_events("dictated-absolute-multifile")

    assert state.execution_status == ConversationStatus.FINISHED, _env_messages(events)
    assert not any("quoted user literal is missing" in msg for msg in _env_messages(events))
    assert "index.html" in executor.sandbox.read_paths
    assert (tmp_path / "release/index.html").read_text(
        encoding="utf-8"
    ) == "<h1>SELECTED RELEASE ONE</h1>"


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_path", ["release/index.html", "release"])
async def test_app_handoff_expands_bounded_multifile_text_bundle(
    tmp_path: Path,
    handoff_path: str,
):
    files: dict[str, str | bytes] = {
        "artifact.txt": "bundle handoff",
        "release/index.html": "<h1>SELECTED RELEASE ONE</h1>",
        "release/assets/theme.css": "body { background: rgb(12, 34, 56); }",
        "release/scripts/app.js": "document.body.dataset.scriptLoaded = 'true';",
        "release/media/hero.svg": "<text>SVG ASSET</text>",
        "release/sw.js": "/* RELIABILITY SERVICE WORKER */",
        "release/fonts/proof.woff2": b"wOF2\x00\x01binary-font",
        "release/node_modules/decoy.js": "DECOY MUST NOT SATISFY",
        "release/.pmx/internal.txt": "INTERNAL MUST NOT SATISFY",
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-app-bundle",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message(
        "Build an app with 'SELECTED RELEASE ONE', CSS 'rgb(12, 34, 56)', JS 'true', "
        "SVG text 'SVG ASSET', and worker comment 'RELIABILITY SERVICE WORKER'."
    )
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-app-bundle",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path=handoff_path,
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("bundle handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-app-bundle")

    assert state.execution_status == ConversationStatus.FINISHED, _env_messages(events)
    assert not any("quoted user literal is missing" in msg for msg in _env_messages(events))
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_release"
        for event in events
    )
    assert not any(
        path.endswith("proof.woff2") or "/node_modules/" in f"/{path}" or "/.pmx/" in f"/{path}"
        for path in executor.sandbox.read_paths
    )


@pytest.mark.asyncio
async def test_selected_app_entry_proof_skips_redundant_bundle_listing(tmp_path: Path):
    (tmp_path / "artifact.txt").write_text("server handoff", encoding="utf-8")
    (tmp_path / "server.py").write_text(
        'PAGE = "<h1>Live Server Up</h1>"',
        encoding="utf-8",
    )
    executor = _FSBuildExecutor(tmp_path)
    listing_calls = 0

    async def _unexpected_listing(path: str, limit: int):  # noqa: ANN202, ARG001
        nonlocal listing_calls
        listing_calls += 1
        raise OSError("bounded listing must not run after complete entry proof")

    executor.sandbox.list_dir_bounded = _unexpected_listing  # type: ignore[method-assign]
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-selected-entry-complete",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message(
        "DIAGNOSTIC PROTOCOL — for EVERY tool call, FIRST output one line "
        '"PREDICT: <expected result>", and AFTER one line "OBSERVED: match".\n'
        "TASK: Create server.py with the h1 'Live Server Up'."
    )
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-selected-entry-complete",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="server",
            path="server.py",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("server handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-selected-entry-complete")

    assert state.execution_status == ConversationStatus.FINISHED, _env_messages(events)
    assert listing_calls == 0
    assert not any(
        isinstance(event, StatusEvent)
        and event.detail in {"dictated_content_inspection_incomplete", "dictated_content_release"}
        for event in events
    )


@pytest.mark.asyncio
async def test_selected_app_entry_miss_still_uses_sibling_bundle_proof(tmp_path: Path):
    (tmp_path / "artifact.txt").write_text("server handoff", encoding="utf-8")
    (tmp_path / "server.py").write_text("# entry omits the heading", encoding="utf-8")
    (tmp_path / "template.html").write_text("<h1>Live Server Up</h1>", encoding="utf-8")
    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-selected-entry-sibling",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message("Create server.py backed by a template with 'Live Server Up'.")
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-selected-entry-sibling",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="server",
            path="server.py",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("server handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-selected-entry-sibling")

    assert state.execution_status == ConversationStatus.FINISHED, _env_messages(events)
    assert "." in executor.sandbox.list_paths
    assert "template.html" in executor.sandbox.read_paths


@pytest.mark.asyncio
async def test_selected_app_entry_miss_and_bundle_error_stays_fail_closed(tmp_path: Path):
    (tmp_path / "artifact.txt").write_text("server handoff", encoding="utf-8")
    (tmp_path / "server.py").write_text("# entry omits the heading", encoding="utf-8")
    executor = _FSBuildExecutor(tmp_path)

    async def _failed_listing(path: str, limit: int):  # noqa: ANN202, ARG001
        raise OSError(20, "list_dir_bounded failed safely: NotADirectoryError")

    executor.sandbox.list_dir_bounded = _failed_listing  # type: ignore[method-assign]
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-selected-entry-list-failure",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message("Create server.py with the h1 'Live Server Up'.")
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-selected-entry-list-failure",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="server",
            path="server.py",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("server handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-selected-entry-list-failure")

    assert state.execution_status == ConversationStatus.PAUSED
    assert any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_inspection_incomplete"
        for event in events
    )
    incomplete = next(
        event
        for event in events
        if isinstance(event, StatusEvent)
        and event.detail == "dictated_content_inspection_incomplete"
    )
    assert incomplete.meta == {"inspection_cause": {"category": "not_a_directory", "errno": 20}}
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_release"
        for event in events
    )


@pytest.mark.asyncio
async def test_missing_selected_app_entry_cannot_be_substituted_by_sibling_copy(tmp_path: Path):
    (tmp_path / "artifact.txt").write_text("server handoff", encoding="utf-8")
    (tmp_path / "template.html").write_text("<h1>Live Server Up</h1>", encoding="utf-8")
    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-selected-entry-missing",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message("Create server.py with the h1 'Live Server Up'.")
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-selected-entry-missing",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="missing server",
            path="server.py",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("server handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-selected-entry-missing")

    assert state.execution_status == ConversationStatus.PAUSED
    assert executor.sandbox.list_paths == []
    assert any("does not exist as a regular file" in msg for msg in _env_messages(events))
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_release"
        for event in events
    )


@pytest.mark.asyncio
async def test_app_bundle_excludes_binary_internal_dependency_and_alias_paths(tmp_path: Path):
    files: dict[str, str | bytes] = {
        "artifact.txt": "bundle handoff",
        "release/index.html": "<h1>ordinary app</h1>",
        "release/fonts/proof.woff2": b"BINARY ONLY",
        "release/node_modules/decoy.js": "DEPENDENCY ONLY",
        "release/.pmx/internal.txt": "INTERNAL ONLY",
        "stale/index.html": "BINARY ONLY DEPENDENCY ONLY INTERNAL ONLY ALIAS ONLY",
        "unrelated/decoy.txt": "ALIAS ONLY",
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    (tmp_path / "release/assets").mkdir(parents=True)
    (tmp_path / "release/assets/alias").symlink_to(tmp_path / "unrelated", target_is_directory=True)

    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-app-bundle-exclusions",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message(
        'Build an app containing "BINARY ONLY", "DEPENDENCY ONLY", "INTERNAL ONLY", '
        'and "ALIAS ONLY".'
    )
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-app-bundle-exclusions",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="stale",
            path="stale/index.html",
            artifact_kind="app",
        ),
    )
    await store.append(
        "dictated-app-bundle-exclusions",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path="release/index.html",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent(
        [
            _write("bundle handoff"),
            _finish(),
            action_step(
                "file_write",
                {
                    "path": "release/index.html",
                    "content": ("<h1>BINARY ONLY DEPENDENCY ONLY INTERNAL ONLY ALIAS ONLY</h1>"),
                },
            ),
            _finish(),
        ]
    )
    state = await loop.run()
    events = await store.get_events("dictated-app-bundle-exclusions")

    assert state.execution_status == ConversationStatus.FINISHED
    assert sum("quoted user literal is missing" in msg for msg in _env_messages(events)) == 1
    assert not any(
        path.endswith("proof.woff2")
        or "/node_modules/" in f"/{path}"
        or "/.pmx/" in f"/{path}"
        or "unrelated" in path
        or "alias" in path
        or path.startswith("stale/")
        for path in executor.sandbox.read_paths
    )


@pytest.mark.asyncio
async def test_app_bundle_truncation_pauses_as_visible_unverifiable(tmp_path: Path):
    (tmp_path / "artifact.txt").write_text("bundle handoff", encoding="utf-8")
    (tmp_path / "release/crowded").mkdir(parents=True)
    (tmp_path / "release/index.html").write_text("<h1>ordinary app</h1>", encoding="utf-8")
    for index in range(257):
        (tmp_path / "release/crowded" / f"asset-{index:03}.txt").write_text(
            "ordinary",
            encoding="utf-8",
        )

    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-app-bundle-truncated",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build an app containing "NEVER SILENTLY OMIT".')
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-app-bundle-truncated",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path="release/index.html",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("bundle handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-app-bundle-truncated")

    assert state.execution_status == ConversationStatus.PAUSED
    assert any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_inspection_incomplete"
        for event in events
    )
    assert any("paused fail-closed" in msg for msg in _env_messages(events))
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_release"
        for event in events
    )


@pytest.mark.asyncio
async def test_invalid_selected_app_path_pauses_fail_closed(tmp_path: Path):
    (tmp_path / "artifact.txt").write_text("ordinary handoff", encoding="utf-8")
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-invalid-selected-app",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build an app containing "INVALID PATH MUST NOT BYPASS".')
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-invalid-selected-app",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="unsafe",
            path="/etc/passwd",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("ordinary handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-invalid-selected-app")

    assert state.execution_status == ConversationStatus.PAUSED
    assert any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_inspection_incomplete"
        for event in events
    )
    assert any("selected app handoff path is unsafe" in msg for msg in _env_messages(events))
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_release"
        for event in events
    )


@pytest.mark.asyncio
async def test_app_bundle_inspection_deadline_pauses_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / "artifact.txt").write_text("bundle handoff", encoding="utf-8")
    (tmp_path / "release").mkdir()
    (tmp_path / "release/index.html").write_text("<h1>ordinary app</h1>", encoding="utf-8")
    executor = _FSBuildExecutor(tmp_path)

    async def _slow_listing(path: str, limit: int):  # noqa: ANN202, ARG001
        await asyncio.sleep(1)
        return [], False

    executor.sandbox.list_dir_bounded = _slow_listing  # type: ignore[method-assign]
    monkeypatch.setattr(content_gates_module, "_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S", 0.01)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-app-bundle-deadline",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build an app containing "DEADLINE MUST BE VISIBLE".')
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-app-bundle-deadline",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path="release",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("bundle handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-app-bundle-deadline")

    assert state.execution_status == ConversationStatus.PAUSED
    assert any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_inspection_incomplete"
        for event in events
    )
    assert any("wall-clock limit" in message for message in _env_messages(events))


@pytest.mark.asyncio
async def test_app_bundle_read_deadline_pauses_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / "artifact.txt").write_text("bundle handoff", encoding="utf-8")
    (tmp_path / "release").mkdir()
    (tmp_path / "release/index.html").write_text("<h1>ordinary app</h1>", encoding="utf-8")
    executor = _FSBuildExecutor(tmp_path)
    real_read = executor.sandbox.read_file

    async def _slow_read(path: str) -> bytes:
        await asyncio.sleep(1)
        return await real_read(path)

    executor.sandbox.read_file = _slow_read  # type: ignore[method-assign]
    monkeypatch.setattr(content_gates_module, "_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S", 0.01)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-app-bundle-read-deadline",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build an app containing "READ DEADLINE MUST BE VISIBLE".')
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-app-bundle-read-deadline",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path="release/index.html",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("bundle handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-app-bundle-read-deadline")

    assert state.execution_status == ConversationStatus.PAUSED
    assert any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_inspection_incomplete"
        for event in events
    )
    assert any("complete app-content inspection" in msg for msg in _env_messages(events))


@pytest.mark.asyncio
async def test_finish_refused_until_dictated_literal_lands(tmp_path: Path):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-finish",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with button text "Get Started".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent(
        [_write("plain content"), _finish(), _write("plain content\nGet Started"), _finish()]
    )
    state = await loop.run()

    events = await store.get_events("dictated-finish")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert any("Get Started" in m and "artifact.txt" in m for m in env)
    assert sum("quoted user literal is missing" in m for m in env) == 1


@pytest.mark.asyncio
async def test_dictated_content_gate_never_targets_binary_deliverables(tmp_path: Path):
    font_bytes = b"wOF2\x00\x01\x00\x00binary-font-payload\xff"
    opaque_bytes = b"opaque\x00binary\xfe"
    font_path = tmp_path / "fonts" / "proof.woff2"
    opaque_path = tmp_path / "assets" / "blob.dat"
    font_path.parent.mkdir(parents=True)
    opaque_path.parent.mkdir(parents=True)
    font_path.write_bytes(font_bytes)
    opaque_path.write_bytes(opaque_bytes)

    loop, store = build_loop(
        ScriptedAgent([_submit_mixed_plan()]),
        conversation_id="dictated-binary-safety",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with exact label "Get Started".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent(
        [_write("plain content"), _finish(), _write("plain content\nGet Started"), _finish()]
    )
    state = await loop.run()

    events = await store.get_events("dictated-binary-safety")
    reminders = [m for m in _env_messages(events) if "quoted user literal is missing" in m]
    assert state.execution_status == ConversationStatus.FINISHED
    assert len(reminders) == 1
    assert "`artifact.txt`" in reminders[0]
    assert "proof.woff2" not in reminders[0]
    assert "blob.dat" not in reminders[0]
    assert "Never add text to a binary asset" in reminders[0]
    assert font_path.read_bytes() == font_bytes
    assert opaque_path.read_bytes() == opaque_bytes


@pytest.mark.asyncio
async def test_finish_gate_allows_selection_edit_to_replace_dictated_literal(
    tmp_path: Path,
):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan("rev1")]),
        conversation_id="dictated-selection-edit",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with heading "NightOwl Coffee".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("NightOwl Coffee"), _finish()])
    await loop.run()

    directive = _scoped_edit_directive(human_label='h1 — "NightOwl Coffee"')
    loop.agent = ScriptedAgent([_submit_plan("rev2")])
    await loop.steer(directive)
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("Midnight Coffee"), _finish()])
    state = await loop.run()

    events = await store.get_events("dictated-selection-edit")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert "NightOwl Coffee" not in (tmp_path / "artifact.txt").read_text(encoding="utf-8")
    assert not any("quoted user literal is missing" in m for m in env)


@pytest.mark.asyncio
async def test_prior_revision_dictated_literal_carries_forward(tmp_path: Path):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan("rev1")]),
        conversation_id="dictated-carry",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with "Alpha".')
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent([_write("Alpha"), _finish()])
    await loop.run()

    loop.agent = ScriptedAgent([_submit_plan("rev2")])
    await loop.send_message('Add "Beta" to the artifact.')
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent([_write("Alpha Beta"), _finish()])
    await loop.run()

    loop.agent = ScriptedAgent([_submit_plan("rev3")])
    await loop.send_message('Add "Gamma" to the artifact.')
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent(
        [_write("Alpha Gamma"), _finish(), _write("Alpha Beta Gamma"), _finish()]
    )
    state = await loop.run()

    events = await store.get_events("dictated-carry")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert any("Beta" in m and "artifact.txt" in m for m in env)
    assert (tmp_path / "artifact.txt").read_text(encoding="utf-8") == "Alpha Beta Gamma"


@pytest.mark.asyncio
async def test_dictated_content_gate_releases_loudly_at_cap(tmp_path: Path):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-cap",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with "Get Started".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("missing literal"), _finish()])
    state = await loop.run()

    events = await store.get_events("dictated-cap")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert sum("quoted user literal is missing" in m for m in env) == _DICTATED_CONTENT_REFUSAL_CAP
    assert any("Finished despite missing dictated content" in m and "Get Started" in m for m in env)
    assert any(
        isinstance(e, StatusEvent) and e.detail == "dictated_content_release" for e in events
    )


@pytest.mark.asyncio
async def test_non_appkit_dictated_content_literal_miss_still_refuses_then_cap_releases(
    tmp_path: Path,
):
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-non-appkit-unchanged",
        executor=_FSBuildExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message('Build the artifact with exact label "Required Copy".')
    await loop.run()
    await loop.approve_plan()

    loop.agent = ScriptedAgent([_write("other copy"), _finish()])
    state = await loop.run()

    events = await store.get_events("dictated-non-appkit-unchanged")
    env = _env_messages(events)
    assert state.execution_status == ConversationStatus.FINISHED
    assert sum("quoted user literal is missing" in m for m in env) == (
        _DICTATED_CONTENT_REFUSAL_CAP
    )
    assert any("Required Copy" in m and "artifact.txt" in m for m in env), env
    assert any("Finished despite missing dictated content" in m for m in env), env


@pytest.mark.asyncio
async def test_root_entry_handoff_skips_node_compile_cache(tmp_path: Path):
    """Counted seed 440023: node homes its module compile cache in the workspace
    (a non-hidden runtime-state tree with hundreds of flat entries, the JS
    analogue of __pycache__). A dev-mode handoff whose entry lives at the
    workspace root must inspect the app files without walking that cache —
    previously the per-directory bound tripped nondeterministically whenever
    node had compiled enough modules."""
    (tmp_path / "artifact.txt").write_text("bundle handoff", encoding="utf-8")
    (tmp_path / "index.html").write_text("<h1>SELECTED RELEASE ONE</h1>", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text(
        "document.title = 'RELIABILITY MARKER TWO';", encoding="utf-8"
    )
    cache = tmp_path / "node-compile-cache" / "v22.22.2-x64-9ac5647c-1000"
    cache.mkdir(parents=True)
    for index in range(300):  # over the 256 per-directory inspection bound
        (cache / f"{index:08x}").write_bytes(b"\x00cache-blob")

    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-compile-cache",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message(
        "Build an app with 'SELECTED RELEASE ONE' and JS title 'RELIABILITY MARKER TWO'."
    )
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-compile-cache",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path="index.html",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("bundle handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-compile-cache")

    assert state.execution_status == ConversationStatus.FINISHED, _env_messages(events)
    assert not any("node-compile-cache" in path for path in executor.sandbox.read_paths)
    assert not any("node-compile-cache" in path for path in executor.sandbox.list_paths)


@pytest.mark.asyncio
async def test_oversized_ordinary_directory_still_fails_closed(tmp_path: Path):
    """Control: the runtime-state skip is exact — an ordinary oversized app
    directory still pauses the finish fail-closed at the inspection bound."""
    (tmp_path / "artifact.txt").write_text("bundle handoff", encoding="utf-8")
    (tmp_path / "index.html").write_text("<h1>SELECTED RELEASE ONE</h1>", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text(
        "document.title = 'RELIABILITY MARKER TWO';", encoding="utf-8"
    )
    data = tmp_path / "data"
    data.mkdir()
    for index in range(300):
        (data / f"row-{index:04d}.txt").write_text("payload", encoding="utf-8")

    executor = _FSBuildExecutor(tmp_path)
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id="dictated-oversized-dir",
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message(
        "Build an app with 'SELECTED RELEASE ONE' and JS title 'RELIABILITY MARKER TWO'."
    )
    await loop.run()
    await loop.approve_plan()
    await store.append(
        "dictated-oversized-dir",
        DeliverableEvent(
            source=EventSource.AGENT,
            title="release",
            path="index.html",
            artifact_kind="app",
        ),
    )

    loop.agent = ScriptedAgent([_write("bundle handoff"), _finish()])
    state = await loop.run()
    events = await store.get_events("dictated-oversized-dir")

    assert state.execution_status == ConversationStatus.PAUSED
    assert any(
        isinstance(event, StatusEvent) and event.detail == "dictated_content_inspection_incomplete"
        for event in events
    )
