"""Unit tests for the Disco messaging bridge — full create→kick→poll→result loop and
the Telegram relay, driven against an httpx.MockTransport (no network, instant poll).
The Telegram I/O itself (a real bot token round-trip) is out of scope here; we verify
the bridge LOGIC: allow-listing, per-chat owner, reply formatting, error relay.
"""

from __future__ import annotations

import json

import httpx
import pytest
from disco_bot import DiscoClient, TaskResult, TelegramBridge, _format_reply


async def _noop_sleep(_s: float) -> None:
    return None


def _agent_handler(state_sequence: list[str], events: list[dict] | None = None):
    """A handler fn standing in for the agent-server. /state returns the next status
    from `state_sequence` each call (repeating the last), so we script RUNNING→FINISHED."""
    calls = {"state": 0, "create": 0, "messages": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/conversations":
            calls["create"] += 1
            return httpx.Response(200, json={"conversation_id": "conv_abc", "surface": "research"})
        if req.method == "POST" and path.endswith("/messages"):
            calls["messages"] += 1
            return httpx.Response(200, json={"event_id": "ev1", "seq": 1})
        if req.method == "GET" and path.endswith("/state"):
            i = min(calls["state"], len(state_sequence) - 1)
            calls["state"] += 1
            return httpx.Response(200, json={"execution_status": state_sequence[i]})
        if req.method == "GET" and path.endswith("/events"):
            return httpx.Response(200, json={"events": events or []})
        return httpx.Response(404, json={"detail": f"unrouted {req.method} {path}"})

    return handler, calls


def _client(handler, **kw) -> DiscoClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agent")
    return DiscoClient("http://agent", http, sleep=_noop_sleep, **kw)


# ---- the core loop -----------------------------------------------------------


async def test_run_finished_returns_report_summary():
    events = [
        {"kind": "message", "message": {"role": "user", "content": "hi"}},
        {"kind": "report", "summary": "RRF blends ranked lists by reciprocal rank."},
    ]
    handler, calls = _agent_handler(["RUNNING", "RUNNING", "FINISHED"], events)
    res = await _client(handler).run("explain RRF", surface="deep_research")
    assert isinstance(res, TaskResult)
    assert res.ok and res.status == "FINISHED"
    assert "reciprocal rank" in res.text
    assert res.conversation_id == "conv_abc"
    assert calls["create"] == 1 and calls["messages"] == 1
    assert calls["state"] == 3  # polled RUNNING, RUNNING, then FINISHED


async def test_run_falls_back_to_last_assistant_message():
    events = [
        {"kind": "message", "message": {"role": "assistant", "content": "first"}},
        {"kind": "message", "message": {"role": "assistant", "content": "final answer"}},
    ]
    handler, _ = _agent_handler(["FINISHED"], events)
    res = await _client(handler).run("q", surface="research")
    assert res.text == "final answer"  # latest assistant message wins


async def test_run_error_status_is_not_ok():
    handler, _ = _agent_handler(["RUNNING", "ERROR"])
    res = await _client(handler).run("q")
    assert not res.ok and res.status == "ERROR"
    assert "ERROR" in res.text


async def test_run_needs_human_is_reported_not_hung():
    # A build task that pauses for confirmation must return promptly, not poll forever.
    handler, _ = _agent_handler(["RUNNING", "WAITING_FOR_CONFIRMATION"])
    res = await _client(handler).run("build me a thing", surface="build")
    assert res.status == "WAITING_FOR_CONFIRMATION"
    assert "needs your input" in res.text


async def test_poll_times_out_without_hanging():
    handler, _ = _agent_handler(["RUNNING"])  # never terminal
    status = await _client(handler).poll_until_done("conv_abc", timeout_s=12, interval_s=4)
    assert status == "TIMEOUT"


def test_url_for_routes_by_surface():
    handler, _ = _agent_handler(["FINISHED"])
    c = _client(handler, app_base="http://ui:8088")
    assert c.url_for("conv_x", "deep_research") == "http://ui:8088/deep/conv_x"
    assert c.url_for("conv_x", "build") == "http://ui:8088/build/conv_x"
    assert c.url_for("conv_x", "research") == "http://ui:8088/history"  # no resume route


# ---- the Telegram bridge -----------------------------------------------------


def _telegram_handler(updates: list[dict], agent_handler):
    """One handler for BOTH the Telegram API and the agent-server (routed by host).
    Records every sendMessage payload in `sent`."""
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.telegram.org":
            if req.url.path.endswith("/getUpdates"):
                return httpx.Response(200, json={"result": updates})
            if req.url.path.endswith("/sendMessage"):
                sent.append(json.loads(req.content))
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404)
        return agent_handler(req)  # delegate agent-server paths

    return handler, sent


def _bridge(handler, **kw) -> TelegramBridge:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramBridge("TOKEN", "http://agent", http, **kw)


async def test_bridge_runs_task_for_allowed_chat_and_replies():
    agent_handler, _ = _agent_handler(
        ["FINISHED"], [{"kind": "message", "message": {"role": "assistant", "content": "done!"}}]
    )
    updates = [{"update_id": 10, "message": {"text": "summarize X", "chat": {"id": 555}}}]
    handler, sent = _telegram_handler(updates, agent_handler)
    bridge = _bridge(handler, allow_chat_ids={555})
    n = await bridge.poll_once(timeout_s=0)  # FINISHED on first poll → real sleep never hit
    assert n == 1
    assert len(sent) == 2  # "Working on it…" + the result reply
    assert sent[0]["chat_id"] == 555
    assert "done!" in sent[1]["text"]
    assert bridge._offset == 11  # advanced past update_id


async def test_bridge_ignores_unlisted_chat():
    agent_handler, _ = _agent_handler(["FINISHED"])
    updates = [{"update_id": 1, "message": {"text": "hi", "chat": {"id": 999}}}]
    handler, sent = _telegram_handler(updates, agent_handler)
    bridge = _bridge(handler, allow_chat_ids={555})
    await bridge.poll_once(timeout_s=0)
    assert sent == []  # silent: 999 is not allow-listed
    assert bridge._offset == 2  # but the offset still advances (no reprocessing)


def test_format_reply_truncates_and_marks_status():
    ok = TaskResult("conv_1", "FINISHED", "hello", "http://ui/history")
    assert _format_reply(ok).startswith("✓ Done")
    assert "http://ui/history" in _format_reply(ok)
    long = TaskResult("conv_1", "FINISHED", "x" * 5000, "/history")
    assert "…" in _format_reply(long) and len(_format_reply(long)) < 1700
    err = TaskResult("conv_1", "ERROR", "boom", "/history")
    assert _format_reply(err).startswith("• ERROR")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
