# Codex CODE Review — P1 / HARN-1b (evidence side: validated product-evidence writer) — IMPLEMENTED

Inspect harness/build_soak/product_evidence.py + tests/test_product_evidence_writer.py.

This is the verified PRODUCER side of the HARN-2 oracle contract (the oracles READ product_evidence +
provider-call-ledger; this WRITES them, validated, so the harness can't emit evidence an oracle would
mis-adjudicate). Pure; does NOT drive a browser (that's HARN-1b's live Playwright spec, which needs a live
stack and is intentionally not written here — writing an unrunnable spec would be a stub).

- validate_product_evidence(ev) → list[str] problems: known slices must be dicts; declared fields when
  present must be well-typed; bool-where-int rejected (a bool count is a capture bug); unknown top-level keys
  allowed (forward-compat).
- write_product_evidence(folder, ev, strict=True): validates BEFORE writing; strict raises ValueError on a
  schema problem and writes NOTHING.
- write_provider_ledger(folder, records): one JSON record per line.
- Tests (10): validation clean/bool/wrong-type/non-dict/unknown-key; strict-refuses-malformed +
  non-strict-persists; and the LOOP — writer → classify_run_folder → oracle verdict (green PASS,
  preview-owner violation FAIL, provider openrouter FAIL).

Tests: 105 passed across the full HARN suite (writer + 8 browser oracles + provider + promotion + classifier
+ output_truth). basedpyright strict 0 errors. (Note: test_bakeoff.py has a PRE-EXISTING import-path quirk
`from build_soak.bakeoff` unrelated to this PR.)

Judge: (a) is the validate→write-strict contract correct + does it actually prevent malformed evidence from
reaching the oracles? (b) is bool-where-int rejection + unknown-key tolerance the right strictness? (c) test
sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
