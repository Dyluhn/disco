"""PKG-06 composition-root ownership and shutdown contracts."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from disco.agent_server import runtime_compatibility
from disco.agent_server.app import _make_runtime_lifespan
from disco.agent_server.runtime import ConversationRuntime
from disco.agent_server.runtime_composition import wire_runtime
from disco.core import SqliteEventStore
from fastapi import FastAPI


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
    assert runtime._dr._live_state is runtime._research_live_state

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

    active_ingress = {
        "aclose",
        "approve_plan",
        "cancel",
        "confirm",
        "kill",
        "pause",
        "pick_alternative",
        "reject",
        "request_plan",
        "resume",
        "send_user_turn",
        "start",
    }
    compatibility = {
        name
        for name, member in vars(runtime_compatibility).items()
        if inspect.isfunction(member)
        and not name.startswith("_")
        and name != "install_runtime_compatibility"
    }

    assert len(active_ingress) == 12
    assert len(compatibility) == 100
    assert public == active_ingress | compatibility
    assert all(
        getattr(ConversationRuntime, name).__module__ == "disco.agent_server.runtime_compatibility"
        for name in compatibility
    )


def test_runtime_compatibility_installers_are_bounded() -> None:
    tree = ast.parse(Path(runtime_compatibility.__file__).read_text())
    installers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and "install" in node.name
    ]

    assert {node.name for node in installers} == {
        "_install_schedule_compatibility",
        "install_runtime_compatibility",
    }
    assert all(len(node.body) <= 95 for node in installers)


def test_application_owners_do_not_retain_the_runtime() -> None:
    owner_modules = (
        "deep_research_service.py",
        "lifecycle.py",
        "preview_service.py",
        "resume_service.py",
        "sessions_service.py",
        "workspace_persistence.py",
        "workspace_service.py",
    )
    source_root = Path(__file__).parents[1] / "src/disco/agent_server"

    for module_name in owner_modules:
        tree = ast.parse((source_root / module_name).read_text())
        retained_runtime = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "_rt"
        ]
        assert retained_runtime == [], module_name

    composition = ast.parse(inspect.getsource(wire_runtime))
    whole_runtime_casts = [
        node
        for node in ast.walk(composition)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cast"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Name)
        and node.args[1].id == "rt"
    ]
    assert whole_runtime_casts == []


def test_runtime_consumers_do_not_reach_through_composition_resources() -> None:
    source_root = Path(__file__).parents[1] / "src/disco/agent_server"
    consumers = [
        source_root / "host_proxy.py",
        source_root / "preview_service.py",
        *sorted((source_root / "routes").glob("*.py")),
    ]

    for path in consumers:
        tree = ast.parse(path.read_text())
        private_resource_access = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "_run_resources"
        ]
        assert private_resource_access == [], path.name


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
    runtime._run_supervisor.cancel_runs = AsyncMock(side_effect=lambda: calls.append("runs"))
    runtime._driver_preflight.aclose = AsyncMock(
        side_effect=lambda: calls.append("driver-preflight")
    )
    runtime._run_supervisor.close_resources = AsyncMock(
        side_effect=lambda: calls.append("resources")
    )

    await runtime.aclose()

    assert calls == ["runs", "driver-preflight", "resources"]


async def test_app_lifespan_closes_runtime_even_when_request_scope_raises() -> None:
    runtime = _runtime()
    runtime._mcp._start_mcp_pool = AsyncMock()
    runtime._mcp._close_mcp_pool = AsyncMock()
    runtime._lifecycle.reconcile_orphaned_runs = AsyncMock()
    runtime._idle_sweeper.run = AsyncMock()
    runtime._schedule._schedule_manager_loop = AsyncMock()
    runtime._drivers.prewarm_model_probe = AsyncMock()
    runtime._drivers.prewarm_vision_probe = AsyncMock()
    runtime.aclose = AsyncMock()

    with pytest.raises(RuntimeError, match="request scope failed"):
        async with _make_runtime_lifespan(":memory:", runtime)(FastAPI()):
            raise RuntimeError("request scope failed")

    runtime.aclose.assert_awaited_once_with()
    runtime._mcp._close_mcp_pool.assert_awaited_once_with()
