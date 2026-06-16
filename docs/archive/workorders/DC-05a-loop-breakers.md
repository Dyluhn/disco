# DC-05a — actionless breaker, valve taxonomy, knowledge dedup, driver retry (DEFECT-4 loop half + DEFECT-5)

**Read `README.md` first. De-complexity Wave 0 (docs/decomplexity-wave-plan.md DC-05).
Scope = packages/core ONLY — the resume-path reconstruction half is dc-05b
(packages/agent-server, runs in parallel; do NOT touch agent-server).**

## Why (evidence: test-record/marathon/DEFECTS.md, DEFECT-4 + DEFECT-5)

Phase B attempt 3 (`conv_c1b46756…`): after a server restart + resume, the
agent never called a tool again — 231 knowledge events (ONE snippet ×179),
3 plan revisions, zero actions — and the loop's only brake (the
3-auto-continue ceiling) landed it **FINISHED detail="partial_plan"** with
steps [1,2,3,4] all undone. Attempt 5 reproduced it in a different register
(message/deliverable spam, null payloads). Separately (DEFECT-5), one
`LLMTransientError` during a local-model restart ERRORs the conversation
after ~12 s — no backoff, no PAUSED.

Code as of HEAD (`packages/core/src/disco/core/loop/engine.py`):
- `self._auto_continue_cap = 3` (line ~735); decision branch ~1960-1963
  (`_plan_is_incomplete` + `_auto_continue_attempts`); FINISHED
  detail="partial_plan" landing ~2015-2020; FINISHED detail="noop_limit"
  ~2069 (`_max_consecutive_noops = 6`, line ~725).
- `_plan_is_incomplete(events)` ~1140-1180 returns `(incomplete, missing)`.
- remember-tool KnowledgeEvent emit ~1740-1750 (`KnowledgeEvent(source=…,
  scope=scope, snippet=fact)`); KnowledgeEvent shape in
  `packages/core/src/disco/core/events.py` ~486 (`scope`, `snippet`).
- `except LLMError as e:` ~1708 emits ErrorEvent → terminal ERROR;
  `LLMTransientError` (subclass, `core/llm/errors.py`) dies there too.

## The decided design (locked)

### 1. Actionless-step breaker (DEFECT-4's missing brake)

- New class constant `_ACTIONLESS_BREAK_CAP: int = 3` next to the existing
  noop/auto-continue constants.
- Track consecutive completions whose response has NO tool calls while the
  latest plan is incomplete. 3 in a row → emit one ENVIRONMENT MessageEvent
  diagnostic ("The agent produced 3 consecutive responses without any tool
  call while plan steps remain undone — pausing instead of burning tokens.
  Resume to continue.") then `StatusEvent(status=PAUSED,
  detail="actionless")` and stop the loop. Never auto-continue past it.
- An actual tool call resets the counter. Plan complete → breaker inert
  (the existing noop ceiling handles chatty-after-done).

### 2. Valve taxonomy for partial-plan landings

- The two landings that today produce FINISHED while the plan is incomplete
  — auto-continue-cap (detail="partial_plan") and noop-limit
  (detail="noop_limit") — must instead land
  `StatusEvent(status=PAUSED, detail="partial_plan")` /
  `(PAUSED, detail="noop_limit")` **when zero ActionEvents (excluding
  plan/finish bookkeeping tools) occurred since the last
  `StatusEvent(RUNNING, detail="resumed")` (or since conversation start when
  never resumed)** — plus one ⚠ ENVIRONMENT MessageEvent ("⚠ finishing was
  blocked: plan steps remain undone and no work happened in this run
  segment."). FINISHED must imply work happened.
- Helper `_actions_since_last_resume(events) -> int` (static, mirrors
  `_plan_is_incomplete` style). When >0 actions, behavior is UNCHANGED
  (FINISHED partial_plan stays for genuine partial work).

### 3. Knowledge dedup (the ×179 CSV bloat)

- At the remember-tool emit site: normalize `fact.strip()`, key =
  `(scope, sha256(normalized))`. Walk prior KnowledgeEvents in `events` once
  to build the seen-set (lazy per call is fine — the log is already in
  memory there). Duplicate → do NOT emit a KnowledgeEvent; the tool
  observation content becomes "Already recorded — not stored again." Fresh →
  emit exactly as today.

### 4. DEFECT-5: LLMTransientError retry + PAUSED, never ERROR

- New constants `_DRIVER_RETRY_BACKOFFS_S: tuple = (10.0, 30.0, 90.0)`
  (sized to the observed 60–90 s local-model restart window).
- Catch `LLMTransientError` SEPARATELY before the broad `except LLMError`
  clause at the model-call site: retry the SAME step up to 3 more attempts,
  sleeping the backoff between (use `await asyncio.sleep` via a
  module-level `_sleep = asyncio.sleep` indirection so tests can patch it).
- All retries exhausted → ENVIRONMENT MessageEvent ("model driver
  unavailable — conversation paused, resume when the model is back") +
  `StatusEvent(status=PAUSED, detail="driver-unavailable")`, return state.
  NEVER an ErrorEvent for the transient subclass. Non-transient LLMError
  behavior unchanged.

### 5. Null-payload deliverable guard (attempt-5 oddity)

- Wherever deliverable events are emitted in the loop: skip emission when
  the payload is empty/None (one guard + a debug log line). 12+ null
  deliverables were emitted during the attempt-5 spam loop.

### Anti-scope

- Do NOT touch packages/agent-server (dc-05b owns the resume path), the
  frontend, or condensation/view code. No config knobs — constants only.
- Do not "fix" anything else you notice — file it in the report.

## Acceptance ladder

1. **Unit — `packages/core/tests/test_dc05_loop.py`** (NEW). Follow the
   fixture style of `test_loop_step.py` (fake model scripted responses).
   MUST cover:
   - breaker: 3 scripted no-tool-call completions with an incomplete plan →
     PAUSED detail="actionless", diagnostic message present, loop stopped;
   - breaker reset: 2 no-tool completions then a real action → no break;
   - breaker inert when plan complete;
   - valve: auto-continue ceiling reached with zero actions since last
     resume → PAUSED detail="partial_plan" + ⚠ message; same ceiling WITH
     prior actions in the segment → FINISHED detail="partial_plan"
     (unchanged contract);
   - dedup: scripted remember calls with the same fact ×179 → exactly 1
     KnowledgeEvent in the log, observations after the first say already
     recorded; different scope or different fact → separate events;
   - DEFECT-5: model raises LLMTransientError twice then succeeds (patched
     `_sleep` recording delays) → step completes, delays == (10.0, 30.0);
     model raises persistently → PAUSED detail="driver-unavailable", NO
     ErrorEvent, exactly 4 attempts;
   - deliverable guard: empty payload → no event emitted.
2. Run ONLY `uv run pytest packages/core/tests/test_dc05_loop.py
   packages/core/tests/test_loop_step.py packages/core/tests/test_loop_integration.py
   -x -q` — never other packages' suites. Log → `test-record/dc-05/units-core.log`
   (create the dir).
3. **Report** — `agent-projects/gemini/dc-05a-report.md`: what changed,
   verbatim test output, deviations declared honestly. Do NOT commit; no
   git commands beyond read-only.

## Manifest (orders.yaml `dc-05a` — touch nothing outside it)

- packages/core/src/disco/core/loop/engine.py
- packages/core/tests/test_dc05_loop.py
- test-record/dc-05/units-core.log
- agent-projects/gemini/dc-05a-report.md
