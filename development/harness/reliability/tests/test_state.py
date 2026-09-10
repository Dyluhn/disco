from __future__ import annotations

import multiprocessing as mp
import subprocess
from pathlib import Path

import pytest

from harness.reliability.matrix import load_matrix
from harness.reliability.state import (
    FAIL,
    INFRA,
    INVALID,
    PASS,
    empty_state,
    load_state,
    product_subject_identity,
    promotion_report,
    record_campaign,
    source_revision,
    state_transaction,
)

MATRIX = Path(__file__).parents[1] / "matrix.yaml"


def _result(
    status: str,
    units: int,
    *,
    device: str | None = None,
    trial_ids: list[str] | None = None,
) -> dict:
    result = {
        "suite_id": "live-build-shapes",
        "kind": "generic",
        "status": status,
        "reason": "test",
        "claims": ["build.api_shapes_live"],
        "units_passed": units,
    }
    if device:
        result["fresh_device_id"] = device
    if trial_ids is not None:
        result["kind"] = "build_soak"
        result["trial_ids"] = trial_ids
    return result


def _record(
    state: dict,
    campaign: str,
    revision: str,
    result: dict,
    *,
    evaluator: str | None = None,
) -> None:
    record_campaign(
        state,
        campaign_id=campaign,
        revision=revision,
        commit="abc",
        dirty=False,
        started_at="start",
        finished_at="finish",
        results=[result],
        product_subject_identity=revision,
        evaluator_identity=evaluator or revision,
    )


def _record_in_process(path: str, index: int) -> None:
    with state_transaction(path) as state:
        _record(state, f"parallel-{index}", "parallel-revision", _result(PASS, 1))


def test_passes_accumulate_only_on_same_untainted_revision() -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    _record(state, "c1", "r1", _result(PASS, 100))
    _record(state, "c2", "r1", _result(PASS, 200))
    report = promotion_report(state, matrix, revision="r1", claim_ids={"build.api_shapes_live"})
    assert report["eligible"] is True
    assert report["claims"][0]["observed"] == 300


def test_one_product_failure_taints_prior_and_later_passes_on_revision() -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    _record(state, "c1", "r1", _result(PASS, 299))
    _record(state, "c2", "r1", _result(FAIL, 0))
    _record(state, "c3", "r1", _result(PASS, 300))
    report = promotion_report(state, matrix, revision="r1", claim_ids={"build.api_shapes_live"})
    assert report["tainted"] is True
    assert report["eligible"] is False
    assert report["claims"][0]["observed"] == 0


def test_infrastructure_failure_does_not_taint_but_never_counts() -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    _record(state, "c1", "r1", _result(PASS, 100))
    _record(state, "c2", "r1", _result(INFRA, 0))
    report = promotion_report(state, matrix, revision="r1", claim_ids={"build.api_shapes_live"})
    assert report["tainted"] is False
    assert report["claims"][0]["observed"] == 100


@pytest.mark.parametrize("status", [INFRA, INVALID])
def test_nonpass_fresh_device_fingerprint_never_counts(status: str) -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    for index in range(10):
        result = _result(status, 0, device=f"{index:024x}")
        result.update(
            kind="fresh_device",
            claims=["fresh.install_boot_upgrade_uninstall"],
        )
        _record(state, f"nonpass-{index}", "subject", result)

    report = promotion_report(
        state,
        matrix,
        revision="subject",
        claim_ids={"fresh.install_boot_upgrade_uninstall"},
    )

    assert report["tainted"] is False
    assert report["eligible"] is False
    assert report["claims"][0]["observed"] == 0

    passed = _result(PASS, 1, device=f"{0:024x}")
    passed.update(
        kind="fresh_device",
        claims=["fresh.install_boot_upgrade_uninstall"],
    )
    _record(state, "pass", "subject", passed)
    report = promotion_report(
        state,
        matrix,
        revision="subject",
        claim_ids={"fresh.install_boot_upgrade_uninstall"},
    )
    assert report["claims"][0]["observed"] == 1


