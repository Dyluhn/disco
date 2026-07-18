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

import hashlib
from typing import Any, Literal

from disco.core.effects import (
    ActionProfile,
    EffectCapability,
    MutationReceipt,
    OpaqueEffectReceipt,
)
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares, narrows
from ..sandbox.base import SandboxFileNotFoundError, SandboxPermissionError
from .files import (
    _BINARY_DELIVERABLE_EXTS,
    _canonical,
    _clear_grounding,
    _commit_file_mutation,
    _file_mutation_receipt,
    _governed_route_text,
    _has_elision_marker,
    _is_governed_artifact,
    _read_state,
    _syntax_errors,
)

_FS = frozenset({Capability.FILESYSTEM})

_MAX_OPS = 200
_MAX_TOTAL_WRITE_BYTES = 2_000_000  # cap committed content (anti-runaway / context-poison)
_READ_RESULT_CAP = 4_000  # per-read content returned to the model (capped)


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


def _fail(op_index: int, error: str, message: str, **extra: Any) -> ToolOutcome:
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"run_project_script aborted at operation #{op_index} (nothing was written): {message}"
        ),
        structured={"kind": "run_script_aborted", "op_index": op_index, "error": error, **extra},
    )


def _success(
    content: str,
    applied: list[str],
    operations_run: int,
    reads: dict[str, Any],
    receipts: tuple[MutationReceipt, ...] = (),
) -> ToolOutcome:
    return ToolOutcome(
        success=True,
        content=content,
        artifacts=applied,
        structured={
            "ok": True,
            "applied": applied,
            "operations_run": operations_run,
            "reads": reads,
        },
        effect_receipts=receipts,
    )


def _commit_failure(
    *,
    applied: list[str],
    receipts: list[MutationReceipt | OpaqueEffectReceipt],
    failed_path: str,
    failed_path_state: Literal["unchanged", "committed", "unknown"],
    reason: str,
    operations_run: int,
    reads: dict[str, Any],
    underlying: dict[str, Any] | None = None,
) -> ToolOutcome:
    if failed_path_state == "unchanged":
        partial = bool(applied)
        error = "SCRIPT_PARTIAL_COMMIT" if partial else "SCRIPT_COMMIT_FAILED"
        kind = "run_script_partial_commit" if partial else "run_script_commit_failed"
        summary = (
            f"run_project_script stopped before committing {failed_path}; "
            f"{len(applied)} earlier file(s) remain committed"
            if partial
            else f"run_project_script could not commit {failed_path}; no file was committed"
        )
    elif failed_path_state == "committed":
        partial = True
        error = "SCRIPT_COMMIT_INTERRUPTED"
        kind = "run_script_commit_interrupted"
        summary = (
            f"the backend reported a failure for {failed_path}, but its exact intended bytes "
            f"were observed afterward; {len(applied)} file(s) are proven committed"
        )
    else:
        partial = True
        error = "SCRIPT_COMMIT_UNCERTAIN"
        kind = "run_script_commit_uncertain"
        summary = (
            f"the backend reported a failure for {failed_path} and its final state could not "
            f"be attributed; {len(applied)} other file(s) are proven committed"
        )
    structured: dict[str, Any] = {
        "kind": kind,
        "applied": applied,
        "failed_path": failed_path,
        "failed_path_state": failed_path_state,
        "operations_run": operations_run,
        "reads": reads,
    }
    if underlying is not None:
        structured["underlying"] = underlying
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"{summary}. The result lists exact proven commits; do not assume any unlisted "
            f"path applied. Reason: {reason}"
        ),
        artifacts=applied,
        structured=structured,
        effect_receipts=tuple(receipts),
    )


