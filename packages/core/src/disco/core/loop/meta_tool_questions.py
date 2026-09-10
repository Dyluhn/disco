"""Internal Loop turn-control collaborator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from ..llm import OperatingMode
from .boundaries import AgentStep
from .control import Disp
from .turn_control_serve_refusals import (
    questions_v2_attempts_since_last_plan as _questions_v2_attempts_since_last_plan,
)
from .turn_control_support import (
    _normalize_clarify_options,
    _normalize_questions_v2_options,
    _questions_v2_used_since_last_plan,
)

if TYPE_CHECKING:
    from ..events import ClarifyQuestionItem, QuestionsV2Item
    from .meta_tool_common import MetaToolLoopFacet

_LOG = logging.getLogger("disco.loop")


class MetaToolQuestionMixin:
    _loop: MetaToolLoopFacet

    async def _refuse_questions_v2(self, step: AgentStep, message: str) -> Disp:
        assert step.tool_call is not None
        action = ActionEvent(
            thought=step.thought,
            tool_call=step.tool_call,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        await self._loop._emit(action)
        await self._loop._emit(
            AgentErrorEvent(
                error=message,
                action_id=action.id,
                tool_call_id=step.tool_call.call_id,
            )
        )
        return Disp.CONTINUE

    @staticmethod
    def _questions_v2_items(raw_items: object) -> list[QuestionsV2Item]:
        from ..events import QuestionsV2Item

        source_items = raw_items if isinstance(raw_items, list) else []
        items: list[QuestionsV2Item] = []
        used_ids: set[str] = set()
        for item in source_items:
            if len(items) >= 4:
                break
            if not isinstance(item, dict):
                continue
            qtext = str(item.get("question") or item.get("label") or "").strip()
            if not qtext:
                continue
            qid = str(item.get("id") or f"q{len(items) + 1}").strip() or f"q{len(items) + 1}"
            if qid in used_ids:
                qid = f"q{len(items) + 1}"
            used_ids.add(qid)
            items.append(
                QuestionsV2Item(
                    id=qid,
                    question=qtext,
                    options=_normalize_questions_v2_options(item.get("options")),
                    allow_free_text=True,
                )
            )
        return items

    async def _land_free_form_question(self, question: str) -> Disp:
        event = MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(
                role="assistant",
                content=(question or "The agent needs clarification before planning."),
            ),
        )
        await self._loop._emit(event)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=event.id,
            )
        )
        return Disp.HALT

    async def _land_structured_question(self, event: Event) -> Disp:
        await self._loop._emit(event)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=event.id,
            )
        )
        return Disp.HALT

    async def handle_questions_v2(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None
        from ..events import QuestionsV2Event

        if self._loop.mode != OperatingMode.PLANNING:
            return await self._refuse_questions_v2(
                step,
                "<system-reminder>\n"
                "questions_v2 refused: structured intake is only available "
                "before submit_plan, while the run is still in PLANNING — this "
                f"run is in {self._loop.mode.value.upper()}. Next move: if "
                "execution is blocked on human input, use ask_user; if the plan "
                "itself is wrong, use propose_plan_update.\n"
                "</system-reminder>",
            )
        if _questions_v2_used_since_last_plan(events):
            return await self._refuse_questions_v2(
                step,
                "<system-reminder>\n"
                "questions_v2 refused: you already used the one allowed "
                f"structured intake round for this planning pass, and this is "
                f"attempt {_questions_v2_attempts_since_last_plan(events)}. Do not "
                "ask another batch before submit_plan. Next move: use the user's "
                "answers, choose reasonable defaults for anything still ambiguous, "
                "and log those assumptions in submit_plan.context.\n"
                "</system-reminder>",
            )
        question = (
            str(
                step.tool_call.arguments.get("question")
                or step.tool_call.arguments.get("summary")
                or ""
            ).strip()
            or step.thought.strip()
        )
        raw_items = (
            step.tool_call.arguments.get("questions") or step.tool_call.arguments.get("items") or []
        )
        items = self._questions_v2_items(raw_items)
        if not items:
            return await self._land_free_form_question(question)
        return await self._land_structured_question(
            QuestionsV2Event(
                question=question or "A few details before I propose a plan.",
                items=items,
            )
        )

    @staticmethod
    def _clarify_items(raw_items: object) -> list[ClarifyQuestionItem]:
        from ..events import ClarifyQuestionItem

        items: list[ClarifyQuestionItem] = []
        source_items = raw_items if isinstance(raw_items, list) else []
        for item in source_items:
            if not isinstance(item, dict):
                continue
            qid = str(item.get("id") or f"q{len(items) + 1}").strip()
            qtext = str(item.get("question") or "").strip()
            if not qtext:
                continue
            qtype = str(item.get("type") or "short_text").strip()
            if qtype not in ("short_text", "long_text", "choice"):
                qtype = "short_text"
            qopts = _normalize_clarify_options(item.get("options"))
            if qtype == "choice" and len(qopts) < 2:
                qtype = "short_text"
                qopts = []
            items.append(ClarifyQuestionItem(id=qid, question=qtext, type=qtype, options=qopts))
        return items

    async def handle_clarify(self, step: AgentStep, events: list[Event]) -> Disp:
        del events
        assert step.tool_call is not None
        from ..events import ClarifyEvent

        question = (
            str(step.tool_call.arguments.get("question") or "").strip() or step.thought.strip()
        )
        items = self._clarify_items(step.tool_call.arguments.get("questions") or [])
        if not items:
            return await self._land_free_form_question(question)
        return await self._land_structured_question(
            ClarifyEvent(
                question=question or "The agent needs clarification before planning.",
                items=items,
            )
        )

    async def handle_ask_user(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        alt = self._loop._alternatives_from_args(step.tool_call.arguments, events)
        if alt is not None:
            # ask_user WITH options → AlternativesEvent + gate
            await self._loop._emit(alt)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.AWAITING_USER_DECISION,
                    detail=alt.id,
                )
            )
            return Disp.HALT
        # ask_user WITHOUT options → free-form question. Emit it as
        # an assistant message (from the tool's `question` arg or
        # the step's thought, whichever has content) and halt at the
        # two-way Ask-gate (AWAITING_USER_QUESTION) — the UI renders
        # an AskPanel with a focused answer box, not muted prose. The
        # status's detail carries the question message's id so the
        # surface can resolve it. The user's reply (send_message /
        # steer) IS the resume signal.
        question = (
            str(step.tool_call.arguments.get("question") or "").strip() or step.thought.strip()
        )
        question_id: str | None = None
        if question:
            q_event = MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=question),
            )
            question_id = q_event.id
            await self._loop._emit(q_event)
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_USER_QUESTION,
                detail=question_id or "free_form_question",
            )
        )
        return Disp.HALT
