"""Meta-tool specifications — static ToolSpec builders for the virtual tools.

Extracted from `engine.py` (god-file decomposition, Wave 1). These build the
static ToolSpec objects for the loop-intercepted virtual tools that never reach
the executor: `ask_user`, `clarify`, `propose_plan_update`, and `notify_user`.

Each builder lazy-imports ToolSpec from ..llm.types inside the function to avoid
a circular import at module-load time; the `_*_tool_singleton()` accessors build
each spec once (cached in a module global) on first access. All pure — no loop
or `self` state. The engine imports the four `_*_tool_singleton()` accessors to
assemble its virtual-tool list.

(The finish / remember / serve / delegate_explore specs follow the identical
pattern but remain in engine.py — they are interleaved there with non-spec
verify-command + fan-out machinery and are out of scope for this Wave-1 move.)
"""

from __future__ import annotations

# The virtual ask_user tool — the model's escape hatch when it (in its own
# reasoning, not at harness nudging) decides it needs the human's judgment.
# This is the Claude Code pattern done honestly: the tool is described, it's
# in the model's tool list, and the model chooses to call it. The harness
# never says "you must use this now"; the model uses it when its reasoning
# concludes that human input is the next-best step.
#
# When called, the loop INTERCEPTS it (the tool is never executed against the
# sandbox). WITH options it becomes an AlternativesEvent + AWAITING_USER_DECISION
# (the user picks a card); WITHOUT options it becomes a free-form question +
# AWAITING_USER_QUESTION (the user types an answer). Either way the reply resumes
# the run.
_ASK_USER_TOOL_NAME = "ask_user"

# The virtual clarify tool — the planner's typed multi-question escape hatch.
# When the request is ambiguous (multiple interpretations, missing specifics),
# the planner calls `clarify` with a set of TYPED questions instead of guessing.
# The loop intercepts the call, emits a ClarifyEvent carrying the typed
# questions, and halts at AWAITING_USER_QUESTION. The user answers each
# question; the answers are re-injected as a user message and planning proceeds
# with the clarified context.
#
# This GENERALIZES ask_user's single free-form question: clarify carries
# MULTIPLE structured questions with types (short_text / long_text / choice)
# so the planner can get precise answers to the exact unknowns, not one
# sprawling free-form block. The planner CHOOSES which tool to call — single
# question (ask_user) or structured batch (clarify) — based on how many
# unknowns it faces.
_CLARIFY_TOOL_NAME = "clarify"
_CLARIFY_DESCRIPTION = (
    "Pause BEFORE planning and ask the user MULTIPLE structured clarification "
    "questions. Use this when the request is ambiguous and you need SEVERAL "
    "specific answers before you can commit to a good plan — a brand color, "
    "a tech preference, a target host, a file name, a layout choice. "
    "Provide an overarching `question` summarizing what you're clarifying, "
    "and a `questions` array of typed items. Each item has: an `id` (short "
    "stable identifier like 'color' or 'stack'), the `question` text, a "
    "`type` field ('short_text' for a one-word answer, 'long_text' for a "
    "sentence, 'choice' for a pick from `options`), and optional `options` "
    "array for choice-type questions. For a 'choice' question, `options` MUST "
    "be a FLAT array of plain strings (the exact labels the user picks, e.g. "
    "[\"Modern\", \"Classic\", \"Your call\"]) — never objects, never nested "
    "arrays. Prefer 2-5 questions — enough to "
    "disambiguate, not an interrogation. Call this BEFORE submit_plan when "
    "the unknowns are genuine blockers to a good plan."
)
_CLARIFY_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "Overarching summary of what you're clarifying (one sentence).",
        },
        "questions": {
            "type": "array",
            "description": "The typed questions to ask (2-5 items).",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Short stable id, e.g. 'color' or 'stack'.",
                    },
                    "question": {
                        "type": "string",
                        "description": "The question text the user sees.",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["short_text", "long_text", "choice"],
                        "description": (
                            "Input kind: short_text (one word), long_text (sentence), "
                            "choice (pick from options)."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Allowed choices for type=choice — a FLAT array of "
                            "plain strings (the labels the user picks), e.g. "
                            '["Modern", "Classic"]. Never objects or nested arrays.'
                        ),
                    },
                },
                "required": ["id", "question"],
            },
            "minItems": 1,
            "maxItems": 5,
        },
    },
    "required": ["question", "questions"],
}
# Original ask_user description follows.
_ASK_USER_DESCRIPTION = (
    "Pause the run and ask the human user for input. Call this tool when YOU "
    "(in your own reasoning) decide that the next step depends on human "
    "judgment, a clarification, or a choice between options you can't pick "
    "with confidence. Typical situations: you've tried 2-3 substantively "
    "different approaches and they all failed; the right path depends on a "
    "preference the user hasn't stated; an action would be irreversible and "
    "you want confirmation of intent (not safety — that's the risk gate). "
    "Provide a clear one-sentence `question` summarizing what you need. "
    "Optionally, provide an `options` list of 2-3 concrete next-step "
    "alternatives the user can click; each option must have a short "
    "`title`, a brief `description`, and a `tool_name` + `arguments` shape "
    "for the action that will run if the user picks it. Without options, "
    "the user simply replies in chat. Do NOT call this on every error — "
    "first attempt to reason about the failure yourself."
)
# Pure JSON Schema (no Pydantic model needed — the loop parses args defensively).
_ASK_USER_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "One-sentence summary of what you need from the user.",
        },
        "options": {
            "type": "array",
            "description": "Optional 2-3 alternatives the user can click. Omit for free-form.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Short stable id, e.g. 'sudo'."},
                    "title": {"type": "string", "description": "Card label (3-5 words)."},
                    "description": {
                        "type": "string",
                        "description": "One sentence: why this might work.",
                    },
                    "tool_name": {"type": "string", "description": "The tool to call if picked."},
                    "arguments": {"type": "object", "description": "Args for that tool."},
                },
                "required": ["title", "tool_name", "arguments"],
            },
            "minItems": 0,
            "maxItems": 3,
        },
    },
    "required": ["question"],
}


