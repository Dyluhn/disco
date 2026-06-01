"""RouterAgent — the concrete `Agent` (agent-loop-contract.md §3).

The brain: given the model-facing View and the available tools, it builds a
CompletionRequest(role=AGENT_DRIVER), calls the LLMRouter, and maps the
response's FIRST ProposedToolCall to a ToolCall — the only place the model is
consulted for action selection. It returns exactly one AgentStep (one-action-
per-iteration). It does NOT execute tools.

Typed against `DefaultLLMRouter` (not the bare `LLMRouter` protocol) because it
uses that router's `CallContext` extension to thread the conversation id, the
(now-advisory) runtime signals, and the per-conversation `model_override` — the
backend hook for the main-screen model pill (router contract §4, v1.2).

v1.2 note: under the deterministic router these `overflow_signal` fields
(difficulty, tool-error streak, confidence) no longer steer model selection; they
are carried as advisory metadata only. The seam is unchanged — the agent still
asks for the AGENT_DRIVER role and gets back the assigned model — so the loop did
NOT need to change. The one addition is `model_override`, which lets the operator
pin the driver model for THIS conversation from the UI.

v1 finish convention [INTERIOR]: if the model returns no tool call, the agent is
treated as finished (it stopped acting). A dedicated `finish` tool is the likely
refinement once the Tool/Sandbox contract lands; the loop already supports a
thought-only no-op step independently of this Agent's choice.

v1 risk [INTERIOR]: the response carries no structured self-risk, so
`self_assessed_risk` stays UNKNOWN — the independent SecurityAnalyzer is the real
signal (BoD §17.2).
"""

from __future__ import annotations

import uuid

from ..events import ToolCall
from ..llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelRole,
    OperatingMode,
    OverflowSignal,
    Requirement,
)
from ..view import View
from .boundaries import AgentStep


class RouterAgent:
    """[CONTRACT] An `Agent` that wraps the LLM router."""

    def __init__(
        self,
        router: DefaultLLMRouter,
        *,
        conversation_id: str | None = None,
        requirements: frozenset[Requirement] = frozenset({Requirement.TOOL_CALLING}),
        model_override: str | None = None,
    ) -> None:
        self._router = router
        self._cid = conversation_id
        self._requirements = requirements
        # v1.2 model-pill hook: the driver model key chosen for THIS conversation,
        # overriding the settings assignment. None → follow settings. Set by the
        # app/agent server from the per-conversation pill selection.
        self._model_override = model_override

    async def step(
        self,
        view: View,
        tools: list,
        *,
        mode: OperatingMode,
        overflow_signal: OverflowSignal,
    ) -> AgentStep:
        req = CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.AGENT_DRIVER,
                difficulty=overflow_signal.difficulty,
                requirements=self._requirements,
                mode=mode,
            ),
            messages=view.messages,
            tools=tools,
            request_id=f"req_{uuid.uuid4().hex}",
        )
        ctx = CallContext(
            conversation_id=self._cid,
            consecutive_tool_errors=overflow_signal.consecutive_tool_errors,
            last_local_confidence=overflow_signal.last_local_confidence,
            model_override=self._model_override,
        )
        resp = await self._router.complete(req, context=ctx)

        if resp.tool_calls:
            # One-action-per-iteration: take the FIRST proposed call (§3).
            pc = resp.tool_calls[0]
            return AgentStep(
                thought=resp.text,
                tool_call=ToolCall(tool_name=pc.tool_name, arguments=pc.arguments),
                finished=False,
                llm_response_id=resp.request_id,
            )
        # No tool call → the agent has stopped acting → finished (v1 convention).
        return AgentStep(
            thought=resp.text, tool_call=None, finished=True, llm_response_id=resp.request_id
        )
