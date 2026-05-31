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
