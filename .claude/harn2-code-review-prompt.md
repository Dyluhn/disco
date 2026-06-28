# Codex CODE Review — P1 / HARN-2 (8 browser product-harness oracles) — IMPLEMENTED

Inspect:
- harness/build_soak/oracles/browser_evidence.py (8 oracles: BrowserWS, Lifecycle, SidecarStop,
  PreviewOwnership, ShowToUser, VerificationGate, ExportDownload, Cleanup + BROWSER_EVIDENCE_ORACLES tuple)
- harness/build_soak/failure_codes.py (8 new P0 codes)
- harness/build_soak/classify.py (one product_evidence param → the family as step 8; each SKIPs without
  its evidence slice)
- harness/build_soak/oracles/__init__.py (exports)
- harness/build_soak/tests/test_browser_evidence_oracles.py

Design: these judge the REAL product path over the HARN-1b browser-evidence dossier (a single
product_evidence dict, documented in the module). Each oracle reads its own slice and SKIPs when absent, so
a HEADLESS run (product_evidence is None) is completely unaffected — proven by
test_classify_unaffected_without_product_evidence. When a product-harness run supplies the evidence, each
enforces one concrete gate (WS connected / clean terminal / sidecar stopped / platform-owned preview /
shown-to-user / verification-gate-through / export-download-delivered / no-orphans). All 8 codes are P0.
They were grouped into ONE module (cohesive family over the same evidence) rather than 8 one-line files.

Tests: 65 passed (per-oracle skip/pass/violation + classify integration showing headless-unaffected,
green-passes, ws-not-connected→FAIL P0). basedpyright strict 0 errors.

Judge: (a) is each oracle's pass/fail logic correct + free of a false-PASS hole (could a real violation
read as green)? (b) is the SKIP-without-slice posture right so headless runs are unaffected while a product
run enforces? (c) is grouping the 8 into one module acceptable vs the repo's one-per-file convention?
(d) test sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
