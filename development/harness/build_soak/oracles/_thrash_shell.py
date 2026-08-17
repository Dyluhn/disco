"""Semantic/background repeat grouping for the thrash oracle.

**Script identity is NOT computed here.** It lives in
`disco.core.script_identity`, the SINGLE owner both this oracle and the loop's
W-39 freshness memo import — the same rule (A6.3 §3.1) that already governs
`disco.core.tool_fingerprint` for the identical-call class.

The rule was applied to only one of the two answered-question classes until
2026-08-06x. The shell parsing and `direct_script_invocations` used to live in
this module, harness-private, so the loop could not compute the class this oracle
counts on: at 2026-08-06v `diag_script_run` seed 97903 re-ran one script under
three different spellings, this oracle grouped all three and faulted the run, and
the memo — keyed on byte-identical calls — never told the agent it already had the
answer. F47. The parsing moved down; the names below are re-exports so frozen
artifacts stay greppable and this module's own call sites are unchanged.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from typing import Any

from disco.core.script_identity import (
    SHELL_TOOLS as _SHELL_TOOLS,
)
from disco.core.script_identity import (
    SOURCE_SUFFIX_FAMILY as _SOURCE_SUFFIX_FAMILY,
)
from disco.core.script_identity import (
    direct_script_invocations as direct_script_invocations,
)
from disco.core.script_identity import (
    shell_segments as shell_segments,
)
from disco.core.script_identity import (
    static_tee_sinks as static_tee_sinks,
)

from ..events import KIND_ACTION, action_id_of, kind_of, seq_of, tool_name_of
from ._thrash_recovery import approved_plan_predicate_scope

_RUNTIME_CLEANUP_TOOLS = frozenset({"shell_kill_process"})
_RUNTIME_CLEANUP_COMMANDS = frozenset({"kill", "killall", "pkill"})
_FILE_MUTATION_TOOLS = frozenset(
    {
        "exact_replace",
        "file_append",
        "file_edit",
        "file_insert_lines",
        "file_replace_lines",
        "file_str_replace",
        "file_write",
        "safe_write_file",
        "write_file",
    }
)


def _is_runtime_cleanup_action(
    action: dict[str, Any], outcomes: dict[str, tuple[bool, str, str]]
) -> bool:
    success, _, _ = outcomes.get(str(action_id_of(action)), (False, "missing outcome", ""))
    if not success:
        return False
    tool = tool_name_of(action)
    if tool in _RUNTIME_CLEANUP_TOOLS:
        return True
    if tool not in _SHELL_TOOLS:
        return False
    args = (action.get("tool_call") or {}).get("arguments") or {}
    command = args.get("command", args.get("cmd"))
    if not isinstance(command, str):
        return False
    return any(
        (executable := posixpath.basename(segment[0]).lower() if segment else "")
        in _RUNTIME_CLEANUP_COMMANDS
        or (executable == "fuser" and "-k" in segment)
        for segment, _terminator in shell_segments(command)
    )


def _explicit_mutation_path(action: dict[str, Any]) -> str | None:
    if tool_name_of(action) not in _FILE_MUTATION_TOOLS:
        return None
    args = (action.get("tool_call") or {}).get("arguments") or {}
    return next(
        (
            posixpath.normpath(raw if raw.startswith("/") else posixpath.join("/workspace", raw))
            for key in ("path", "file_path")
            if isinstance((raw := args.get(key)), str) and raw.strip()
        ),
        None,
    )


def _update_generations(
    action: dict[str, Any],
    outcomes: dict[str, tuple[bool, str, str]],
    generations: Counter[str],
) -> None:
    mutation_path = _explicit_mutation_path(action)
    mutation_succeeded, _, _ = outcomes.get(
        str(action_id_of(action)), (False, "missing outcome", "")
    )
    if mutation_path is None or not mutation_succeeded:
        return
    family = _SOURCE_SUFFIX_FAMILY.get(posixpath.splitext(mutation_path)[1].lower())
    if family is not None:
        generations[family] += 1


def _script_command(action: dict[str, Any]) -> str | None:
    if tool_name_of(action) not in _SHELL_TOOLS:
        return None
    if (action.get("meta") or {}).get("verify_probe"):
        return None
    args = (action.get("tool_call") or {}).get("arguments") or {}
    command = args.get("command", args.get("cmd"))
    return command if isinstance(command, str) else None


def largest_semantic_shell_repeat_group(
    events: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
) -> tuple[int, str, list[int]]:
    """Find repeated successful direct-script executions in one verifier scope.

    UNCHANGED at 2026-08-06z, deliberately and on evidence. The currency
    unification briefly added the shared currency epoch to this group key, on the
    theory that "the cap must not count a re-verification made legitimate by
    intervening workspace change" meant ANY intervening write. Three standing
    guard tests refuted it —
    `test_documentation_write_does_not_hide_repeated_script_verification`,
    `test_failed_semantic_script_rewrite_does_not_hide_repeated_verification`,
    `test_same_predicate_reapproval_cannot_reset_cosmetic_wrapper_thrash` — and
    they are right: a doc write does not legitimize re-running a Python test, and
    a FAILED write changes nothing at all.

    The operative word in the owner's clause is *legitimate*, and the
    legitimating rules already exist and are narrower than "a write happened":
    the family generation below, and the trusted mutation receipt in the shared
    owner. The defect was never that the cap counted too much; it was that the
    ECHO went silent on a cruder rule than the one the cap counts by. So the echo
    moved onto this predicate and this predicate did not move.
    """
    generations: Counter[str] = Counter()
    plan_scope = ("<no-approved-plan>",)
    groups: dict[tuple[str, int, tuple[str, ...]], list[int]] = {}
    for action in events:
        if (approved_scope := approved_plan_predicate_scope(action)) is not None:
            plan_scope = approved_scope
            continue
        if kind_of(action) != KIND_ACTION:
            continue
        _update_generations(action, outcomes, generations)
        command = _script_command(action)
        success, _, _ = outcomes.get(str(action_id_of(action)), (False, "missing outcome", ""))
        if command is None or not success:
            continue
        for fingerprint, family, background in sorted(set(direct_script_invocations(command))):
            if not background:
                groups.setdefault((fingerprint, generations[family], plan_scope), []).append(
                    seq_of(action)
                )
    if not groups:
        return 0, "", []
    (fingerprint, _generation, _plan_scope), seqs = max(
        groups.items(), key=lambda item: (len(item[1]), item[1])
    )
    return len(seqs), fingerprint, seqs


def largest_background_script_restart_group(
    actions: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
) -> tuple[int, str, list[int], list[int], int]:
    """Find repeated background script starts with one bounded recovery credit."""
    generations: Counter[str] = Counter()
    groups: dict[tuple[str, int], list[int]] = {}
    cleanup_seqs: list[int] = []
    for action in actions:
        _update_generations(action, outcomes, generations)
        if _is_runtime_cleanup_action(action, outcomes):
            cleanup_seqs.append(seq_of(action))
        command = _script_command(action)
        if command is None:
            continue
        for fingerprint, family, background in sorted(set(direct_script_invocations(command))):
            if background:
                groups.setdefault((fingerprint, generations[family]), []).append(seq_of(action))
    if not groups:
        return 0, "", [], [], 0
    (fingerprint, _generation), start_seqs = max(
        groups.items(), key=lambda item: (len(item[1]), item[1])
    )
    qualifying_cleanup = [seq for seq in cleanup_seqs if start_seqs[0] < seq <= start_seqs[-1]]
    cleanup_credit = 1 if qualifying_cleanup else 0
    return len(start_seqs), fingerprint, start_seqs, qualifying_cleanup, cleanup_credit
