"""Decompose the user's query into sub-questions = the research plan's steps.

Mirrors `RouterQueryRewriter.rewrite()`'s call shape (QUERY_REWRITER role, one
model call), but asks for SUB-QUESTIONS / SECTIONS rather than paraphrases. The
output becomes the `steps` field of the PlanEvent the agent loop's plan-mode
intercept already turns into AWAITING_PLAN_APPROVAL — so the human can edit
sub-questions before the gather phase spends any inference budget.

Defensive about model shape drift: the rewriter prompt asks for "one per line"
and we parse line by line (the same shape `rewrite()` uses). A model that
returns one big paragraph is treated as a single sub-question. A model that
returns more than `max_subq` is truncated (with `bounded_by="subquestions"`
flowing back from the engine).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)


@dataclass(frozen=True)
class SubQuestion:
    """One sub-question / section of the plan. `title` is what the user sees
    in the plan-approval gate AND what the synthesis renders as a section
    heading on the final report — same string serves both."""

    title: str

    def to_plan_step(self) -> dict[str, str]:
        """The shape the agent's `submit_plan` tool ingests as one step."""
        return {"title": self.title}


_PROMPT_TEMPLATE = (
    "You are decomposing a research question into focused sub-questions that "
    "together ANSWER the SPECIFIC question asked. Each sub-question becomes one "
    "section of a multi-section research report.\n\n"
    "Question: {query}\n\n"
    "RULES — read carefully, they affect what the reader actually learns:\n\n"
    "1. ANSWER THE SPECIFIC QUESTION. Identify what the question is really "
    "asking for (e.g. 'state of commercialization' → who is shipping or close, "
    "realistic timelines, production vs. prototype reality, cost/economics, "
    "key players). Make those the CORE sub-questions. Background / "
    "fundamentals are CONTEXT, not the bulk — at most 1 of the {n} should be "
    "purely background, and only if it materially supports the core.\n\n"
    "2. ORDER BY IMPORTANCE TO THE QUESTION. List the most directly relevant "
    "sub-question FIRST, then in descending order of importance. Runs that hit "
    "their depth budget cover the early sub-questions first — so the most "
    "important content must be earliest. Background goes LAST, never first.\n\n"
    "3. Each sub-question must be SPECIFIC and ANSWERABLE — not 'What is X?' "
    "but 'Which X products are shipping today vs. promised vs. discontinued?' "
    "Tight, investigatable, the kind a domain analyst would ask.\n\n"
    "Output EXACTLY {n} sub-questions, ONE PER LINE, in priority order. No "
    "numbering, no bullets, no explanation, no header. Just the questions."
)


def _clean(line: str) -> str:
    """Strip common list prefixes (numbering, bullets) and trailing punctuation
    quirks. Mirrors RouterQueryRewriter's per-line cleanup so a verbose model
    gets a consistent shape regardless of decoration."""
    line = line.strip()
    # remove leading "1." / "1)" / "- " / "* " / "• " / bullet patterns
    line = re.sub(r"^\s*(\d+[.)]\s*|[-*•]\s+)", "", line).strip()
    # drop trailing colons that some models add to "headers"
    return line.rstrip(":").strip()


def _recency_preamble(recency_window: str | None) -> str:
    """Build the date + recency context prefix for the decompose prompt.

    When recency_window is None the function returns an empty string so the
    prompt is byte-identical to the pre-DR-3 version (the OFF assertion)."""
    if recency_window is None:
        return ""
    today = datetime.date.today().isoformat()
    label = "month" if recency_window == "month" else "week"
    return (
        f"Today's date is {today}. "
        f"The user wants research focused on the PAST {label.upper()}. "
        f"Phrase sub-questions to elicit recent information, "
        f"recent events, and up-to-date figures rather than historical background.\n\n"
    )


async def decompose_query(
    router: LLMRouter,
    query: str,
    *,
    max_subq: int,
    recency_window: str | None = None,
) -> list[SubQuestion]:
    """Decompose `query` into up to `max_subq` sub-questions via the
    QUERY_REWRITER role. Returns at least one sub-question (falls back to the
    original query) so the planner can never propose an empty plan.

    ``recency_window`` is ``"month"`` or ``"week"`` (DR-3 E4): when set, the
    prompt is prefixed with today's date and a recency directive so the model
    frames sub-questions toward recent sources.  ``None`` → byte-identical to
    a call without the argument (the OFF assertion)."""
    preamble = _recency_preamble(recency_window)
    instruction = preamble + _PROMPT_TEMPLATE.format(query=query.strip(), n=max_subq)
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.QUERY_REWRITER),
        messages=[LLMMessage(role="user", content=instruction)],
        temperature=0.0,
    )
    resp = await router.complete(req)
    lines = [_clean(ln) for ln in resp.text.splitlines() if ln.strip()]
    # dedup while preserving order — some models repeat near-paraphrases
    seen: set[str] = set()
    unique: list[str] = []
    for ln in lines:
        if ln and ln.lower() not in seen:
            seen.add(ln.lower())
            unique.append(ln)
    if not unique:
        unique = [query.strip()]
    return [SubQuestion(title=t) for t in unique[:max_subq]]
