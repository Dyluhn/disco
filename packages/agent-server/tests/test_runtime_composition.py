"""PKG-06 composition-root ownership and shutdown contracts."""

from __future__ import annotations

import ast
import inspect
from unittest.mock import AsyncMock

from disco.agent_server.runtime import ConversationRuntime
from disco.agent_server.runtime_composition import wire_runtime
from disco.core import SqliteEventStore


def _runtime() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"))


def test_runtime_owns_no_cross_domain_mutable_collections() -> None:
    runtime = _runtime()

    direct_collections = {
        name: type(value).__name__
        for name, value in vars(runtime).items()
        if isinstance(value, (dict, list, set, bytearray))
    }

    assert direct_collections == {}


def test_run_owners_form_an_explicit_construction_graph() -> None:
    runtime = _runtime()

    assert runtime._run_controller._registry is runtime._run_registry
    assert runtime._run_supervisor._registry is runtime._run_registry
    assert runtime._run_supervisor._resources is runtime._run_resources
    assert runtime._run_kills._runs is runtime._run_registry
    assert runtime._run_kills._resources is runtime._run_resources
    assert runtime._control._controller is runtime._run_controller
    assert runtime._conversation_control._pins is runtime._kernel_pins
    assert runtime._conversation_control._runs is runtime._run_registry
    assert runtime._run_finalizer._kernels is runtime._kernel_pins
    assert runtime._run_sweep._completion is runtime._run_finalizer
    assert runtime._dr._state is runtime._research_state

    explicit_owners = (
        runtime._run_controller,
        runtime._run_supervisor,
        runtime._run_kills,
        runtime._control,
        runtime._conversation_control,
        runtime._run_finalizer,
        runtime._run_sweep,
        runtime._run_execution,
    )
    assert all(not hasattr(owner, "_rt") for owner in explicit_owners)


def test_runtime_public_surface_is_frozen_and_bounded() -> None:
    public = {
        name
        for name, member in vars(ConversationRuntime).items()
        if callable(member) and not name.startswith("_")
    }

    assert public == {
        "aclose",
        "cancel",
        "ensure_preview",
        "kill",
        "live_session",
        "port_upstream",
        "preview",
        "project_store",
        "send_user_turn",
        "start",
        "wake_for_preview",
        "workspace_lock",
    }


def test_wire_runtime_body_is_wiring_only() -> None:
    function = ast.parse(inspect.getsource(wire_runtime)).body[0]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    statements = [
        statement
        for statement in function.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]

    assert all(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id.startswith("_wire_")
        for statement in statements
    )
    assert [statement.value.func.id for statement in statements] == [
        "_wire_foundation",
        "_wire_domains",
        "_wire_loops",
        "_wire_runs",
    ]


async def test_shutdown_order_is_deterministic() -> None:
    runtime = _runtime()
    calls: list[str] = []
    runtime._run_supervisor.close = AsyncMock(side_effect=lambda: calls.append("runs"))
    runtime._driver_preflight.aclose = AsyncMock(
        side_effect=lambda: calls.append("driver-preflight")
    )

    await runtime.aclose()

    assert calls == ["runs", "driver-preflight"]
