"""Gate-context projection for the Disco Operator.

Extracted from ``OperatorClient._gate_context`` (PKG-08-VERIFY) so the
event-scanning logic is cohesive pure helpers rather than one large inline
loop body. The parent module's ``_gate_context`` delegates to
``scan_gate_events`` + ``assemble_gate_context``; this module owns the
per-event scanning and the status-based assembly that produces the exact same
context dict.
"""

from __future__ import annotations

from typing import Any

__all__ = ["assemble_gate_context", "scan_gate_events"]


def _scan_plan_event(e: dict[str, Any], k: Any) -> dict[str, Any] | None:
    """Extract the plan dict from a PlanEvent (kind='plan')."""
    if k != "plan":
        return None
    return {
        "summary": e.get("summary"),
        "steps": e.get("steps"),
        "revision": e.get("revision"),
    }


def _scan_ask_alternatives(
    e: dict[str, Any], k: Any
) -> tuple[Any, Any]:
    """Extract alternatives/question from an alternatives/ask/clarify event.

    The stuck-detector / clarify path emits a dedicated event whose KIND is
    "alternatives", carrying summary + options at the top level — NOT a
    tool_call. The operator was blind to exactly the gate that needs it most
    (F5/F6 from the build stress-test): surface the summary + option ids so
    the operator knows what to decide and that the verb is `pick <id>`.
    """
    if k not in ("alternatives", "ask", "clarify"):
        return None, None
    alternatives = None
    question = None
    if e.get("options"):
        alternatives = {"summary": e.get("summary"), "options": e.get("options")}
    if e.get("question") or e.get("summary"):
        question = e.get("question") or e.get("summary")
    return alternatives, question


def _scan_tool_call_gate(
    e: dict[str, Any],
) -> tuple[bool, Any, bool, Any]:
    """Extract question/alternatives from an ActionEvent's tool_call.

    The tool call lives under ``tool_call`` (name + arguments). Returns
    ``(question, alternatives)`` — either may be None when the tool call is
    not gate-relevant.
    """
    tc = e.get("tool_call") or {}
    name = tc.get("name") or tc.get("tool_name")
    question_matched = name in ("ask_user", "clarify")
    alternatives_matched = (
        name in ("propose_alternatives", "offer_alternatives")
        or bool(e.get("alternatives"))
    )
    question = None
    alternatives = None
    if question_matched:
        question = tc.get("arguments") or tc.get("args") or e.get("thought")
    if alternatives_matched:
        alternatives = e.get("alternatives") or tc.get("arguments")
    return question_matched, question, alternatives_matched, alternatives


def scan_gate_events(events: list[dict[str, Any]]) -> tuple[
    dict[str, Any] | None,
    Any,
    Any,
    dict[str, Any],
]:
    """Scan the event log and return ``(plan, question, alternatives, ctx)``.

    ``ctx`` carries extra operator-relevant fields (currently
    ``last_agent_message``). The caller assembles the final gate context from
    these accumulators based on the current status.
    """
    plan: dict[str, Any] | None = None
    question: Any = None
    alternatives: Any = None
    ctx: dict[str, Any] = {}
    for e in events:
        k = e.get("kind")
        p = _scan_plan_event(e, k)
        if p is not None:
            plan = p
        alt, q = _scan_ask_alternatives(e, k)
        if alt is not None:
            alternatives = alt
        if q is not None:
            question = question or q
        question_matched, tq, alternatives_matched, ta = _scan_tool_call_gate(e)
        if question_matched:
            question = tq
        if alternatives_matched:
            alternatives = ta
        if k == "message" and e.get("source") == "agent":
            msg = e.get("message", {}).get("content")
            if msg:
                ctx["last_agent_message"] = str(msg)[:1500]
    return plan, question, alternatives, ctx


def assemble_gate_context(
    status: str | None,
    plan: dict[str, Any] | None,
    question: Any,
    alternatives: Any,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the final gate context dict based on the pending status.

    Only the field matching the current gate is added (plan for
    AWAITING_PLAN_APPROVAL, question for AWAITING_USER_QUESTION, alternatives
    for AWAITING_USER_DECISION). ``ctx`` (e.g. ``last_agent_message``) is
    always carried through.
    """
    if status == "AWAITING_PLAN_APPROVAL" and plan:
        ctx["plan"] = plan
    if status == "AWAITING_USER_QUESTION" and question:
        ctx["question"] = question
    if status == "AWAITING_USER_DECISION" and alternatives:
        ctx["alternatives"] = alternatives
    return ctx
