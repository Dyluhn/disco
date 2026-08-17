"""The single owner of receipt CURRENCY ("is the answer the agent already holds
still current?").

Third sibling of `tool_fingerprint.py` (is this the same *call*?) and
`script_identity.py` (is this the same *script*?). Those two own the IDENTITY
half of the answered-question question. This module owns the TIME half, and it
exists for the same binding reason (A6.3 §3.1) applied to the same defect family
one dimension over.

The GROUNDED FEEDBACK owner record (2026-08-06 ~21:35 CDT), constraint 2:

    **One currency predicate, two consumers.** The feedback echo ("this check
    already succeeded and is still current") and the repeat-cap share a SINGLE
    staleness predicate derived from the ledger's own receipt-currency rules. The
    echo fires only while the prior result is still current; the cap must not
    count a re-verification made legitimate by intervening workspace change.

Before this module the two consumers used DIFFERENT windows:

* the loop's W-39 freshness boundary reset on ANY workspace-mutating ACTION, on a
  user message, and on a blocking environment message;
* the oracle's epoch reset on an approved plan-predicate scope change, a TRUSTED
  mutation receipt, a preview-generation change, a user message, and a blocking
  environment message.

The gap that mattered ran ONE way. A bare workspace-mutating action reset the
echo but not the cap, so the memo fell silent — judging the prior answer stale —
while the cap kept counting the repeat as culpable. **That is the F47 shape:
counted, but never told.** And in the other direction the oracle's three
boundaries were invisible to the echo, so the memo could assert "nothing has
changed since" across an epoch the grader had already closed — a false assurance,
the one failure mode W-39's own docstring promises never to have.

The union is the predicate: every kind either side recognised, plus one
correction each side needed. The echo gains the three boundaries only the oracle
had, so it can no longer claim currency across an epoch the grader has closed.
The exact-repeat cap gains the workspace mutation only the echo had, so it can no
longer count a repeat the product declined to warn about — which is the owner's
"re-verification made legitimate by intervening workspace change", and the F47
shape itself. And both now decline to treat a mutation PROVEN to have failed as a
boundary, a rule the oracle already held
(`test_failed_semantic_script_rewrite_does_not_hide_repeated_verification`) and
the echo did not: a write that failed changed nothing, so silencing the memo on
it took the agent's answer away and gave nothing back. Note the direction — a
mutation of UNKNOWN outcome still ends currency. Unknown is not proof of a no-op,
and reading it as one is the false assurance W-39 exists to avoid.

**What is deliberately NOT in scope.** The FAILED-CALL check
(`check_tool_error_thrash`) keeps the narrower `PROGRESS_BOUNDARY_KINDS`: no
currency question arises for a call that failed, because a failed prior
occurrence IS its own notice (the F47 general invariant), and
`test_receiptless_success_is_not_a_progress_boundary` pins the consequence — a
hollow edit must not launder a repeated tool error.

The SEMANTIC-SHELL class likewise keeps its own
narrower currency rule — the per-language-family generation counter in
`_thrash_shell.py` — and does not consult this predicate. An attempt to unify it
here was refuted by three standing guard tests
(`test_documentation_write_does_not_hide_repeated_script_verification`,
`test_failed_semantic_script_rewrite_does_not_hide_repeated_verification`,
`test_same_predicate_reapproval_cannot_reset_cosmetic_wrapper_thrash`) and they
are right: a documentation write must not launder a repeated script verification.
That class groups three DIFFERENT spellings of one script, so its notion of
relevance is genuinely finer-grained than "the workspace changed", and forcing it
onto this predicate would have weakened a check that was already correct.

With ONE predicate the exact-repeat pair cannot disagree by construction. Notice
and culpability share a window, so "the cap counted a repeat the product never
warned about, on the currency dimension" becomes unreachable rather than merely
absent.

Import-free by design (stdlib only), for the same reason `tool_fingerprint` and
`script_identity` are: the harness depends on it to adjudicate frozen evidence
without pulling product machinery into that adjudication.

**Shape of the inputs.** The predicates read the SERIALIZED event shape — plain
mappings with `kind` / `seq` / `source` / `detail` / `tool_call` / `tool_result` /
`meta` / `action_id` / `plan_verification_transition` keys. That is exactly what
the harness already holds, so the harness passes its events through unchanged and
its behaviour is preserved name-for-name. The loop projects its typed events into
the same shape with a small adapter (`loop/dedup.py::_currency_view`); it does not
re-implement any rule here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# --- the canonical boundary vocabulary -------------------------------------
# Every kind of event that ends the currency of an earlier successful result.
# This tuple IS the contract between the two consumers: a kind added here must
# be honoured by both, and `test_receipt_currency_shared.py` fails if either
# consumer's coverage drifts from it.

BOUNDARY_USER_MESSAGE = "user_message"
BOUNDARY_BLOCKING_ENVIRONMENT = "blocking_environment"
BOUNDARY_APPROVED_PLAN_SCOPE = "approved_plan_scope"
BOUNDARY_TRUSTED_MUTATION_RECEIPT = "trusted_mutation_receipt"
BOUNDARY_PREVIEW_GENERATION = "preview_generation"
BOUNDARY_WORKSPACE_MUTATION = "workspace_mutation"

CURRENCY_BOUNDARY_KINDS: frozenset[str] = frozenset(
    {
        BOUNDARY_USER_MESSAGE,
        BOUNDARY_BLOCKING_ENVIRONMENT,
        BOUNDARY_APPROVED_PLAN_SCOPE,
        BOUNDARY_TRUSTED_MUTATION_RECEIPT,
        BOUNDARY_PREVIEW_GENERATION,
        BOUNDARY_WORKSPACE_MUTATION,
    }
)

# The mutating tools whose SUCCESSFUL call ends currency — either as a bare
# landed write (`workspace_mutation_ends_currency`) or through an exact typed
# receipt (`trusted_mutation_receipt_outcome`). What is NOT a boundary is a write
# that was merely ATTEMPTED: it changed nothing, so it must neither silence the
# echo nor reset the cap.
#
# This is the harness's receipt set, the wider of the two that existed. The loop's
# narrower A8 list left four spellings of "the workspace changed" invisible to the
# echo, and currency must fail toward stale.
#
# NOT the same list as `loop/dedup.py::_WORKSPACE_MUTATING_TOOLS`, which is the
# A8 snapshot working set and answers a different question (which files does the
# agent's deliverable consist of). That list is untouched by this module.
# AppKit publishes an exact batch receipt after it lands changes.  Keep these
# receipt-owned tools out of the action-only set below: an empty successful
# AppKit call is a proven no-op and must not launder a later repeated check.
_APPKIT_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "app_add_primitive",
        "app_add_section",
        "app_create",
        "app_set_design",
        "app_update_content",
    }
)

WORKSPACE_MUTATION_TOOLS: frozenset[str] = frozenset(
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

_RECEIPT_APPLIED_TOOLS: frozenset[str] = frozenset({"run_project_script"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PREVIEW_GENERATION_RE = re.compile(r"^pv_[0-9a-f]{32}$")

_KIND_ACTION = "action"
_KIND_OBSERVATION = "observation"
_KIND_STATUS = "status"
_KIND_MESSAGE = "message"


def _kind_of(event: Mapping[str, Any]) -> str:
    return str(event.get("kind"))


def _seq_of(event: Mapping[str, Any]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else -1


# --- the individual boundary rules -----------------------------------------
# Bodies MOVED here verbatim from `development/harness/build_soak/oracles/_thrash_recovery.py`
# and `oracles/thrash.py` (2026-08-06z). The harness re-exports them rather than
# keeping a second copy, exactly as `_thrash_shell.py` re-exports
# `script_identity` — object identity is asserted by the shared test.


def _applied_paths_are_exact(structured: Mapping[str, Any]) -> bool:
    applied = structured.get("applied")
    return (
        isinstance(applied, list)
        and bool(applied)
        and all(isinstance(item, str) and bool(item) for item in applied)
    )


def _file_receipt_is_exact(structured: Mapping[str, Any]) -> bool:
    path = structured.get("path")
    sha256 = structured.get("sha256")
    return (
        isinstance(path, str)
        and bool(path.strip())
        and isinstance(sha256, str)
        and _SHA256_RE.fullmatch(sha256) is not None
    )


def _appkit_receipt_is_exact(structured: Mapping[str, Any]) -> bool:
    files_written = structured.get("files_written")
    return (
        isinstance(files_written, list)
        and bool(files_written)
        and all(isinstance(path, str) and bool(path.strip()) for path in files_written)
    )


def trusted_mutation_receipt_outcome(
    event: Mapping[str, Any], *, action_ids: frozenset[str] | None = None
) -> bool:
    """Whether one observation event carries a trusted changed-state receipt."""
    if _kind_of(event) != _KIND_OBSERVATION:
        return False
    action_id = event.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return False
    if action_ids is not None and action_id not in action_ids:
        return False
    result = event.get("tool_result")
    if not isinstance(result, Mapping) or result.get("success") is not True:
        return False
    structured = result.get("structured")
    if not isinstance(structured, Mapping):
        return False
    if structured.get("state_changed") is True:
        return True
    tool_name = result.get("tool_name")
    if tool_name in _RECEIPT_APPLIED_TOOLS:
        return _applied_paths_are_exact(structured)
    if tool_name in _APPKIT_MUTATION_TOOLS:
        return _appkit_receipt_is_exact(structured)
    if tool_name in WORKSPACE_MUTATION_TOOLS:
        return _file_receipt_is_exact(structured)
    return False


def _valid_plan_transition(transition: Mapping[str, Any]) -> bool:
    revision = transition.get("new_plan_revision")
    plan_event_id = transition.get("new_plan_event_id")
    return all(
        (
            transition.get("new_authority") == "plan",
            transition.get("reason") in {"approved_initial_plan", "approved_plan_revision"},
            isinstance(revision, int),
            not isinstance(revision, bool),
            isinstance(revision, int) and revision >= 1,
            isinstance(plan_event_id, str),
            isinstance(plan_event_id, str) and bool(plan_event_id),
        )
    )


def _predicate_fingerprints(transition: Mapping[str, Any]) -> tuple[str, ...] | None:
    fingerprints = transition.get("new_predicate_fingerprints")
    if not isinstance(fingerprints, list):
        return None
    if any(not isinstance(item, str) or not item for item in fingerprints):
        return None
    return tuple(sorted(fingerprints))


def approved_plan_predicate_scope(
    event: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Return one trusted approved plan-verifier authority scope."""
    if (
        _kind_of(event) != _KIND_STATUS
        or event.get("source") != "system"
        or event.get("detail") != "plan_approved"
    ):
        return None
    transition = event.get("plan_verification_transition")
    if not isinstance(transition, Mapping) or not _valid_plan_transition(transition):
        return None
    return _predicate_fingerprints(transition)


