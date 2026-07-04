"""PR I1-wiring / lifecycle — the REAL `PiKernel` (Disco Pi Build Kernel Campaign,
Wave 2 Batch 3).

Drives the kernel end-to-end against a FAKE Node-less sidecar (a Python script run
with `sys.executable`, mirroring `test_pi_process.py`): the kernel ISSUES a run
token, spawns + handshakes the sidecar, DRAINS its outbound frames through the I1
mapper into the event store, and on cancel REVOKES the token + kills the process
*tree*. A second test proves an `agent_end` frame concludes the run (FINISHED +
token revoke). No model, no gateway HTTP — the sidecar emits agent_event frames
straight onto its stdout, exactly the shape `PiProcess.events()` yields.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.build_kernel.pi_kernel import PiKernel
from disco.agent_server.pi_inference import InvalidGatewayToken, PiInferenceTokenStore
from disco.core import (
    ConversationStatus,
    MessageEvent,
    SqliteEventStore,
)
from disco.core.llm import DefaultLLMRouter, ModelEntry, RouterConfig
from disco.tools import ProcessSandboxService

pytestmark = pytest.mark.asyncio

CID = "c-pi-kernel"
MODEL_KEY = "m"


class _IdleProvider:
    name = "fake"

    async def complete(self, req, *, model):  # pragma: no cover - never called
        raise AssertionError("the kernel must not invoke the model directly")

    async def stream_complete(self, req, *, model):  # pragma: no cover
        raise AssertionError("the kernel must not invoke the model directly")
        yield

    def supports(self, requirement, *, model):
        return True


def _runtime(store: SqliteEventStore) -> tuple[ConversationRuntime, PiInferenceTokenStore]:
    cfg = RouterConfig(
        models={MODEL_KEY: ModelEntry(model_id=MODEL_KEY, provider="fake", context_window=8192)},
        default_model=MODEL_KEY,
    )
    router = DefaultLLMRouter(cfg, {"fake": _IdleProvider()})
    rt = ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())
    token_store = PiInferenceTokenStore()
    rt.attach_pi_token_store(token_store)
    return rt, token_store


# A fake sidecar: read `init`, emit `ready`, read the first `prompt`, emit a
# message_end (assistant text) + a tool_execution_end + then idle on stdin so the
# kernel's cancel/aclose can reap it. Optionally emit `agent_end` (AGENT_END=1).
_FAKE_SIDECAR = r"""
import json, os, sys

