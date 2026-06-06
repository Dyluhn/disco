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

import re
from dataclasses import dataclass

from perpleximanus.core import LLMMessage
from perpleximanus.core.llm import (
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
    "together cover its scope. Each sub-question must be answerable in its "
    "own section of a multi-section research report.\n\n"
    "Question: {query}\n\n"
    "Output EXACTLY {n} sub-questions, one per line. Each line is a "
    "self-contained question or topic heading (no numbering, no bullets, no "
    "explanation). Order them logically (background → state of the art → "
    "open questions / outlook)."
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


async def decompose_query(
    router: LLMRouter,
    query: str,
    *,
    max_subq: int,
) -> list[SubQuestion]:
    """Decompose `query` into up to `max_subq` sub-questions via the
    QUERY_REWRITER role. Returns at least one sub-question (falls back to the
    original query) so the planner can never propose an empty plan."""
    instruction = _PROMPT_TEMPLATE.format(query=query.strip(), n=max_subq)
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
