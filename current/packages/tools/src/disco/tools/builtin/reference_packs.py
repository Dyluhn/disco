"""Agent-side Reference Pack creation action.

The host integration supplies ``store`` and an active workspace reader through
the function seam below.  The tool wrapper remains narrow and never discovers
or reads the process working directory.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Protocol

from disco.core import SecurityRisk
from disco.core.effects import ActionProfile, EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares, narrows


class ReferencePackFileArg(BaseModel):
    path: str = Field(description="workspace-relative file path from the active Agent workspace")
    name: str | None = Field(default=None, description="optional display name for the copied file")
    media_type: str | None = Field(default=None, description="optional MIME type")


class CreateReferencePackArgs(BaseModel):
    name: str = Field(description="human-readable reusable pack name")
    description: str = Field(default="", description="short description of the reference material")
    files: list[ReferencePackFileArg] = Field(
        min_length=1,
        description="files already present in the active workspace; paths are relative to it",
    )


class ReferenceInspectArgs(BaseModel):
    pack_id: str = Field(description="ID of the pinned Reference Pack to inspect")
    file_name: str = Field(description="workspace-relative file name inside that pinned pack")
    question: str = Field(
        default="",
        description="optional question for the host vision observer when inspecting an image",
    )


class PinnedReferenceInspector(Protocol):
    """Host callback backed by owner/conversation pinned-binding validation.

    The callback must resolve the binding from trusted host state before reading
    any bytes.  Model-supplied ``pack_id``/``file_name`` values are selectors,
    not filesystem paths or authority, and the callback returns the bounded
    ``ReferenceInspection.as_dict()`` projection.
    """

    def __call__(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        pack_id: str,
        file_name: str,
        question: str,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


class ReferencePackVersion(Protocol):
    files: Sequence[object]


class ReferencePackRecord(Protocol):
    id: str
    name: str
    current: ReferencePackVersion
    current_version_id: str


class ReferencePackStore(Protocol):
    def acreate(
        self,
        owner_id: str,
        name: str,
        description: str,
        files: list[ReferencePackFileArg | dict[str, Any] | str],
        *,
        reader: Any,
    ) -> Awaitable[ReferencePackRecord]: ...


async def create_reference_pack(
    name: str,
    description: str,
    files: Sequence[ReferencePackFileArg | dict[str, Any] | str],
    *,
    owner_id: str,
    store: ReferencePackStore,
    reader: Any,
) -> Any:
    """Create one pack through the host-injected async ingress seam."""

    return await store.acreate(owner_id, name, description, list(files), reader=reader)


class CreateReferencePackTool:
    def __init__(
        self, *, store: ReferencePackStore | None = None, reader_factory: Any | None = None
    ) -> None:
        """Bind host authorities once at composition time.

        ``ToolContext`` is frozen and intentionally has no ad-hoc collaborator
        attributes.  The factory receives the context's owner/conversation
        identity and returns the already-authorized active workspace reader.
        """
        self._store = store
        self._reader_factory = reader_factory

    definition = ToolDef(
        name="create_reference_pack",
        description=(
            "Create a reusable inert Reference Pack from files in this Agent workspace. "
            "Use only when the user explicitly asks to save reference material for a future "
            "Build. This copies exact bytes and never grants tools or permissions."
        ),
        args_model=CreateReferencePackArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.MEDIUM,
        runs_in="in_process",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_CONTENT_READ, planner_safe=False),
    )

    def action_profile(self, args: CreateReferencePackArgs) -> ActionProfile:
        del args
        return narrows(EffectCapability.WORKSPACE_CONTENT_READ)

    async def run(self, args: CreateReferencePackArgs, ctx: ToolContext) -> ToolOutcome:
        # Runtime fan-in attaches these host-only collaborators to the context
        # without exposing them in the model schema.  A standalone invocation
        # fails closed instead of guessing a root or using server cwd.
        store = self._store
        reader = self._reader_factory(ctx) if self._reader_factory is not None else None
        if inspect.isawaitable(reader):
            reader = await reader
        if store is None or reader is None:
            return ToolOutcome(
                success=False,
                error="reference_pack_host_seam_unavailable",
                content=(
                    "Reference Pack creation is unavailable until the active "
                    "workspace reader is bound."
                ),
            )
        try:
            pack = await create_reference_pack(
                args.name,
                args.description,
                args.files,
                owner_id=ctx.owner_id,
                store=store,
                reader=reader,
            )
        except Exception as exc:  # domain errors become truthful tool failures
            return ToolOutcome(success=False, error="reference_pack_rejected", content=str(exc))
        return ToolOutcome(
            success=True,
            content=(
                f"Created Reference Pack '{pack.name}' with {len(pack.current.files)} files. "
                "It is available to select when starting a Build."
            ),
            structured={
                "pack_id": pack.id,
                "name": pack.name,
                "file_count": len(pack.current.files),
                "current_version_id": pack.current_version_id,
                "use_in_build": True,
            },
        )


class ReferenceInspectTool:
    """Inspect only bytes authorized by a host-pinned binding callback."""

    definition = ToolDef(
        name="reference_inspect",
        description=(
            "Inspect one file from the immutable Reference Packs pinned to this Build. "
            "Text returns bounded text; images use the host vision observer when available; "
            "other binaries are honestly reported as asset-only."
        ),
        args_model=ReferenceInspectArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
        behavior=declares(EffectCapability.WORKSPACE_CONTENT_READ, planner_safe=True),
    )

    def __init__(self, *, inspector: PinnedReferenceInspector | None = None) -> None:
        self._inspector = inspector

    async def run(self, args: ReferenceInspectArgs, ctx: ToolContext) -> ToolOutcome:
        inspector = self._inspector
        if inspector is None:
            return ToolOutcome(
                success=False,
                error="reference_inspect_host_seam_unavailable",
                content="Reference Pack inspection is unavailable in this session.",
            )
        try:
            result = inspector(
                owner_id=ctx.owner_id,
                conversation_id=ctx.conversation_id,
                pack_id=args.pack_id,
                file_name=args.file_name,
                question=args.question,
            )
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Mapping):
                raise TypeError("pinned inspector returned a non-mapping result")
            structured = dict(result)
            status = str(structured.get("status") or "asset_only")
            content = str(structured.get("content") or status)
            return ToolOutcome(success=True, content=content, structured=structured)
        except Exception as exc:  # host errors are truthful tool failures
            return ToolOutcome(success=False, error="reference_inspect_failed", content=str(exc))


__all__ = [
    "CreateReferencePackArgs",
    "CreateReferencePackTool",
    "PinnedReferenceInspector",
    "ReferenceInspectArgs",
    "ReferenceInspectTool",
    "ReferencePackFileArg",
    "create_reference_pack",
]
