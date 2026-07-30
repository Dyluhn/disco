"""Prompt-variant selection — llm-router-contract.md §8.

"The prompt is part of the model abstraction" (BoD §12.6). The router (via a
PromptProvider it owns) selects the SYSTEM prompt by model family × operating
mode. It owns the *selection*, not the prose — prompt text lives in versioned
templates ([INTERIOR] engine/layout).
"""

from __future__ import annotations

# Verbatim prompt constants remain importable from this compatibility facade.
# ruff: noqa: F401
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ._prompt_templates import (
    _AGENT_PLANNING_CAPABILITY_BLOCK,
    _APPKIT_ASSISTED_SCOPE_GUIDANCE,
    _APPKIT_AUTONOMOUS_SCOPE_GUIDANCE,
    _APPKIT_EXECUTION_DRIVER_PROMPT,
    _APPKIT_PLANNING_DRIVER_PROMPT,
    _AUTONOMOUS_PROMPT_PREFIX,
    _EXECUTION_DRIVER_PROMPT,
    _EXECUTION_DRIVER_PROMPT_SMALL,
    _HOST_VERIFY_MANDATE_CAPABLE,
    _HOST_VERIFY_MANDATE_SMALL,
    _MENTIONED_ELEMENT_GUIDANCE,
    _PLANNING_DRIVER_PROMPT,
    _SELF_VERIFY_MANDATE_CAPABLE,
    _SELF_VERIFY_MANDATE_SMALL,
    _WORKFLOW_ROUTER_DRIVER_PROMPT,
    _soften_self_verify_mandate,
)
from .types import ModelRole, OperatingMode, Requirement

