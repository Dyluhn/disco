# Codex CODE Review — P1 / HARN-3 (centralized product promotion policy) — IMPLEMENTED

Inspect harness/build_soak/promotion.py + tests/test_promotion.py.

evaluate_product_promotion(classifications, *, product_harness=None, require_product_harness=False)
→ {eligible, checks:[{check, ok, detail}]} (mirrors bakeoff.evaluate_gate's shape/convention: ok=None is
informational/non-blocking; eligible iff no check is False). Headless gates enforced now: runs present;
all runs PASS; no INVALID_RUN; no UNKNOWN_FAILURE; provider constraint (no PROVIDER_FORBIDDEN/_WRONG_MODEL/
_CALL_AFTER_TERMINAL). Product-harness gate (browser_ws_connected/preview_visible/download_verified/
cleanup_ok/zero_provider_calls_after_terminal) is enforced only when require_product_harness=True (default
False until HARN-1b exists → reports ok=None, not blocking). bakeoff.py (kernel Pi-vs-Disco) is untouched
and separate.

Tests: 11 passed (eligible/all-pass, no-runs, failing-run, INVALID_RUN, UNKNOWN_FAILURE, provider violations,
product-harness not-required/required-absent/green/failed-subgate). basedpyright strict 0 errors.

Judge: (a) is the gate composition correct + free of a false-ELIGIBLE hole (could a non-promotable run slip
through)? (b) is the ok=None non-blocking convention + the require_product_harness default-False posture
right (so HARN-1b's absence doesn't block, but a real soak can require it)? (c) test sufficiency. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
