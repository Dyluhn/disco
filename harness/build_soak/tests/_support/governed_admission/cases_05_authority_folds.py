"""Moved authority folds collection implementations."""

from __future__ import annotations

from ._shared import GOVERNED_ADMISSION_BYPASSED
from .helpers_01 import (
    _insert_mutation_before_terminal,
    _result,
    _segment,
    _shift_verdict_and_insert_mutation,
)


def _impl_test_run_reclaim_between_receipt_and_verdict_is_not_an_authority_change() -> None:
    """Pilot seed 405512: a re-plan/approve cycle re-claims the SAME registered
    run between the verification receipt and the finish verdict. Run-admission
    bookkeeping (`agent.run-claimed`) moves no workspace bytes — its emitter
    verifies the registered run intent is unchanged — so the exact receipt must
    stay valid."""
    events = _segment()
    _shift_verdict_and_insert_mutation(events, "agent.run-claimed")

    result = _result(events)

    assert result.passed


def _impl_test_real_mutation_between_receipt_and_verdict_still_fails_closed() -> None:
    """Control: an operation that can move workspace bytes in the same window
    still invalidates the receipt — the bookkeeping exclusion is exact."""
    events = _segment()
    _shift_verdict_and_insert_mutation(events, "agent.run-intent.host-mutation")

    result = _result(events)

    assert result.failed
    assert result.code == GOVERNED_ADMISSION_BYPASSED


def _impl_test_terminal_manifest_fold_after_verdict_is_not_an_authority_change() -> None:
    """Epic-4 seed 460000: under DISCO_ARTIFACT_MANIFEST_SHADOW=1 the terminal
    pipeline records `agent.artifact-manifest-fold` after the PASS verdict and
    before FINISHED. It writes only `.disco/context/artifact_manifest.json`
    under the terminal event's own view and cannot change the verified entry —
    the product excludes it from its own staleness fence, and so must this
    oracle. The exemption previously matched the un-namespaced
    `"artifact-manifest-fold"`, which the emitter never writes, so every
    governed run that EARNED a PASS receipt false-FAILed at
    `verifier_receipt -> terminal`."""
    events = _segment()
    _insert_mutation_before_terminal(events, "agent.artifact-manifest-fold")

    result = _result(events)

    assert result.passed


def _impl_test_real_mutation_after_verdict_still_fails_closed() -> None:
    """Control: a byte-moving operation in that same post-verdict window still
    invalidates the receipt — verification must describe the delivered bytes."""
    events = _segment()
    _insert_mutation_before_terminal(events, "agent.run-intent.host-mutation")

    result = _result(events)

    assert result.failed
    assert result.code == GOVERNED_ADMISSION_BYPASSED
