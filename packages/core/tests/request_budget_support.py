"""Shared builders and fakes for request-budget tests.

Extracted from ``test_request_budget.py`` so the test module stays under the
test-module logical-LOC limit. These helpers are imported by both
``test_request_budget.py`` and ``test_request_budget_loop.py``.
"""

from __future__ import annotations

from typing import Any

from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
)
from disco.core.llm.request_budget import RequestBudgetEstimate
from llm_fakes import FakeModelProvider
from loop_fakes import ScriptedAgent

MSGS = [
    LLMMessage(role="system", content="You are helpful."),
    LLMMessage(role="user", content="test"),
]


def _req(**kw: Any) -> CompletionRequest:
    defaults: dict[str, Any] = {
        "profile": CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        "messages": list(MSGS),
        "request_id": "req_11111111111111111111111111111111",
    }
    defaults.update(kw)
    return CompletionRequest(**defaults)  # type: ignore[arg-type]


def _estimate(**kw: int | None) -> RequestBudgetEstimate:
    defaults: dict[str, int | None] = {
        "driver_context_window": 65536,
        "max_output_tokens": None,
        "canonical_payload_bytes": 5000,
        "messages_json_bytes": 3000,
        "tools_json_bytes": 1500,
        "message_count": 3,
        "tool_count": 1,
    }
    defaults.update(kw)
    return RequestBudgetEstimate(**defaults)  # type: ignore[arg-type]


class BudgetAwareFakeProvider(FakeModelProvider):
    """A FakeModelProvider that also supports request_budget_preview."""

    def __init__(self, name: str = "fake", *, budget_estimate=None, **kw: Any) -> None:
        super().__init__(name, **kw)
        self._budget_estimate = budget_estimate

    def request_budget_preview(
        self, req: CompletionRequest, model: str
    ) -> RequestBudgetEstimate | None:
        self.seen_requests.append(req)
        return self._budget_estimate


class PreviewScriptedAgent(ScriptedAgent):
    """A ScriptedAgent that also supports request_budget_preview."""

    def __init__(self, steps, *, budget_estimate=None, **kw):
        super().__init__(steps, **kw)
        self._budget_estimate = budget_estimate
        self.preview_calls = 0
        self.preview_tools_seen: list = []
        self.preview_views: list = []

    def request_budget_preview(
        self, view, tools, *, mode, overflow_signal, temperature=None, assist=False
    ) -> RequestBudgetEstimate | None:
        self.preview_calls += 1
        self.preview_tools_seen.append(tools)
        self.preview_views.append(view)
        return self._budget_estimate


class RefRecordingAgent(ScriptedAgent):
    """An agent that records raw tool-list references (not just names)."""

    def __init__(self, steps, *, budget_estimate=None, **kw):
        super().__init__(steps, **kw)
        self._budget_estimate = budget_estimate
        self.preview_calls = 0
        self.preview_tool_lists: list = []
        self.step_tool_lists: list = []

    def request_budget_preview(
        self, view, tools, *, mode, overflow_signal, temperature=None, assist=False
    ) -> RequestBudgetEstimate | None:
        self.preview_calls += 1
        self.preview_tool_lists.append(tools)
        return self._budget_estimate

    async def step(
        self,
        view,
        tools,
        *,
        mode,
        overflow_signal,
        on_stream=None,
        temperature=None,
        assist=False,
        attempt=1,
        provider_prefs=None,
    ):
        self.step_tool_lists.append(tools)
        return await super().step(
            view,
            tools,
            mode=mode,
            overflow_signal=overflow_signal,
            on_stream=on_stream,
            temperature=temperature,
            assist=assist,
            attempt=attempt,
            provider_prefs=provider_prefs,
        )