def emit(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

sys.stdin.readline()  # init
emit({"type": "ready", "protocolVersion": 1, "piVersion": "fake",
      "model": "disco-selected",
      "tools": {"activeToolNames": [], "customToolCount": 14, "noTools": "builtin"}})
sys.stdin.readline()  # prompt
emit({"type": "agent_event",
      "event": {"kind": "message_end",
                "message": {"role": "assistant", "text": "Building the app now."}}})
emit({"type": "agent_event",
      "event": {"kind": "tool_execution_end", "name": "file_write"}})
if os.environ.get("AGENT_END") == "1":
    emit({"type": "agent_event", "event": {"kind": "agent_end"}})
sys.stdin.read()  # block until EOF (aclose)
"""


def _make_kernel(
    rt: ConversationRuntime, tmp_path: Path, *, agent_end: bool = False
) -> PiKernel:
    fake = tmp_path / "fake_sidecar.py"
    fake.write_text(_FAKE_SIDECAR)
    env_extra = {"AGENT_END": "1"} if agent_end else {}
    # The kernel reads os.environ at spawn; set the flag there for this test.
    for k, v in env_extra.items():
        os.environ[k] = v
    return PiKernel(
        rt,
        node_bin=sys.executable,  # run the fake with Python, not node
        entry=str(fake),
        cwd=str(tmp_path),
        artifact_base=str(tmp_path / "evidence"),
    )


async def _wait(pred, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await pred() if asyncio.iscoroutinefunction(pred) else pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _MemoryPiProcess:
    instances: list["_MemoryPiProcess"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.init: dict[str, Any] | None = None
        self.prompts: list[str] = []
        self.followups: list[str] = []
        self.closed = False
        _MemoryPiProcess.instances.append(self)

    async def start(self, init: dict[str, Any]) -> None:
        self.init = init

    async def prompt(self, text: str) -> None:
        self.prompts.append(text)

    async def followup(self, text: str) -> None:
        self.followups.append(text)

    async def cancel(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}


def _use_memory_pi_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _MemoryPiProcess.instances.clear()
    monkeypatch.setattr(
        "disco.agent_server.build_kernel.pi_kernel.PiProcess",
        _MemoryPiProcess,
    )


async def _memory_prompt_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str,
    context_pack: bool,
    build_kind: str | None = None,
    contract_seam: bool = True,
) -> tuple[PiKernel, _MemoryPiProcess]:
    if context_pack:
        monkeypatch.setenv("DISCO_CONTEXT_PACK", "1")
    else:
        monkeypatch.delenv("DISCO_CONTEXT_PACK", raising=False)
        monkeypatch.delenv("PMX_CONTEXT_PACK", raising=False)
    _use_memory_pi_process(monkeypatch)
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    store.create_conversation(CID, owner_id="local")
    rt, _token_store = _runtime(store)
    rt.set_surface(CID, "build")
    if build_kind is not None:
        rt.set_build_kind(CID, build_kind)
    if not contract_seam:
        monkeypatch.setattr(rt, "_build_contract_for", None)
    kernel = PiKernel(
        rt,
        entry=str(tmp_path / "fake_sidecar.js"),
        cwd=str(tmp_path),
        artifact_base=str(tmp_path / "evidence"),
    )
    await kernel.send_user_turn(CID, text)
    assert _MemoryPiProcess.instances
    return kernel, _MemoryPiProcess.instances[-1]


async def test_context_pack_flag_off_bootstrap_prompt_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kernel, proc = await _memory_prompt_run(
        tmp_path,
        monkeypatch,
        text="Build a static launch page.",
        context_pack=False,
        build_kind="static.site",
    )

    assert proc.prompts == ["Build a static launch page."]


async def test_context_pack_flag_on_bootstrap_prompt_uses_assembly_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kernel, proc = await _memory_prompt_run(
        tmp_path,
        monkeypatch,
        text="Build a static launch page.",
        context_pack=True,
        build_kind="static.site",
    )

    assert len(proc.prompts) == 1
    prompt = proc.prompts[0]
    assert prompt.count("<context-pack>") == 1
    assert prompt.count("</context-pack>") == 1
    assert prompt.count("--- system ---") == 2
    assert prompt.count("--- user ---") == 2
    final_turn = "--- user ---\nBuild a static launch page."
    assert "ready_for_static_site_verification" in prompt
    assert prompt.index("--- system ---") < prompt.index("## Role")
    assert prompt.index("## Role") < prompt.index("<context-pack>")
    assert prompt.index("</context-pack>") < prompt.index(final_turn)


async def test_context_pack_flag_on_resume_prompt_uses_same_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, proc = await _memory_prompt_run(
        tmp_path,
        monkeypatch,
        text="Build a static launch page.",
        context_pack=True,
        build_kind="static.site",
    )

    await kernel.resume(CID)

    assert len(proc.prompts) == 2
    prompt = proc.prompts[1]
    final_turn = "--- user ---\nContinue the build from where you left off."
    assert prompt.count("<context-pack>") == 1
    assert "ready_for_static_site_verification" in prompt
    assert prompt.index("## Role") < prompt.index("<context-pack>")
    assert prompt.index("</context-pack>") < prompt.index(final_turn)


async def test_context_pack_flag_on_absent_contract_omits_prompt_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kernel, proc = await _memory_prompt_run(
        tmp_path,
        monkeypatch,
        text="Build a custom artifact.",
        context_pack=True,
        contract_seam=False,
    )

    assert len(proc.prompts) == 1
    prompt = proc.prompts[0]
    assert prompt.count("<context-pack>") == 1
    assert "## Role" not in prompt
    assert prompt.count("--- system ---") == 1
    assert prompt.index("</context-pack>") < prompt.index("--- user ---\nBuild a custom artifact.")


async def test_start_drains_frames_into_store_then_cancel_revokes_and_kills(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    store.create_conversation(CID, owner_id="local")
    rt, token_store = _runtime(store)
    rt.set_surface(CID, "build")
    kernel = _make_kernel(rt, tmp_path)

    # send_user_turn spawns + handshakes + prompts (awaited).
    await kernel.send_user_turn(CID, "build me an app")
    session = kernel._sessions[CID]
    token = session.token
    pgid = session.proc.pgid  # type: ignore[union-attr]
    assert pgid is not None
    # The run token is live, bound to this conversation.
    rec = token_store.validate(token)
    assert rec.conversation_id == CID

    # The drain maps the assistant message_end → a stored MessageEvent (the
    # tool_execution_end maps to nothing — the bridge owns Action/Observation).
    async def _has_assistant_msg() -> bool:
        events = await store.get_events(CID)
        return any(
            isinstance(e, MessageEvent) and e.message.role == "assistant"
            and "Building the app now." in (e.message.content or "")
            for e in events
        )

    await _wait(_has_assistant_msg)

    # Cancel revokes the token AND kills the whole process tree.
    await kernel.cancel(CID)
    with pytest.raises(InvalidGatewayToken) as ei:
        token_store.validate(token)
    assert ei.value.reason == "revoked"

    for _ in range(100):
        if not _pgid_alive(pgid):
            break
        await asyncio.sleep(0.02)
    assert not _pgid_alive(pgid), "the Pi sidecar tree survived cancel"
    assert CID not in kernel._sessions  # session deregistered (idempotent)
    await kernel.cancel(CID)  # idempotent second cancel is a harmless no-op

    # The sanitized session artifact was written and contains NO run token.
    art_dir = tmp_path / "evidence" / CID
    files = list(art_dir.glob("pi-session-*.jsonl"))
    assert files, "no session artifact written"
    assert token not in files[0].read_text()


async def test_agent_end_concludes_run_and_revokes_token(tmp_path: Path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    store.create_conversation(CID, owner_id="local")
    rt, token_store = _runtime(store)
    rt.set_surface(CID, "build")
    kernel = _make_kernel(rt, tmp_path, agent_end=True)
    try:
        await kernel.send_user_turn(CID, "build me an app")
        token = kernel._sessions[CID].token

        async def _finished() -> bool:
            st = await store.get_state(CID)
            return st.execution_status is ConversationStatus.FINISHED

        await _wait(_finished)
        with pytest.raises(InvalidGatewayToken):
            token_store.validate(token)
    finally:
        os.environ.pop("AGENT_END", None)
        await kernel.cancel(CID)
