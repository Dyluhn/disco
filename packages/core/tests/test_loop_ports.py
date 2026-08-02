"""The loop ports must stay derived from the surface they were measured against.

`deep-map/18-COUPLING-INVENTORY.md` §4 derived eight capability clusters from a
measured attribute-access matrix. These tests hold that derivation in place:

* every port member is really declared on the compatibility surface, so a port
  cannot invent capability the loop does not have;
* the ports stay disjoint, so a name has exactly one owning port;
* the loop's own internal collaborator handles never become port members —
  they exist because the bag exists and they disappear with it.

The `TYPE_CHECKING` block additionally makes basedpyright prove, on every run,
that the compatibility surface still satisfies all eight protocols. Before this
existed nothing checked that the loop provided what its consumers annotated.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from disco.core.loop.loop_facade_compat import _AgentLoopCompatibility
    from disco.core.loop.ports import (
        ContextGroundingPort,
        ConversationModePort,
        FinishVerificationPort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
    )

    def _statically_satisfies(loop: _AgentLoopCompatibility) -> None:
        """Fails the type gate if any port drifts from the compat surface."""
        _p1: LoopEventPort = loop
        _p2: ToolExecutionPort = loop
        _p3: ConversationModePort = loop
        _p4: PlanLifecyclePort = loop
        _p5: GateCounterPort = loop
        _p6: FinishVerificationPort = loop
        _p7: ContextGroundingPort = loop
        _p8: TurnControlPort = loop


_LOOP_ROOT = Path(__file__).resolve().parents[1] / "src" / "disco" / "core" / "loop"

# Handles to the loop's own internal collaborators. Inventory §4 excludes these
# deliberately: a port over them would re-expose the whole object graph.
_COLLABORATOR_HANDLES = frozenset(
    {
        "_runtime",
        "_planning_gates",
        "_transition",
        "_conversation_controls",
        "_plan_controls",
        "_replanning",
        "_meta",
        "_router",
        "_model_policy",
    }
)


def _declared_names(path: Path, class_name: str) -> set[str]:
    """Every name a class declares, including under `if TYPE_CHECKING`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    names: set[str] = set()

    def walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
            elif isinstance(stmt, ast.Assign):
                names.update(
                    t.id for t in stmt.targets if isinstance(t, ast.Name)
                )
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(stmt.name)
            elif isinstance(stmt, ast.If):
                walk(stmt.body)

    walk(node.body)
    return names


def _port_members() -> dict[str, set[str]]:
    tree = ast.parse((_LOOP_ROOT / "ports.py").read_text(encoding="utf-8"))
    return {
        node.name: _declared_names(_LOOP_ROOT / "ports.py", node.name)
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }


def test_every_port_member_is_declared_on_the_compat_surface() -> None:
    """A port may narrow the bag; it may never invent capability."""
    surface = _declared_names(_LOOP_ROOT / "loop_facade_compat.py", "_AgentLoopCompatibility")
    invented = {
        port: sorted(members - surface) for port, members in _port_members().items()
    }
    assert {p: m for p, m in invented.items() if m} == {}


def test_ports_are_disjoint() -> None:
    """Each name has exactly one owning port, so ownership is unambiguous."""
    seen: dict[str, str] = {}
    duplicated: list[str] = []
    for port, members in sorted(_port_members().items()):
        for member in sorted(members):
            if member in seen:
                duplicated.append(f"{member}: {seen[member]} and {port}")
            seen[member] = port
    assert duplicated == []


def test_collaborator_handles_are_not_ports() -> None:
    """`_runtime`, `_transition`, ... are internal wiring, not capability."""
    leaked = {
        port: sorted(members & _COLLABORATOR_HANDLES)
        for port, members in _port_members().items()
    }
    assert {p: m for p, m in leaked.items() if m} == {}


def test_the_eight_measured_ports_are_all_present() -> None:
    """The clustering is a derived result; losing one silently would hide work."""
    assert sorted(_port_members()) == [
        "ContextGroundingPort",
        "ConversationModePort",
        "FinishVerificationPort",
        "GateCounterPort",
        "LoopEventPort",
        "PlanLifecyclePort",
        "ToolExecutionPort",
        "TurnControlPort",
    ]
