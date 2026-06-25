"""PR D2 + D3 — the Pi tool bridge endpoint.

``POST /internal/pi-kernel/{kernel_id}/tools/{tool_name}`` is the loopback,
run-scoped seam through which a Pi custom tool (running in the sidecar) drives ONE
Disco tool call. The sidecar holds no capability of its own (§1.1 "tool bridge is
HTTP, not stdio"): it forwards ``{call_id, arguments}`` here, and the orchestrator
runs the action against the conversation's ``DefaultToolExecutor`` and appends the
Action/Observation pair to the event store — the SAME pairing the in-process agent
loop uses (``observe.execute_and_observe``).

Security mirrors the inference gateway (``routes/pi_inference.py``):

* **Loopback only.** A remote peer is rejected even with a token (the bridge is a
  LOCAL capability, never a network credential). Reuses ``_client_is_local``.
* **Bearer-gated + kernel-bound.** The request must carry a live, unrevoked,
  unexpired run-token (``PiInferenceTokenStore``) AND that token's ``kernel_id``
  must match the path ``{kernel_id}`` — so a token minted for one kernel cannot
  drive another's tools. The conversation is resolved from the TOKEN, never the
  request.
* **D3 server-side allowlist.** Only the fixed minimal tool set
  (``_ALLOWED_PI_TOOLS``) is accepted; anything else is REFUSED with a structured
  blocked ``ToolResult`` (never executed). Defense in depth on top of the
  client-side ``tools.ts`` set.
* **Never raises.** The executor contract is "always a ``ToolResult``, never
  raise"; this route preserves it — every failure becomes a structured result.

Gate tools (``submit_plan`` / ``ask_user`` / ``clarify``) are handed off to the
``PiKernel`` long-poll handlers (E1/E2/E3): the kernel appends the existing Disco
gate event (``PlanEvent`` / ``AWAITING_PLAN_APPROVAL`` etc.), parks, and awaits an
asyncio future that ``approve_plan`` / ``reject_plan`` / the user's next turn
resolves — the verdict/answer returns here as the tool result, pausing Pi's loop
over THIS held request. Write tools (file_write/replace/insert, shell_exec) are
additionally refused with a structured block until a plan is approved.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from disco.core import ToolCall, ToolResult
from disco.core.inspect import inspect_enabled
from disco.core.obs import log_event
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..pi_inference import InvalidGatewayToken, PiInferenceTokenStore
from .pi_inference import _bearer, _client_is_local, _read_request_body_bounded

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

_LOG = logging.getLogger("disco.pi_tools")

# Cap on the bridged request body. Generous (a file_write content can be large)
# but bounded so an arbitrarily large body can't be buffered into memory (DoS).
_MAX_TOOL_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB

# D3 — the EXACT minimal tool set the Pi build kernel may call. Mirrors the
# client-side ``DISCO_TOOL_NAMES`` in ``pi-kernel/src/tools.ts``; enforced here as
# the authoritative backstop (a tool outside this set is refused, never executed).
_ALLOWED_PI_TOOLS: frozenset[str] = frozenset(
    {
        "file_read",
        "file_write",
        "file_replace_lines",
        "file_insert_lines",
        "file_list",
        "shell_exec",
        "preview_start",
        "preview_status",
        "preview_logs",
        "finish",
        "think",
        "submit_plan",
        "ask_user",
        "clarify",
    }
)

# Gate tools — their plan-pause / ask / clarify handlers are held PYTHON-side by
# `PiKernel` (E1/E2/E3): the bridge hands off to the kernel's long-poll handler.
_GATE_TOOLS: frozenset[str] = frozenset({"submit_plan", "ask_user", "clarify"})

# NOTE (EPIC-D graft): the former narrow `_WRITE_TOOLS` pre-approval gate was REMOVED.
# It keyed on a kernel bookkeeping flag and only covered the four write tools — so
# non-write side effects (e.g. preview_start) could run pre-approval. Every non-gate
# tool now routes through the in-process `PiToolBridge` (PiKernel.route_pi_tool),
# whose REAL planning gate (loop.mode == PLANNING blocks writes/shell/finish until
# `approve_plan` flips the loop into execution mode — engine.py:945-969) is the SOLE,
# authoritative pre-approval gate. The bridge ALSO applies hard-deny, risk/confirm,
# the K1 elision guard, the halted-state gate, and the stale-plan replan gate.


def _span(name: str, conversation_id: str, **fields: object) -> None:
    """Emit one §7.11 inspect span (I2) into the shared inspect sink, gated on
    `DISCO_INSPECT=1`. The kernel emits the lifecycle/plan spans; the bridge owns
    the per-tool `pi_tool_start` / `pi_tool_end` spans (it is the single place a
    bridged tool executes)."""
    if not inspect_enabled():
        return
    try:
        log_event(name, cid=conversation_id, **fields)
    except Exception:  # noqa: BLE001 — tracing must never break a tool call
        pass


def _result_json(result: ToolResult) -> JSONResponse:
    """Serialize a ToolResult as the bridge's JSON response (always HTTP 200 — the
    executor contract surfaces failure via ``success=false``, not an HTTP error)."""
    return JSONResponse(result.model_dump(mode="json"))


def _blocked_result(tool_name: str, call_id: str) -> ToolResult:
    """A structured 'refused by the allowlist' result (D3). success=false; nothing
    was executed and no Action/Observation was appended."""
    return ToolResult(
        call_id=call_id,
        tool_name=tool_name,
        success=False,
        content=(
            f"tool {tool_name!r} is not in the Pi build-kernel allowlist and was "
            "refused; it was NOT executed"
        ),
        structured={"kind": "tool_not_allowed", "tool_name": tool_name},
        error=f"tool {tool_name!r} is not allowed",
    )


def _not_yet_wired_result(tool_name: str, call_id: str) -> ToolResult:
    """A structured placeholder for an allowlisted GATE tool whose handler is not
    yet wired (E batch). success=false; nothing was executed."""
    return ToolResult(
        call_id=call_id,
        tool_name=tool_name,
        success=False,
        content=(
            f"gate tool {tool_name!r} is allowlisted but its handler is not yet "
            "wired (lands in the E batch: submit_plan/ask_user/clarify plan gate). "
            "No action was taken."
        ),
        structured={"kind": "not_yet_wired", "tool_name": tool_name},
        error=f"gate tool {tool_name!r} not yet wired",
    )


def make_pi_tools_router(
    token_store: PiInferenceTokenStore,
    runtime: ConversationRuntime | None,
    *,
    trust_local_no_peer: bool = False,
) -> APIRouter:
    """Build the Pi tool-bridge router. ``token_store`` is the shared run-scoped
    token store (the SAME one the inference gateway validates against).
    ``trust_local_no_peer`` opts a trusted unix-socket deployment into accepting
    peerless requests; defaults False (fail-closed), matching the gateway."""
    router = APIRouter()

    @router.post("/internal/pi-kernel/{kernel_id}/tools/{tool_name}")
    async def pi_tool_bridge(kernel_id: str, tool_name: str, request: Request) -> Response:
        # 1) Loopback-only. A remote peer never reaches the executor, token or not.
        if not _client_is_local(request, trust_local_no_peer=trust_local_no_peer):
            return JSONResponse(
                {"error": {"message": "tool bridge is loopback-only", "type": "forbidden"}},
                status_code=403,
            )

        # 2) Validate the bearer run-token (live, unrevoked, unexpired). NEVER log it.
        token = _bearer(request)
        try:
            rec = token_store.validate(token)
        except InvalidGatewayToken as exc:
            _LOG.info("pi-tools rejected token: %s", exc.reason)  # reason only, never the token
            return JSONResponse(
                {"error": {"message": "invalid gateway token", "type": "unauthorized"}},
                status_code=401,
            )

        # 2b) Bind the token to THIS kernel: a token minted for one kernel must not
        #     drive another's tools. The conversation is resolved from the token.
        if rec.kernel_id != kernel_id:
            _LOG.info(
                "pi-tools token/kernel mismatch token=%s path_kernel=%s",
                rec.fingerprint, kernel_id,
            )
            return JSONResponse(
                {"error": {"message": "token not valid for this kernel", "type": "forbidden"}},
                status_code=403,
            )

        # 3) Read + parse the bridged body (bounded). The call_id is needed even for
        #    a refusal so the result correlates to Pi's tool call.
        raw = await _read_request_body_bounded(request, _MAX_TOOL_BODY_BYTES)
        if raw is None:
            return JSONResponse(
                {"error": {"message": "request body too large", "type": "payload_too_large"}},
                status_code=413,
            )
        try:
            body = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            body = None
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "request body must be a JSON object", "type": "bad_request"}},
                status_code=400,
            )
        call_id = body.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"pi_{uuid4().hex}"
        arguments = body.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        # 4) D3 allowlist — refuse anything outside the minimal set BEFORE the
        #    executor is ever consulted. Structured block, never executed.
        if tool_name not in _ALLOWED_PI_TOOLS:
            _LOG.info(
                "pi-tools blocked out-of-allowlist tool=%s token=%s",
                tool_name, rec.fingerprint,
            )
            return _result_json(_blocked_result(tool_name, call_id))

        if runtime is None:
            # Wire-only mode (no loop / executor): there is nothing to execute.
            return _result_json(
                ToolResult(
                    call_id=call_id,
                    tool_name=tool_name,
                    success=False,
                    content="tool bridge has no runtime wired; cannot execute",
                    structured={"kind": "no_runtime"},
                    error="no runtime",
                )
            )

        conversation_id = rec.conversation_id

        # The single PiKernel instance owns the plan/ask/clarify gate state for every
        # conversation it drives (the pinned kernel IS this instance when Pi is
        # selected). The raw D2 bridge (tests / no Pi run) has no kernel → gates +
        # write-block degrade off, so D2 behavior is unchanged.
        pi_kernel = getattr(runtime, "_pi_kernel", None)

        # 5) Gate tools (E1/E2/E3): the plan-pause / ask / clarify gate is held
        #    PYTHON-side over THIS request — the kernel appends the Disco gate event,
        #    parks at AWAITING_PLAN_APPROVAL / AWAITING_USER_QUESTION, and LONG-POLLS
        #    an asyncio future until a control op (approve/reject/answer) resolves it.
        #    The verdict/answer returns here as the tool result, so Pi's loop pauses
        #    naturally and then continues the SAME session.
        if tool_name in _GATE_TOOLS:
            if pi_kernel is None:
                return _result_json(_not_yet_wired_result(tool_name, call_id))
            _span("pi_tool_start", conversation_id, tool=tool_name, call_id=call_id, gate=True)
            try:
                result = await pi_kernel.handle_gate_tool(
                    conversation_id, tool_name, arguments, call_id
                )
            except Exception as exc:  # noqa: BLE001 — a gate failure must not 500 the bridge
                _LOG.warning(
                    "pi-tools gate %s failed token=%s: %s", tool_name, rec.fingerprint, exc
                )
                result = ToolResult(
                    call_id=call_id,
                    tool_name=tool_name,
                    success=False,
                    content=f"gate tool {tool_name!r} failed: {exc}",
                    structured={"kind": "gate_error", "tool_name": tool_name},
                    error=str(exc),
                )
            _span("pi_tool_end", conversation_id, tool=tool_name, success=result.success, gate=True)
            return _result_json(result)

        # 6) Route through the in-process SAFETY WRAPPER (EPIC-D graft). A Pi tool call
        #    is driven through `PiKernel.route_pi_tool` → `PiToolBridge.route`, which
        #    applies the SAME ordered gate stack `AgentLoop._run_drive` does (reconcile
        #    → halted → replan → planning → virtual → hard-deny → risk/confirm → append
        #    → execute/observe) — NOT a bare `executor.execute()`. The bridge appends
        #    the Action/Observation (or AgentErrorEvent) pair and the kernel maps the
        #    structured outcome (incl. VIRTUAL finish/DoD + CONFIRM_REQUIRED long-poll)
        #    back to the ToolResult Pi expects. When no PiKernel is wired (a bare D2
        #    bridge with no managed run), fall back to the executor-only path.
        tool_call = ToolCall(tool_name=tool_name, arguments=arguments, call_id=call_id)
        _span("pi_tool_start", conversation_id, tool=tool_name, call_id=call_id)
        try:
            if pi_kernel is not None:
                result = await pi_kernel.route_pi_tool(conversation_id, tool_call)
            else:
                result = await runtime.execute_pi_tool(conversation_id, tool_call)
        except Exception as exc:  # noqa: BLE001 — the bridge/executor may raise; never propagate
            _LOG.warning(
                "pi-tools bridge could not execute tool=%s token=%s: %s",
                tool_name, rec.fingerprint, exc,
            )
            result = ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                content=f"tool bridge could not execute {tool_name!r}: {exc}",
                structured={"kind": "bridge_error", "tool_name": tool_name},
                error=str(exc),
            )
        _span("pi_tool_end", conversation_id, tool=tool_name, success=result.success)
        return _result_json(result)

    return router
