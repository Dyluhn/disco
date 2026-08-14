"""A daemon that declares its own failure keeps its verdict — F-29.

Counted P1, main-86 seed 600041 (`p4_ff_react_continue`, 2026-07-28): the
model's sandbox cleanup (`pkill -f node`) killed Playwright's node driver. The
daemon's internal-error path then replied `ok:false` with an honest
`browser_daemon_unavailable / internal_error` classification but WITHOUT its
freshness acknowledgement, and the client reported "browser freshness protocol
error: freshness acknowledgement schema mismatch — do not retry the identical
call" instead. The real verdict was destroyed, the agent was told its
well-formed call was malformed, every recommended alternative rode the same
dead daemon, and the run adjudicated TOOL_ERROR_THRASH (pattern P7, third
member beside F-27 and F-28; same family as the 2026-07-27 navigate-waiver in
`test_browser_navigate_failure_freshness.py`, one layer earlier).

Two corrections, pinned here:

- client: an `ok:false` reply that carries a daemon-classified error but no
  acknowledgement AT ALL surfaces that error verbatim with the missing
  acknowledgement DISCLOSED beside it. A PRESENT-but-wrong acknowledgement
  still fails closed (a wrong daemon / nonce / epoch may not even be answering
  this request), and `ok:true` NEVER bypasses freshness — stale success
  evidence stays refused.
- daemon: once a request's protocol fields parse, every reply acknowledges —
  including the internal-error catch — and a dead Playwright transport is
  healed at most once per request by restarting the browser stack.
"""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from disco.tools import ToolContext
from disco.tools.builtin import _browser_daemon as daemon
from disco.tools.builtin.browser import _FRESHNESS_KEYS, BrowserArgs, BrowserTool

_GENERATION = "f" * 32


def _ctx(*, epoch: int | None = 2) -> ToolContext:
    return ToolContext(
        sandbox=None,
        workspace_path=".",
        timeout_s=1,
        capabilities=None,
        owner_id="owner",
        conversation_id="conversation",
        browser_workspace_epoch=epoch,
        browser_generation=_GENERATION,
        browser_lane="agent",
    )


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


def _wire(reply: dict[str, Any]) -> _WireSandbox:
    return _WireSandbox(
        [
            SimpleNamespace(exit_code=0, stdout="5" * 32, stderr=""),
            SimpleNamespace(exit_code=0, stdout=json.dumps(reply), stderr=""),
        ]
    )


# ---- client: the regression (the exact live shape) -------------------------


@pytest.mark.asyncio
async def test_daemon_declared_failure_without_ack_keeps_its_verdict_on_the_wire() -> None:
    BrowserTool._daemon_identity_pins.pop(_GENERATION, None)
    reply = {
        "ok": False,
        "error": "browser daemon internal error: TargetClosedError",
        "error_class": "browser_daemon_unavailable",
        "error_reason": "internal_error",
    }
    ctx = _ctx(epoch=1).model_copy(update={"sandbox": _wire(reply), "sessions": object()})
    outcome = await _ReadyBrowser().run(
        BrowserArgs(action="navigate", url="http://localhost:8000/"), ctx
    )
    assert outcome.success is False
    content = outcome.content or ""
    first_line = content.splitlines()[0] if content else ""
    # The daemon's own verdict is primary and verbatim…
    assert "browser daemon internal error: TargetClosedError" in first_line
    # …never replaced by the caller-blaming protocol error the live run saw.
    assert not first_line.startswith("browser freshness protocol error")
    # The missing acknowledgement is disclosed beside the verdict, not hidden.
    assert "freshness unverified" in content
    assert outcome.structured is not None
    assert outcome.structured["error_class"] == "browser_daemon_unavailable"
    assert (
        outcome.structured["freshness_unverified"] == "freshness acknowledgement schema mismatch"
    )
    # An unavailable daemon heals itself, so the advice must invite ONE retry —
    # the live run was stranded by "do not retry the identical call" here.
    assert "retry this call once" in content
    assert "do not retry the identical call" not in content


# ---- client: what must STILL fail closed -----------------------------------


@pytest.mark.asyncio
async def test_success_without_ack_is_still_a_protocol_failure_on_the_wire() -> None:
    BrowserTool._daemon_identity_pins.pop(_GENERATION, None)
    reply = {"ok": True, "url": "http://localhost:8000/", "title": "t"}
    ctx = _ctx(epoch=1).model_copy(update={"sandbox": _wire(reply), "sessions": object()})
    outcome = await _ReadyBrowser().run(
        BrowserArgs(action="navigate", url="http://localhost:8000/"), ctx
    )
    assert outcome.success is False
    assert "freshness acknowledgement schema mismatch" in (outcome.content or "")
    assert outcome.structured is not None
    assert outcome.structured["error_class"] == "freshness_protocol_invalid"


