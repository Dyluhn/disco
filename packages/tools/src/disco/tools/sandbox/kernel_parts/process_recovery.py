"""`ProcessKernel` timeout / protocol-failure recovery.

Jupyter transports the shell reply and the IOPub idle notice on independent
ZMQ channels that may arrive in either order; recovery here proves BOTH
signals (or restarts) before reporting a result, so a still-in-flight reply
is never mistaken for a hung kernel (H299) and a genuinely hung kernel is
never reported as a false success.
"""

from __future__ import annotations

import asyncio
import logging
import time
from queue import Empty
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..kernel import KernelResult, ProcessKernel
    from .text import _BoundedTextCapture

_LOG = logging.getLogger(__name__)


async def wait_for_execute_reply(
    pk: ProcessKernel,
    kc: Any,
    msg_id: str,
    *,
    deadline: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a typed shell reply or a sanitized protocol-failure reason."""
    from .. import kernel

    polls = 0
    while polls < kernel._KERNEL_SHELL_REPLY_MAX_POLLS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "matching execute_reply was not received after idle"
        polls += 1
        try:
            reply = await kc.get_shell_msg(timeout=min(0.1, remaining))
        except (TimeoutError, Empty):
            continue
        except Exception as exc:  # noqa: BLE001 - sanitize transport failure
            return None, f"shell receive failed ({type(exc).__name__})"
        if not isinstance(reply, dict):
            return None, "shell channel returned a malformed message"
        parent_header = reply.get("parent_header")
        if not isinstance(parent_header, dict):
            return None, "shell channel returned a malformed parent header"
        if parent_header.get("msg_id") == msg_id:
            header = reply.get("header")
            content = reply.get("content")
            if (
                not isinstance(header, dict)
                or header.get("msg_type") != "execute_reply"
                or not isinstance(content, dict)
                or content.get("status") not in {"ok", "error", "abort"}
            ):
                return None, "current request received an invalid shell reply"
            return reply, None
        stale = parent_header.get("msg_id")
        _LOG.debug("Skipping non-matching shell message for %s", stale)
    return None, "matching execute_reply exceeded the shell-message budget"


async def recover_from_protocol_failure(
    pk: ProcessKernel,
    *,
    stdout: _BoundedTextCapture,
    stderr: _BoundedTextCapture,
    result_repr: str | None,
    error_traceback: str | None,
    images: list[str],
    detail: str,
) -> KernelResult:
    """Quarantine a desynchronized shell channel without fabricating success."""
    from .. import kernel

    _LOG.error("Kernel protocol failure after idle; restarting the kernel")
    restart_failure: str | None = None
    try:
        await asyncio.wait_for(pk.restart(), timeout=kernel._KERNEL_RESTART_CALL_TIMEOUT_S)
    except TimeoutError:
        restart_failure = "restart timed out"
    except Exception as exc:  # noqa: BLE001 - preserve protocol-failure truth
        restart_failure = f"restart failed ({type(exc).__name__})"

    pk._restart_failed_closed = restart_failure is not None

    protocol_error = f"KernelProtocolError: {detail}"
    if restart_failure is None:
        protocol_error += "; kernel restarted and state was lost"
    else:
        protocol_error += f"; {restart_failure}"
    if error_traceback:
        protocol_error = f"{error_traceback}\n{protocol_error}"
    result = kernel.KernelResult(
        ok=False,
        stdout="".join(stdout),
        stderr="".join(stderr),
        result_repr=result_repr,
        error_traceback=protocol_error,
        images=images,
        restarted=restart_failure is None,
        restart_attempted=True,
        restart_failed=restart_failure is not None,
        protocol_failed=True,
    )
    return await pk._apply_discipline(result)


async def recover_from_timeout(
    pk: ProcessKernel,
    kc: Any,
    msg_id: str,
    stdout: _BoundedTextCapture,
    stderr: _BoundedTextCapture,
) -> KernelResult:
    """Bound interrupt/settle/restart while preserving timeout truth."""
    from .. import kernel

    deadline = time.monotonic() + kernel._KERNEL_INTERRUPT_GRACE_S
    interrupt_failure: str | None = None
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(
            pk.interrupt(),
            timeout=min(kernel._KERNEL_INTERRUPT_CALL_TIMEOUT_S, remaining),
        )
    except TimeoutError:
        interrupt_failure = "interrupt timed out"
    except Exception as exc:  # noqa: BLE001 - recovery must remain typed
        interrupt_failure = f"interrupt failed ({type(exc).__name__})"

    settled = False
    if interrupt_failure is None:
        try:
            settled = await pk._wait_for_interrupt_settle(
                kc,
                msg_id,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 - broken channels require restart
            _LOG.exception("Kernel channels failed while settling interrupt")
            interrupt_failure = f"interrupt settle failed ({type(exc).__name__})"

    if settled:
        return kernel.KernelResult(
            ok=False,
            stdout="".join(stdout),
            stderr="".join(stderr),
            error_traceback="KeyboardInterrupt: execution timed out",
            timed_out=True,
            interrupt_attempted=True,
        )

    _LOG.warning("Kernel failed to complete interrupt protocol, restarting...")
    restart_failure: str | None = None
    try:
        await asyncio.wait_for(pk.restart(), timeout=kernel._KERNEL_RESTART_CALL_TIMEOUT_S)
    except TimeoutError:
        restart_failure = "restart timed out"
    except Exception as exc:  # noqa: BLE001 - preserve timeout result truth
        restart_failure = f"restart failed ({type(exc).__name__})"

    pk._restart_failed_closed = restart_failure is not None

    detail = interrupt_failure or "interrupt did not complete both response channels"
    if restart_failure is None:
        error = f"Kernel timed out and was restarted after {detail}"
    else:
        error = f"Kernel timed out after {detail}; {restart_failure}"
    return kernel.KernelResult(
        ok=False,
        stdout="".join(stdout),
        stderr="".join(stderr),
        error_traceback=error,
        timed_out=True,
        restarted=restart_failure is None,
        interrupt_attempted=True,
        interrupt_failed=interrupt_failure is not None,
        restart_attempted=True,
        restart_failed=restart_failure is not None,
    )


async def wait_for_interrupt_settle(
    pk: ProcessKernel,
    kc: Any,
    msg_id: str,
    *,
    deadline: float | None = None,
) -> bool:
    """Wait boundedly for the interrupted request's IOPub idle + shell reply.

    Jupyter transports the two messages on independent ZMQ channels and may
    deliver them in either order. We read IOPub first but the shell reply stays
    queued until consumed. ``queue.Empty`` is jupyter_client's normal async
    poll timeout and must be retried just like ``TimeoutError``; letting it
    escape used to return a non-timeout failure while the handler was live.
    """
    from .. import kernel

    if deadline is None:
        deadline = time.monotonic() + kernel._KERNEL_INTERRUPT_GRACE_S
    idle = False
    while not idle:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            msg = await kc.get_iopub_msg(timeout=min(0.1, remaining))
        except (TimeoutError, Empty):
            continue
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        idle = (
            msg.get("header", {}).get("msg_type") == "status"
            and msg.get("content", {}).get("execution_state") == "idle"
        )

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            reply = await kc.get_shell_msg(timeout=min(0.1, remaining))
        except (TimeoutError, Empty):
            continue
        if (
            reply.get("parent_header", {}).get("msg_id") == msg_id
            and reply.get("header", {}).get("msg_type") == "execute_reply"
        ):
            return True
