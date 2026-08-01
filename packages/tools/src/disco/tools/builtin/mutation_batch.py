"""Target-agnostic commit boundary for deterministic multi-file producers.

Generators prepare every final byte before calling this module.  The boundary
resolves resource identities, reads every current revision before the first
write, removes byte-identical intents, and commits in a deterministic order.
Portable filesystems do not provide a multi-file transaction, so handled
backend failures return exact proven commits plus an explicit unknown receipt
when the failed path cannot be attributed.

The helper deliberately knows nothing about AppKit, web frameworks, documents,
or target layouts.  Callers choose which planned paths commit last when one
file acts as their canonical retry/consistency marker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from disco.core.effects import (
    EffectCapability,
    MutationReceipt,
    OpaqueEffectReceipt,
)

from ..anatomy import ToolContext, ToolOutcome
from .files import (
    _canonical,
    _clear_grounding,
    _commit_file_deletion,
    _commit_file_mutation,
    _file_mutation_receipt,
)


@dataclass(frozen=True, slots=True)
class PlannedFileMutation:
    """One complete desired workspace-file state; ``None`` means deletion."""

    path: str
    after: bytes | None


@dataclass(frozen=True, slots=True)
class DeterministicBatchResult:
    """Exact changed paths and receipts after a fully acknowledged batch."""

    changed_paths: tuple[str, ...]
    receipts: tuple[MutationReceipt, ...]


def _exception_detail(exc: Exception) -> dict[str, str]:
    message = " ".join(str(exc).split())[:500]
    return {"type": type(exc).__name__, "message": message or "no detail supplied"}


def _prevalidation_failure(
    *,
    error: str,
    message: str,
    path: str | None = None,
    underlying: dict[str, str] | None = None,
) -> ToolOutcome:
    structured: dict[str, Any] = {"kind": "deterministic_batch_prevalidation_failed"}
    if path is not None:
        structured["path"] = path
    if underlying is not None:
        structured["underlying"] = underlying
    return ToolOutcome(success=False, error=error, content=message, structured=structured)


def _commit_failure(
    *,
    applied: list[str],
    receipts: list[MutationReceipt | OpaqueEffectReceipt],
    failed_path: str,
    failed_path_state: Literal["unchanged", "committed", "unknown"],
    reason: str,
    underlying: dict[str, Any] | None = None,
) -> ToolOutcome:
    if failed_path_state == "unchanged":
        partial = bool(applied)
        error = "BATCH_PARTIAL_COMMIT" if partial else "BATCH_COMMIT_FAILED"
        kind = "deterministic_batch_partial_commit" if partial else "deterministic_batch_failed"
        summary = (
            f"deterministic batch stopped before committing {failed_path}; "
            f"{len(applied)} earlier file(s) remain committed"
            if partial
            else f"deterministic batch could not commit {failed_path}; no file was committed"
        )
    elif failed_path_state == "committed":
        error = "BATCH_COMMIT_INTERRUPTED"
        kind = "deterministic_batch_commit_interrupted"
        summary = (
            f"the backend reported a failure for {failed_path}, but its intended state was "
            f"observed afterward; {len(applied)} file(s) are proven committed"
        )
    else:
        error = "BATCH_COMMIT_UNCERTAIN"
        kind = "deterministic_batch_commit_uncertain"
        summary = (
            f"the backend reported a failure for {failed_path} and its final state could not "
            f"be attributed; {len(applied)} other file(s) are proven committed"
        )
    structured: dict[str, Any] = {
        "kind": kind,
        "applied": applied,
        "failed_path": failed_path,
        "failed_path_state": failed_path_state,
    }
    if underlying is not None:
        structured["underlying"] = underlying
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"{summary}. Re-read the failed or uncertain path before retrying; do not assume "
            f"any unlisted path applied. Reason: {reason}"
        ),
        artifacts=applied,
        structured=structured,
        effect_receipts=tuple(receipts),
    )


async def _resolved_path(ctx: ToolContext, path: str) -> str:
    assert ctx.sandbox is not None
    resolver = getattr(ctx.sandbox, "resolve_relpath", None)
    if resolver is None:
        return _canonical(path)
    return _canonical(await resolver(path))


async def _resolve_planned_intents(
    ctx: ToolContext,
    intents: Sequence[PlannedFileMutation],
    commit_last: Sequence[str],
) -> tuple[dict[str, bytes | None], list[str]] | ToolOutcome:
    """Resolve every intent + ``commit_last`` path and validate the plan is unambiguous.

    Returns ``(planned, commit_last_resolved)`` once every path has resolved to
    exactly one final state and ``commit_last`` names only planned, alias-free
    paths — or the prevalidation-failure `ToolOutcome` for the first problem found.
    """
    planned: dict[str, bytes | None] = {}
    try:
        for intent in intents:
            resolved = await _resolved_path(ctx, intent.path)
            if intent.after is None and _canonical(intent.path) != resolved:
                return _prevalidation_failure(
                    error="BATCH_DELETE_ALIAS_REFUSED",
                    message=(
                        f"deterministic batch refused before writing: deletion target "
                        f"{intent.path!r} resolves through an alias to {resolved!r}. Generated "
                        "cleanup never follows symlinks into another resource; remove the alias "
                        "or use a fresh workspace."
                    ),
                    path=_canonical(intent.path),
                )
            if resolved in planned and planned[resolved] != intent.after:
                return _prevalidation_failure(
                    error="BATCH_PLAN_CONFLICT",
                    message=(
                        f"deterministic batch refused before writing: {intent.path!r} resolves "
                        f"to {resolved!r}, which has two different planned final states."
                    ),
                    path=resolved,
                )
            planned[resolved] = intent.after
        commit_last_resolved = [await _resolved_path(ctx, path) for path in commit_last]
    except Exception as exc:  # noqa: BLE001 - path resolution is a pre-write boundary
        detail = _exception_detail(exc)
        return _prevalidation_failure(
            error="BATCH_PATH_RESOLUTION_FAILED",
            message=(
                "deterministic batch refused before writing because a target path could not "
                f"be resolved: {detail['type']}: {detail['message']}"
            ),
            underlying=detail,
        )

    if len(commit_last_resolved) != len(set(commit_last_resolved)):
        return _prevalidation_failure(
            error="BATCH_PLAN_CONFLICT",
            message="deterministic batch refused before writing: commit_last contains aliases.",
        )
    unknown_last = [path for path in commit_last_resolved if path not in planned]
    if unknown_last:
        return _prevalidation_failure(
            error="BATCH_PLAN_CONFLICT",
            message=(
                "deterministic batch refused before writing: commit_last names unplanned "
                f"path(s): {', '.join(unknown_last)}."
            ),
        )
    return planned, commit_last_resolved


async def _read_before_states(
    sandbox: Any, planned: dict[str, bytes | None]
) -> dict[str, bytes | None] | ToolOutcome:
    """Read every planned path's current on-disk content before any write."""
    before: dict[str, bytes | None] = {}
    for resolved in sorted(planned):
        try:
            before[resolved] = await sandbox.read_file(resolved)
        except FileNotFoundError:
            before[resolved] = None
        except Exception as exc:  # noqa: BLE001 - unreadable is never absence
            detail = _exception_detail(exc)
            return _prevalidation_failure(
                error="BATCH_PREVALIDATION_READ_FAILED",
                message=(
                    f"deterministic batch refused before writing because {resolved} could not "
                    f"be read: {detail['type']}: {detail['message']}"
                ),
                path=resolved,
                underlying=detail,
            )
    return before


