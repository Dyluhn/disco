"""A preview-generation replacement is progress; an idempotent restart is not.

Counted-promotion failure 2026-07-27 (`p4_ff_node_pause` seed 400025): two
browser failures separated by a `preview_stop`, a REPLACEMENT `preview_start`
(`pv_ad5f…` → `pv_c1b9…`), a successful `verify_web_app`, and a curl proving the
new service were grouped into one progress epoch and adjudicated
TOOL_ERROR_THRASH. Replacing the serving authority is real progress.

The opposite error would be worse: if any `preview_start` opened a new epoch, an
agent could launder an unlimited repeat by re-calling it. Both directions are
pinned here.
"""

from __future__ import annotations

from typing import Any

from harness.build_soak.oracles.thrash import (
    ProgressEpochs,
    preview_generation_of,
    progress_epoch_boundary,
)

_GEN_A = "pv_ad5f8a1d45de4faeae04fd507e10ceab"
_GEN_B = "pv_c1b9fd26756346458397ce15068e7b62"


def _preview_receipt(generation: str, *, success: bool = True) -> dict[str, Any]:
    """The shape a host-authored preview projection actually persists."""
    return {
        "seq": 1,
        "kind": "observation",
        "action_id": "evt_preview",
        "tool_result": {
            "success": success,
            "structured": {
                "name": "node-server",
                "port": 8000,
                "status": "running",
                "url": "http://127.0.0.1:44889",
                "in_sandbox_url": "http://localhost:8000/",
                "generation": generation,
                "projection_id": generation,
            },
        },
    }


def test_a_replacement_generation_opens_a_new_epoch():
    epochs = ProgressEpochs()
    assert epochs.crosses(_preview_receipt(_GEN_A)) is False  # establishes
    assert epochs.crosses(_preview_receipt(_GEN_B)) is True  # replaces


def test_an_idempotent_restart_cannot_launder_a_repeat():
    # Same generation returned again: the authority did not change, so nothing
    # about the run's progress changed either.
    epochs = ProgressEpochs()
    assert epochs.crosses(_preview_receipt(_GEN_A)) is False
    for _ in range(10):
        assert epochs.crosses(_preview_receipt(_GEN_A)) is False


def test_the_first_generation_establishes_authority_and_is_not_a_boundary():
    # Nothing was replaced, so there is no progress to credit.
    assert ProgressEpochs().crosses(_preview_receipt(_GEN_A)) is False


def test_a_failed_preview_receipt_establishes_nothing():
    epochs = ProgressEpochs()
    assert epochs.crosses(_preview_receipt(_GEN_A, success=False)) is False
    # …so the next SUCCESSFUL one is still only establishing, not replacing.
    assert epochs.crosses(_preview_receipt(_GEN_B)) is False


def test_prose_mentioning_a_generation_cannot_forge_a_boundary():
    forged = {
        "seq": 2,
        "kind": "message",
        "source": "environment",
        "message": {"role": "user", "content": f"preview generation is now {_GEN_B}"},
    }
    epochs = ProgressEpochs()
    epochs.crosses(_preview_receipt(_GEN_A))
    assert epochs.crosses(forged) is False


def test_a_receipt_whose_projection_id_disagrees_is_not_trusted():
    bad = _preview_receipt(_GEN_A)
    bad["tool_result"]["structured"]["projection_id"] = _GEN_B
    assert preview_generation_of(bad) is None


def test_a_malformed_generation_token_is_rejected():
    for token in ("pv_short", "ad5f8a1d45de4faeae04fd507e10ceab", "pv_" + "Z" * 32, ""):
        bad = _preview_receipt(_GEN_A)
        bad["tool_result"]["structured"]["generation"] = token
        bad["tool_result"]["structured"]["projection_id"] = token
        assert preview_generation_of(bad) is None, token


def test_the_stateless_boundaries_still_apply_through_the_tracker():
    # The tracker must not shadow the existing typed boundaries.
    user_turn = {"seq": 3, "kind": "message", "source": "user", "message": {"role": "user"}}
    assert progress_epoch_boundary(user_turn) is True
    assert ProgressEpochs().crosses(user_turn) is True


def test_a_genuine_adjacent_repeat_with_no_authority_change_still_has_one_epoch():
    epochs = ProgressEpochs()
    noise = {"seq": 4, "kind": "message", "source": "environment", "message": {"role": "user"}}
    epochs.crosses(_preview_receipt(_GEN_A))
    crossings = sum(1 for _ in range(5) if epochs.crosses(noise))
    assert crossings == 0
