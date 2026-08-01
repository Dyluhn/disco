"""CD-TOOLS-7 — a host-owned buffered batch of deterministic project-file transforms.

The model supplies a list of declarative operations (read / ls / replace_text / save). They run
against an in-memory BUFFER lazily loaded from the workspace; EVERY guard (governed-artifact,
path-escape, elision, literal no-match, shrink, save-grounding, syntax) is checked BEFORE a single
byte is written. After prevalidation, each changed file is sent to the sandbox's single-file commit
primitive in canonical path order. Filesystems do not provide a portable multi-file transaction,
so a handled backend failure may stop after a proven prefix; the returned outcome identifies that
prefix and any ambiguous failed-path state explicitly. This lets a build do many deterministic
edits in one auditable call without hiding partial state or requiring arbitrary code execution.
"""

from __future__ import annotations

import hashlib as hashlib
from typing import Any as Any
from typing import Literal

from disco.core.effects import (
    ActionProfile,
    EffectCapability,
)
from disco.core.effects import (
    MutationReceipt as MutationReceipt,
)
from disco.core.effects import (
    OpaqueEffectReceipt as OpaqueEffectReceipt,
)
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares, narrows
from ..sandbox.base import SandboxFileNotFoundError as SandboxFileNotFoundError
from ..sandbox.base import SandboxPermissionError
from .files import _BINARY_DELIVERABLE_EXTS as _BINARY_DELIVERABLE_EXTS
from .files import _canonical as _canonical
from .files import _clear_grounding as _clear_grounding
from .files import _commit_file_mutation as _commit_file_mutation
from .files import _file_mutation_receipt as _file_mutation_receipt
from .files import _governed_route_text, _is_governed_artifact, _read_state
from .files import _has_elision_marker as _has_elision_marker
from .files import _syntax_errors as _syntax_errors
from .run_script_parts.commit import _commit_run_script_files
from .run_script_parts.ops import _READ_RESULT_CAP as _READ_RESULT_CAP
from .run_script_parts.ops import _dispatch_op
from .run_script_parts.outcomes import _commit_failure as _commit_failure
from .run_script_parts.outcomes import _exception_detail as _exception_detail
from .run_script_parts.outcomes import _fail, _success
from .run_script_parts.prevalidate import _MAX_TOTAL_WRITE_BYTES as _MAX_TOTAL_WRITE_BYTES
from .run_script_parts.prevalidate import _prevalidate_effective_mutations
from .run_script_parts.state import _ScriptState

_FS = frozenset({Capability.FILESYSTEM})

_MAX_OPS = 200


class RunScriptOp(BaseModel):
    op: Literal["read", "ls", "replace_text", "save"] = Field(description="The operation kind.")
    path: str = Field(description="Workspace-relative path the op targets.")
    # replace_text
    old: str | None = Field(default=None, description="replace_text: exact literal text to find.")
    new: str | None = Field(
        default=None, description="replace_text: literal replacement (no regex)."
    )
    replace_all: bool = Field(
        default=False, description="replace_text: replace ALL occurrences (default: first)."
    )
    # save
    content: str | None = Field(default=None, description="save: full UTF-8 content to write.")
    allow_shrink: bool = Field(
        default=False, description="save: permit a >50% shrink of an existing file."
    )
    expected_sha256: str | None = Field(
        default=None, description="save: grounds the write — must equal the file's current sha256."
    )


class RunScriptArgs(BaseModel):
    operations: list[RunScriptOp] = Field(
        description="Ordered operations prevalidated as one batch, then committed per file."
    )


