"""Compatibility exports for the thrash oracle's bounded helper owners."""

from ._thrash_recovery import (
    approved_plan_predicate_scope,
    recovered_blocked_marker,
    trusted_mutation_receipt_outcome,
)
from ._thrash_shell import (
    direct_script_invocations,
    largest_background_script_restart_group,
    largest_semantic_shell_repeat_group,
    shell_segments,
    static_tee_sinks,
)

__all__ = [
    "approved_plan_predicate_scope",
    "direct_script_invocations",
    "largest_background_script_restart_group",
    "largest_semantic_shell_repeat_group",
    "recovered_blocked_marker",
    "shell_segments",
    "static_tee_sinks",
    "trusted_mutation_receipt_outcome",
]
