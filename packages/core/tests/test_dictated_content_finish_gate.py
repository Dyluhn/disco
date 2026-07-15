"""REL-RC-O — dictated quoted content is a finish condition.

Quoted user literals in build prompts/follow-ups are event-derived content
floors. The finish gate refuses while the primary deliverable lacks them, carries
prior revisions forward, and releases loudly at the shared cap discipline.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop.finish import _DICTATED_CONTENT_REFUSAL_CAP
from disco.core.loop.finish import content_gates as content_gates_module
from disco.core.loop.finish.common import _safe_deliverable_file_path
from disco.core.loop.plan_conditions import (
    dictated_content_conditions_from_events,
    extract_dictated_content_literals,
)
from disco.core.selection_edit import SourceSelectionRef, build_scoped_edit_directive
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop


class _Sandbox:
    def __init__(self, root: Path) -> None:
        self.workspace_path = str(root)
        self._root = root
        self.read_paths: list[str] = []
        self.list_paths: list[str] = []

    def _target(self, path: str) -> Path:
        relative = path.removeprefix("/workspace/") if path.startswith("/workspace/") else path
        return self._root / relative

    async def file_exists(self, path: str) -> bool:
        return self._target(path).is_file()

    async def read_file(self, path: str) -> bytes:
        self.read_paths.append(path)
        return self._target(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        self.list_paths.append(path)
        target = self._target(path)
        return [p.name for p in target.iterdir()]

    async def list_dir_bounded(
        self,
        path: str,
        limit: int,
    ) -> tuple[list[tuple[str, str]], bool]:
        names = await self.list_dir(path)
        entries: list[tuple[str, str]] = []
        for name in sorted(names)[:limit]:
            target = self._target(f"{path}/{name}")
            kind = (
                "other"
                if target.is_symlink()
                else "file"
                if target.is_file()
                else "directory"
                if target.is_dir()
                else "other"
            )
            entries.append((name, kind))
        return entries, len(names) > limit

    async def resolve_relpath(self, path: str) -> str:
        return self._target(path).resolve().relative_to(self._root.resolve()).as_posix()

    async def exec_shell(self, command: str, timeout_s: int = 5):  # noqa: ANN201, ARG002
        """Implement the DoD sandbox's non-empty-file evidence probe."""

        relative = shlex.split(command)[-1]
        target = self._target(relative)
        passed = target.is_file() and target.stat().st_size > 0
        return SimpleNamespace(
            exit_code=0 if passed else 1,
            stdout="",
            stderr="",
            timed_out=False,
        )


class _FSBuildExecutor(FakeExecutor):
    def __init__(self, root: Path) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="shell", description="shell", parameters_schema={}),
            ]
        )
        self.sandbox = _Sandbox(root)
        self.root = root

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name == "file_write":
            path = self.sandbox._target(str(call.arguments.get("path") or ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(call.arguments.get("content") or ""), encoding="utf-8")
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="ok",
        )


def _submit_plan(summary: str = "p") -> object:
    return action_step(
        "submit_plan",
        {
            "summary": summary,
            "steps": [
                {
                    "title": "write artifact",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "artifact.txt",
                    },
                }
            ],
        },
    )


def _submit_mixed_plan() -> object:
    return action_step(
        "submit_plan",
        {
            "summary": "text plus binary deliverables",
            "steps": [
                {
                    "title": "write artifact",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "artifact.txt",
                    },
                },
                {
                    "title": "include font",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "fonts/proof.woff2",
                    },
                },
                {
                    "title": "include opaque binary",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "assets/blob.dat",
                    },
                },
            ],
        },
    )


def _submit_absolute_multifile_plan() -> object:
    paths = [
        "/workspace/index.html",
        "/workspace/release/index.html",
        "/workspace/release/assets/theme.css",
        "/workspace/release/scripts/app.js",
    ]
    return action_step(
        "submit_plan",
        {
            "summary": "multi-file selected release",
            "steps": [
                {
                    "title": f"write {path}",
                    "done_condition": {"kind": "file_exists", "path": path},
                }
                for path in paths
            ],
        },
    )


def _write(content: str) -> object:
    return action_step("file_write", {"path": "artifact.txt", "content": content})


def _finish() -> object:
    return action_step("finish", {"summary": "done"})


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _user(text: str, seq: int) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=text),
    ).model_copy(update={"seq": seq})


def _plan(revision: int, seq: int) -> PlanEvent:
    return PlanEvent(summary=f"rev {revision}", steps=[], revision=revision).model_copy(
        update={"seq": seq}
    )


def _status(detail: str, seq: int) -> StatusEvent:
    return StatusEvent(status=ConversationStatus.RUNNING, detail=detail).model_copy(
        update={"seq": seq}
    )


