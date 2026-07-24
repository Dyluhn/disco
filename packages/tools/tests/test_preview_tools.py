"""EPIC F — preview_* tools: the model-facing surface proves there is NO port arg, and
the tools return the platform-assigned URL via the PreviewManager bound to the sandbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest
from disco.agent_server.preview_manager import PreviewManager
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.preview import (
    PreviewLogsTool,
    PreviewStartArgs,
    PreviewStartTool,
    PreviewStatusTool,
    PreviewStopTool,
)

_PORT_RE = re.compile(r"(?:http\.server\s+|--port[= ]|-p[= ]|PORT=)(\d+)")


@dataclass
class _View:
    running: bool
    output: str


class _FakeSessions:
    def __init__(self, serving: set[int]) -> None:
        self.namespace = ""
        self._serving = serving
        self._running: dict[str, bool] = {}
        self._name_port: dict[str, int] = {}
        self._logs: dict[str, str] = {}

    async def exec(self, name: str, command: str, exec_dir: str | None) -> None:
        m = _PORT_RE.search(command)
        if m:
            port = int(m.group(1))
            self._name_port[name] = port
            self._serving.add(port)
        self._running[name] = True
        self._logs[name] = f"serving {command}\n"

    async def view(self, name: str, tail_chars: int = 2000) -> _View:
        return _View(self._running.get(name, False), self._logs.get(name, ""))

    async def kill_foreground(self, name: str) -> str:
        self._running[name] = False
        port = self._name_port.get(name)
        if port is not None:
            self._serving.discard(port)
        return "killed"


class _FakeSandbox:
    def __init__(self) -> None:
        self.backend_name = "gvisor"
        self.workspace_path = "/workspace"
        self._serving: set[int] = set()
        self.sessions = _FakeSessions(self._serving)

    def expose_port(self, port: int) -> str | None:
        return f"http://preview.test/{port}/"

    async def fetch_inside(self, port: int, path: str, *, timeout_s: int = 5):
        return (200, b"", "text/html") if port in self._serving else None


def _ctx(sandbox: _FakeSandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="local",
        conversation_id="conv_test",
    )


def _attach_manager(sandbox: _FakeSandbox) -> None:
    sandbox._preview_manager = PreviewManager(
        sandbox, port_pool=[3000, 5173], health_attempts=1, health_interval_s=0.0
    )


# --------------------------------------------------------------------------- no port arg


def test_preview_start_args_have_no_port_field() -> None:
    """The defining property of EPIC F: the model literally cannot supply a port."""
    fields = set(PreviewStartArgs.model_fields)
    assert "port" not in fields
    assert {"serve_dir", "command", "framework"} <= fields


def test_preview_start_spec_schema_has_no_port() -> None:
    schema = PreviewStartTool.definition.to_spec().parameters_schema
    assert "port" not in schema.get("properties", {})
    command_help = schema["properties"]["command"]["description"]
    assert "PORT environment variable" in command_help
    assert "{port}" in command_help
    assert "configured runtime adapter" in command_help


def test_preview_start_description_distinguishes_startup_from_runtime_recovery() -> None:
    description = PreviewStartTool.definition.description
    assert "startup failure is attempted once" in description
    assert "call preview_start again" in description
    assert "After a preview has run" in description


# --------------------------------------------------------------------------- forbid extra


def test_preview_start_args_forbid_extra_keys() -> None:
    """P2 #5: an invented key (e.g. a model-supplied `port`) is REJECTED, not silently
    dropped — `preview_start(serve_dir='dist', port=3000)` must fail validation, which the
    executor surfaces as `invalid_arguments` (never a silently-ignored, false-affordance
    port)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as ei:
        PreviewStartArgs.model_validate({"serve_dir": "dist", "port": 3000})
    assert "port" in str(ei.value)  # the offending extra key is named


def test_all_preview_arg_models_forbid_extra_keys() -> None:
    """P2 #5: every preview_* arg model forbids unknown keys."""
    from disco.tools.builtin.preview import (
        PreviewLogsArgs,
        PreviewStatusArgs,
        PreviewStopArgs,
    )
    from pydantic import ValidationError

    for model in (PreviewStartArgs, PreviewStatusArgs, PreviewLogsArgs, PreviewStopArgs):
        with pytest.raises(ValidationError):
            model.model_validate({"bogus_key": 1})


