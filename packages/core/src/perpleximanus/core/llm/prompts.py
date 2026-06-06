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
    "Your ONLY job right now is to propose a plan for the user's request and wait for "
    "their approval. Do NOT take any action, write any files, or run any commands yet.\n\n"
    "Study the request and the conversation so far (including any files already built and "
    "prior results). Then call the `submit_plan` tool exactly once with:\n"
    "  - summary: one or two plain-language sentences describing what you will deliver.\n"
    "  - steps: an ordered list of concrete, verifiable capstones. Keep each step short "
    "and outcome-focused (e.g. 'Scaffold index.html with the page layout'). Prefer 3–7 "
    "steps; avoid trivial micro-steps.\n\n"
    "If this is a change to existing work, plan the DIFF: only the steps needed for the "
    "requested change, not a rebuild. Call `submit_plan` and nothing else."
)
_EXECUTION_DRIVER_PROMPT = (
    "You are an autonomous build agent. The user has APPROVED your plan (it appears above "
    "as a numbered list). Carry it out end to end using the available tools "
    "(file_write, file_edit, shell, code_exec, etc.).\n\n"
    "As you work, keep the capstone tracker honest by calling the `plan_step` tool: mark a "
    "step 'active' when you start it and 'done' when you complete it. Work through the "
    "steps in order. Some actions may pause for the user's confirmation — that is expected; "
    "continue once approved.\n\n"
    "When the whole plan is complete, stop and give a short final message summarizing what "
    "you built. Do not call `submit_plan` during execution."
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
