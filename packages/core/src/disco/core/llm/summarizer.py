"""RouterSummarizer — llm-router-contract.md §9.1 (v1.1: async seam).

Implements the event/state contract's `Summarizer` protocol by routing a
SUMMARIZER-role completion. Uses the cheap local model (never the agent model),
per BoD §7.3. This is the concrete object the memory condenser depends on.

`summarize` is **async** (event-state-contract v1.2 §5.2): it makes an async
router call, awaited by the condenser, which is itself awaited by the loop's
`_materialize_view`. The earlier sync shim (which deadlocked inside the loop's
running event loop) is gone — the seam is async end-to-end.

HS-02 (anchored-checkpoint compaction): the instruction is now an ANCHORED
6-heading template (GOAL / CONSTRAINTS / PROGRESS / DECISIONS / NEXT / FILES),
and `summarize()` branches on whether a prior anchored summary is already
present in the messages it's handed. When a prior summary is detected, the
UPDATE-in-place directive is used (merge new progress, restate only changed
sections, preserve the template) rather than a freeform recap. The FAILED-
approaches content from the A-S4 contract is preserved — folded into
CONSTRAINTS/NEXT so it is never lost (load-bearing: prevents the agent from
repeating work that already failed).
"""

from __future__ import annotations

from ..events import LLMMessage
from .routing import LLMRouter
from .types import CapabilityProfile, CompletionRequest, ModelRole

# HS-02: the anchored 6-heading template. These are the six headings the
# create-fresh directive mandates and the update-in-place directive preserves.
_ANCHORED_HEADINGS: tuple[str, ...] = (
    "GOAL:",
    "CONSTRAINTS:",
    "PROGRESS:",
    "DECISIONS:",
    "NEXT:",
    "FILES:",
)

# Both directives end with the same prohibition, because the summarizer request
# binds NO tools -- any tool-call syntax in the reply is pure imitation of the
# transcript being summarized, and a small model handed a conversation full of
# tool calls will keep writing them. Seed 460000 produced 22 such summaries.
#
# The condenser's guard catches them, but catching costs a second round-trip to
# repair and, when the repair also fails, degrades the span to a content-free
# host fallback. Saying it up front is strictly cheaper than repairing it after:
# prevention removes the call, the latency and the lost context at once, and
# across a 100-trial promotion those round-trips are the difference between
# fitting the run ceiling and not. Phrased dialect-neutrally on purpose -- the
# model that produced the residue writes `<｜｜DSML｜｜invoke …>`, so a list of
# ASCII examples alone reads to it as being about somebody else's syntax.
_NO_PROTOCOL_CLAUSE = (
    "\nAnswer with the summary TEXT ONLY. Do not call a tool, and do not emit "
    "tool-call syntax in any dialect: no <parameter>, <invoke> or "
    "<function_calls>, no tool_calls JSON payload, and none of your own special "
    "tool-call delimiters even when they are written with unusual characters. "
    "Ordinary code, HTML or shell snippets are fine when they are part of what "
    "you are describing."
)

# A-S4 carried forward + HS-02 reshaped: the FAILED-approaches content is
# load-bearing (prevents the agent from repeating work that already failed) and
# MUST survive every condensation. The anchored template folds it under
# CONSTRAINTS (preferred home — failures are constraints on the next attempt)
# and cross-references it from NEXT (unresolved threads often include
# "re-try this differently"). Both directives demand the content explicitly.
_SUMMARIZE_FRESH_INSTRUCTION = (
    "Condense the conversation above (the most recent turns at the tail are "
    "kept as-is by the condenser — do NOT restate them verbatim) into a "
    "STRUCTURED summary. Use these EXACT headings, in this order (omit a "
    "heading only if truly empty):\n"
    "GOAL: the user's task and acceptance criteria.\n"
    "CONSTRAINTS: hard rules, dependencies, environmental limits; ALSO list "
    "FAILED approaches here (things tried and the reason they failed) — this "
    "is load-bearing and must NOT be lost across condensations.\n"
    "PROGRESS: which plan steps are done vs still open; for each, the "
    "concrete outcome (path / commit / status).\n"
    "DECISIONS: key choices made (tech stack, architecture, naming) and why.\n"
    "NEXT: unresolved threads / what remains; cross-reference FAILED "
    "approaches in CONSTRAINTS where relevant.\n"
    "FILES: every file created or modified, by path, with a one-line note of "
    "what it contains.\n"
    "Be concise but do not drop file paths, the plan state, FAILED "
    "approaches, or the goal/constraints." + _NO_PROTOCOL_CLAUSE
)

