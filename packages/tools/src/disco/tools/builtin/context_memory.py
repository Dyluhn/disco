"""`context_memory` — read/write the durable ``.disco/context/*`` files.

Gives the agent a manual handle on its own file-backed memory (CXT-2): read any
durable context kind, and write the NARRATIVE (markdown) kinds — current_goal,
todo, decisions, assumptions. The STRUCTURED (json) kinds — resource_manifest,
direct_edits, unresolved_comments, source_priority, verifier_failures — are
read-only via this tool: they are managed by the runtime/store so the model never
hand-authors malformed JSON (no false affordance — a write attempt is rejected
with a clear reason, not silently dropped).
"""

from __future__ import annotations

import json
from typing import Literal

from disco.core import SecurityRisk
from disco.core.context import ArtifactMemoryKind, ArtifactMemoryStore, ContextRecoveryError
from disco.core.context.store import _MD_KINDS, _SINGLETON_KINDS
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

# [REL-2a] ARTIFACT_MANIFEST is an INTERNAL runtime manifest (host-folded per-artifact state), not
# narrative/context memory the model reads or writes — exclude it from the model-facing kind list.
_INTERNAL_KINDS: frozenset[ArtifactMemoryKind] = frozenset({ArtifactMemoryKind.ARTIFACT_MANIFEST})
_DURABLE_KINDS: list[str] = sorted(k.value for k in _SINGLETON_KINDS - _INTERNAL_KINDS)
_WRITABLE_KINDS: list[str] = sorted(k.value for k in _MD_KINDS)


class ContextMemoryArgs(BaseModel):
    action: Literal["read", "write", "list"] = Field(
        description="read a durable context file, write a narrative one, or list the kinds."
    )
    kind: str = Field(
        default="",
        description=f"the context kind. One of: {_DURABLE_KINDS}. Ignored for action=list.",
    )
    content: str | None = Field(
        default=None, description="markdown body for action=write (narrative kinds only)."
    )


class ContextMemoryTool:
    definition = ToolDef(
        name="context_memory",
        description=(
            "Durable working memory under .disco/context/. action='list' shows the "
            "kinds; action='read' returns a kind's current content (or '(empty)'); "
            "action='write' updates a NARRATIVE kind (current_goal/todo/decisions/"
            "assumptions). Structured kinds (resource_manifest/direct_edits/"
            "unresolved_comments/source_priority/verifier_failures) are read-only here — "
            "the runtime manages them. This memory survives context truncation and resume."
        ),
        args_model=ContextMemoryArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: ContextMemoryArgs, ctx: ToolContext) -> ToolOutcome:
        if args.action == "list":
            return ToolOutcome(
                success=True,
                content="\n".join(_DURABLE_KINDS),
                structured={"kinds": _DURABLE_KINDS, "writable": _WRITABLE_KINDS},
            )

        try:
            kind = ArtifactMemoryKind(args.kind)
        except ValueError:
            return ToolOutcome(
                success=False,
                error="invalid_kind",
                content=f"unknown context kind {args.kind!r}; valid: {_DURABLE_KINDS}",
            )
        if kind not in _SINGLETON_KINDS or kind in _INTERNAL_KINDS:
            return ToolOutcome(
                success=False,
                error="invalid_kind",
                content=(
                    f"{kind.value} is not a durable singleton context kind; valid: {_DURABLE_KINDS}"
                ),
            )

        assert ctx.sandbox is not None  # runs_in="sandbox" → executor supplies one
        store = ArtifactMemoryStore(ctx.sandbox)

        if args.action == "read":
            if kind in _MD_KINDS:
                text = await store.read_markdown(kind)
                return ToolOutcome(success=True, content=text if text is not None else "(empty)")
            try:
                raw = await store.read_json_raw(kind)
            except ContextRecoveryError as exc:
                return ToolOutcome(success=False, error="parse_error", content=str(exc))
            return ToolOutcome(
                success=True, content="(empty)" if raw is None else json.dumps(raw, indent=2)
            )

        # action == "write"
        if kind not in _MD_KINDS:
            return ToolOutcome(
                success=False,
                error="structured_kind_readonly_via_tool",
                content=(
                    f"{kind.value} is structured JSON managed by the runtime and is not "
                    f"writable via context_memory; writable kinds: {_WRITABLE_KINDS}"
                ),
            )
        if args.content is None:
            return ToolOutcome(
                success=False, error="invalid_arguments", content="action=write requires `content`"
            )
        ref = await store.write_markdown(kind, args.content)
        return ToolOutcome(
            success=True, content=f"wrote {ref.rel_path}", structured={"rel_path": ref.rel_path}
        )
