"""`RunProjectScriptTool.run`'s commit phase.

Guards are complete by the time this runs, but the portable sandbox contract
has no multi-file transaction. Preserve exact proven commits and identify an
ambiguous failed path on every handled backend error instead of claiming a
rollback the backend cannot guarantee.
"""

from __future__ import annotations

from typing import Any, Literal

from disco.core.effects import EffectCapability, MutationReceipt, OpaqueEffectReceipt

from ...anatomy import ToolContext, ToolOutcome
from ..files import _clear_grounding, _commit_file_mutation, _file_mutation_receipt
from .outcomes import _commit_failure, _exception_detail, _success


async def _attribute_commit_failure(
    ctx: ToolContext,
    sbx: Any,
    canon: str,
    intended: bytes,
    before: bytes | None,
    *,
    applied: list[str],
    receipts: list[MutationReceipt | OpaqueEffectReceipt],
) -> Literal["unchanged", "committed", "unknown"]:
    """Re-read once to attribute a backend failure's true final state.

    A transport can report failure before or after applying its write
    primitive. Intended bytes prove commit, unchanged bytes prove no commit,
    and any third or unreadable state remains explicitly opaque. Mutates
    `applied`/`receipts` in place exactly as the pre-extraction inline logic
    did.
    """
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
        return "committed"
    if observed_known and observed == before:
        return "unchanged"
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
    return "unknown"


async def _commit_run_script_files(
    ctx: ToolContext,
    sbx: Any,
    after_bytes: dict[str, bytes],
    original_bytes: dict[str, bytes | None],
    mutated: set[str],
    operations_run: int,
    reads_out: dict[str, Any],
) -> ToolOutcome:
    """Commit each path in `mutated` (canonical order), stopping at the first
    handled failure; return the success/failure `ToolOutcome` either way."""
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
            failed_path_state = await _attribute_commit_failure(
                ctx,
                sbx,
                canon,
                intended,
                before,
                applied=applied,
                receipts=receipts,
            )
            detail = _exception_detail(exc)
            return _commit_failure(
                applied=applied,
                receipts=receipts,
                failed_path=canon,
                failed_path_state=failed_path_state,
                reason=f"{detail['type']}: {detail['message']}",
                operations_run=operations_run,
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
                operations_run=operations_run,
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
        operations_run,
        reads_out,
        tuple(receipt for receipt in receipts if isinstance(receipt, MutationReceipt)),
    )
