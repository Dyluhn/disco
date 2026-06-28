# Codex CODE Review — P1 / HARN-1a (provider-call ledger + ProviderLedgerOracle) — IMPLEMENTED

P1's first PR: the MiniMax-only / no-OpenRouter / zero-calls-after-terminal enforcement (the campaign's
HARD soak constraint), as a zero-opinion oracle over a provider-call-ledger evidence stream. Inspect:
NEW:
- harness/build_soak/provider_ledger.py (ledger record contract + parse_relay_log/_lines — tolerant
  relay-log → records; JSONL or loose URL+model lines; hostless lines skipped)
- harness/build_soak/oracles/provider_ledger.py (ProviderLedgerOracle: OPT-IN per scenario
  assertions.provider; SKIP when required-but-no-ledger; FAIL on forbidden host (default openrouter) /
  non-required host / wrong model / after_terminal call)
- harness/build_soak/tests/test_provider_ledger_oracle.py (14 tests: oracle pos/neg/skip + parser + e2e classify)
CHANGED:
- harness/build_soak/failure_codes.py (PROVIDER_FORBIDDEN / PROVIDER_WRONG_MODEL /
  PROVIDER_CALL_AFTER_TERMINAL, all P0)
- harness/build_soak/classify.py (provider_ledger param + ProviderLedgerOracle as step 7;
  classify_run_folder reads provider-call-ledger.jsonl if present)
- harness/build_soak/oracles/__init__.py (export)

Design intent: enforcement is OPT-IN (only when scenario declares assertions.provider) so dev scenarios
aren't failed for other providers; absent ledger when required → SKIP (absent evidence is not a verdict),
the P17 final soak will require the ledger present. Live POPULATION of the ledger (writing
provider-call-ledger.jsonl from the relay log during a run) is the remaining wiring, done at soak setup;
the READ + enforce pipeline is complete + tested now.

Tests: 14 passed (incl classify() FAIL=PROVIDER_FORBIDDEN P0 on an openrouter ledger entry; PASS on
minimax; SKIP without ledger). basedpyright strict 0 errors.

Judge: (a) is the oracle's enforcement logic correct + free of false-pass holes (could a violation slip
through)? (b) is OPT-IN + SKIP-on-absent-ledger the right severity posture, or should required-but-absent be
a FAIL/INVALID_RUN? (c) is the tolerant relay-log parser sound (no silent host miss that would under-report
an openrouter call)? (d) test sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS +
REQUIRED_REVISIONS.
