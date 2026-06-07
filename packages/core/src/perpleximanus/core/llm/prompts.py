"""Prompt-variant selection — llm-router-contract.md §8.

"The prompt is part of the model abstraction" (BoD §12.6). The router (via a
PromptProvider it owns) selects the SYSTEM prompt by model family × operating
mode. It owns the *selection*, not the prose — prompt text lives in versioned
templates ([INTERIOR] engine/layout).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import ModelRole, OperatingMode

# Per-role default operating mode when the caller doesn't specify one (§8).
_DEFAULT_MODE_BY_ROLE: dict[ModelRole, OperatingMode] = {
    ModelRole.AGENT_DRIVER: OperatingMode.LONG_HORIZON,
    ModelRole.RAG_ANSWERER: OperatingMode.INTERACTIVE,
    ModelRole.QUERY_REWRITER: OperatingMode.INTERACTIVE,
    ModelRole.SUMMARIZER: OperatingMode.INTERACTIVE,
    ModelRole.NLI_VERIFIER: OperatingMode.INTERACTIVE,
}

# Ordered (substring -> family) derivation. First match wins. [VERIFY] tags.
_FAMILY_PATTERNS: list[tuple[str, str]] = [
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("gpt", "gpt"),
    ("o1", "gpt"),
    ("qwen", "qwen"),
    ("llama", "llama"),
    ("mistral", "mistral"),
    ("gemma", "gemma"),
    ("deberta", "deberta"),
]


def default_mode_for_role(role: ModelRole) -> OperatingMode:
    return _DEFAULT_MODE_BY_ROLE.get(role, OperatingMode.INTERACTIVE)


def derive_family(model_id: str) -> str:
    """Best-effort family tag from a model id (§8). Used when a ModelEntry does
    not pin `family` explicitly. Lowercased substring match; 'unknown' if none."""
    lid = model_id.lower()
    for needle, family in _FAMILY_PATTERNS:
        if needle in lid:
            return family
    return "unknown"


@runtime_checkable
class PromptProvider(Protocol):
    """[CONTRACT] Returns the system prompt for a given model family and mode.
    Owned alongside the router. Prompt text lives in versioned template files;
    this only resolves WHICH one."""

    def system_prompt(
        self, *, model_family: str, mode: OperatingMode | None, role: ModelRole
    ) -> str: ...


class StaticPromptProvider:
    """A simple PromptProvider backed by an in-memory template table.

    Lookup precedence (most→least specific):
      (family, mode, role) -> (family, mode, None) -> (family, None, None) -> fallback.

    The real system uses versioned template files with `model_specific/<family>`
    overlays ([INTERIOR]); this satisfies the [CONTRACT] selection behavior and
    is what the headless tests bind to.
    """

    def __init__(
        self,
        templates: dict[tuple[str, OperatingMode | None, ModelRole | None], str] | None = None,
        *,
        fallback: str = "You are a helpful, precise assistant.",
    ) -> None:
        self._templates = templates or {}
        self._fallback = fallback

    def system_prompt(
        self, *, model_family: str, mode: OperatingMode | None, role: ModelRole
    ) -> str:
        for key in (
            (model_family, mode, role),
            (model_family, mode, None),
            (model_family, None, None),
        ):
            if key in self._templates:
                return self._templates[key]
        return self._fallback


# Plan-mode driver prompts (Build). Kept here next to the selection logic; the
# prose is [INTERIOR] and tunable. The plan→approve→build loop's quality leans on
# these, so they are explicit about the two phases and the meta tools.
_PLANNING_DRIVER_PROMPT = (
    "You are an autonomous build agent, currently in PLANNING mode.\n"
    "Your job right now is to propose a plan for the user's request and wait for "
    "their approval. You may NOT write files, edit files, or run any state-changing "
    "commands until the plan is approved.\n\n"
    "TALK TO THE USER FIRST. Before you call any tool, write ONE short, friendly "
    "sentence (as a plain message, no tool call) acknowledging what they asked for "
    "and saying what you're about to do — e.g. \"Got it — you want a live stock "
    "ticker page. Let me check what free APIs are available and think through the "
    "architecture, then I'll lay out a plan.\" This is the user hearing back from "
    "you before work begins; don't skip it. Keep it to one or two sentences — warm "
    "but not chatty.\n\n"
    "You DO have READ tools available — use them to gather context before proposing:\n"
    "  • `file_list` — see what already exists in /workspace.\n"
    "  • `file_read` — inspect any file the previous build wrote, or any config the user "
    "mentioned. Re-planning a change? Re-read the relevant files NOW so the plan reflects "
    "the current state, not what you remember.\n"
    "  • `search` and `extract` — when the request involves external context (APIs, "
    "library docs, a URL the user shared).\n\n"
    "Workflow (mirrors a careful engineer):\n"
    "  Phase 0 — ACKNOWLEDGE. The one-sentence greeting above.\n"
    "  Phase 1 — UNDERSTAND. Read what's relevant. For a fresh task, this may be nothing. "
    "For a re-plan, list and re-read the files the prior build produced.\n"
    "  Phase 2 — PROPOSE. Call the `submit_plan` tool exactly once with:\n"
    "    - summary: one or two plain-language sentences describing what you will deliver.\n"
    "    - steps: an ordered list of concrete, verifiable capstones. Keep each step short "
    "and outcome-focused (e.g. 'Scaffold index.html with the page layout'). Prefer 3–7 "
    "steps; avoid trivial micro-steps.\n"
    "    - context: a short markdown block explaining what you found while exploring, the "
    "rationale for this approach, and any trade-offs the human should know before "
    "approving. This is the WHY behind the WHAT.\n\n"
    "If this is a change to existing work, plan the DIFF: only the steps needed for the "
    "requested change, not a rebuild. After you have enough context, call `submit_plan` "
    "and stop."
)
_EXECUTION_DRIVER_PROMPT = (
    "You are an autonomous build agent. The user has APPROVED your plan (it appears above "
    "as a numbered list). Carry it out end to end using the available tools "
    "(file_write, file_edit, shell, code_exec, etc.).\n\n"
    "TALK TO THE USER AS YOU WORK. Every tool call carries a `thought` — write it in "
    "plain, friendly language describing what you're doing and why ('Wiring up the "
    "fetch call to the Yahoo endpoint so the chart has live data'). The user reads "
    "these; they are how they follow along. Be concise but human.\n\n"
    "RESPOND TO THE USER FIRST. If a message from the user appears mid-run (a question, "
    "a correction, a steer), your VERY NEXT response must be a short plain-language "
    "reply to them — answer their question or acknowledge their change directly, as a "
    "message with no tool call — BEFORE you take your next action. They asked you "
    "something; reply like a colleague would, then continue working. Never silently "
    "ignore a user message and just keep running tools.\n\n"
    "KEEP THE PLAN HONEST. Call the `plan_step` tool to mark a step 'active' when you "
    "start it and 'done' when you complete it. Work through the steps in order. Do NOT "
    "declare the whole task finished until every step is marked done — if you believe "
    "the work is complete, first make sure each step has a matching plan_step(idx, "
    "'done'). Some actions may pause for the user's confirmation — that is expected; "
    "continue once approved.\n\n"
    "IF THE PLAN IS WRONG. If a step fails in a way that means the plan itself needs to "
    "change (an approach doesn't work, a discovery invalidates the path), call "
    "`propose_plan_update` with a revised plan — don't grind on a broken approach. If "
    "you genuinely need the user to make a decision you can't make confidently, call "
    "`ask_user` with a clear question (and 2-3 concrete options when you can). Use these "
    "sparingly — try to reason through problems yourself first.\n\n"
    "When the whole plan is complete (every step marked done), stop and give a short "
    "final message summarizing what you built. Do not call `submit_plan` during execution."
)


class DriverPrompts:
    """[CONTRACT role] A PromptProvider that gives the AGENT_DRIVER role phase-aware
    system prompts: a PLANNING prompt (propose a plan via `submit_plan`, take no
    action) and an EXECUTION prompt (carry out the approved plan, report capstones
    via `plan_step`). Every other role/mode defers to a wrapped base provider, so
    Research and the non-driver roles are unaffected."""

    def __init__(
        self,
        base: PromptProvider | None = None,
        *,
        planning_prompt: str = _PLANNING_DRIVER_PROMPT,
        execution_prompt: str = _EXECUTION_DRIVER_PROMPT,
    ) -> None:
        self._base = base or StaticPromptProvider()
        self._planning = planning_prompt
        self._execution = execution_prompt

    def system_prompt(
        self, *, model_family: str, mode: OperatingMode | None, role: ModelRole
    ) -> str:
        if role == ModelRole.AGENT_DRIVER:
            if mode == OperatingMode.PLANNING:
                return self._planning
            return self._execution
        return self._base.system_prompt(model_family=model_family, mode=mode, role=role)