def _ask_user_tool_spec():
    """Lazy-imported ToolSpec for ask_user — avoids a circular import on
    module load (ToolSpec lives in ..llm which doesn't yet exist when this
    module imports)."""
    from ..llm.types import ToolSpec

    return ToolSpec(
        name=_ASK_USER_TOOL_NAME,
        description=_ASK_USER_DESCRIPTION,
        parameters_schema=_ASK_USER_PARAMETERS_SCHEMA,
    )


# The propose_plan_update tool — the model's auto-recovery affordance for
# when its current plan is no longer right. Call this when a step fails in a
# way that invalidates the path, OR when discoveries during execution suggest
# a different decomposition is better, OR when the user steers toward a new
# goal. The user accepts/refines/rejects via the existing plan-approval gate.
_PROPOSE_PLAN_UPDATE_DESCRIPTION = (
    "Propose a REVISED plan when the current plan is no longer the right path. "
    "Use this when: a step has failed in a way that means the whole plan needs "
    "rethinking; you've discovered something during execution that suggests a "
    "different decomposition; or the user's steer message implies a new goal. "
    "The user will see the new plan in the chat alongside the prior one (it "
    "slots chronologically), and chooses to Approve, Refine, or implicitly "
    "reject by sending a different message. Provide a `summary` (one sentence "
    "describing what changed and why), an ordered list of `steps` (each with a "
    "`title`), and an optional `context` markdown body explaining your "
    "reasoning. Do NOT call this on every error — only when the current plan "
    "is structurally wrong. Small course corrections inside a single step "
    "should be handled with another tool call."
)
_PROPOSE_PLAN_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence: what changed in the plan and why.",
        },
        "steps": {
            "type": "array",
            "description": "The new ordered list of steps.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title"],
            },
            "minItems": 1,
        },
        "context": {
            "type": "string",
            "description": "Optional markdown body: what you learned, why this plan now.",
        },
    },
    "required": ["summary", "steps"],
}


def _propose_plan_update_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="propose_plan_update",
        description=_PROPOSE_PLAN_UPDATE_DESCRIPTION,
        parameters_schema=_PROPOSE_PLAN_UPDATE_SCHEMA,
    )


def _clarify_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name=_CLARIFY_TOOL_NAME,
        description=_CLARIFY_DESCRIPTION,
        parameters_schema=_CLARIFY_PARAMETERS_SCHEMA,
    )


# Lazy singletons — built on first access so module-level import order stays clean.
_ASK_USER_TOOL_SPEC = None
_PROPOSE_PLAN_UPDATE_TOOL_SPEC = None
_CLARIFY_TOOL_SPEC = None


def _ask_user_tool_singleton():
    global _ASK_USER_TOOL_SPEC
    if _ASK_USER_TOOL_SPEC is None:
        _ASK_USER_TOOL_SPEC = _ask_user_tool_spec()
    return _ASK_USER_TOOL_SPEC


def _propose_plan_update_tool_singleton():
    global _PROPOSE_PLAN_UPDATE_TOOL_SPEC
    if _PROPOSE_PLAN_UPDATE_TOOL_SPEC is None:
        _PROPOSE_PLAN_UPDATE_TOOL_SPEC = _propose_plan_update_tool_spec()
    return _PROPOSE_PLAN_UPDATE_TOOL_SPEC


def _clarify_tool_singleton():
    global _CLARIFY_TOOL_SPEC
    if _CLARIFY_TOOL_SPEC is None:
        _CLARIFY_TOOL_SPEC = _clarify_tool_spec()
    return _CLARIFY_TOOL_SPEC


# GAP B turn-taking — `notify_user` (non-blocking progress / mid-run reply).
# The loop intercepts the call and never reaches the executor.
_NOTIFY_USER_DESCRIPTION = (
    "Send the user a NON-BLOCKING message — a progress update, an explanation of "
    "what you're doing, or a reply to something they said mid-run. The run does "
    "NOT stop; you keep working on your next step right after. Use this freely to "
    "narrate and to answer the user — it is the correct way to 'talk' during a "
    "build. (For a question you genuinely need answered before continuing, use "
    "`ask_user` instead, which halts.)"
)
_NOTIFY_USER_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string", "description": "The message to the user."}},
    "required": ["message"],
}


def _notify_user_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="notify_user",
        description=_NOTIFY_USER_DESCRIPTION,
        parameters_schema=_NOTIFY_USER_SCHEMA,
    )


_NOTIFY_USER_TOOL_SPEC = None


def _notify_user_tool_singleton():
    global _NOTIFY_USER_TOOL_SPEC
    if _NOTIFY_USER_TOOL_SPEC is None:
        _NOTIFY_USER_TOOL_SPEC = _notify_user_tool_spec()
    return _NOTIFY_USER_TOOL_SPEC