# Per-role default operating mode when the caller doesn't specify one (§8).
_DEFAULT_MODE_BY_ROLE: dict[ModelRole, OperatingMode] = {
    ModelRole.AGENT_DRIVER: OperatingMode.LONG_HORIZON,
    ModelRole.RAG_ANSWERER: OperatingMode.INTERACTIVE,
    ModelRole.QUERY_REWRITER: OperatingMode.INTERACTIVE,
    ModelRole.SUMMARIZER: OperatingMode.INTERACTIVE,
    ModelRole.NLI_VERIFIER: OperatingMode.INTERACTIVE,
    ModelRole.VERIFIER: OperatingMode.INTERACTIVE,
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
    this only resolves WHICH one.

    `assist` is the per-conversation weak-model gate (req.assist on the wire).
    It defaults to False so every existing implementation and call site keeps
    its current behavior byte-identical. A provider MAY switch to a tightened
    variant when assist=True (e.g. small-model execution prompt); a provider
    that does not implement assist behavior simply ignores the flag."""

    def system_prompt(
        self,
        *,
        model_family: str,
        mode: OperatingMode | None,
        role: ModelRole,
        capabilities: frozenset[Requirement] | None = None,
        assist: bool = False,
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
        self,
        *,
        model_family: str,
        mode: OperatingMode | None,
        role: ModelRole,
        capabilities: frozenset[Requirement] | None = None,
        assist: bool = False,
    ) -> str:
        # Static templates are role/family keyed; the `assist` flag is a
        # weak-model selection hint (see DriverPrompts) — this provider does
        # not differentiate, so the flag is accepted and ignored, keeping
        # the lookup byte-identical to pre-C21 behavior.
        del capabilities, assist
        for key in (
            (model_family, mode, role),
            (model_family, mode, None),
            (model_family, None, None),
        ):
            if key in self._templates:
                return self._templates[key]
        return self._fallback


# Plan-mode driver prompt constants live in ``_prompt_templates`` and are
# imported above so every public symbol and byte of prompt text stays exact.
# [runthru-v2 #3] the legacy incremental progress tool is retired from the advertised
# surface for ALL tiers.  It is therefore named in NO planning block — the single
# capability block above already omits it, so weak and standard share one block.
# update_plan_progress is not mentioned in any planning block either, so no per-tier
# variant is needed.
# variant is needed.


class DriverPrompts:
    """[CONTRACT role] A PromptProvider that gives the AGENT_DRIVER role phase-aware
    system prompts: a PLANNING prompt (propose a plan via `submit_plan`, take no
    action) and an EXECUTION prompt (carry out the approved plan, report progress
    via the declarative `update_plan_progress`). Every other role/mode defers to a
    wrapped base provider, so
    Research and the non-driver roles are unaffected.

    `skills_block` is an optional rendered block of the user's enabled SKILLS
    (reusable instructions, Claude-Code style). When present it is prepended to
    BOTH driver prompts so the agent follows the user's standing guidance during
    planning and execution. Empty by default — users with no skills see the
    unchanged prompts."""

    def __init__(
        self,
        base: PromptProvider | None = None,
        *,
        planning_prompt: str = _PLANNING_DRIVER_PROMPT,
        execution_prompt: str = _EXECUTION_DRIVER_PROMPT,
        execution_prompt_small: str = _EXECUTION_DRIVER_PROMPT_SMALL,
        skills_block: str = "",
        flavor: str = "build",
        autonomous: bool = False,
        host_verify_authoritative: bool = False,
        workflow_router_active: Callable[[], bool] | None = None,
        appkit_mode: bool = False,
        appkit_mode_active: Callable[[], bool] | None = None,
    ) -> None:
        self._base = base or StaticPromptProvider()
        self._appkit_mode = appkit_mode
        self._appkit_mode_active = appkit_mode_active
        self._ordinary_profile: DriverPrompts | None = None
        if appkit_mode:
            # A confirmed request_custom_build widens the live executor to ordinary
            # Build. Keep an ordinary profile built from the SAME caller inputs so
            # the next model request switches its system contract atomically with
            # that shared phase state instead of retaining semantic-only guidance.
            if appkit_mode_active is not None:
                self._ordinary_profile = DriverPrompts(
                    base=self._base,
                    planning_prompt=planning_prompt,
                    execution_prompt=execution_prompt,
                    execution_prompt_small=execution_prompt_small,
                    skills_block=skills_block,
                    flavor=flavor,
                    autonomous=autonomous,
                    host_verify_authoritative=host_verify_authoritative,
                    workflow_router_active=workflow_router_active,
                )
            planning_prompt = _APPKIT_PLANNING_DRIVER_PROMPT
            execution_prompt = _APPKIT_EXECUTION_DRIVER_PROMPT
            execution_prompt_small = _APPKIT_EXECUTION_DRIVER_PROMPT
            if autonomous:
                planning_prompt += _APPKIT_AUTONOMOUS_SCOPE_GUIDANCE
                execution_prompt += _APPKIT_AUTONOMOUS_SCOPE_GUIDANCE
                execution_prompt_small += _APPKIT_AUTONOMOUS_SCOPE_GUIDANCE
            else:
                # request_custom_build is intentionally withheld by the loop's
                # read-only planning backstop. Advertise the confirmed hatch only
                # in execution, where it is actually offered and allowed.
                execution_prompt += _APPKIT_ASSISTED_SCOPE_GUIDANCE
                execution_prompt_small += _APPKIT_ASSISTED_SCOPE_GUIDANCE
        # Autonomous mode (issue A): reinforce the tool-level suppression of ask_user
        # with an explicit instruction to assume + proceed (OpenHands "never ask for
        # human help" + Cline "make reasonable assumptions, don't end with questions").
        # The prefix applies to BOTH execution variants (capable + small-model) so
        # the assist gate stays orthogonal to autonomous behavior.
        if autonomous:
            planning_prompt = _AUTONOMOUS_PROMPT_PREFIX + planning_prompt
            execution_prompt = _AUTONOMOUS_PROMPT_PREFIX + execution_prompt
            execution_prompt_small = _AUTONOMOUS_PROMPT_PREFIX + execution_prompt_small
        # `flavor` reframes the driver's IDENTITY for the agent surface — a general
        # task agent rather than a software builder — while keeping every mechanic
        # (plan→approve→execute, the meta-tools, the finish/verify gates) byte-
        # identical. v1 is an identity-only swap; a deeper task-framed rewrite is
        # deferred (it needs eval passes). "build" leaves the prompts untouched.
        # Applied to BOTH execution variants so the small-model prompt is also
        # identity-consistent with the surface it is rendering.
        if flavor == "agent":
            planning_prompt = planning_prompt.replace(
                "autonomous build agent", "autonomous task agent"
            )
            execution_prompt = execution_prompt.replace(
                "autonomous build agent", "autonomous task agent"
            )
            execution_prompt_small = execution_prompt_small.replace(
                "autonomous build agent", "autonomous task agent"
            )
        if host_verify_authoritative:
            execution_prompt = _soften_self_verify_mandate(execution_prompt)
            execution_prompt_small = _soften_self_verify_mandate(execution_prompt_small)
        # [E5/R6] Tell the planning model about execution tools it gains on approval
        # so it doesn't falsely deny owning browser/shell/slides/etc. The planner hides
        # write tools (read_only=False) from the PLANNING schema for BOTH the build and
        # agent flavors, so BOTH need this hint — appending it agent-only made build-flavor
        # plans (e.g. the DR→slides handoff) insist they "can't use slides_generate".
        # [runthru-v2 #3] One capability block for both tiers: the legacy progress tool is retired
        # from the advertised tool surface for ALL tiers, so it is named in NO planning
        # prompt (no per-tier variant needed — prompting a tool that isn't in the tool
        # list causes the model to call a tool it can't).
        if appkit_mode:
            self._planning = planning_prompt
            self._execution = execution_prompt
            self._execution_small = execution_prompt_small
        else:
            self._planning = (
                planning_prompt + _MENTIONED_ELEMENT_GUIDANCE + _AGENT_PLANNING_CAPABILITY_BLOCK
            )
            self._execution = execution_prompt + _MENTIONED_ELEMENT_GUIDANCE
            # [C21] Tightened execution prompt for small open models. Selected ONLY
            # when the assist gate is ON (req.assist=True) at prompt-injection time.
            # Capable-model (assist OFF) path keeps using self._execution verbatim.
            self._execution_small = execution_prompt_small + _MENTIONED_ELEMENT_GUIDANCE
        self._skills_block = skills_block.strip()
        self._workflow_router_active = workflow_router_active

    def _with_skills(self, prompt: str, capabilities: frozenset[Requirement] | None = None) -> str:
        # [BP-00] Vision bullet: rendered ONLY when the driver has VISION.
        vision_bullet = ""
        if not self._appkit_mode and capabilities and Requirement.VISION in capabilities:
            vision_bullet = (
                "  • You can SEE your latest browser screenshot. After navigating, look at it: "
                "check layout, styling, and that the page is not blank or broken before "
                "moving on.\n"
            )

        # [CD-TOOLS-8] Anchored-edit bullet: names exact_replace only for an
        # anchored-edit-capable driver — the SAME capability the tool surface withholds on (a
        # standard-but-non-anchored model is NOT offered exact_replace), so naming it here can
        # never be a false affordance. Mirrors the VISION-bullet capabilities gate.
        anchored_bullet = ""
        if not self._appkit_mode and capabilities and Requirement.ANCHORED_EDIT in capabilities:
            anchored_bullet = (
                "  • For a PRECISE targeted edit, prefer `exact_replace` (an atomic exact-string "
                "replace — `file_read` first so your `old` matches the file verbatim) over a broad "
                "rewrite.\n"
            )

        bullets = vision_bullet + anchored_bullet
        if not self._skills_block:
            return f"{bullets}\n{prompt}" if bullets else prompt
        if bullets:
            return f"{self._skills_block}\n\n---\n\n{bullets}\n{prompt}"
        return f"{self._skills_block}\n\n---\n\n{prompt}"

    def _workflow_router_is_active(self) -> bool:
        return self._workflow_router_active is not None and self._workflow_router_active()

    def system_prompt(
        self,
        *,
        model_family: str,
        mode: OperatingMode | None,
        role: ModelRole,
        capabilities: frozenset[Requirement] | None = None,
        assist: bool = False,
    ) -> str:
        if self._ordinary_profile is not None and self._appkit_mode_active is not None:
            try:
                strict_appkit_active = self._appkit_mode_active()
            except Exception:  # fail closed if the shared phase reader is unavailable
                strict_appkit_active = True
            if not strict_appkit_active:
                return self._ordinary_profile.system_prompt(
                    model_family=model_family,
                    mode=mode,
                    role=role,
                    capabilities=capabilities,
                    assist=assist,
                )
        if role == ModelRole.AGENT_DRIVER:
            if self._workflow_router_is_active():
                return self._with_skills(_WORKFLOW_ROUTER_DRIVER_PROMPT, capabilities)
            if mode == OperatingMode.PLANNING:
                # [runthru-v2 #3] the legacy progress tool is retired from the advertised tool
                # surface for ALL tiers, so the single planning prompt names it for
                # NEITHER tier — weak and standard share self._planning.
                return self._with_skills(self._planning, capabilities)
            # [C21] assist gate: ON → small-model variant (crisper tool-use rules
            # for weak open models); OFF → original capable-model prompt, returned
            # byte-identical (no skills block re-formatting, no flavor side-effects).
            base = self._execution_small if assist else self._execution
            return self._with_skills(base, capabilities)
        return self._base.system_prompt(
            model_family=model_family,
            mode=mode,
            role=role,
            capabilities=capabilities,
            assist=assist,
        )
