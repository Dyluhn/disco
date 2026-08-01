"""`GatewayKernel.execute` — the WebSocket message dispatch loop.

Kernel Gateway multiplexes the shell `execute_reply` and IOPub `idle` onto one
WebSocket without guaranteeing their cross-channel order, so completion
requires BOTH signals (H222) — returning on idle alone can falsely report a
successful first cell as failed when its `execute_reply` is the next frame.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .text import _BoundedTextCapture, _cap_kernel_scalar, _cap_kernel_traceback

if TYPE_CHECKING:
    from ..kernel import GatewayKernel, KernelResult


@dataclass
class _GatewayAccumulator:
    stdout: _BoundedTextCapture = field(default_factory=_BoundedTextCapture)
    stderr: _BoundedTextCapture = field(default_factory=_BoundedTextCapture)
    result_repr: str | None = None
    error_traceback: str | None = None
    images: list[str] = field(default_factory=list)
    ok: bool = False
    reply_received: bool = False
    idle_received: bool = False


def _build_execute_request(code: str, msg_id: str, session_id: str) -> dict[str, Any]:
    return {
        "header": {
            "msg_id": msg_id,
            "msg_type": "execute_request",
            "session": session_id,
            "version": "5.3",
            "date": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
            "username": "agent",
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
        "channel": "shell",
    }


async def _recv_with_timeout_recovery(
    gk: GatewayKernel,
    ws: Any,
    msg_id: str,
    timeout_s: int,
    stdout: _BoundedTextCapture,
    stderr: _BoundedTextCapture,
) -> tuple[dict[str, Any] | None, KernelResult | None]:
    """Receive one WS frame; on timeout, interrupt+wait-for-idle, then restart.

    Returns ``(message, None)`` on an ordinary receive, or ``(None, result)``
    with the terminal `KernelResult` when the timeout-recovery path itself
    concluded the exec (interrupted-and-idle, or restarted after a stuck
    interrupt).
    """
    from .. import kernel

    try:
        raw_msg = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
        return json.loads(raw_msg), None
    except TimeoutError:
        pass

    kernel._LOG.warning("Kernel execution timed out, interrupting...")
    await gk.interrupt()

    # Wait up to 5s for idle
    try:
        while True:
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            msg = json.loads(raw_msg)
            if msg.get("parent_header", {}).get("msg_id") == msg_id:
                is_status = msg.get("header", {}).get("msg_type") == "status"
                if is_status and msg["content"]["execution_state"] == "idle":
                    return None, kernel.KernelResult(
                        ok=False,
                        stdout="".join(stdout),
                        stderr="".join(stderr),
                        error_traceback="KeyboardInterrupt: execution timed out",
                        timed_out=True,
                    )
    except TimeoutError:
        kernel._LOG.warning("Interrupt timed out, restarting...")
        await gk.restart()
        return None, kernel.KernelResult(
            ok=False,
            stdout="".join(stdout),
            stderr="".join(stderr),
            error_traceback="KeyboardInterrupt: execution timed out, state lost",
            timed_out=True,
            restarted=True,
        )


def _handle_gateway_stream(content: dict[str, Any], acc: _GatewayAccumulator) -> None:
    if content.get("name") == "stdout":
        acc.stdout.append(content.get("text", ""))
    elif content.get("name") == "stderr":
        acc.stderr.append(content.get("text", ""))


async def _handle_gateway_display_data(
    gk: GatewayKernel, content: dict[str, Any], acc: _GatewayAccumulator
) -> None:
    from .text import _KERNEL_IMAGE_MAX_BYTES

    data = content.get("data", {})
    if "image/png" not in data:
        return
    gk._seq += 1
    img_path = f".pmx/plots/{gk._seq:04d}.png"
    # Since this is a container, we use the sandbox file API to write
    encoded = data["image/png"]
    if len(encoded) <= (_KERNEL_IMAGE_MAX_BYTES * 4 // 3) + 8:
        png = base64.b64decode(encoded)
        await gk._sandbox.write_file(img_path, png)
        acc.images.append(img_path)


async def _dispatch_gateway_message(
    gk: GatewayKernel, msg_type: str | None, content: dict[str, Any], acc: _GatewayAccumulator
) -> None:
    if msg_type == "stream":
        _handle_gateway_stream(content, acc)
    elif msg_type == "execute_result":
        acc.result_repr = _cap_kernel_scalar(content.get("data", {}).get("text/plain"))
    elif msg_type == "display_data":
        await _handle_gateway_display_data(gk, content, acc)
    elif msg_type == "error":
        acc.error_traceback = _cap_kernel_traceback(content.get("traceback", []))
    elif msg_type == "execute_reply":
        acc.ok = content.get("status") == "ok"
        acc.reply_received = True
    elif msg_type == "status" and content.get("execution_state") == "idle":
        acc.idle_received = True


async def execute(gk: GatewayKernel, code: str, *, timeout_s: int) -> KernelResult:
    from .. import kernel

    if not gk._ws:
        await gk.start()
    assert gk._ws is not None  # start() always populates _ws
    ws = gk._ws

    msg_id = str(uuid.uuid4())
    await ws.send(json.dumps(_build_execute_request(code, msg_id, gk._session_id)))

    acc = _GatewayAccumulator()
    # Shell ``execute_reply`` carries the authoritative success/error status,
    # while IOPub ``idle`` says output publication has drained. Kernel Gateway
    # multiplexes those channels onto one WebSocket but does not guarantee
    # their cross-channel order, so completion requires BOTH signals. Returning
    # on idle alone can falsely report a successful first cell as ``ok=False``
    # when its execute_reply is the next frame (H222).

    try:
        while True:
            msg, early_result = await _recv_with_timeout_recovery(
                gk, ws, msg_id, timeout_s, acc.stdout, acc.stderr
            )
            if early_result is not None:
                return early_result
            assert msg is not None

            msg_type = msg.get("header", {}).get("msg_type")
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue

            content = msg.get("content", {})
            await _dispatch_gateway_message(gk, msg_type, content, acc)

            if acc.reply_received and acc.idle_received:
                res = kernel.KernelResult(
                    ok=acc.ok,
                    stdout="".join(acc.stdout),
                    stderr="".join(acc.stderr),
                    result_repr=acc.result_repr,
                    error_traceback=acc.error_traceback,
                    images=acc.images,
                )
                return await gk._apply_discipline(res)

    except Exception as e:
        kernel._LOG.exception("Kernel execution failed")
        return kernel.KernelResult(
            ok=False,
            stdout="".join(acc.stdout),
            stderr="".join(acc.stderr),
            error_traceback=str(e),
        )
