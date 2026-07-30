"""Fail-closed authority boundaries for the extracted AgentLoop."""

from __future__ import annotations

import ast
import inspect
import tokenize
from io import StringIO
from pathlib import Path

import disco.core.loop.engine as engine_module
import pytest
from disco.core import ConversationStatus, Event, StatusEvent
from disco.core.loop.engine import AgentLoop
from disco.core.loop.loop_runtime import LoopRuntime
from disco.core.loop.transition import TransitionCoordinator

_LOOP_ROOT = Path(engine_module.__file__).resolve().parent
_FORBIDDEN_DEPENDENCY_PARTS = (
    "agent_server",
    "app_server",
    "context_bag",
    "dependency_bag",
    "service_locator",
)


def _parsed_loop_modules() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text()))
        for path in sorted(_LOOP_ROOT.rglob("*.py"))
    ]


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _logical_line_count(source: str) -> int:
    string_lines: set[int] = set()
    for token in tokenize.generate_tokens(StringIO(source).readline):
        if token.type == tokenize.STRING:
            string_lines.update(range(token.start[0], token.end[0] + 1))
    return sum(
        bool(line.strip())
        and (not line.strip().startswith("#") or number in string_lines)
        for number, line in enumerate(source.splitlines(), 1)
    )


def test_core_loop_has_no_concrete_server_or_locator_dependency() -> None:
    offenders: list[str] = []
    for path, tree in _parsed_loop_modules():
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for target in imported:
            if any(part in target.casefold() for part in _FORBIDDEN_DEPENDENCY_PARTS):
                offenders.append(f"{path.relative_to(_LOOP_ROOT)}:{target}")
    assert offenders == []


def test_agent_loop_facade_stays_below_epic_cap() -> None:
    source_lines, _ = inspect.getsourcelines(AgentLoop)
    assert _logical_line_count("".join(source_lines)) < 250


def test_terminal_success_has_one_persistence_route() -> None:
    """Only the runtime sink may append and only transition may call it for success."""
    offenders: list[str] = []
    for path, tree in _parsed_loop_modules():
        relative = path.relative_to(_LOOP_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _dotted_name(node.func)
            if target.endswith("store.append") and relative != "engine_contracts.py":
                offenders.append(f"{relative}:{node.lineno}:{target}")
            if target.endswith("_runtime._emit") and relative not in {
                "engine.py",
                "transition.py",
            }:
                offenders.append(f"{relative}:{node.lineno}:{target}")
            if target == "_route_event" and relative != "loop_runtime.py":
                offenders.append(f"{relative}:{node.lineno}:{target}")
    assert offenders == []


async def test_finished_publication_crosses_transition_and_commit_hook() -> None:
    class Store:
        def __init__(self) -> None:
            self.appended: list[Event] = []

        async def append(self, _conversation_id: str, event: Event) -> Event:
            self.appended.append(event)
            return event

    store = Store()
    committed: list[StatusEvent] = []

    async def commit(event: StatusEvent) -> Event:
        committed.append(event)
        return event

    loop = object.__new__(AgentLoop)
    loop.store = store  # type: ignore[assignment]
    loop.conversation_id = "authority-test"
    loop._terminal_commit_hook = commit
    loop._runtime = LoopRuntime(loop)
    loop._transition = TransitionCoordinator(loop)
    finished = StatusEvent(status=ConversationStatus.FINISHED)

    assert await loop._emit(finished) is finished
    assert committed == [finished]
    assert store.appended == []


async def test_transition_rejects_non_success_terminal_request() -> None:
    loop = object.__new__(AgentLoop)
    loop._transition = TransitionCoordinator(loop)

    with pytest.raises(ValueError, match="FINISHED only"):
        await loop._transition._land_terminal_success(
            StatusEvent(status=ConversationStatus.STUCK)
        )