@pytest.mark.asyncio
async def test_preview_start_rejects_hardcoded_port_command() -> None:
    """P1 #1 at the tool seam: a raw command binding a hardcoded port is surfaced as a
    recoverable `invalid_command` failure with guidance — never silently run on the
    model's port."""
    sandbox = _FakeSandbox()
    _attach_manager(sandbox)
    out = await PreviewStartTool().run(
        PreviewStartArgs(command="python3 -m http.server 9999 -d dist"), _ctx(sandbox)
    )
    assert not out.success
    assert out.error == "invalid_command"
    assert "{port}" in out.content  # tells the model the safe placeholder


# --------------------------------------------------------------------------- behavior


@pytest.mark.asyncio
async def test_preview_start_returns_platform_url() -> None:
    sandbox = _FakeSandbox()
    _attach_manager(sandbox)
    out = await PreviewStartTool().run(
        PreviewStartArgs(serve_dir="dist", name="app"), _ctx(sandbox)
    )
    assert out.success
    assert out.structured is not None
    assert out.structured["port"] == 3000  # platform-assigned
    assert out.structured["url"] == "http://preview.test/3000/"
    assert "http://preview.test/3000/" in out.content


@pytest.mark.asyncio
async def test_preview_start_requires_intent() -> None:
    sandbox = _FakeSandbox()
    _attach_manager(sandbox)
    out = await PreviewStartTool().run(PreviewStartArgs(), _ctx(sandbox))
    assert not out.success
    assert out.error == "no_intent"


@pytest.mark.asyncio
async def test_status_logs_stop_tools() -> None:
    sandbox = _FakeSandbox()
    _attach_manager(sandbox)
    ctx = _ctx(sandbox)
    await PreviewStartTool().run(PreviewStartArgs(serve_dir="dist", name="app"), ctx)

    status = await PreviewStatusTool().run(_status_args(), ctx)
    assert status.success and "running" in status.content

    logs = await PreviewLogsTool().run(_logs_args(), ctx)
    assert logs.success and "app" in logs.content

    stop = await PreviewStopTool().run(_stop_args(), ctx)
    assert stop.success and "app" in stop.content


@pytest.mark.asyncio
async def test_tool_lazily_constructs_manager_when_absent() -> None:
    """No runtime pre-attached a manager → the tool builds one (and caches it),
    proving the production path works without explicit wiring in this test."""
    sandbox = _FakeSandbox()
    assert getattr(sandbox, "_preview_manager", None) is None
    out = await PreviewStartTool().run(
        PreviewStartArgs(framework="static", serve_dir="dist", name="app"), _ctx(sandbox)
    )
    assert out.success
    assert getattr(sandbox, "_preview_manager", None) is not None


# ----------------------------------------------------------------- agent-scope resolution


@pytest.mark.parametrize(
    "tool_name", ["preview_start", "preview_status", "preview_logs", "preview_stop"]
)
@pytest.mark.parametrize("tier", ["standard", "weak"])
def test_preview_tools_resolve_in_agent_scope(tool_name: str, tier: str) -> None:
    """P1 #2 regression: the preview_* tools are REGISTERED *and* in the agent
    security scope, so `ToolRegistry.get()` resolves them (instead of returning None,
    which the executor turns into unknown_tool). Without the AGENT_TOOLS entry the
    agent literally could not call a registered tool — a false affordance."""
    from disco.core.llm import ModelExecutionPolicy
    from disco.tools import agent_scope, build_default_registry

    policy = (
        ModelExecutionPolicy.standard()
        if tier == "standard"
        else ModelExecutionPolicy(tier="weak", anchored_edit=False)
    )
    registry = build_default_registry()
    scope = agent_scope(model_policy=policy)

    # In the security allowlist (callable) — get() returns the tool, not None.
    assert tool_name in scope.allowed_tools
    resolved = registry.get(tool_name, scope=scope)
    assert resolved is not None, f"{tool_name} resolved as unknown_tool in agent scope"
    assert resolved.definition.name == tool_name


def test_superseded_deploy_preview_is_gone_from_agent_scope() -> None:
    """The old `deploy_preview` placeholder is superseded by the EPIC F preview_*
    surface — it must no longer linger in the scope as a name with no registered tool."""
    from disco.tools import AGENT_TOOLS

    assert "deploy_preview" not in AGENT_TOOLS
    assert {"preview_start", "preview_status", "preview_logs", "preview_stop"} <= AGENT_TOOLS


# small arg-model constructors (the status/logs/stop arg models)
def _status_args():
    from disco.tools.builtin.preview import PreviewStatusArgs

    return PreviewStatusArgs()


def _logs_args():
    from disco.tools.builtin.preview import PreviewLogsArgs

    return PreviewLogsArgs()


def _stop_args():
    from disco.tools.builtin.preview import PreviewStopArgs

    return PreviewStopArgs()