def test_unmasking_is_narrower_than_the_ok_false_path() -> None:
    err = "freshness acknowledgement schema mismatch"
    unmask = BrowserTool._daemon_failure_despite_missing_freshness
    # ok:true never bypasses freshness.
    assert unmask({"ok": True, "error": "x"}, err) is None
    # A PRESENT acknowledgement — even malformed — keeps failing closed; the
    # wrong-authority pins in test_browser_navigate_failure_freshness.py stay
    # in force.
    assert unmask({"ok": False, "freshness": {}, "error": "x"}, err) is None
    # A failure that does not even declare an error has no verdict to keep.
    assert unmask({"ok": False}, err) is None
    # The live shape unmasks.
    kept = unmask({"ok": False, "error": "browser daemon internal error: TargetClosedError"}, err)
    assert kept is not None
    assert kept.success is False


# ---- daemon: every post-protocol reply acknowledges ------------------------


def _handler() -> Any:
    return daemon.BrowserHandler.__new__(daemon.BrowserHandler)


def _params(nonce: str, *, epoch: int | None = 2) -> dict[str, Any]:
    return {
        "action": "navigate",
        "url": "http://localhost:8000/",
        "workspace_epoch": epoch,
        "executor_generation": _GENERATION,
        "browser_lane": "agent",
        "request_nonce": nonce,
    }


def test_dispatch_exception_replies_with_a_full_valid_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(daemon.BrowserHandler, "_dispatch_parsed", _raise)
    monkeypatch.setattr(daemon.BrowserHandler, "_heal_dead_transport", staticmethod(lambda: False))
    nonce = secrets.token_hex(16)
    reply = _handler()._handle_action("navigate", _params(nonce))
    assert reply["ok"] is False
    assert reply["error_class"] == "browser_daemon_unavailable"
    assert reply["error_reason"] == "internal_error"
    assert "browser daemon internal error: RuntimeError" in reply["error"]
    fresh = reply["freshness"]
    assert set(fresh) == set(_FRESHNESS_KEYS)
    assert fresh["request_nonce"] == nonce
    assert fresh["lane"] == "agent"
    assert fresh["executor_generation"] == _GENERATION
    assert fresh["requested_epoch"] == 2
    # End-to-end: the internal-error reply passes the client's own freshness
    # validation, so the daemon's verdict — not a protocol error — reaches the
    # agent.
    BrowserTool._daemon_identity_pins.pop(_GENERATION, None)
    validation = BrowserTool._freshness_response_error(
        reply,
        ctx=_ctx(epoch=2),
        request_nonce=nonce,
        expected_daemon_id=daemon.DAEMON_INSTANCE_ID,
    )
    assert validation is None


def test_dead_transport_heals_once_and_the_action_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"dispatch": 0, "stop": 0, "start": 0}

    def _dispatch(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls["dispatch"] += 1
        if calls["dispatch"] == 1:
            raise RuntimeError("transport is closed")
        return {"ok": True, "freshness": {"stub": True}}

    monkeypatch.setattr(daemon.BrowserHandler, "_dispatch_parsed", _dispatch)
    monkeypatch.setattr(
        daemon.state, "browser", SimpleNamespace(is_connected=lambda: False), raising=False
    )
    monkeypatch.setattr(
        daemon.state, "stop", lambda: calls.__setitem__("stop", calls["stop"] + 1), raising=False
    )
    monkeypatch.setattr(
        daemon.state,
        "start",
        lambda display=None: calls.__setitem__("start", calls["start"] + 1),
        raising=False,
    )
    reply = _handler()._handle_action("navigate", _params(secrets.token_hex(16)))
    assert reply["ok"] is True
    assert calls == {"dispatch": 2, "stop": 1, "start": 1}


def test_a_live_transport_never_triggers_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"start": 0}

    def _raise(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise ValueError("a genuine action bug")

    monkeypatch.setattr(daemon.BrowserHandler, "_dispatch_parsed", _raise)
    monkeypatch.setattr(
        daemon.state, "browser", SimpleNamespace(is_connected=lambda: True), raising=False
    )
    monkeypatch.setattr(
        daemon.state,
        "start",
        lambda display=None: calls.__setitem__("start", calls["start"] + 1),
        raising=False,
    )
    reply = _handler()._handle_action("navigate", _params(secrets.token_hex(16)))
    assert reply["ok"] is False
    assert "browser daemon internal error: ValueError" in reply["error"]
    assert set(reply["freshness"]) == set(_FRESHNESS_KEYS)
    assert calls["start"] == 0


def test_acknowledgement_survives_an_unreadable_page(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DeadPage:
        @property
        def url(self) -> str:
            raise RuntimeError("transport is closed")

    lane = daemon.state.lane("agent")
    monkeypatch.setattr(lane, "page", _DeadPage())
    fresh = daemon.BrowserHandler._freshness("agent", _GENERATION, "a" * 32, 2, False)
    assert fresh["page_kind"] == "uninitialized"