def preview_generation_of(event: Mapping[str, Any]) -> str | None:
    """The preview generation a successful, typed preview receipt establishes."""
    if _kind_of(event) != _KIND_OBSERVATION:
        return None
    result = event.get("tool_result") or {}
    if not isinstance(result, Mapping) or result.get("success") is not True:
        return None
    structured = result.get("structured")
    if not isinstance(structured, Mapping):
        return None
    generation = structured.get("generation")
    if not isinstance(generation, str) or _PREVIEW_GENERATION_RE.fullmatch(generation) is None:
        return None
    if structured.get("projection_id") != generation:
        return None
    return generation


def workspace_mutation_ends_currency(
    event: Mapping[str, Any], *, failed_action_ids: frozenset[str] | None = None
) -> bool:
    """Whether one ACTION event is a workspace mutation that ends currency.

    The loop has always treated a workspace mutation as ending currency — A6.3
    §6.2's named positive control, `test_no_memo_across_workspace_mutation`,
    exists to keep it that way: the echo must never assure the agent that nothing
    changed immediately after it changed something. The oracle's answered-question
    caps did not have this boundary, and that asymmetry is the F47 shape: the memo
    fell silent while `_longest_identical_streak` kept counting.

    One clarification comes with the unification, and its DIRECTION is the whole
    point. A write that is PROVEN not to have landed changed nothing, so it must
    not end currency — the oracle already held that rule
    (`test_failed_semantic_script_rewrite_does_not_hide_repeated_verification`),
    and without it a failed write silences the memo, taking the agent's answer
    away and giving nothing back.

    But "not proven to have landed" is not the same as "proven not to have
    landed", and the difference is a false assurance. An action with no
    observation yet has an UNKNOWN outcome; the loop's own
    `test_script_class_respects_the_freshness_boundary` pins that case, and it is
    right to. So the predicate is stated as a REFUSAL rather than a requirement:
    a workspace mutation ends currency UNLESS its failure is on the record.
    Unknown falls to the safe side — the memo goes quiet — because W-39's
    contract is that its failure mode is silence, never a claim that nothing
    changed when something might have.

    Deliberately NOT filtered by file type. The first attempt at 2026-08-06z
    restricted this to "source" files via
    `script_identity.SOURCE_SUFFIX_FAMILY`, reasoning that a documentation write
    should not legitimize re-verification. That map answers "which interpreter
    runs this script" (`.py`, `.js`, `.sh`, …) and does not contain `.jsx`,
    `.css` or `.html` — the files a web build actually edits — so the filter
    silenced the memo's own control while claiming to be more precise. The two
    oracle tests that motivated it grade the SEMANTIC-SHELL class, which keeps its
    own narrower per-language generation counter and never consults this
    predicate. There was no conflict to resolve.

    `failed_action_ids` is a parameter rather than an inference because neither
    consumer can decide it from the action event alone, and both already hold the
    outcome map that answers it. It must contain ONLY positively-recorded
    failures; an action absent from it is unknown, not successful.
    """
    if _kind_of(event) != _KIND_ACTION:
        return False
    call = event.get("tool_call")
    if not isinstance(call, Mapping):
        return False
    if call.get("tool_name") not in WORKSPACE_MUTATION_TOOLS:
        return False
    action_id = event.get("id")
    if not isinstance(action_id, str) or not action_id:
        return True
    return failed_action_ids is None or action_id not in failed_action_ids