def _scoped_edit_directive(
    *,
    human_label: str | None,
    instruction: str = "Change it to Midnight Coffee.",
) -> str:
    return build_scoped_edit_directive(
        SourceSelectionRef(oid="index.html:1", file="index.html", line=1),
        instruction,
        human_label=human_label,
    )


def test_extracts_prompt_and_followup_literals_but_skips_commands_and_paths():
    text = (
        'Build a hero "Launch Day" with CTA \'Get Started\', keep "Plans / Pricing", '
        'then run "npm run build" and edit "src/app.js".'
    )
    assert extract_dictated_content_literals(text) == [
        "Launch Day",
        "Get Started",
        "Plans / Pricing",
    ]

    events = [
        _user(text, 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user('Change every CTA button to say "Start Now"; run "python -m pytest -q".', 4),
        _status("planning", 5),
        _plan(2, 6),
    ]
    conditions = dictated_content_conditions_from_events(events)
    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "Launch Day"),
        (1, "Get Started"),
        (1, "Plans / Pricing"),
        (2, "Start Now"),
    ]


def test_serve_argument_literals_are_metadata_not_dictated_content():
    text = (
        'Build a hero with heading "Launch Day". Then call the\nserve tool exactly once '
        'with path "/workspace/release" and\ntitle "Selected reliability release".'
    )

    assert extract_dictated_content_literals(text) == ["Launch Day"]
    assert extract_dictated_content_literals(
        'Build a page title "Selected reliability release".'
    ) == ["Selected reliability release"]


def test_serve_file_path_dot_does_not_turn_following_title_into_visible_copy():
    """H069 live-8: punctuation inside the path argument is not a sentence break."""
    text = (
        "Build a normal HTML document with visible h1 text 'SELECTED RELEASE ONE'. "
        "Use ordinary workspace write/shell tools. Verify the files and links. "
        "Then call the\nserve tool exactly once with path 'release/index.html' "
        "(the entry FILE, not release,\ndot, or the workspace root), title "
        "'Selected reliability release', and finish."
    )
    assert extract_dictated_content_literals(text) == ["SELECTED RELEASE ONE"]
    conditions = dictated_content_conditions_from_events(
        [_user(text, 1), _plan(1, 2), _status("plan_approved", 3)]
    )
    assert [condition.literal for condition in conditions] == ["SELECTED RELEASE ONE"]

    # An actual sentence boundary still ends serve metadata scope. The later
    # page title is visible content and must remain a hard finish condition.
    visible_copy = (
        "Call serve with path 'release/index.html'. "
        "Then make the page title 'Selected reliability release'."
    )
    assert extract_dictated_content_literals(visible_copy) == ["Selected reliability release"]


def test_deliverable_path_jail_accepts_only_the_canonical_workspace_root():
    assert _safe_deliverable_file_path("/workspace/release/index.html") == ("release/index.html")
    assert _safe_deliverable_file_path("/workspace", app_root=True) == "index.html"
    assert _safe_deliverable_file_path("/workspace/../secret.txt") is None
    assert _safe_deliverable_file_path("/workspace/../../etc/passwd") is None
    assert _safe_deliverable_file_path("/workspaces/index.html") is None
    assert _safe_deliverable_file_path("/etc/passwd") is None


@pytest.mark.asyncio
async def test_absolute_workspace_plan_paths_preserve_multifile_literal_scope(
    tmp_path: Path,
):
    """H076: literals in sibling text assets must not be copied into the entry."""

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

    loop, store = build_loop(
        ScriptedAgent([_submit_absolute_multifile_plan()]),
        conversation_id="dictated-absolute-multifile",
        executor=_FSBuildExecutor(tmp_path),
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


def test_scoped_edit_supersedes_old_literal_without_harvesting_label_text():
    directive = _scoped_edit_directive(human_label='h1 — "NightOwl Coffee"')
    events = [
        _user('Build a hero titled "NightOwl Coffee" and include "Contact us today".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(directive, 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "Contact us today"),
    ]


def test_label_less_scoped_edit_supersedes_no_prior_literal():
    directive = _scoped_edit_directive(
        human_label=None,
        instruction="Make the selected heading shorter.",
    )
    events = [
        _user('Build a hero titled "NightOwl Coffee".', 1),
        _plan(1, 2),
        _status("plan_approved", 3),
        _user(directive, 4),
        _status("planning", 5),
        _plan(2, 6),
    ]

    conditions = dictated_content_conditions_from_events(events)

    assert [(c.revision, c.literal) for c in conditions] == [
        (1, "NightOwl Coffee"),
    ]


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
