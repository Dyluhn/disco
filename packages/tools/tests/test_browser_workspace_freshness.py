"""BF1 adversarial gates for browser/workspace coherence.

These tests are model-free. They exercise the capability clock, daemon protocol,
fixed page lanes, and the nested verifier context without naming a framework or
workspace file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from disco.core import EffectCapability, ToolBehavior, ToolCall
from disco.tools import (
    DefaultToolExecutor,
    ToolContext,
    ToolDef,
    ToolOutcome,
    ToolRegistry,
    ToolScope,
)
from disco.tools.builtin import _browser_daemon as daemon
from disco.tools.builtin.browser import BrowserArgs, BrowserTool
from disco.tools.builtin.verify_app import VerifyWebAppArgs, VerifyWebAppTool
from pydantic import BaseModel, ConfigDict


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ClockTool:
    def __init__(self, name: str, capabilities: set[EffectCapability], *, succeeds: bool = True):
        self.definition = ToolDef(
            name=name,
            description="clock probe",
            args_model=_NoArgs,
            runs_in="in_process",
            read_only=not capabilities,
            behavior=ToolBehavior(
                planner_safe=not capabilities,
                possible_capabilities=frozenset(capabilities),
            ),
        )
        self.succeeds = succeeds
        self.contexts: list[ToolContext] = []

    async def run(self, args: _NoArgs, ctx: ToolContext) -> ToolOutcome:
        del args
        self.contexts.append(ctx)
        return ToolOutcome(
            success=self.succeeds,
            content="ok" if self.succeeds else "failed",
            error=None if self.succeeds else "failed",
        )


def _clock_executor(*tools: _ClockTool) -> DefaultToolExecutor:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset(tool.definition.name for tool in tools)),
    )


@pytest.mark.asyncio
async def test_epoch_advances_after_any_executed_mutation_capability_profile() -> None:
    reader = _ClockTool("write_in_name_only", set())
    failed = _ClockTool("failed_mutator", {EffectCapability.WORKSPACE_MUTATE}, succeeds=False)
    shell_shaped = _ClockTool("opaque_command", {EffectCapability.WORKSPACE_MUTATE})
    differently_named = _ClockTool("z", {EffectCapability.WORKSPACE_MUTATE})
    executor = _clock_executor(reader, failed, shell_shaped, differently_named)

    generation = (await executor._build_context(reader.definition)).browser_generation
    await executor.execute(ToolCall(tool_name=reader.definition.name, arguments={}))
    await executor.execute(ToolCall(tool_name=failed.definition.name, arguments={}))
    after_failed_mutator = await executor._build_context(reader.definition)
    assert after_failed_mutator.browser_workspace_epoch == 1

    await executor.execute(ToolCall(tool_name=shell_shaped.definition.name, arguments={}))
    after_one = await executor._build_context(reader.definition)
    assert after_one.browser_workspace_epoch == 2
    assert after_one.browser_generation == generation
    assert after_one.browser_lane == "agent"

    await executor.execute(ToolCall(tool_name=differently_named.definition.name, arguments={}))
    assert (await executor._build_context(reader.definition)).browser_workspace_epoch == 3


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1"])
def test_tool_context_rejects_non_exact_positive_wire_epochs(value: object) -> None:
    with pytest.raises(ValueError):
        ToolContext(
            sandbox=None,
            workspace_path=".",
            timeout_s=1,
            capabilities=None,
            owner_id="o",
            conversation_id="c",
            browser_workspace_epoch=value,  # type: ignore[arg-type]
        )


def _wire_context(*, generation: str = "f" * 32, epoch: int | None = 2) -> ToolContext:
    return ToolContext(
        sandbox=None,
        workspace_path=".",
        timeout_s=1,
        capabilities=None,
        owner_id="owner",
        conversation_id="conversation",
        browser_workspace_epoch=epoch,
        browser_generation=generation,
        browser_lane="agent",
    )


def _ack(
    *,
    daemon_id: str,
    generation: str,
    ok: object = True,
    synchronized_epoch: object = 2,
    error_class: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": ok,
        "freshness": {
            "schema_version": 1,
            "daemon_instance_id": daemon_id,
            "executor_generation": generation,
            "lane": "agent",
            "request_nonce": "e" * 32,
            "requested_epoch": 2,
            "synchronized_epoch": synchronized_epoch,
            "sync_performed": False,
            "page_kind": "local_preview",
        },
    }
    if error_class is not None:
        result.update(
            {
                "error": "bounded failure",
                "error_class": error_class,
                "error_reason": "selector_not_found",
            }
        )
    return result


def test_daemon_identity_pin_rejects_replay_and_live_preflight_rotates() -> None:
    generation = "f" * 32
    first_id = "a" * 32
    replacement_id = "b" * 32
    BrowserTool._daemon_identity_pins.pop(generation, None)
    ctx = _wire_context(generation=generation)

    assert (
        BrowserTool._freshness_response_error(
            _ack(daemon_id=first_id, generation=generation),
            ctx=ctx,
            request_nonce="e" * 32,
        )
        is None
    )
    assert (
        BrowserTool._freshness_response_error(
            _ack(daemon_id=replacement_id, generation=generation),
            ctx=ctx,
            request_nonce="e" * 32,
        )
        is not None
    )

    # Only a live /identity preflight is allowed to rotate the bounded pin.
    BrowserTool._pin_daemon_identity(generation, replacement_id, allow_rotation=True)
    assert (
        BrowserTool._freshness_response_error(
            _ack(daemon_id=replacement_id, generation=generation),
            ctx=ctx,
            request_nonce="e" * 32,
            expected_daemon_id=replacement_id,
        )
        is None
    )


def test_invalid_acknowledgement_cannot_poison_daemon_identity_pin() -> None:
    generation = "7" * 32
    BrowserTool._daemon_identity_pins.pop(generation, None)
    ctx = _wire_context(generation=generation)
    invalid = _ack(daemon_id="8" * 32, generation=generation)
    invalid["freshness"]["request_nonce"] = "wrong"
    assert (
        BrowserTool._freshness_response_error(invalid, ctx=ctx, request_nonce="e" * 32) is not None
    )
    assert generation not in BrowserTool._daemon_identity_pins
    assert (
        BrowserTool._freshness_response_error(
            _ack(daemon_id="9" * 32, generation=generation),
            ctx=ctx,
            request_nonce="e" * 32,
        )
        is None
    )


@pytest.mark.parametrize("ok", [1, 0, "true", None])
def test_nonboolean_success_flag_is_never_accepted(ok: object) -> None:
    generation = "1" * 32
    BrowserTool._daemon_identity_pins.pop(generation, None)
    error = BrowserTool._freshness_response_error(
        _ack(daemon_id="2" * 32, generation=generation, ok=ok),
        ctx=_wire_context(generation=generation),
        request_nonce="e" * 32,
    )
    assert error is not None


def test_browser_action_failure_requires_current_epoch_ack() -> None:
    generation = "3" * 32
    BrowserTool._daemon_identity_pins.pop(generation, None)
    ctx = _wire_context(generation=generation)
    stale = _ack(
        daemon_id="4" * 32,
        generation=generation,
        ok=False,
        synchronized_epoch=1,
        error_class="browser_action_failed",
    )
    current = _ack(
        daemon_id="4" * 32,
        generation=generation,
        ok=False,
        synchronized_epoch=2,
        error_class="browser_action_failed",
    )
    assert BrowserTool._freshness_response_error(stale, ctx=ctx, request_nonce="e" * 32) is not None
    assert BrowserTool._freshness_response_error(current, ctx=ctx, request_nonce="e" * 32) is None


class _Keyboard:
    def __init__(self, page: _Page) -> None:
        self.page = page

    def press(self, key: str) -> None:
        self.page.pressed.append(key)


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> _Locator:
        return self

    def _entry(self) -> dict[str, Any] | None:
        return self.page.elements.get(self.selector)

    def count(self) -> int:
        return int(self._entry() is not None)

    def is_visible(self) -> bool:
        entry = self._entry()
        return bool(entry and entry.get("visible", True))

    def is_enabled(self) -> bool:
        entry = self._entry()
        return bool(entry and entry.get("enabled", True))

    def click(self, *, trial: bool = False, timeout: int | None = None) -> None:
        del timeout
        entry = self._entry()
        if entry is None or entry.get("blocked"):
            raise RuntimeError("blocked")
        if not trial:
            entry["clicks"] = int(entry.get("clicks", 0)) + 1

    def fill(self, text: str) -> None:
        entry = self._entry()
        if entry is None or entry.get("blocked"):
            raise RuntimeError("blocked")
        entry["value"] = text

    def evaluate(self, script: str) -> str:
        del script
        entry = self._entry()
        return str((entry or {}).get("tag", "button"))

    def focus(self) -> None:
        self.page.focused = self.selector


class _Page:
    def __init__(self, versions: dict[str, dict[str, dict[str, Any]]], current: dict[str, str]):
        self._versions = versions
        self._current = current
        self.version = current["version"]
        self.url = "about:blank"
        self.reload_count = 0
        self.closed = False
        self.viewport = None
        self.waits: list[int] = []
        self.pressed: list[str] = []
        self.focused = ""
        self.keyboard = _Keyboard(self)

    @property
    def elements(self) -> dict[str, dict[str, Any]]:
        return self._versions[self.version]

    def on(self, *_args: object) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def goto(self, url: str, wait_until: str = "load") -> None:
        del wait_until
        self.url = url
        self.version = self._current["version"]

    def reload(self, wait_until: str = "load") -> None:
        del wait_until
        self.reload_count += 1
        self.version = self._current["version"]

    def go_back(self) -> None:
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def set_viewport_size(self, value: dict[str, int]) -> None:
        self.viewport = value

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    def title(self) -> str:
        return self.version

    def evaluate(self, script: str) -> Any:
        if "standardSelectors" in script:
            return [f"{i}[:] <button>{name}</button>" for i, name in enumerate(self.elements, 1)]
        if "document.body.innerText" in script:
            return self.version
        if "data-appkit-section" in script:
            return []
        if "querySelectorAll('canvas')" in script:
            return 0
        return 1

    def screenshot(self, path: str, full_page: bool = False) -> None:
        del path, full_page


class _Context:
    def __init__(self, factory):
        self.factory = factory
        self.storage: dict[str, str] = {}
        self.pages: list[_Page] = []
        self.closed = False
        self.init_scripts: list[str] = []

    def new_page(self) -> _Page:
        page = self.factory()
        page.context_storage = self.storage
        self.pages.append(page)
        return page

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def close(self) -> None:
        self.closed = True
        for page in self.pages:
            if not page.closed:
                page.close()


class _Browser:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.contexts: list[_Context] = []

    def new_context(self, **_kwargs: object) -> _Context:
        context = _Context(self.factory)
        self.contexts.append(context)
        return context


@dataclass
class _Harness:
    handler: daemon.BrowserHandler
    page: _Page
    current: dict[str, str]
    generation: str

    def call(self, action: str, *, epoch: object = None, **values: object) -> dict[str, Any]:
        params = {
            "action": action,
            "workspace_epoch": epoch,
            "executor_generation": self.generation,
            "browser_lane": "agent",
            "request_nonce": daemon.secrets.token_hex(16),
            **values,
        }
        return self.handler._handle_action(action, params)


@pytest.fixture
def browser_harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    versions = {
        "old": {"#old": {"clicks": 0}, "#recovery": {"clicks": 0}},
        "new": {"#new": {"clicks": 0}, "#recovery": {"clicks": 0}},
    }
    current = {"version": "old"}
    page = _Page(versions, current)
    state = daemon.BrowserState()
    state.render_ready = True
    state.page = page
    state.context = _Context(lambda: _Page(versions, current))
    page.context_storage = state.context.storage
    state.browser = _Browser(lambda: _Page(versions, current))
    monkeypatch.setattr(daemon, "state", state)
    monkeypatch.setattr(daemon, "SCREENSHOT_DIR", "/tmp")
    handler = daemon.BrowserHandler.__new__(daemon.BrowserHandler)
    handler._count_visible_semantic_elements = lambda _page: 1  # type: ignore[method-assign]
    return _Harness(handler, page, current, "a" * 32)


@pytest.mark.parametrize("raw", [b"[]", b'"value"', b"1", b"true", b"null"])
def test_daemon_nonobject_json_is_a_structured_protocol_error(raw: bytes) -> None:
    sent: list[tuple[dict[str, Any], int]] = []
    handler = daemon.BrowserHandler.__new__(daemon.BrowserHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = BytesIO(raw)
    handler._send_json = lambda data, status=200: sent.append((data, status))  # type: ignore[method-assign]

    handler.do_POST()

    assert sent == [
        (
            {
                "ok": False,
                "error": "browser request must be a JSON object",
                "error_class": "freshness_protocol_invalid",
                "error_reason": "invalid_request",
            },
            400,
        )
    ]


def test_unhashable_lane_is_rejected_before_membership() -> None:
    result = daemon.BrowserHandler._protocol(
        {
            "workspace_epoch": 1,
            "executor_generation": "a" * 32,
            "browser_lane": [],
            "request_nonce": "b" * 32,
        }
    )
    assert result[-1] == "invalid browser lane"


def test_daemon_identity_endpoint_and_request_binding(
    browser_harness: _Harness,
) -> None:
    statuses: list[int] = []
    handler = daemon.BrowserHandler.__new__(daemon.BrowserHandler)
    handler.path = "/identity"
    handler.wfile = BytesIO()
    handler.send_response = lambda status: statuses.append(status)  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]

    handler.do_GET()

    assert statuses == [200]
    assert handler.wfile.getvalue().decode("ascii") == daemon.DAEMON_INSTANCE_ID
    rejected = browser_harness.handler._handle_action(
        "screenshot",
        {
            "action": "screenshot",
            "workspace_epoch": 1,
            "executor_generation": browser_harness.generation,
            "browser_lane": "agent",
            "request_nonce": "7" * 32,
            "expected_daemon_instance_id": "0" * 32,
        },
    )
    assert rejected["error_class"] == "freshness_protocol_invalid"


class _WireSandbox:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses

    async def write_file(self, *_args: object) -> None:
        return None

    async def read_file(self, *_args: object) -> bytes:
        raise FileNotFoundError

    async def delete_file(self, *_args: object) -> None:
        raise FileNotFoundError

    async def exec_shell(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return self.responses.pop(0)


class _ReadyBrowser(BrowserTool):
    async def _ensure_daemon(self, ctx: ToolContext) -> str:
        del ctx
        return "http://127.0.0.1:1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "error_class"),
    [
        (
            [SimpleNamespace(exit_code=7, stdout="", stderr="offline")],
            "browser_daemon_unavailable",
        ),
        (
            [
                SimpleNamespace(exit_code=0, stdout="5" * 32, stderr=""),
                SimpleNamespace(exit_code=0, stdout="{bad-json", stderr=""),
            ],
            "freshness_protocol_invalid",
        ),
        (
            [
                SimpleNamespace(exit_code=0, stdout="5" * 32, stderr=""),
                SimpleNamespace(exit_code=0, stdout=json.dumps([]), stderr=""),
            ],
            "freshness_protocol_invalid",
        ),
    ],
)
async def test_browser_transport_and_malformed_responses_are_machine_classified(
    responses: list[SimpleNamespace], error_class: str
) -> None:
    sandbox = _WireSandbox(responses)
    ctx = _wire_context(generation="6" * 32, epoch=1).model_copy(
        update={"sandbox": sandbox, "sessions": object()}
    )
    outcome = await _ReadyBrowser().run(BrowserArgs(action="screenshot"), ctx)
    assert outcome.success is False
    assert outcome.structured is not None
    assert outcome.structured["error_class"] == error_class


def test_local_mutation_refreshes_once_and_same_epoch_preserves_state(
    browser_harness: _Harness,
) -> None:
    h = browser_harness
    first = h.call("navigate", epoch=1, url="http://127.0.0.1:8123/")
    assert first["ok"] is True
    h.current["version"] = "new"

    changed = h.call("click", epoch=2, selector="#new")
    assert changed["ok"] is True
    assert h.page.reload_count == 1
    assert h.page.elements["#new"]["clicks"] == 1
    assert changed["freshness"]["sync_performed"] is True

    h.page.elements["#recovery"]["value"] = "preserved"
    same = h.call("click", epoch=2, selector="#recovery")
    assert same["ok"] is True
    assert h.page.reload_count == 1
    assert h.page.elements["#recovery"]["value"] == "preserved"


def test_successful_reload_is_acknowledged_when_following_action_fails(
    browser_harness: _Harness,
) -> None:
    h = browser_harness
    assert h.call("navigate", epoch=1, url="http://localhost:8123/")["ok"]
    h.current["version"] = "new"

    failed = h.call("click", epoch=2, selector="#missing")
    assert failed["error_class"] == "browser_action_failed"
    assert failed["error_reason"] == "selector_not_found"
    assert failed["freshness"]["synchronized_epoch"] == 2
    assert failed["freshness"]["sync_performed"] is True
    assert h.page.reload_count == 1

    recovered = h.call("click", epoch=2, selector="#recovery")
    assert recovered["ok"] is True
    assert h.page.reload_count == 1


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"visible": False}, "not_visible"),
        ({"enabled": False}, "disabled"),
        ({"blocked": True}, "interaction_blocked"),
    ],
)
def test_action_failures_have_structured_primary_classification(
    browser_harness: _Harness,
    entry: dict[str, Any],
    reason: str,
) -> None:
    h = browser_harness
    h.page.elements["#target"] = entry
    assert h.call("navigate", epoch=1, url="http://127.0.0.1:8123/")["ok"]
    result = h.call("click", epoch=1, selector="#target")
    assert result["error_class"] == "browser_action_failed"
    assert result["error_reason"] == reason


def test_external_page_is_never_reloaded_for_workspace_epoch(browser_harness: _Harness) -> None:
    h = browser_harness
    assert h.call("navigate", epoch=1, url="https://example.invalid/")["ok"]
    result = h.call("screenshot", epoch=2)
    assert result["ok"] is True
    assert h.page.reload_count == 0
    assert result["freshness"]["page_kind"] == "external"
    assert result["freshness"]["sync_performed"] is False


def test_new_generation_gets_blank_page_not_old_acknowledgement(browser_harness: _Harness) -> None:
    h = browser_harness
    assert h.call("navigate", epoch=1, url="http://127.0.0.1:8123/")["ok"]
    h.generation = "b" * 32
    result = h.call("screenshot", epoch=2)
    assert result["error_class"] == "browser_session_uninitialized"
    assert result["freshness"]["page_kind"] == "uninitialized"
    assert h.page.closed is True


def test_new_generation_epoch_zero_cannot_return_blank_success(
    browser_harness: _Harness,
) -> None:
    h = browser_harness
    assert h.call("navigate", epoch=None, url="http://127.0.0.1:8123/")["ok"]
    h.generation = "b" * 32
    result = h.call("screenshot", epoch=None)
    assert result["ok"] is False
    assert result["error_class"] == "browser_session_uninitialized"


def test_same_epoch_back_preserves_state_and_pending_back_reloads_once(
    browser_harness: _Harness,
) -> None:
    h = browser_harness
    assert h.call("navigate", epoch=1, url="http://127.0.0.1:8123/")["ok"]

    same = h.call("back", epoch=1)
    assert same["ok"] is True
    assert h.page.reload_count == 0

    h.current["version"] = "new"
    pending = h.call("back", epoch=2)
    assert pending["ok"] is True
    assert h.page.reload_count == 1
    assert pending["freshness"]["sync_performed"] is True


@pytest.mark.parametrize("epoch", [True, False, 0, -1, 1.5, "2"])
def test_daemon_rejects_non_exact_epochs_before_action(
    browser_harness: _Harness, epoch: object
) -> None:
    result = browser_harness.call("screenshot", epoch=epoch)
    assert result["error_class"] == "freshness_protocol_invalid"


def test_fixed_lanes_do_not_share_page_or_freshness(browser_harness: _Harness) -> None:
    h = browser_harness
    assert h.call("navigate", epoch=1, url="http://127.0.0.1:8123/agent")["ok"]
    agent_url = daemon.state.page.url
    host = h.handler._handle_action(
        "navigate",
        {
            "action": "navigate",
            "url": "http://127.0.0.1:8123/host",
            "workspace_epoch": 1,
            "executor_generation": h.generation,
            "browser_lane": "host_verifier",
            "request_nonce": "c" * 32,
        },
    )
    assert host["ok"] is True
    assert daemon.state.lane("host_verifier").page is not daemon.state.page
    assert daemon.state.page.url == agent_url
    assert daemon.state.lane("agent").synchronized_epoch == 1

    closed = h.handler._handle_action(
        "_close_lane",
        {
            "action": "_close_lane",
            "workspace_epoch": 1,
            "executor_generation": h.generation,
            "browser_lane": "host_verifier",
            "request_nonce": "d" * 32,
        },
    )
    assert closed["ok"] is True
    assert daemon.state.lane("host_verifier").page is None
    assert daemon.state.page.url == agent_url


def test_host_lane_uses_and_closes_a_separate_browser_context(
    browser_harness: _Harness,
) -> None:
    h = browser_harness
    agent_context = daemon.state.context
    assert agent_context is not None
    agent_context.storage.update(
        {
            "cookie": "agent-cookie",
            "localStorage": "agent-local",
            "sessionStorage": "agent-session",
            "serviceWorker": "agent-worker",
        }
    )

    host = h.handler._handle_action(
        "navigate",
        {
            "action": "navigate",
            "url": "http://127.0.0.1:8123/host",
            "workspace_epoch": 1,
            "executor_generation": h.generation,
            "browser_lane": "host_verifier",
            "request_nonce": "8" * 32,
        },
    )
    assert host["ok"] is True
    host_context = daemon.state.host_context
    assert host_context is not None
    assert host_context is not agent_context
    assert host_context.storage == {}
    host_context.storage["cookie"] = "host-cookie"
    assert agent_context.storage["cookie"] == "agent-cookie"

    closed = h.handler._handle_action(
        "_close_lane",
        {
            "action": "_close_lane",
            "workspace_epoch": 1,
            "executor_generation": h.generation,
            "browser_lane": "host_verifier",
            "request_nonce": "9" * 32,
        },
    )
    assert closed["ok"] is True
    assert host_context.closed is True
    assert daemon.state.host_context is None
    assert agent_context.closed is False


@pytest.mark.parametrize(
    "url",
    [
        "file://localhost/tmp/site.html",
        "ftp://127.0.0.1/site",
        "javascript://[::1]/value",
        "custom://localhost/app",
    ],
)
def test_non_http_loopback_looking_urls_are_never_auto_reload_trusted(url: str) -> None:
    assert daemon._page_kind(url) == "external"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8123/",
        "https://127.0.0.1:8123/",
        "http://[::1]:8123/",
    ],
)
def test_literal_http_loopback_origins_are_local_preview(url: str) -> None:
    assert daemon._page_kind(url) == "local_preview"


def test_model_facing_browser_and_verifier_schemas_do_not_expose_protocol() -> None:
    internal = {
        "workspace_epoch",
        "executor_generation",
        "browser_lane",
        "request_nonce",
        "expected_daemon_instance_id",
    }
    assert internal.isdisjoint(BrowserArgs.model_fields)
    assert internal.isdisjoint(VerifyWebAppArgs.model_fields)


@pytest.mark.asyncio
async def test_nested_verify_web_app_preserves_lane_epoch_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int | None, str]] = []

    async def reachable(self, ctx, url):  # noqa: ANN001
        del self, ctx, url
        return True, 200

    async def browser_run(self, args, ctx):  # noqa: ANN001
        del self, args
        seen.append((ctx.browser_lane, ctx.browser_workspace_epoch, ctx.browser_generation))
        return ToolOutcome(
            success=True,
            content="rendered",
            structured={
                "ok": True,
                "url": "http://127.0.0.1:8123/",
                "title": "Ready",
                "text": "Ready",
                "elements": [],
                "console": [],
                "network": [],
                "visible_semantic_elements": 1,
                "screenshot_path": ".pmx/screenshots/probe.png",
            },
        )

    monkeypatch.setattr(VerifyWebAppTool, "_probe_http", reachable)
    monkeypatch.setattr(BrowserTool, "run", browser_run)
    ctx = ToolContext(
        sandbox=object(),
        workspace_path=".",
        timeout_s=5,
        capabilities=None,
        owner_id="owner",
        conversation_id="conv",
        browser_workspace_epoch=7,
        browser_generation="e" * 32,
        browser_lane="agent",
    )
    outcome = await VerifyWebAppTool().run(VerifyWebAppArgs(url="http://127.0.0.1:8123/"), ctx)
    assert outcome.success is True
    assert seen == [("agent", 7, "e" * 32)]
