"""PR I2 — the DISCO_INSPECT trace for PiKernel (§7.11).

With ``DISCO_INSPECT=1`` the kernel + the tool bridge emit the nine §7.11 spans
into the SAME inspect sink the Disco loop writes to, so they surface at
``GET /api/debug/trace/{cid}``:

  kernel_start, model_selected, pi_event, pi_tool_start, pi_tool_end,
  pause_for_plan, resume_after_approval, finish_request, verification_result

This test drives every span source — a fake-sidecar PiKernel run (lifecycle +
``agent_end`` finish), a bridged tool call (tool spans), and a plan gate
(pause/resume) — against one conversation, then asserts all nine names appear in
the real debug-trace snapshot. No model, no real network.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.build_kernel.pi_kernel import PiKernel
from disco.agent_server.pi_inference import PiInferenceTokenStore
from disco.agent_server.routes.debug import make_debug_router
from disco.agent_server.routes.pi_tools import make_pi_tools_router
from disco.core import ConversationStatus, SqliteEventStore
from disco.core.inspect import install, registry
from disco.core.llm import DefaultLLMRouter, ModelEntry, RouterConfig
from disco.tools import ProcessSandboxService
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio

CID = "c-inspect"
KERNEL = "k-inspect"
MODEL_KEY = "m"

_NINE_SPANS = {
    "kernel_start",
    "model_selected",
    "pi_event",
    "pi_tool_start",
    "pi_tool_end",
    "pause_for_plan",
    "resume_after_approval",
    "finish_request",
    "verification_result",
}

_FAKE = r"""
import json, sys
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
sys.stdin.readline()
emit({"type":"ready","protocolVersion":1,"piVersion":"fake","model":"disco-selected",
      "tools":{"activeToolNames":[],"customToolCount":14,"noTools":"builtin"}})
sys.stdin.readline()
emit({"type":"agent_event","event":{"kind":"message_end",
      "message":{"role":"assistant","text":"done"}}})
emit({"type":"agent_event","event":{"kind":"agent_end"}})
sys.stdin.read()
"""


class _IdleProvider:
    name = "fake"

    async def complete(self, req, *, model):  # pragma: no cover
        raise AssertionError("model not used")

    async def stream_complete(self, req, *, model):  # pragma: no cover
        raise AssertionError("model not used")
        yield

    def supports(self, requirement, *, model):
        return True


async def _post(app, kernel_id, tool, token, body):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as c:
        return await c.post(
            f"/internal/pi-kernel/{kernel_id}/tools/{tool}",
            json=body,
            headers={"authorization": f"Bearer {token}"},
        )


async def test_inspect_trace_shows_the_nine_pikernel_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_INSPECT", "1")
    install()  # attach the span handler (idempotent)
    registry().clear()

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    store.create_conversation(CID, owner_id="local")
    cfg = RouterConfig(
        models={MODEL_KEY: ModelEntry(model_id=MODEL_KEY, provider="fake", context_window=8192)},
        default_model=MODEL_KEY,
    )
    router = DefaultLLMRouter(cfg, {"fake": _IdleProvider()})
    rt = ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())
    rt.set_surface(CID, "build")
    token_store = PiInferenceTokenStore()
    rt.attach_pi_token_store(token_store)

    app = FastAPI()
    app.include_router(make_pi_tools_router(token_store, rt))
    app.include_router(make_debug_router(store, rt))

    # (a) a bridged tool call → pi_tool_start / pi_tool_end (file_list is not a write).
    bridge_token = token_store.issue(
        kernel_id=KERNEL, conversation_id=CID, model_key=MODEL_KEY, ttl_s=300, budget_tokens=1000
    )
    rt._pi_kernel._managed.add(CID)  # so the write-block path is live (file_list still runs)
    rt._pi_kernel._plan_approved[CID] = True  # don't block this read-only probe path
    r = await _post(app, KERNEL, "file_list", bridge_token, {"call_id": "t1", "arguments": {}})
    assert r.status_code == 200

    # (b) a plan gate → pause_for_plan / resume_after_approval.
    pk = rt._pi_kernel
    gate = asyncio.create_task(
        pk._handle_submit_plan(CID, {"summary": "s", "steps": [{"title": "x"}]}, "p1")
    )

    async def _awaiting() -> bool:
        return (await store.get_state(CID)).execution_status is ConversationStatus.AWAITING_PLAN_APPROVAL

    for _ in range(200):
        if await _awaiting():
            break
        await asyncio.sleep(0.01)
    await pk.approve_plan(CID)
    await gate

    # (c) a fake-sidecar run → kernel_start, model_selected, pi_event, finish_request,
    #     verification_result (agent_end concludes the run).
    fake = tmp_path / "fake.py"
    fake.write_text(_FAKE)
    kernel = PiKernel(
        rt, node_bin=sys.executable, entry=str(fake), cwd=str(tmp_path),
        artifact_base=str(tmp_path / "ev"),
    )
    # Drive the lifecycle through THIS kernel instance (the runtime's _pi_kernel is a
    # different instance, but spans are keyed by cid, so they share one trace).
    try:
        await kernel.send_user_turn(CID, "build it")

        async def _finished() -> bool:
            return (await store.get_state(CID)).execution_status is ConversationStatus.FINISHED

        for _ in range(300):
            if await _finished():
                break
            await asyncio.sleep(0.01)
    finally:
        await kernel.cancel(CID)

    # The real debug-trace endpoint shows all nine span names for this conversation.
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as c:
        resp = await c.get(f"/api/debug/trace/{CID}")
    assert resp.status_code == 200
    span_names = {s.get("span") for s in resp.json()["spans"]}
    missing = _NINE_SPANS - span_names
    assert not missing, f"missing spans: {missing}"