def _check_delete_availability(
    sandbox: Any, planned: dict[str, bytes | None], effective: set[str]
) -> ToolOutcome | None:
    """Refuse up front if the plan includes a deletion this sandbox cannot perform."""
    if any(planned[path] is None for path in effective) and not callable(
        getattr(sandbox, "delete_file", None)
    ):
        return _prevalidation_failure(
            error="BATCH_DELETE_UNAVAILABLE",
            message=(
                "deterministic batch refused before writing because this sandbox cannot delete "
                "a stale generated file. Use a supported sandbox or a fresh workspace."
            ),
        )
    return None


def _order_commits(effective: set[str], commit_last_resolved: list[str]) -> list[str]:
    """Canonical path order, with ``commit_last`` members moved to the end in caller order."""
    last_rank = {path: index for index, path in enumerate(commit_last_resolved)}
    return sorted(
        effective,
        key=lambda path: (1, last_rank[path]) if path in last_rank else (0, path),
    )


async def _attribute_commit_failure(
    ctx: ToolContext,
    sandbox: Any,
    resolved: str,
    intended: bytes | None,
    expected: bytes | None,
    *,
    applied: list[str],
    receipts: list[MutationReceipt | OpaqueEffectReceipt],
) -> Literal["unchanged", "committed", "unknown"]:
    """Re-read a path once to attribute a backend failure's true final state.

    A transport can report failure before or after applying its write primitive.
    Intended bytes prove commit, unchanged bytes prove no commit, and any third
    or unreadable state remains explicitly opaque. Mutates ``applied``/
    ``receipts`` in place exactly as the caller's pre-extraction inline logic did.
    """
    observed_known = True
    try:
        observed: bytes | None = await sandbox.read_file(resolved)
    except FileNotFoundError:
        observed = None
    except Exception:  # noqa: BLE001 - exact state is genuinely unknown
        observed = None
        observed_known = False

    if observed_known and observed == intended:
        receipts.append(_file_mutation_receipt(resolved, before=expected, after=intended))
        applied.append(resolved)
        _clear_grounding(ctx.conversation_id, resolved)
        return "committed"
    if observed_known and observed == expected:
        return "unchanged"
    receipts.append(
        OpaqueEffectReceipt(
            capability=EffectCapability.WORKSPACE_MUTATE,
            reason=(
                f"deterministic batch state for {resolved} could not be attributed "
                "after a backend failure"
            ),
        )
    )
    _clear_grounding(ctx.conversation_id, resolved)
    return "unknown"