def _exception_detail(exc: Exception) -> dict[str, str]:
    """Keep a bounded, single-line backend cause without repr/debug payloads."""
    message = " ".join(str(exc).split())[:500]
    return {"type": type(exc).__name__, "message": message or "no detail supplied"}


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

        # EVERYTHING is keyed by the REAL (symlink-followed) workspace-relative path so an alias
        # (a symlink or a `..` path) can never make the checks validate one file while the commit
        # writes another (codex CD-TOOLS-7 round-1). The same key is used for the buffer, the
        # grounding `seen` set, AND the atomic_write commit path — they can never diverge.
        buffer: dict[str, str] = {}  # REAL relpath -> current (possibly mutated) content
        original: dict[
            str, str | None
        ] = {}  # REAL relpath -> disk content at first load (None=absent)
        original_bytes: dict[str, bytes | None] = {}
        mutated: set[str] = set()  # REAL relpaths the buffer changed
        seen: set[str] = set()  # GROUNDED real relpaths (explicitly read or transformed this batch)
        reads_out: dict[str, Any] = {}
        rsw = (_read_state.get(ctx.conversation_id) or {}).get("read_since_write") or set()

        async def _key(path: str) -> str:
            """The REAL (symlink-followed) workspace-relative path; raises SandboxPermissionError on
            a jail escape so the caller aborts with SCRIPT_PATH_ESCAPE."""
            rr = getattr(sbx, "resolve_relpath", None)
            if rr is not None:
                return str(await rr(path)).replace("\\", "/")
            return _canonical(path)

        async def _disk_read(path: str) -> tuple[str | None, bytes]:
            """Return (text or None-if-absent, raw bytes)."""
            try:
                raw = await sbx.read_file(path)
            except (SandboxFileNotFoundError, FileNotFoundError):
                return None, b""
            return raw.decode("utf-8", errors="replace"), raw

        async def _load(canon: str, path: str) -> str | None:
            if canon in buffer:
                return buffer[canon]
            text, raw = await _disk_read(path)
            original[canon] = text
            original_bytes[canon] = raw if text is not None else None
            if text is not None:
                buffer[canon] = text
            return text

        for i, op in enumerate(ops):
            try:
                canon = await _key(op.path)
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
                if op.op == "ls":
                    try:
                        reads_out[op.path] = await sbx.list_dir(op.path)
                    except (SandboxFileNotFoundError, FileNotFoundError):
                        reads_out[op.path] = []
                elif op.op == "read":
                    text = await _load(canon, op.path)
                    seen.add(canon)
                    reads_out[op.path] = (text or "")[:_READ_RESULT_CAP]
                elif op.op == "replace_text":
                    if op.old is None or op.new is None:
                        return _fail(i, "SCRIPT_OP_INVALID", "replace_text needs `old` and `new`.")
                    if _has_elision_marker(op.old, op.new):
                        return _fail(
                            i,
                            "ELISION_MARKER_REJECTED",
                            "edit text contains an elision placeholder.",
                        )
                    cur = await _load(canon, op.path)
                    if cur is None:
                        return _fail(i, "SCRIPT_NO_MATCH", f"{op.path} does not exist.")
                    if op.old not in cur:
                        return _fail(i, "SCRIPT_NO_MATCH", f"`old` not found in {op.path}.")
                    seen.add(canon)  # matched the file's REAL current content → grounded
                    buffer[canon] = (
                        cur.replace(op.old, op.new)
                        if op.replace_all
                        else cur.replace(op.old, op.new, 1)
                    )
                    mutated.add(canon)
                elif op.op == "save":
                    if op.content is None:
                        return _fail(i, "SCRIPT_OP_INVALID", "save needs `content`.")
                    if _has_elision_marker(op.content):
                        return _fail(
                            i, "ELISION_MARKER_REJECTED", "content contains an elision placeholder."
                        )
                    existed_text, raw = await _disk_read(op.path)
                    if canon not in original:
                        original[canon] = existed_text
                        original_bytes[canon] = raw if existed_text is not None else None
                    if existed_text is not None:
                        # EXISTING file → require grounding (no blind clobber of an unread file).
                        grounded = (canon in seen) or (canon in rsw)
                        if op.expected_sha256 is not None:
                            if op.expected_sha256 != hashlib.sha256(raw).hexdigest():
                                return _fail(
                                    i, "STALE_FILE_CONTEXT", f"{op.path} changed since you read it."
                                )
                            grounded = True
                        if not grounded:
                            return _fail(
                                i,
                                "FRESH_READ_REQUIRED",
                                f"cannot save over the existing {op.path} without reading it first "
                                "(add a 'read' op on it, transform it with replace_text, "
                                "or pass expected_sha256).",
                            )
                        # extension from the REAL resolved path (canon), not the alias the model
                        # typed — so alias.txt -> deck.pptx can't validate one ext +
                        # clobber another.
                        ext = canon.rsplit(".", 1)[-1].lower() if "." in canon else ""
                        if ext in _BINARY_DELIVERABLE_EXTS:
                            return _fail(
                                i,
                                "binary_deliverable_clobber",
                                f"{op.path} is an existing {ext} binary.",
                            )
                        matching_sha = op.expected_sha256 is not None
                        if (
                            len(op.content) < 0.5 * len(existed_text)
                            and not op.allow_shrink
                            and not matching_sha
                        ):
                            return _fail(
                                i,
                                "SAFE_WRITE_SHRINK_REJECTED",
                                f"save would shrink {op.path} from {len(existed_text)} "
                                f"to {len(op.content)} chars "
                                "(>50%); pass allow_shrink=true if intended.",
                            )
                    buffer[canon] = op.content
                    seen.add(canon)
                    mutated.add(canon)
            except SandboxPermissionError:
                return _fail(i, "SCRIPT_PATH_ESCAPE", f"path escapes the workspace: {op.path!r}")

        if not mutated:
            return _success(
                "run_project_script: inspection only — no files changed.", [], len(ops), reads_out
            )

        # A requested mutation is not an effective mutation merely because it
        # matched and entered the buffer. Keep only byte-changing paths (a new
        # empty file still changes filesystem state because its original is None).
        after_bytes = {canon: buffer[canon].encode("utf-8") for canon in mutated}
        effective_mutated = {
            canon for canon in mutated if after_bytes[canon] != original_bytes.get(canon)
        }
        if not effective_mutated:
            return _fail(
                len(ops) - 1,
                "SCRIPT_NO_CHANGES",
                "all requested mutations were byte-identical to current files.",
            )
        mutated = effective_mutated

        # (2) caps + syntax pre-check IN MEMORY — a syntax-introducing batch writes nothing.
        total = sum(len(after_bytes[c]) for c in mutated)
        if total > _MAX_TOTAL_WRITE_BYTES:
            return _fail(
                len(ops) - 1,
                "SCRIPT_TOO_LARGE",
                f"committed content {total}B exceeds {_MAX_TOTAL_WRITE_BYTES}B.",
            )
        for canon in mutated:
            pre = _syntax_errors(canon, original.get(canon) or "")
            introduced = [e for e in _syntax_errors(canon, buffer[canon]) if e not in pre]
            if introduced:
                return _fail(
                    len(ops) - 1,
                    "SCRIPT_BATCH_FAILED",
                    f"the batch would introduce syntax error(s) in {canon}: "
                    f"{'; '.join(introduced)}.",
                )

        # (3) COMMIT — guards are complete, but the portable sandbox contract has no multi-file
        # transaction. Preserve exact proven commits and identify an ambiguous failed path on every
        # handled backend error instead of claiming a rollback the backend cannot guarantee.
        applied: list[str] = []
        receipts: list[MutationReceipt | OpaqueEffectReceipt] = []
        for canon in sorted(mutated):
            intended = after_bytes[canon]
            before = original_bytes.get(canon)
            try:
                committed = await _commit_file_mutation(
                    ctx,
                    canon,
                    intended,
                    expected_before=before,
                )
            except Exception as exc:  # noqa: BLE001 — preserve any committed prefix honestly
                # A transport can report failure before or after applying its write primitive.
                # Re-read once: intended bytes prove commit, unchanged bytes prove no commit,
                # and any third or unreadable state remains explicitly opaque.
                observed_known = True
                try:
                    observed: bytes | None = await sbx.read_file(canon)
                except FileNotFoundError:
                    observed = None
                except Exception:  # noqa: BLE001 — genuinely unattributable final state
                    observed = None
                    observed_known = False
                if observed_known and observed == intended:
                    receipts.append(_file_mutation_receipt(canon, before=before, after=intended))
                    applied.append(canon)
                    _clear_grounding(ctx.conversation_id, canon)
                    failed_path_state: Literal["unchanged", "committed", "unknown"] = "committed"
                elif observed_known and observed == before:
                    failed_path_state = "unchanged"
                else:
                    receipts.append(
                        OpaqueEffectReceipt(
                            capability=EffectCapability.WORKSPACE_MUTATE,
                            reason=(
                                f"run_project_script commit state for {canon} changed but could "
                                "not be attributed exactly after a backend failure"
                            ),
                        )
                    )
                    _clear_grounding(ctx.conversation_id, canon)
                    failed_path_state = "unknown"
                detail = _exception_detail(exc)
                return _commit_failure(
                    applied=applied,
                    receipts=receipts,
                    failed_path=canon,
                    failed_path_state=failed_path_state,
                    reason=f"{detail['type']}: {detail['message']}",
                    operations_run=len(ops),
                    reads=reads_out,
                    underlying=detail,
                )
            if isinstance(committed, ToolOutcome):
                if not applied:
                    return committed
                return _commit_failure(
                    applied=applied,
                    receipts=receipts,
                    failed_path=canon,
                    failed_path_state="unchanged",
                    reason=(f"{committed.error or 'stale file context'}: {committed.content}"),
                    operations_run=len(ops),
                    reads=reads_out,
                    underlying={
                        "error": committed.error,
                        "content": committed.content,
                        "structured": committed.structured,
                    },
                )
            receipts.append(committed)
            # [REL-RC-D] a script commit is an EXTERNAL (non-anchored) mutation → fully un-ground
            # both bits so the next edit/write requires a genuine fresh read.
            _clear_grounding(ctx.conversation_id, canon)
            applied.append(canon)
        return _success(
            f"run_project_script committed {len(applied)} file(s): {', '.join(applied)}.",
            applied,
            len(ops),
            reads_out,
            tuple(receipt for receipt in receipts if isinstance(receipt, MutationReceipt)),
        )
