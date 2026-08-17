"""`FindAndEditTool.run`'s decision-application phase.

Turns accepted LLM decisions into grouped `ExactReplaceEdit`s, commits them
per file through `ExactReplaceTool`, and summarizes the resulting match
statuses. A provider-truncated decision anywhere in a file keeps that whole
file byte-identical instead of applying a silently partial edit set; a
regex match whose literal text recurs elsewhere with a different accepted
replacement (or a different total occurrence count than what was decided) is
skipped as ambiguous rather than guessed at.
"""

from __future__ import annotations

from typing import Any

from ...anatomy import ToolContext
from ..files import ExactReplaceArgs, ExactReplaceEdit, ExactReplaceTool, _all_occurrences
from .scan import _Match, _ReadFile


def _mark_skipped(match_result: dict[str, Any], reason: str) -> None:
    match_result["status"] = "skipped"
    match_result["reason"] = reason
    match_result.pop("replacement", None)


def _prepare_exact_edits(
    file: _ReadFile,
    decisions: dict[int, dict[str, Any]],
) -> tuple[list[ExactReplaceEdit], bool]:
    matches_by_old: dict[str, list[_Match]] = {}
    edits_by_old: dict[str, list[_Match]] = {}
    for match in file.matches:
        matches_by_old.setdefault(match.text, []).append(match)
        decision = decisions.get(
            match.global_index,
            {"status": "skipped", "reason": "missing_decision"},
        )
        rec = file.result["matches"][match.file_index]
        rec.update(decision)
        if decision.get("status") == "accepted":
            edits_by_old.setdefault(match.text, []).append(match)

    # A provider-truncated decision makes the requested edit set incomplete. Keep
    # the whole file byte-identical instead of applying a silently partial subset.
    if any(match_result.get("status") == "failed" for match_result in file.result["matches"]):
        for match_result in file.result["matches"]:
            if match_result.get("status") == "accepted":
                _mark_skipped(match_result, "file_decision_failed")
        return [], False

    edits: list[ExactReplaceEdit] = []
    multi = False
    for old_string, accepted_matches in edits_by_old.items():
        replacements = {str(decisions[m.global_index].get("replacement")) for m in accepted_matches}
        occurrences = len(_all_occurrences(file.text, old_string))
        all_regex_matches = matches_by_old.get(old_string, [])
        if (
            len(replacements) != 1
            or occurrences != len(all_regex_matches)
            or len(accepted_matches) != len(all_regex_matches)
        ):
            for match in accepted_matches:
                _mark_skipped(
                    file.result["matches"][match.file_index],
                    "duplicate_match_ambiguous",
                )
            continue
        replacement = next(iter(replacements))
        edits.append(ExactReplaceEdit(old_string=old_string, new_string=replacement))
        if occurrences > 1:
            multi = True
    return edits, multi


async def _apply_file_edits(
    file: _ReadFile,
    ctx: ToolContext,
    decisions: dict[int, dict[str, Any]],
) -> int:
    edits, multi = _prepare_exact_edits(file, decisions)
    if not edits:
        if any(match.get("status") == "failed" for match in file.result["matches"]):
            file.result["status"] = "failed"
            file.result["reason"] = "decision_failed"
        else:
            file.result["status"] = "unchanged"
        return 0

    exact_args = ExactReplaceArgs(
        path=file.path,
        edits=edits,
        expected_sha256=file.sha256,
        multi=multi,
    )
    outcome = await ExactReplaceTool().run(exact_args, ctx)
    accepted_results = [m for m in file.result["matches"] if m.get("status") == "accepted"]
    if not outcome.success:
        reason = f"exact_replace_failed:{outcome.error or 'unknown'}"
        file.result["status"] = "skipped"
        file.result["apply_error"] = outcome.error or outcome.content
        for match_result in accepted_results:
            _mark_skipped(match_result, reason)
        return 0

    edited = 0
    for match_result in accepted_results:
        match_result["status"] = "edited"
        edited += 1
    file.result["status"] = "edited" if edited else "unchanged"
    file.result["applied_edits"] = len(edits)
    return edited


async def apply_all_edits(
    files: list[_ReadFile],
    ctx: ToolContext,
    decisions: dict[int, dict[str, Any]],
) -> tuple[int, list[str]]:
    """Apply accepted decisions file-by-file.

    Returns `(edited_count, changed_paths)`, in the same order as `files`.
    """
    edited = 0
    artifacts: list[str] = []
    for file in files:
        file_edited = await _apply_file_edits(file, ctx, decisions)
        edited += file_edited
        if file_edited:
            artifacts.append(file.path)
    return edited, artifacts


def summarize_match_results(file_results: list[dict[str, Any]]) -> tuple[int, int]:
    """Count `skipped`/`failed` matches across every scanned file's result record."""
    skipped = sum(
        1
        for file in file_results
        for match in file.get("matches", [])
        if match.get("status") == "skipped"
    )
    failed = sum(
        1
        for file in file_results
        for match in file.get("matches", [])
        if match.get("status") == "failed"
    )
    return skipped, failed