async def _commit_ordered(
    ctx: ToolContext,
    sandbox: Any,
    planned: dict[str, bytes | None],
    before: dict[str, bytes | None],
    ordered: list[str],
) -> DeterministicBatchResult | ToolOutcome:
    """Commit each path in ``ordered``, in order, stopping at the first handled failure."""
    applied: list[str] = []
    receipts: list[MutationReceipt | OpaqueEffectReceipt] = []
    for resolved in ordered:
        intended = planned[resolved]
        expected = before[resolved]
        try:
            if intended is None:
                committed = await _commit_file_deletion(
                    ctx,
                    resolved,
                    expected_before=expected,
                )
            else:
                committed = await _commit_file_mutation(
                    ctx,
                    resolved,
                    intended,
                    expected_before=expected,
                )
        except Exception as exc:  # noqa: BLE001 - retain handled partial-state truth
            failed_state = await _attribute_commit_failure(
                ctx,
                sandbox,
                resolved,
                intended,
                expected,
                applied=applied,
                receipts=receipts,
            )
            detail = _exception_detail(exc)
            return _commit_failure(
                applied=applied,
                receipts=receipts,
                failed_path=resolved,
                failed_path_state=failed_state,
                reason=f"{detail['type']}: {detail['message']}",
                underlying=detail,
            )

        if isinstance(committed, ToolOutcome):
            if not applied:
                return committed
            return _commit_failure(
                applied=applied,
                receipts=receipts,
                failed_path=resolved,
                failed_path_state="unchanged",
                reason=f"{committed.error or 'stale file context'}: {committed.content}",
                underlying={
                    "error": committed.error,
                    "content": committed.content,
                    "structured": committed.structured,
                },
            )

        receipts.append(committed)
        applied.append(resolved)
        _clear_grounding(ctx.conversation_id, resolved)

    return DeterministicBatchResult(
        changed_paths=tuple(applied),
        receipts=tuple(receipt for receipt in receipts if isinstance(receipt, MutationReceipt)),
    )


async def commit_deterministic_file_batch(
    ctx: ToolContext,
    intents: Sequence[PlannedFileMutation],
    *,
    commit_last: Sequence[str] = (),
) -> DeterministicBatchResult | ToolOutcome:
    """Commit a fully materialized file plan with exact host-observed evidence.

    All path resolution, conflict checks, reads, deletion-capability checks, and
    no-op comparisons finish before the first mutation.  Ordinary paths commit
    in canonical order, followed by ``commit_last`` in the caller's order.
    """

    assert ctx.sandbox is not None
    sandbox = ctx.sandbox
    if not intents:
        return DeterministicBatchResult(changed_paths=(), receipts=())

    resolved_plan = await _resolve_planned_intents(ctx, intents, commit_last)
    if isinstance(resolved_plan, ToolOutcome):
        return resolved_plan
    planned, commit_last_resolved = resolved_plan

    before = await _read_before_states(sandbox, planned)
    if isinstance(before, ToolOutcome):
        return before

    effective = {path for path, after in planned.items() if before[path] != after}
    if not effective:
        return DeterministicBatchResult(changed_paths=(), receipts=())

    unavailable = _check_delete_availability(sandbox, planned, effective)
    if unavailable is not None:
        return unavailable

    ordered = _order_commits(effective, commit_last_resolved)
    return await _commit_ordered(ctx, sandbox, planned, before, ordered)


__all__ = [
    "DeterministicBatchResult",
    "PlannedFileMutation",
    "commit_deterministic_file_batch",
]