# --- the fold ---------------------------------------------------------------


# The PROGRESS question is not the CURRENCY question, and the difference is one
# member — asserted here, not assumed, because collapsing them breaks a standing
# guarantee in each direction.
#
# * CURRENCY — "is the successful answer the agent already holds still good?"
#   Consumers: the W-39 echo and the exact-repeat cap. A workspace mutation ends
#   it: the earlier answer described a workspace that no longer exists.
#
# * PROGRESS — "has this run moved forward since that FAILED call?" Consumer:
#   `check_tool_error_thrash`, which skips successes outright (`if success:
#   continue`). Here a bare workspace mutation must NOT count:
#   `test_receiptless_success_is_not_a_progress_boundary` pins it, and the F47
#   general invariant explains it — *broken-tool repetition is culpable
#   immediately*, because a failed prior occurrence IS its own notice. No
#   currency question arises, so no notice is owed, and widening the epoch would
#   let a hollow edit launder a repeated tool error.
PROGRESS_BOUNDARY_KINDS: frozenset[str] = CURRENCY_BOUNDARY_KINDS - {BOUNDARY_WORKSPACE_MUTATION}


class CurrencyBoundaries:
    """Stateful classifier over an event stream, in order.

    Stateful only because the preview-generation boundary is a CHANGE of value,
    which cannot be decided from one event alone. Every other kind is a pure
    function of the event; the class exists so both consumers get the same
    stateful handling for free rather than each tracking generation themselves.

    ``include_workspace_mutation=False`` yields the PROGRESS boundary set (see
    above) — the same rules, one member fewer, for the failed-call consumer.
    """

    def __init__(self, *, include_workspace_mutation: bool = True) -> None:
        self._preview_generation: str | None = None
        self._include_workspace_mutation = include_workspace_mutation

    @property
    def kinds(self) -> frozenset[str]:
        """The boundary kinds this instance can report. Lets a consumer prove its
        coverage rather than asserting it in a comment."""
        return (
            CURRENCY_BOUNDARY_KINDS if self._include_workspace_mutation else PROGRESS_BOUNDARY_KINDS
        )

    def crossing(
        self,
        event: Mapping[str, Any],
        *,
        action_ids: frozenset[str] | None = None,
        failed_action_ids: frozenset[str] | None = None,
    ) -> str | None:
        """The boundary kind this event crosses, or None. Order is significant
        only for the generation tracker, which must observe every receipt."""
        generation = preview_generation_of(event)
        if generation is not None:
            previous, self._preview_generation = self._preview_generation, generation
            if previous is not None and previous != generation:
                return BOUNDARY_PREVIEW_GENERATION
        if approved_plan_predicate_scope(event) is not None:
            return BOUNDARY_APPROVED_PLAN_SCOPE
        if trusted_mutation_receipt_outcome(event, action_ids=action_ids):
            return BOUNDARY_TRUSTED_MUTATION_RECEIPT
        if self._include_workspace_mutation and workspace_mutation_ends_currency(
            event, failed_action_ids=failed_action_ids
        ):
            return BOUNDARY_WORKSPACE_MUTATION
        if _kind_of(event) != _KIND_MESSAGE:
            return None
        if event.get("source") == "user":
            return BOUNDARY_USER_MESSAGE
        meta = event.get("meta")
        blocking = meta.get("blocking") if isinstance(meta, Mapping) else None
        if event.get("source") == "environment" and isinstance(blocking, str) and bool(blocking):
            return BOUNDARY_BLOCKING_ENVIRONMENT
        return None