# HS-02 update-in-place: when a prior anchored summary is already in the
# messages (re-condensation, the prior summary was emitted by a previous
# condensation into the View), the summarizer must UPDATE it in place rather
# than write a fresh recap. This keeps stable context (goal, constraints,
# file list) stable and only re-derives what changed.
_SUMMARIZE_UPDATE_INSTRUCTION = (
    "There is a PRIOR STRUCTURED SUMMARY in the conversation above (look for "
    "the 'GOAL:' heading — that is the template marker). NEW events have "
    "occurred since that summary was written. UPDATE the prior summary IN "
    "PLACE — do NOT write a fresh recap.\n"
    "Procedure:\n"
    "1. Locate the prior summary in the messages above (it uses the anchored "
    "template: GOAL / CONSTRAINTS / PROGRESS / DECISIONS / NEXT / FILES).\n"
    "2. MERGE the new events (the events between the prior summary and the "
    "most recent tail) into the existing sections.\n"
    "3. RESTATE ONLY sections that have CHANGED. Unchanged sections may be "
    "kept verbatim from the prior summary.\n"
    "4. PRESERVE the same template (same six headings, same order).\n"
    "5. APPEND or UPDATE any new FAILED approaches (things tried and failed "
    "since the prior summary) under CONSTRAINTS or NEXT — do NOT drop this, "
    "it is load-bearing across condensations.\n"
    "6. Do NOT restate the most recent turns verbatim (the tail is kept as-is "
    "by the condenser).\n"
    "Output the COMPLETE updated summary (all six headings, even if "
    "unchanged), with file paths, plan state, and FAILED approaches "
    "preserved." + _NO_PROTOCOL_CLAUSE
)

# Stable template marker: the create-fresh directive mandates a "GOAL:" line as
# the leading heading. The summarizer scans `messages` for this marker to
# detect a prior anchored summary and select the UPDATE-in-place directive.
# It is intentionally a heading prefix (not a markdown fence or marker
# comment) so the rendered summary is still human-readable and the detection
# is robust against minor whitespace changes.
_SUMMARY_MARKER = "GOAL:"


def _has_prior_anchored_summary(messages: list[LLMMessage]) -> bool:
    """Return True iff any message in `messages` carries the anchored-template
    marker (the `GOAL:` heading). This is the signal that the messages
    already contain a prior summary, so the next condensation should UPDATE
    it in place rather than recap from scratch.

    The scan is content-only and case-sensitive on purpose: the create-fresh
    directive mandates the exact `GOAL:` prefix (line-start, uppercase), and
    the model is faithful to it because the instruction is explicit. A
    normal user/agent turn will not contain a `GOAL:` line — false positives
    would mean the model is hallucinating the template, which the test
    catches as a regression."""
    for m in messages:
        c = m.content
        if not isinstance(c, str):
            continue
        if _SUMMARY_MARKER in c:
            return True
    return False


def _select_summarize_instruction(messages: list[LLMMessage]) -> str:
    """Pick the directive: UPDATE-in-place when a prior anchored summary is
    in the messages, CREATE-fresh otherwise. This is the OBSERVABLE
    branching on the built CompletionRequest — the test pins both arms."""
    if _has_prior_anchored_summary(messages):
        return _SUMMARIZE_UPDATE_INSTRUCTION
    return _SUMMARIZE_FRESH_INSTRUCTION


class RouterSummarizer:
    """[CONTRACT] Satisfies the event contract's `Summarizer` protocol."""

    def __init__(self, router: LLMRouter) -> None:
        self._router = router

    async def summarize(self, messages: list[LLMMessage]) -> str:
        instruction = _select_summarize_instruction(messages)
        req = CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
            messages=[*messages, LLMMessage(role="user", content=instruction)],
            temperature=0.0,
        )
        resp = await self._router.complete(req)
        return resp.text
