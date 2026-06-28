# Codex CODE Review — P0B Lifecycle/Safety (LIFE-1..5) — IMPLEMENTED

Context: P0B's safety plumbing was found LARGELY ALREADY PRESENT in this clone (Disco-Pi build-kernel
branch). Verdict to review: LIFE-1/2/4/5 are pre-satisfied; LIFE-3 (auto-suspend active-work guard) was the
real gap and is now implemented.

Inspect:
- packages/agent-server/src/disco/agent_server/lifecycle.py: NEW `_has_active_work(conversation_id)` (True
  iff a live not-done run task in self._rt._tasks OR a live Pi sidecar session in self._rt._pi_kernel._sessions);
  called in BOTH `_suspend` (after the RUNNING check) and `sweep_idle_once` (after the connections check).
- packages/agent-server/tests/test_lifecycle.py: 3 new tests (live run task blocks suspend; live Pi sidecar
  blocks suspend; a DONE task does NOT block).

Pre-satisfied (verified by running their suites — all green):
- LIFE-1 sidecar kill + token revoke: pi_kernel._conclude → _revoke_pi_tokens + proc.aclose; gateway 401s
  revoked tokens. (test_pi_inference_revoke_lifecycle.py passes)
- LIFE-2 WS truth: runtime._connections ledger + on_connect/on_disconnect + canonical agentWsUrl builder.
- LIFE-4 eager teardown + snapshot: _maybe_snapshot then _teardown_sandbox + orphan sweep. (test_lifecycle.py)
- LIFE-5 gateway clamp: 3-layer max_tokens clamp + budget reserve. (test_pi_inference_gateway.py passes)

Judge: (a) Is the LIFE-3 active-work signal (live run task OR live Pi sidecar session) correct + sufficient
for "disconnect/idle must never kill in-flight work", or is there a missed active-work signal that would let
a suspend destroy live work? (b) Is excluding a sandbox-internal preview server from the guard correct (it's
restored from snapshot on resume)? (c) Is the pre-satisfied verdict for LIFE-1/2/4/5 sound, or is any of them
actually NOT fully covered? (d) test sufficiency.

Tests: 89 passed (LIFE-1/3/4/5). basedpyright strict 0 errors on lifecycle.py. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