def latest_currency_boundary_seq(
    events: Iterable[Mapping[str, Any]],
    *,
    before_seq: int | None = None,
    action_ids: frozenset[str] | None = None,
    failed_action_ids: frozenset[str] | None = None,
) -> int:
    """Seq of the most recent currency boundary, or 0 if there has been none.

    `before_seq` bounds the scan to events STRICTLY BEFORE it, so a caller can
    ask "was the world stable up to the call I am about to make?" without the
    call itself perturbing the answer.
    """
    boundaries = CurrencyBoundaries()
    latest = 0
    for event in events:
        seq = _seq_of(event)
        seq = seq if seq > 0 else 0
        # The generation tracker must observe every receipt to detect a CHANGE,
        # so classify first and discard the out-of-range result afterwards.
        kind = boundaries.crossing(
            event, action_ids=action_ids, failed_action_ids=failed_action_ids
        )
        if before_seq is not None and seq >= before_seq:
            continue
        if kind is not None:
            latest = max(latest, seq)
    return latest


def prior_result_is_current(
    events: Iterable[Mapping[str, Any]],
    *,
    prior_seq: int,
    before_seq: int | None = None,
    action_ids: frozenset[str] | None = None,
    failed_action_ids: frozenset[str] | None = None,
) -> bool:
    """Whether a successful result observed at `prior_seq` is STILL current.

    The one question both consumers ask. The echo asks it before handing an
    answer back; the cap asks it before counting a repeat as culpable. They now
    get the same answer for the same run, which is the whole point.
    """
    return (
        latest_currency_boundary_seq(
            events,
            before_seq=before_seq,
            action_ids=action_ids,
            failed_action_ids=failed_action_ids,
        )
        < prior_seq
    )
