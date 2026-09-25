# Preserve review capacity after report repair

Owner: PKG-24-RESEARCH-REVIEW-RESERVE. This is an incremental transition after
the 68b99f81e Preview authority baseline, not a new full UI acceptance claim.

The audited report spent two initial review decisions and then borrowed its two
reserved decisions on the same draft. It subsequently changed the prose, leaving
no capacity to review that changed draft. Existing tests explicitly permitted
that sequence. The application now releases the configured final reserve only
after an actual repair. Established findings can trigger the existing single
repair; without actionable findings, incomplete review ends honestly instead of
retrying the unchanged draft in an empty final-check loop. An exhausted legacy
checkpoint keeps its draft and findings rather than creating new unreviewable
prose. Limits, durable charges, legacy defaults, and exact-draft invalidation
remain intact; there is no budget reset or model/provider-specific behavior.

The following existing test identities intentionally receive stronger policy
expectations, with no renames or removals:

- `test_writer_recovery.py::test_unused_repair_reserve_resolves_conflicting_verdict_without_new_budget`: repair precedes the reserved changed-draft review within the same three decisions.
- `test_writer_recovery.py::test_stop_before_released_reserve_preserves_feedback_and_one_remaining_call`: JSON resume retains the remaining decision for repaired prose.
- `test_writer_recovery.py::test_exhausted_conflict_feedback_keeps_one_scoped_repair_and_no_false_pass[False]` and `[True]`: a changed-draft review occurs before the existing budget is exhausted; uncertain assessment remains incomplete.
- `test_writer_recovery.py::test_unresolved_initial_verdict_is_not_labelled_as_checking_revisions`: no findings means truthful unavailability after the initial allocation, without an unused-reserve retry loop.
- `test_writer_draft.py::test_a_missing_model_verdict_is_recorded_on_the_report`: preserves missing-review disclosure after the initial allocation.

Six added cases in `packages/retrieval/tests/test_review_reserve.py` cover Stop
and serialized resume before repair, after changed prose, after a reserved but
undispatched final call, and legacy pre-repair/exhausted checkpoints. They verify
the new draft hash, final-review input, original total, single repair, conservative
charges and zero replay after completion. All 257 scoped writer/review cases
passed, with no skipped cases, under a 1 GiB process limit.

No public API declarations change. Current strict replay, integrated source
checks, and a fresh real-browser report remain distinct validation work. These
tests do not establish factual quality or acceptable end-to-end latency, and do
not change the source-reader allocation policy.
