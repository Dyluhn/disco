"""HARN-3 tests: the centralized product promotion policy."""

from __future__ import annotations

from harness.build_soak.promotion import evaluate_product_promotion


def _pass(**extra):
    return {"status": "PASS", "code": None, **extra}


def _fail(code="ARTIFACT_TRUTH_MISMATCH"):
    return {"status": "FAIL", "code": code}


def _check(result, name):
    return next(c for c in result["checks"] if c["check"] == name)


def test_all_pass_is_eligible():
    # Scoped to the run-classification gates, which is what this test is about. The
    # product-harness gate is opted out EXPLICITLY because it defaults to required as of
    # A4 step 4; without the opt-out this would assert "eligible" while silently
    # depending on that gate staying informational. The gate's own behaviour is pinned by
    # `test_product_harness_is_required_by_default` and the three tests below it.
    r = evaluate_product_promotion([_pass(), _pass()], require_product_harness=False)
    assert r["eligible"] is True


def test_no_runs_is_not_eligible():
    r = evaluate_product_promotion([])
    assert r["eligible"] is False
    assert _check(r, "runs present")["ok"] is False


def test_a_failing_run_blocks():
    r = evaluate_product_promotion([_pass(), _fail()])
    assert r["eligible"] is False
    assert _check(r, "all runs PASS")["ok"] is False


def test_invalid_run_blocks():
    r = evaluate_product_promotion(
        [{"status": "INVALID_RUN", "code": "WORKSPACE_SNAPSHOT_NOT_READY"}]
    )
    assert r["eligible"] is False
    assert _check(r, "no INVALID_RUN")["ok"] is False


def test_unknown_failure_blocks():
    r = evaluate_product_promotion([_fail(code="UNKNOWN_FAILURE")])
    assert r["eligible"] is False
    assert _check(r, "no UNKNOWN_FAILURE")["ok"] is False


def test_provider_violation_blocks():
    r = evaluate_product_promotion([_fail(code="PROVIDER_FORBIDDEN")])
    assert r["eligible"] is False
    pc = _check(r, "provider constraint (MiniMax-only / no-OpenRouter / no calls after terminal)")
    assert pc["ok"] is False and "PROVIDER_FORBIDDEN" in pc["detail"]


def test_provider_call_after_terminal_blocks():
    r = evaluate_product_promotion([_fail(code="PROVIDER_CALL_AFTER_TERMINAL")])
    assert r["eligible"] is False


# --- product-harness gate -----------------------------------------------------
def test_product_harness_not_required_is_informational():
    # The opt-out is now EXPLICIT. Before A4 step 4 this called
    # `evaluate_product_promotion([_pass()])` bare and relied on the default being
    # False; the default is True as of the flip, so the bare call is covered by
    # `test_product_harness_is_required_by_default` below instead. The informational
    # branch itself is unchanged and still reachable — that is what this pins.
    r = evaluate_product_promotion([_pass()], require_product_harness=False)
    ph = _check(r, "product harness green")
    assert ph["ok"] is None  # not blocking
    assert r["eligible"] is True


def test_product_harness_is_required_by_default():
    """A4 step 4: `require_product_harness` defaults to True.

    Pinned as its own test because the DEFAULT is the thing A4 authorized changing, and
    a default is exactly the kind of behaviour that regresses silently — every other
    test in this file passes the flag explicitly and would stay green if it flipped back.
    """
    r = evaluate_product_promotion([_pass()])
    assert _check(r, "product harness green")["ok"] is False
    assert r["eligible"] is False


def test_product_harness_required_but_absent_blocks():
    r = evaluate_product_promotion([_pass()], require_product_harness=True)
    assert r["eligible"] is False
    assert _check(r, "product harness green")["ok"] is False


def test_product_harness_required_and_green_passes():
    ph = {
        "browser_ws_connected": True,
        "preview_visible": True,
        "download_verified": True,
        "cleanup_ok": True,
        "zero_provider_calls_after_terminal": True,
    }
    r = evaluate_product_promotion([_pass()], product_harness=ph, require_product_harness=True)
    assert r["eligible"] is True


def test_product_harness_required_with_failed_subgate_blocks():
    ph = {
        "browser_ws_connected": True,
        "preview_visible": False,  # the build never showed the user a preview
        "download_verified": True,
        "cleanup_ok": True,
        "zero_provider_calls_after_terminal": True,
    }
    r = evaluate_product_promotion([_pass()], product_harness=ph, require_product_harness=True)
    assert r["eligible"] is False
    assert "preview_visible" in _check(r, "product harness green")["detail"]