class RunProjectScriptTool:
    """Prevalidate a buffered batch, then commit each changed file in stable order."""

    definition = ToolDef(
        name="run_project_script",
        description=(
            "Apply MANY deterministic file edits as one prevalidated batch. Pass `operations`: "
            "a list of "
            "{op, path, ...} where op is 'read'/'ls' (inspect), 'replace_text' "
            "(literal find/replace "
            "— pass exact `old` + `new`, optional replace_all), or 'save' (write full `content`). "
            "All guards run before commit, so a validation failure (no match, shrink, unreadable "
            "file, .disco/ artifact, syntax error) writes NOTHING. Changed files then commit "
            "one at a time through the sandbox; a handled backend failure reports exact proven "
            "commits and flags any ambiguous file state. "
            "To save over an existing file, "
            "read it first (a 'read' op on it, or a prior replace_text, or pass expected_sha256). "
            "Each element of `operations` is an OBJECT, never a bare string. Example call:\n"
            "  run_project_script(operations=[\n"
            '    {"op": "read", "path": "index.html"},\n'
            '    {"op": "replace_text", "path": "index.html", "old": "Launch Day", '
            '"new": "Grand Opening"},\n'
            '    {"op": "save", "path": "styles.css", "content": "body { margin: 0; }"}\n'
            "  ])"
        ),
        args_model=RunScriptArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(
            EffectCapability.WORKSPACE_CONTENT_READ,
            EffectCapability.WORKSPACE_INVENTORY_READ,
            EffectCapability.WORKSPACE_MUTATE,
            planner_safe=False,
        ),
    )

    def action_profile(self, args: RunScriptArgs) -> ActionProfile:
        capabilities: set[EffectCapability] = set()
        for operation in args.operations:
            if operation.op == "read":
                capabilities.add(EffectCapability.WORKSPACE_CONTENT_READ)
            elif operation.op == "ls":
                capabilities.add(EffectCapability.WORKSPACE_INVENTORY_READ)
            else:
                capabilities.add(EffectCapability.WORKSPACE_MUTATE)
        return narrows(*capabilities)

    async def run(self, args: RunScriptArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        sbx = ctx.sandbox
        ops = args.operations
        if not ops:
            return _fail(0, "SCRIPT_BATCH_FAILED", "no operations supplied.")
        if len(ops) > _MAX_OPS:
            return _fail(
                0, "SCRIPT_TOO_LARGE", f"{len(ops)} operations exceeds the cap of {_MAX_OPS}."
            )

        # EVERYTHING in `state` is keyed by the REAL (symlink-followed) workspace-relative
        # path so an alias (a symlink or a `..` path) can never make the checks validate one
        # file while the commit writes another (codex CD-TOOLS-7 round-1).
        state = _ScriptState(
            sbx=sbx,
            rsw=(_read_state.get(ctx.conversation_id) or {}).get("read_since_write") or set(),
        )

        for i, op in enumerate(ops):
            try:
                canon = await state.key(op.path)
            except SandboxPermissionError:
                return _fail(i, "SCRIPT_PATH_ESCAPE", f"path escapes the workspace: {op.path!r}")
            mutating = op.op in ("replace_text", "save")

            # (1) governed guard — a MUTATING op whose REAL path (canon) is in the host-managed
            #     .disco/ namespace is routed to its semantic tool. Checked on `canon` (the same key
            #     used for the commit) so a symlink/alias can never split the check from the write.
            if mutating and _is_governed_artifact(canon):
                tool, route = _governed_route_text(canon, allowed_tools=ctx.scope_allowed_tools)
                return _fail(
                    i,
                    "GOVERNED_ARTIFACT_REJECTED",
                    f"{op.path} resolves to the host-managed .disco/ namespace ({canon}). {route}",
                    resolved=canon,
                    route_to=tool,
                )

            try:
                outcome = await _dispatch_op(state, i, op, canon)
            except SandboxPermissionError:
                return _fail(i, "SCRIPT_PATH_ESCAPE", f"path escapes the workspace: {op.path!r}")
            if outcome is not None:
                return outcome

        if not state.mutated:
            return _success(
                "run_project_script: inspection only — no files changed.",
                [],
                len(ops),
                state.reads_out,
            )

        # (2) prevalidate IN MEMORY — byte-diff, size cap, and introduced-syntax-error checks;
        # a batch that fails any of these writes nothing.
        prevalidated = _prevalidate_effective_mutations(
            state.buffer, state.original, state.original_bytes, state.mutated, len(ops) - 1
        )
        if isinstance(prevalidated, ToolOutcome):
            return prevalidated
        after_bytes, effective_mutated = prevalidated

        # (3) COMMIT — guards are complete, but the portable sandbox contract has no multi-file
        # transaction. `_commit_run_script_files` preserves exact proven commits and identifies
        # an ambiguous failed path on every handled backend error instead of claiming a rollback
        # the backend cannot guarantee.
        return await _commit_run_script_files(
            ctx,
            sbx,
            after_bytes,
            state.original_bytes,
            effective_mutated,
            len(ops),
            state.reads_out,
        )
