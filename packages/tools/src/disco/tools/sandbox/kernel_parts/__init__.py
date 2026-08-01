"""Interior modules of `ProcessKernel` / `GatewayKernel` (`..kernel`).

Both kernel transports implement the same `KernelSession` protocol and share
the same execute/recover/discipline shape; this package holds the cohesive
pieces the two transports (and the shared plumbing) compose:

* `text` — transport-agnostic helpers with no kernel state: bounded output
  capture, gateway-diagnostic sanitizing, the auth-token derivation, the idle-
  timeout default, and the ROOT-1 `/workspace`-literal rewrite for the process
  transport.
* `process_execute` — `ProcessKernel.execute`'s IOPub dispatch loop.
* `process_recovery` — `ProcessKernel`'s timeout/protocol-failure recovery
  (interrupt, restart, quarantine).
* `gateway_startup` — `GatewayKernel._ensure_gateway`'s launch-or-reach flow.
* `gateway_execute` — `GatewayKernel.execute`'s WS message dispatch loop.

Functions that read a tuning constant tests monkeypatch on `..kernel`
(`_KERNEL_SHELL_REPLY_GRACE_S`, `_KERNEL_SHELL_REPLY_MAX_POLLS`,
`_KERNEL_RESTART_CALL_TIMEOUT_S`, `_KERNEL_INTERRUPT_GRACE_S`,
`_KERNEL_INTERRUPT_CALL_TIMEOUT_S`, `_GATEWAY_SESSION_CLEANUP_S`) resolve it
through the imported PARENT MODULE at call time (`kernel._KERNEL_...`), never
a name captured once at import top, so the patch still takes effect when the
call originates here instead of the class body.
"""

from __future__ import annotations
