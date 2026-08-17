"""The §17 no-fluke replay policy (pure function).

A failed run that PASSES on exact replay is NOT a pass — it is an intermittent
failure: status stays FAIL, code becomes INTERMITTENT_<original_code> with the
SAME severity. A replay that fails the same way is a deterministic failure
(unchanged). The word "fluke" never yields PASS.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc


def intermittent_classification(
    original: dict[str, Any],
    *,
    replay_status: str,
    replay_code: str | None = None,
    replay_run_id: str | None = None,
) -> dict[str, Any]:
    """Apply the no-fluke replay policy (§17) to an originally-FAILED run.

    Returns a NEW classification dict; the input is not mutated."""
    if original.get("status") != fc.FAIL:
        raise ValueError("no-fluke policy only applies to an originally-FAILED run")
    original_code = str(original.get("code"))
    updated = dict(original)

    if replay_status == fc.PASS:
        intermittent_code = fc.INTERMITTENT_PREFIX + original_code
        updated["status"] = fc.FAIL
        updated["code"] = intermittent_code
        updated["severity"] = fc.severity_for(intermittent_code)
        updated["replay"] = {
            "attempted": True,
            "result": "passed",
            "run_id": replay_run_id,
        }
        return updated

    if replay_status == fc.FAIL and replay_code == original_code:
        updated["replay"] = {"attempted": True, "result": "same_failure", "run_id": replay_run_id}
        return updated

    if replay_status == fc.FAIL:
        updated["replay"] = {
            "attempted": True,
            "result": "different_failure",
            "run_id": replay_run_id,
        }
        return updated

    # INVALID_RUN / INFRA_FAILURE on replay: cannot prove a pass; keep the failure
    # and record the inconclusive replay.
    updated["replay"] = {"attempted": True, "result": "not_attempted", "run_id": replay_run_id}
    return updated
