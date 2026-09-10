"""`ProcessKernel.execute` — the IOPub message dispatch loop.

Reads Jupyter IOPub messages for one `execute_request` until the matching
`execute_reply` + IOPub idle pair proves completion (or the wall-clock
timeout fires, handed to `process_recovery.recover_from_timeout`).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from queue import Empty
from typing import TYPE_CHECKING, Any

from . import process_recovery
from .text import _BoundedTextCapture, _cap_kernel_scalar, _cap_kernel_traceback

if TYPE_CHECKING:
    from ..kernel import KernelResult, ProcessKernel


@dataclass
class _ExecAccumulator:
    stdout: _BoundedTextCapture = field(default_factory=_BoundedTextCapture)
    stderr: _BoundedTextCapture = field(default_factory=_BoundedTextCapture)
    result_repr: str | None = None
    error_traceback: str | None = None
    images: list[str] = field(default_factory=list)


def _handle_stream(acc: _ExecAccumulator, content: dict[str, Any]) -> None:
    if content.get("name") == "stdout":
        acc.stdout.append(content.get("text", ""))
    elif content.get("name") == "stderr":
        acc.stderr.append(content.get("text", ""))


def _handle_display_data(pk: ProcessKernel, acc: _ExecAccumulator, content: dict[str, Any]) -> None:
    from .text import _KERNEL_IMAGE_MAX_BYTES

    data = content.get("data", {})
    if "image/png" not in data:
        return
    pk._seq += 1
    img_path = f".pmx/plots/{pk._seq:04d}.png"
    full_path = pk._workspace / img_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = data["image/png"]
    if len(encoded) <= (_KERNEL_IMAGE_MAX_BYTES * 4 // 3) + 8:
        with open(full_path, "wb") as f:
            f.write(base64.b64decode(encoded))
        acc.images.append(img_path)


async def _handle_idle_status(
    pk: ProcessKernel, kc: Any, msg_id: str, acc: _ExecAccumulator
) -> KernelResult:
    from .. import kernel

    reply, protocol_failure = await pk._wait_for_execute_reply(
        kc,
        msg_id,
        deadline=time.monotonic() + kernel._KERNEL_SHELL_REPLY_GRACE_S,
    )
    if reply is None:
        assert protocol_failure is not None
        return await process_recovery.recover_from_protocol_failure(
            pk,
            stdout=acc.stdout,
            stderr=acc.stderr,
            result_repr=acc.result_repr,
            error_traceback=acc.error_traceback,
            images=acc.images,
            detail=protocol_failure,
        )

    ok = reply.get("content", {}).get("status") == "ok"
    res = kernel.KernelResult(
        ok=ok,
        stdout="".join(acc.stdout),
        stderr="".join(acc.stderr),
        result_repr=acc.result_repr,
        error_traceback=acc.error_traceback,
        images=acc.images,
    )
    return await pk._apply_discipline(res)


async def _dispatch_iopub_message(
    pk: ProcessKernel, kc: Any, msg_id: str, msg: dict[str, Any], acc: _ExecAccumulator
) -> KernelResult | None:
    content = msg.get("content", {})
    msg_type = msg.get("header", {}).get("msg_type")

    if msg.get("parent_header", {}).get("msg_id") != msg_id:
        return None

    if msg_type == "stream":
        _handle_stream(acc, content)
    elif msg_type == "execute_result":
        acc.result_repr = _cap_kernel_scalar(content.get("data", {}).get("text/plain"))
    elif msg_type == "display_data":
        _handle_display_data(pk, acc, content)
    elif msg_type == "error":
        acc.error_traceback = _cap_kernel_traceback(content.get("traceback", []))
    elif msg_type == "status" and content.get("execution_state") == "idle":
        return await _handle_idle_status(pk, kc, msg_id, acc)
    return None


async def execute(pk: ProcessKernel, code: str, *, timeout_s: int) -> KernelResult:
    from .. import kernel
    from .text import _rewrite_process_workspace_literals

    if pk._restart_failed_closed:
        return kernel.KernelResult(
            ok=False,
            stdout="",
            stderr="",
            error_traceback=(
                "KernelUnavailableError: previous kernel restart failed; "
                "recreate the sandbox session"
            ),
            restart_failed=True,
        )
    if not pk._kc:
        await pk.start()
    assert pk._kc is not None  # start() always populates both _km and _kc
    kc = pk._kc

    try:
        code = _rewrite_process_workspace_literals(code, pk._workspace)
    except kernel.SandboxError as exc:
        return kernel.KernelResult(
            ok=False,
            stdout="",
            stderr="",
            error_traceback=f"SandboxError: {exc}",
        )

    msg_id = kc.execute(code)
    acc = _ExecAccumulator()

    try:
        while True:
            try:
                # We use a smaller interval to check for timeout more frequently
                msg = await kc.get_iopub_msg(timeout=timeout_s)
            except (TimeoutError, Empty):
                # Timeout protocol (EXACT): interrupt -> prove BOTH cross-channel
                # completion signals within 5s -> restart if either is missing.
                # An IOPub idle alone is not enough: its matching shell reply can
                # still be in flight, and starting the next cell in that window
                # races the interrupted handler. On teardown that race surfaced as
                # ipykernel's "Socket operation on non-socket" and a lost result.
                kernel._LOG.warning("Kernel execution timed out, interrupting...")
                return await process_recovery.recover_from_timeout(
                    pk, kc, msg_id, acc.stdout, acc.stderr
                )

            result = await _dispatch_iopub_message(pk, kc, msg_id, msg, acc)
            if result is not None:
                return result

    except Exception as e:
        kernel._LOG.exception("Kernel execution failed")
        return kernel.KernelResult(
            ok=False,
            stdout="".join(acc.stdout),
            stderr="".join(acc.stderr),
            error_traceback=str(e),
        )