def test_invalid_cohort_preserves_its_completed_pass_cells() -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    trials = [f"build-soak-seed:{seed}" for seed in range(53)]
    _record(state, "c1", "subject", _result(INVALID, 53, trial_ids=trials), evaluator="v19")

    report = promotion_report(
        state, matrix, revision="subject", claim_ids={"build.api_shapes_live"}
    )

    assert report["tainted"] is False
    assert report["claims"][0]["observed"] == 53


def test_evaluator_change_preserves_credit_but_product_failure_taints_it() -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    _record(state, "c1", "subject", _result(PASS, 100), evaluator="evaluator-a")
    _record(state, "c2", "subject", _result(PASS, 100), evaluator="evaluator-b")
    report = promotion_report(
        state, matrix, revision="subject", claim_ids={"build.api_shapes_live"}
    )
    assert report["claims"][0]["observed"] == 200

    _record(state, "c3", "subject", _result(FAIL, 0), evaluator="evaluator-c")
    report = promotion_report(
        state, matrix, revision="subject", claim_ids={"build.api_shapes_live"}
    )
    assert report["tainted"] is True
    assert report["claims"][0]["observed"] == 0


def test_duplicate_build_soak_trial_identity_is_rejected() -> None:
    state = empty_state()
    result = _result(PASS, 1, trial_ids=["build-soak-seed:92419"])
    _record(state, "c1", "subject", result, evaluator="evaluator-a")

    with pytest.raises(ValueError, match="already credited"):
        _record(state, "c2", "subject", result, evaluator="evaluator-b")


def test_suite_result_cannot_omit_its_kind() -> None:
    state = empty_state()
    result = _result(PASS, 1)
    result.pop("kind")

    with pytest.raises(ValueError, match="unsupported kind"):
        _record(state, "c1", "subject", result)


def test_build_soak_units_require_exact_trial_ids() -> None:
    state = empty_state()
    result = _result(PASS, 1)
    result["kind"] = "build_soak"

    with pytest.raises(ValueError, match="requires exact trial_ids"):
        _record(state, "c1", "subject", result)


def test_new_revision_starts_after_fix_streak_from_zero() -> None:
    matrix = load_matrix(MATRIX)
    state = empty_state()
    _record(state, "c1", "r1", _result(FAIL, 0))
    _record(state, "c2", "r2", _result(PASS, 100))
    report = promotion_report(state, matrix, revision="r2", claim_ids={"build.api_shapes_live"})
    assert report["tainted"] is False
    assert report["claims"][0]["observed"] == 100


def test_parallel_campaign_processes_do_not_lose_ledger_updates(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    context = mp.get_context("fork")
    processes = [
        context.Process(target=_record_in_process, args=(str(state_path), index))
        for index in range(12)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    state = load_state(state_path)
    assert len(state["campaign_ids"]) == 12
    assert len(state["revisions"]["parallel-revision"]["campaigns"]) == 12


def test_nested_untracked_file_bytes_change_source_revision(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "reliability@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Reliability Test"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    nested = tmp_path / "new-package" / "matrix.yaml"
    nested.parent.mkdir()
    nested.write_text("target: 1\n", encoding="utf-8")
    first, _, dirty = source_revision(tmp_path)
    nested.write_text("target: 300\n", encoding="utf-8")
    second, _, _ = source_revision(tmp_path)

    assert dirty is True
    assert first != second


def test_product_subject_identity_ignores_evaluator_bytes_only(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    product = tmp_path / "packages" / "core" / "src" / "disco" / "runtime.py"
    harness = tmp_path / "development" / "harness" / "reliability" / "oracle.py"
    product.parent.mkdir(parents=True)
    harness.parent.mkdir(parents=True)
    product.write_text("VALUE = 1\n", encoding="utf-8")
    harness.write_text("RULE = 1\n", encoding="utf-8")
    first = product_subject_identity(tmp_path, bindings={"model": "deepseek"})

    harness.write_text("RULE = 2\n", encoding="utf-8")
    assert product_subject_identity(tmp_path, bindings={"model": "deepseek"}) == first

    product.write_text("VALUE = 2\n", encoding="utf-8")
    assert product_subject_identity(tmp_path, bindings={"model": "deepseek"}) != first
    assert product_subject_identity(tmp_path, bindings={"model": "other"}) != first
