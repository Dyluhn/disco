"""disco.core.workflows — WorkflowPromptPacks (P3).

A WorkflowPromptPack is the mode-specific operating manual the model runs inside for a
given artifact contract: its role, the workflow steps, the allowed/forbidden tools, the
targeted-edit law, preview/verify/export rules, context policy, and done criteria. Both
kernels (DiscoKernel/PiKernel) consume the SAME pack, so product behavior can't diverge.

WPP-1 ships the pack FORMAT (parsed from markdown section headers) + a loader/registry
over the bundled ``prompt_packs/*.md`` files. Pure; no runtime imports.
"""

from __future__ import annotations

from .assembly import assemble_workflow_prompt
from .prompt_pack import (
    REQUIRED_SECTIONS,
    PromptPack,
    PromptPackRegistry,
    parse_prompt_pack,
)
from .skill_mount import is_skill_mountable, resolve_mounted_skills

__all__ = [
    "REQUIRED_SECTIONS",
    "PromptPack",
    "PromptPackRegistry",
    "assemble_workflow_prompt",
    "is_skill_mountable",
    "parse_prompt_pack",
    "resolve_mounted_skills",
]
