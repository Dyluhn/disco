# Disco Build Soak Guidelines

**Recommended main-branch path:** `docs/build-soak-guidelines.md`
**Purpose:** make the bare Build loop boringly reliable before AppKit or other higher-level Build primitives are promoted.
**Status:** normative test and repair policy for Build loop hardening.
**Primary owner:** Build Soak harness.
**Patch agent:** Claude Code.
**Independent RCA/review agent:** Codex, read-only by default.
**Adjudicator:** deterministic evidence oracle only.

---

## 1. Why this exists

AppKit, Cloudflare export, visual editing, and any product-grade Build primitive all depend on one lower-level contract:

```text
submit → plan → approve → execute → produce output → revise → re-plan → execute → verify output
```

The bare Build loop must survive repeated revisions, wrong tool attempts, stale plans, runtime reconnects, preview checks, and UI/API transport paths before higher-level AppKit work can be trusted.

This document defines the main-branch gate that proves that loop. It is intentionally factual. Claude Code, Codex, ChatGPT, and humans may explain or patch failures, but they do not decide whether the loop passed.

---

## 2. Core rule

```text
Agents may explain.
Agents may patch.
Agents may hypothesize.
Agents may not adjudicate.
```

A run is accepted or rejected only by deterministic checks over durable evidence:

```text
scenario.yaml
+ exact seed
+ repo commit
+ event log
+ state snapshots
+ websocket/network/UI logs
+ tool observations
+ workspace/artifact/preview truth
+ oracle predicates
= PASS / FAIL / INVALID_RUN / INFRA_FAILURE
```

Banned adjudication language:

```text
probably fine
likely a fluke
seems stable
model hiccup
Claude thinks fixed
Codex thinks fixed
```

The word `fluke` has no operational meaning. A failed run that later passes on replay is an intermittent failure, not a pass.

---

## 3. What "bulletproof" means

The Build loop may be promoted only when all of this is true:

```text
1. API bare Build smoke: 100/100 consecutive PASS.
2. UI bare Build smoke: 50/50 consecutive PASS.
3. Revision scenarios: 25/25 consecutive PASS.
4. Complex revision scenarios: 10/10 consecutive PASS with at least 3 follow-up turns each.
5. Tool-rejection simulator: 100% PASS.
6. No P0/P1 failure in the final full soak run.
7. No UNKNOWN_FAILURE in the final full soak run.
8. No INVALID_RUN in the final full soak run.
9. Every historical P0/P1 failure has:
   - frozen evidence folder,
   - deterministic classification,
   - RCA,
   - patch,
   - exact-seed replay,
   - regression test.
10. AppKit live harness is run only after this bare Build gate is green.
```

This is a promotion gate, not a vibes check.

---

## 4. Repository layout

Add the harness under:

```text
harness/build_soak/
  scenarios.yaml
  run.py
  replay.py
  classify.py
  repair_loop.py
  report.py
  evidence.py
  failure_codes.py
  infra_signatures.yaml
  intermittent.py
  patch_gate.py
  oracles/
    __init__.py
    schema.py
    harness_validity.py
    contract.py
    event_chain.py
    tool_scope.py
    revision.py
    output_truth.py
    ui_truth.py
    transport.py
    runtime.py
  prompts/
    claude_fix.md.j2
    codex_rca.md.j2
    merge_review.md.j2
  adapters/
    claude_code.py
    codex.py
    disco_api.py
    playwright_ui.py
    git.py
  fixtures/
    fake_model.py
    fake_tools.py
  tests/
    test_classifier.py
    test_replay.py
    test_patch_gate.py
    test_oracle_schema.py
    test_harness_validity_oracle.py
    test_event_chain_oracle.py
    test_revision_oracle.py
    test_output_truth_oracle.py
    test_no_fluke_policy.py

frontend/e2e-live/
  build-soak.spec.ts
  support/buildSoakHarness.ts

scripts/
  build_soak_once.sh
  build_soak_loop.sh
  build_soak_repair.sh
  build_soak_report.sh

.github/workflows/
  build-soak-experimental.yml
```

The harness may be used in experimental branches, but this guideline belongs in main because it defines the baseline Build reliability contract.

---

## 5. Required evidence folder

Every run writes a complete dossier:

```text
test-record/build-soak/<run-id>/
  manifest.json
  timeline.md
  classification.json
  failure.md                  # only if failed
  prompt.txt
  followups.json
  ui-actions.jsonl            # UI mode only
  network.jsonl               # UI mode only
  websockets.jsonl            # UI mode only
  console.jsonl               # UI mode only
  page-errors.jsonl           # UI mode only
  screenshots/                # UI mode only
  conversations/<cid>/
    events.jsonl
    state.initial.json
    state.final.json
    inspect-trace.json
    workspace-manifest.json
    sessions.json
    artifacts/
    preview/
      health.json
      screenshot.png
  logs/
    agent-server.log
    app-server.log
    frontend.log
  repair/
    codex_rca.md
    claude_fix_prompt.md
    claude_patch.diff
    targeted_tests.txt
    rerun_result.json
```

`timeline.md` must answer:

```text
What did the user ask?
Was a user event appended?
Was a plan requested?
Was a plan emitted?
Was it approved?
Did execution start?
What tools were exposed?
What tools were called?
Were any tools rejected?
Did the agent recover?
Was a follow-up sent?
Did a revised plan appear?
Did any write happen before revised approval?
What output/artifact/preview resulted?
Where did the first broken link occur?
```

---

## 6. Evidence lock

Every run folder must include immutable `manifest.json` metadata and SHA256 hashes of evidence files.

Required shape:

```json
{
  "run_id": "build_soak_2026_06_23_001",
  "scenario_id": "revise_after_finish",
  "scenario_sha256": "...",
  "seed": 12345,
  "repo_commit": "...",
  "repo_dirty": false,
  "model": "...",
  "provider": "...",
  "assist": true,
  "autonomous": false,
  "surface": "build",
  "mode": "api | ui | fake_model",
  "started_at": "...",
  "finished_at": "...",
  "evidence_files": {
    "events": "conversations/conv_x/events.jsonl",
    "state_initial": "conversations/conv_x/state.initial.json",
    "state_final": "conversations/conv_x/state.final.json",
    "trace": "conversations/conv_x/inspect-trace.json",
    "workspace_manifest": "conversations/conv_x/workspace-manifest.json"
  },
  "evidence_hashes": {
    "events.jsonl": "sha256:...",
    "state.final.json": "sha256:...",
    "workspace-manifest.json": "sha256:..."
  }
}
```

The repair loop may copy a frozen run folder, but may not mutate it. If evidence changes after classification, the run becomes `INVALID_RUN`.

---

## 7. Truth hierarchy

When evidence conflicts, use this hierarchy:

```text
1. Scenario contract
2. Event log
3. Final reconstructed state
4. Tool observations
5. Workspace / artifact / preview truth
6. HTTP/WebSocket/browser logs
7. Runtime/debug trace
8. Server logs
9. Claude/Codex written analysis
```

Claude/Codex analysis is useful only for repair. It cannot override any higher layer.

Example:

```text
Claude says: "The build probably worked."
Oracle says: "FALSE_FINISH_PREVIEW_BROKEN because preview health returned 404."
Result: FAIL.
```

---

## 8. Allowed run outcomes

Every run ends in exactly one state:

```text
PASS
FAIL
INVALID_RUN
INFRA_FAILURE
```

### PASS

The scenario completed and all required oracle predicates passed.

### FAIL

The product, harness, model behavior, generated output, UI, or runtime violated the scenario contract.

If the model failed to follow the product contract, the product failed to constrain, recover, or surface the issue. This is not an infra failure.

### INVALID_RUN

The harness did not collect enough evidence to adjudicate.

Examples:

```text
missing events.jsonl
missing state.final.json
missing manifest.json
corrupt classification.json
test aborted before scenario actually began
```

Invalid runs do not count as passes. They block promotion until the harness is fixed.

### INFRA_FAILURE

A predeclared external infrastructure condition prevented the test from running.

This category is narrow and must include machine-readable evidence.

Allowed examples:

```text
model provider returned documented 5xx before conversation creation
browser process failed to launch before UI loaded
port allocation failed before app-server startup
network DNS failure to the model provider before conversation creation
```

Not allowed:

```text
agent never planned
agent used wrong tool
WS frame was dropped after the run started
preview 404ed
no action after approval
model stopped early
output missing
```

Those are product failures unless the oracle proves otherwise.

---

## 9. Infra failure gate

`INFRA_FAILURE` must satisfy all of these:

```text
1. Failure happened before conversation creation OR before first product action became testable.
2. Failure matches a known infra signature.
3. Evidence includes provider/browser/process error code.
4. Retry policy is predeclared.
5. It does not hide any product-level incomplete event chain.
```

Known signatures live in:

```text
harness/build_soak/infra_signatures.yaml
```

Example:

```yaml
- id: provider_5xx_before_conversation
  stage: before_conversation_creation
  match:
    http_status: [500, 502, 503, 504]
    component: model_provider
  max_retries: 2
```

If an infra signature fires repeatedly, it becomes a release blocker because the test environment is not reliable enough to prove anything.

---

## 10. Objective oracle layers

The classifier is deterministic. It must not call an LLM.

Required oracle layers:

```text
HarnessValidityOracle
ContractOracle
EventChainOracle
ToolScopeOracle
RevisionOracle
OutputTruthOracle
UITruthOracle
TransportOracle
RuntimeOracle
```

Each oracle emits structured facts, not prose.

Example:

```json
{
  "oracle": "RevisionOracle",
  "status": "FAIL",
  "code": "NO_REPLAN_AFTER_REVISION",
  "facts": {
    "followup_user_event_seq": 42,
    "latest_plan_revision_before_followup": 1,
    "latest_plan_revision_after_followup": 1,
    "first_write_tool_after_followup_seq": 44,
    "write_before_revised_plan_approval": true
  }
}
```

---

## 11. Required invariants

### 11.1 First-turn planning invariant

For Build scenarios requiring the plan gate:

```text
First meaningful agent action must be submit_plan, ask_user, clarify, think, or safe read.
No write/shell/browser mutation before plan approval.
```

Failure codes:

```text
NO_PLAN_AFTER_USER_TURN
WRITE_TOOL_ALLOWED_IN_PLANNING
WRITE_TOOL_ATTEMPTED_IN_PLANNING
WRONG_TOOL_REJECTION_ABORTS_LOOP
```

### 11.2 Plan approval invariant

After plan approval:

```text
approval event exists
mode/state leaves awaiting approval
execution starts exactly once
at least one action or terminal explicit failure appears
```

Failure codes:

```text
APPROVE_PLAN_NO_EXECUTION
DUPLICATE_EXECUTION_AFTER_APPROVAL
PLAN_APPROVED_STATUS_MISSING
```

### 11.3 Action/observation invariant

For every tool action requiring observation:

```text
ActionEvent(action_id=X) -> ObservationEvent(action_id=X)
```

Failure codes:

```text
ACTION_NO_OBSERVATION
OBSERVATION_WITHOUT_ACTION
TOOL_REJECTION_NOT_VISIBLE_TO_MODEL
```

### 11.4 Revision invariant

For follow-up turns after a completed or approved plan:

```text
new user event exists
new plan revision exists OR explicit direct-patch workflow is recorded
no write action occurs before revised plan approval unless direct-patch workflow was explicitly allowed
```

Failure codes:

```text
NO_REPLAN_AFTER_REVISION
STALE_PLAN_USED_AFTER_FOLLOWUP
PLAN_REVISION_NOT_INCREMENTED
WRITE_BEFORE_REVISION_APPROVAL
```

### 11.5 Finish/output invariant

If state says finished:

```text
required files exist
required content exists
preview truth passes when scenario requires preview
artifact/download truth passes when scenario requires artifact
```

Failure codes:

```text
FALSE_FINISH_NO_OUTPUT
FALSE_FINISH_PREVIEW_BROKEN
ARTIFACT_TRUTH_MISMATCH
PREVIEW_TRUTH_MISMATCH
```

### 11.6 UI control invariant

In UI mode:

```text
click -> network/ws frame -> event append -> visible state change or visible error
```

Failure codes:

```text
UI_ENABLED_DEAD_CONTROL
WS_FRAME_DROPPED_NO_ERROR
NO_USER_EVENT_AFTER_SUBMIT
NO_CLEAR_FAILURE_TO_USER
```

### 11.7 Tool-scope invariant

A hidden tool is still unsafe if it remains callable.

```text
A tool disallowed by the current mode must not exist in ToolScope.allowed_tools.
It is not enough to remove it from advertised_tools.
```

Failure codes:

```text
TOOL_SCOPE_MISMATCH
WRITE_TOOL_ALLOWED_IN_PLANNING
WRONG_TOOL_REJECTION_ABORTS_LOOP
```

---

## 12. Failure taxonomy

Every `FAIL` must use a known code. No unknown failures in promotion runs.

### P0 — release blockers

```text
NO_USER_EVENT_AFTER_SUBMIT
NO_PLAN_AFTER_USER_TURN
NO_REPLAN_AFTER_REVISION
WRITE_TOOL_ALLOWED_IN_PLANNING
WRITE_TOOL_ATTEMPTED_IN_PLANNING
WRONG_TOOL_REJECTION_ABORTS_LOOP
APPROVE_PLAN_NO_EXECUTION
DUPLICATE_EXECUTION_AFTER_APPROVAL
EXECUTION_NO_ACTION
ACTION_NO_OBSERVATION
FALSE_FINISH_NO_OUTPUT
FALSE_FINISH_PREVIEW_BROKEN
DUPLICATE_KICK_ON_RESUME
EVENT_LOG_STATE_DIVERGENCE
WS_FRAME_DROPPED_NO_ERROR
UI_ENABLED_DEAD_CONTROL
UNKNOWN_FAILURE
```

### P1 — major reliability bugs

```text
PLAN_REVISION_NOT_INCREMENTED
STALE_PLAN_USED_AFTER_FOLLOWUP
PLAN_APPROVED_STATUS_MISSING
TOOL_SCOPE_MISMATCH
TOOL_REJECTION_NOT_VISIBLE_TO_MODEL
MODEL_TOLD_WRONG_MODE
STUCK_RUNNING_NO_EVENTS
PREVIEW_TRUTH_MISMATCH
ARTIFACT_TRUTH_MISMATCH
NO_CLEAR_FAILURE_TO_USER
OBSERVATION_WITHOUT_ACTION
WRITE_BEFORE_REVISION_APPROVAL
```

### P2 — quality bugs

```text
SLOW_FIRST_PLAN
EXCESSIVE_READS_BEFORE_PLAN
MISSING_DONE_CONDITIONS
LOW_QUALITY_PLAN
MISSING_PROGRESS_EVENTS
WEAK_FINAL_SUMMARY
```

---

## 13. Classifier schema

Every run writes:

```text
classification.json
```

Required schema:

```json
{
  "status": "PASS | FAIL | INVALID_RUN | INFRA_FAILURE",
  "severity": "P0 | P1 | P2 | NONE",
  "code": "NO_REPLAN_AFTER_REVISION",
  "first_broken_link": "followup_user_event -> revised_plan_event",
  "scenario_id": "revise_after_finish",
  "run_id": "...",
  "conversation_id": "...",
  "commit": "...",
  "seed": 12345,
  "facts": {},
  "oracle_results": [],
  "required_evidence_present": true,
  "replay": {
    "attempted": true,
    "result": "same_failure | passed | different_failure | not_attempted",
    "run_id": "..."
  },
  "accepted_by": "oracle",
  "agent_comments_ignored_for_adjudication": true
}
```

During development, `UNKNOWN_FAILURE` may exist, but it is P0 until the classifier is updated. `UNKNOWN_FAILURE` is not allowed in promotion runs.

---

## 14. Scenario format

Scenarios must be machine-readable. They cannot depend on Claude's interpretation.

Example:

```yaml
id: revise_after_finish
mode: api
prompt: "Create an index.html with a blue hero and the heading 'Launch Day'."
followups:
  - text: "Revise the hero heading to 'Grand Opening' and add a pricing section."
    requires_plan_revision: true
  - text: "Now change the pricing section to three tiers and update the CTA to 'Book a Visit'."
    requires_plan_revision: true
assertions:
  event_chain:
    require_user_event: true
    require_plan_before_execution: true
    require_action_observation_pairs: true
  revisions:
    expected_final_plan_revision: 3
    forbid_write_before_revision_approval: true
  workspace:
    files:
      - path: "index.html"
        must_contain:
          - "Grand Opening"
          - "Book a Visit"
          - "pricing"
  preview:
    required: true
    must_contain:
      - "Grand Opening"
      - "Book a Visit"
```

The harness translates this into oracle predicates.

---

## 15. Scenario matrix

Start with bare Build scenarios. Do not start with AppKit.

### 15.1 Smoke scenario

```yaml
- id: static_html_minimal
  mode: api
  prompt: "Create an index.html with the heading 'Build Smoke OK', serve it, and finish."
  assertions:
    event_chain:
      require_user_event: true
      require_plan_before_execution: true
      require_action_observation_pairs: true
    workspace:
      files:
        - path: "index.html"
          must_contain: ["Build Smoke OK"]
    preview:
      required: true
      must_contain: ["Build Smoke OK"]
    terminal_status_in: ["FINISHED", "VERIFIED"]
```

### 15.2 Plan discipline scenario

```yaml
- id: must_plan_before_tool
  mode: api
  prompt: "Create a simple landing page for a local bakery."
  assertions:
    event_chain:
      require_user_event: true
      require_plan_before_execution: true
    planning:
      first_agent_tool_in: ["submit_plan", "ask_user", "clarify", "think", "file_read", "file_list", "search", "extract"]
      forbid_write_before_plan_approval: true
    tool_scope:
      planning_disallows: ["file_write", "shell_exec", "browser", "serve", "finish"]
```

### 15.3 Revision scenario

```yaml
- id: revise_after_finish
  mode: api
  prompt: "Create an index.html with a blue hero and the heading 'Launch Day'."
  followups:
    - text: "Revise the hero heading to 'Grand Opening' and add a pricing section."
      requires_plan_revision: true
    - text: "Now change the pricing section to three tiers and update the CTA to 'Book a Visit'."
      requires_plan_revision: true
  assertions:
    revisions:
      initial_plan_revision: 1
      expected_final_plan_revision: 3
      forbid_write_before_revision_approval: true
    workspace:
      files:
        - path: "index.html"
          must_contain: ["Grand Opening", "Book a Visit"]
```

### 15.4 Mid-run steering scenario

```yaml
- id: steer_while_running_requires_plan_update_or_clear_execution_note
  mode: api
  prompt: "Create a two-page static site with Home and About pages."
  inject_followup_when:
    after_first_file_write: "Also add a Contact page and update nav links."
  assertions:
    revisions:
      require_acknowledged_followup: true
      require_revised_plan_or_explicit_plan_step_update: true
      forbid_stale_plan_finish: true
    workspace:
      files:
        - path: "index.html"
          must_contain: ["Contact"]
```

### 15.5 Tool rejection recovery scenario

Use fake model/tool fixtures to force a wrong call deterministically.

```yaml
- id: planning_write_tool_rejection_recovers
  mode: fake_model
  fake_model_script:
    - call_tool: file_write
      args: {path: "index.html", content: "bad"}
    - call_tool: submit_plan
      args:
        summary: "Plan after rejection"
        steps: [{title: "Create file"}]
  assertions:
    tool_rejection:
      wrong_tool_rejected: true
      rejection_visible_to_model: true
      valid_tool_after_rejection_exists: true
      loop_did_not_abort: true
```

### 15.6 Complex multi-turn UI scenario

```yaml
- id: complex_static_site_three_revisions
  mode: ui
  prompt: "Create a polished static website for a mobile dog grooming business with a home page, services section, FAQ, testimonials, and contact form."
  followups:
    - text: "Make it feel more premium and less playful; keep the business local-service oriented."
      requires_plan_revision: true
    - text: "Add a comparison section that explains mobile grooming versus salon grooming."
      requires_plan_revision: true
    - text: "The CTA should focus on booking an appointment; update all CTAs consistently."
      requires_plan_revision: true
  assertions:
    revisions:
      expected_final_plan_revision: 4
      forbid_write_before_revision_approval: true
    runtime:
      forbid_duplicate_kick: true
    preview:
      required: true
      must_contain: ["mobile grooming", "booking", "comparison"]
```

---

## 16. Deterministic classifier behavior

Classifier order matters. The first broken link wins.

Classify in this order:

```text
harness validity
scenario contract
submit -> user event
user event -> plan event
plan event -> approval status
approval status -> execution action
execution action -> observation
tool rejection -> recovery visibility
follow-up user event -> revised plan
write after follow-up -> revision approval
finish -> output truth
preview claim -> preview truth
UI control -> WS/API frame -> event
runtime/debug trace inconsistencies
```

Required event predicates:

```python
def has_user_message(events, after_seq=0): ...
def has_plan(events, revision=None, after_seq=0): ...
def has_status(events, detail): ...
def has_action(events, tool_name=None, after_seq=0): ...
def has_observation_for_action(events, action_id): ...
def has_tool_rejection(events): ...
def terminal_status(events): ...
def latest_plan_revision(events): ...
```

Example output:

```json
{
  "status": "FAIL",
  "severity": "P0",
  "code": "NO_REPLAN_AFTER_REVISION",
  "conversation_id": "conv_abc",
  "first_broken_link": "followup_user_event -> revised_plan_event",
  "facts": {
    "followup_event_seq": 42,
    "latest_plan_revision_before": 1,
    "latest_plan_revision_after": 1,
    "first_write_after_followup_seq": 44,
    "write_tool_before_replan": true
  },
  "likely_files": [
    "packages/core/src/disco/core/loop/messages.py",
    "packages/core/src/disco/core/loop/plans.py",
    "packages/core/src/disco/core/loop/turn_control.py",
    "packages/core/src/disco/core/loop/engine.py",
    "packages/agent-server/src/disco/agent_server/runtime.py"
  ]
}
```

---

## 17. No-fluke replay policy

If a run fails once:

```text
1. Freeze the run folder.
2. Classify the first broken link.
3. Replay the exact seed.
4. If replay fails the same way: deterministic failure.
5. If replay passes: intermittent failure.
6. Intermittent failure is still FAIL-INTERMITTENT, not PASS.
7. Add the scenario to the intermittent regression set until fixed or proven infra.
```

If exact replay passes after a failure:

```text
status: FAIL
code: INTERMITTENT_<original_code>
severity: same as original code
```

Then run:

```text
same scenario
same seed class
same model
same branch
N=20 times
```

If any fail, promotion is blocked. If none fail, the original failure remains recorded and promotion can continue only if no P0/P1 intermittent appears in the final gate.

---

## 18. Patch acceptance protocol

A patch is accepted only if all steps pass:

```text
1. Original frozen seed fails before patch.
2. New regression test fails before patch.
3. Patch is applied.
4. Regression test passes after patch.
5. Exact failing seed passes after patch.
6. Scenario neighborhood passes after patch.
7. Full relevant scenario class passes.
8. Classification for new run is PASS.
9. No scenario assertion was weakened.
10. No product gate was loosened without explicit approved migration.
```

Claude Code writes the patch. Codex may do RCA or review. The oracle accepts or rejects.

---

## 19. Assertion weakening detector

The repair loop fails if a patch changes scenario assertions, failure-class severity, or verifier requirements without a migration.

Tracked files:

```text
harness/build_soak/scenarios.yaml
harness/build_soak/classify.py
harness/build_soak/oracles/*.py
harness/build_soak/failure_codes.py
packages/core/tests/test_*plan*.py
packages/core/tests/test_*replan*.py
packages/core/tests/test_*tool_scope*.py
```

If any of those change in a repair patch, require:

```text
harness/build_soak/migrations/<timestamp>_<reason>.md
```

The migration must state:

```text
what assertion changed
why the previous assertion was wrong
which product behavior replaces it
which tests prove the new behavior
```

Without a migration, the patch is rejected.

---

## 20. Bare Build contract tests before live soak

These unit/contract tests must exist before spending live model money.

### 20.1 Planning contract

File:

```text
packages/core/tests/test_build_plan_contract.py
```

Required tests:

```text
first turn in PLANNING only exposes submit_plan/read/clarify/ask/think
write tool in PLANNING produces recoverable tool-rejection observation
submit_plan resets planning-read counter
plan revision increments on follow-up planning
```

### 20.2 Replan contract

File:

```text
packages/core/tests/test_build_replan_contract.py
```

Required tests:

```text
after plan_approved, new user message enters PLANNING or explicit revision gate
revision framing message is emitted once
agent cannot write before revised plan approval unless workflow explicitly permits direct patch
revision PlanEvent has revision = previous + 1
```

### 20.3 Tool rejection recovery

File:

```text
packages/core/tests/test_tool_rejection_recovery.py
```

Required tests:

```text
disallowed tool call emits AgentErrorEvent or ObservationEvent visible to model
loop continues after rejection
next valid submit_plan is accepted
state remains consistent
```

### 20.4 Plan approval execution

File:

```text
packages/core/tests/test_plan_approval_execution.py
```

Required tests:

```text
approve_plan flips mode to execution
approval is idempotent
execution does not duplicate initial user task
runtime kick after approval produces action or terminal failure
```

---

## 21. Repair loop

Pseudo-code:

```python
while campaign_not_green:
    run_batch = build_soak.run(matrix=scenarios, iterations=N)
    report = classify(run_batch)

    if report.all_passed:
        update_streaks(report)
        if release_gate_met:
            stop_success()
        continue

    for failure in report.failures_by_priority():
        freeze_failure_dossier(failure)

        codex_rca = run_codex_rca(failure, mode="read_only")
        claude_patch = run_claude_fix(failure, codex_rca)

        run_targeted_tests(claude_patch, failure)
        rerun_exact_failure_seed(failure)
        rerun_neighborhood(failure)

        if passes:
            commit_patch_with_failure_id()
            add_regression_test(failure)
        else:
            write_unresolved_failure_report()
            continue_repair_or_escalate()
```

No patch is accepted because Claude says it fixed the issue. A patch is accepted only when the exact failing seed and its neighboring scenario class pass.

---

## 22. Claude Code and Codex roles

### Claude Code

Allowed:

```text
inspect frozen evidence
patch code
add regression tests
run tests
write patch summary
```

Not allowed:

```text
mark pass
change failure status
delete failing run folder
weaken oracle assertions
call a product failure infra without oracle support
```

### Codex

Allowed:

```text
read-only RCA
identify likely files/functions
suggest regression test shape
review patch diff
```

Not allowed by default:

```text
patch main worktree
mark pass
override classifier
```

### Human

Allowed:

```text
approve migration when product behavior intentionally changes
merge patches
change promotion bar deliberately
```

Not allowed under zero-opinion mode:

```text
dismiss evidence without changing classifier/scenario contract
```

---

## 23. Claude Code repair prompt template

Path:

```text
harness/build_soak/prompts/claude_fix.md.j2
```

Template:

```md
# Disco Build Soak Failure Repair

You are Claude Code fixing a concrete Build loop failure in Disco.

## Non-negotiable rules

- Do not broaden product behavior to hide the failure.
- Do not skip planning gates to make the test pass.
- Do not weaken tool-scope enforcement.
- Do not mark runs finished without output truth.
- Add or update a regression test that fails before your patch and passes after.
- Keep the patch minimal.

## Failure

Code: {{ failure.code }}
Severity: {{ failure.severity }}
Broken link: {{ failure.first_broken_link }}
Conversation: {{ failure.conversation_id }}
Run folder: {{ failure.run_folder }}

## Evidence

{{ failure.evidence_markdown }}

## Codex independent RCA

{{ codex_rca }}

## Likely files

{{ likely_files }}

## Required output

1. Root cause.
2. Minimal patch.
3. Regression test.
4. Exact commands run.
5. Confirmation that exact failing seed passes after patch.
```

---

## 24. Codex RCA prompt template

Path:

```text
harness/build_soak/prompts/codex_rca.md.j2
```

Template:

```md
# Independent RCA: Disco Build Loop Failure

You are Codex performing independent root-cause analysis.

Default mode: read-only. Do not modify files unless explicitly invoked in patch-proposal mode.

## Failure

Code: {{ failure.code }}
Broken link: {{ failure.first_broken_link }}
Run folder: {{ failure.run_folder }}

## Evidence to inspect

- {{ failure.timeline }}
- {{ failure.events }}
- {{ failure.state_final }}
- {{ failure.inspect_trace }}
- {{ failure.logs }}

## Questions to answer

1. What is the first code path that likely caused this failure?
2. Is this a planning-mode problem, execution-mode problem, runtime-kick problem, tool-scope problem, event-log/state problem, or UI transport problem?
3. Which files should Claude patch?
4. What regression test would have caught this?
5. What patch shape should be avoided?

Return concise file/function-level RCA with evidence. Do not propose product-scope expansion.
```

---

## 25. Commands

### One scenario

```bash
python -m harness.build_soak.run \
  --mode api \
  --scenario static_html_minimal \
  --iterations 1 \
  --out test-record/build-soak
```

### Multi-turn soak

```bash
python -m harness.build_soak.run \
  --mode api \
  --scenario-set revisions \
  --iterations 25 \
  --model "$DISCO_SOAK_MODEL" \
  --out test-record/build-soak
```

### UI soak

```bash
cd frontend
npx playwright test --config playwright.live.config.ts e2e-live/build-soak.spec.ts
```

### Classify latest

```bash
python -m harness.build_soak.classify test-record/build-soak/latest
```

### Repair latest failure

```bash
python -m harness.build_soak.repair_loop \
  --run test-record/build-soak/latest \
  --mode write_prompts_only
```

### Full local loop with caps

```bash
python -m harness.build_soak.repair_loop \
  --scenario-set bare,revisions,tool_rejection \
  --iterations-per-batch 10 \
  --max-repair-cycles 20 \
  --max-cost-usd 50 \
  --claude-mode invoke \
  --codex-mode rca \
  --require-human-merge true
```

---

## 26. Stop / continue policy

Continue automatically for:

```text
P2 failures
single-scenario failure with clear deterministic classifier
known infra failures only after oracle confirms INFRA_FAILURE and retry policy passes
```

Stop and require human review for:

```text
P0 repeated after two patch attempts
patch touches security boundary
patch weakens planning/tool gating
patch weakens verifier/export requirements
patch deletes tests
patch changes scenario assertions to pass
cost cap reached
Codex and Claude disagree on root cause in a way that affects patch direction
```

Human review may approve a migration, but may not dismiss evidence.

---

## 27. Implementation PR order

### PR S1 — Evidence lock and oracle schema

Files:

```text
harness/build_soak/evidence.py
harness/build_soak/oracles/schema.py
harness/build_soak/tests/test_evidence_lock.py
harness/build_soak/tests/test_oracle_schema.py
```

Done when:

```text
Every run folder has manifest.json, evidence hashes, and oracle results validate.
```

### PR S2 — Classifier and core oracles

Files:

```text
harness/build_soak/classify.py
harness/build_soak/failure_codes.py
harness/build_soak/oracles/harness_validity.py
harness/build_soak/oracles/event_chain.py
harness/build_soak/oracles/revision.py
harness/build_soak/oracles/output_truth.py
harness/build_soak/tests/test_classifier.py
harness/build_soak/tests/test_event_chain_oracle.py
harness/build_soak/tests/test_revision_oracle.py
harness/build_soak/tests/test_output_truth_oracle.py
```

Done when:

```text
Given fixture event logs, classifier emits exact failure codes and first broken links.
```

### PR S3 — Headless API Build runner

Files:

```text
harness/build_soak/adapters/disco_api.py
harness/build_soak/run.py
harness/build_soak/scenarios.yaml
```

Done when:

```bash
python -m harness.build_soak.run --mode api --scenario static_html_minimal --iterations 1
```

produces a complete run folder.

### PR S4 — UI Build runner

Files:

```text
frontend/e2e-live/build-soak.spec.ts
frontend/e2e-live/support/buildSoakHarness.ts
harness/build_soak/adapters/playwright_ui.py
```

Done when:

```text
actual Build buttons are exercised and ui-actions/network/ws logs are saved.
```

### PR S5 — Multi-turn revision scenarios

Files:

```text
harness/build_soak/scenarios.yaml
harness/build_soak/tests/test_revision_scenarios.py
```

Done when:

```text
each follow-up requires a new PlanEvent revision unless scenario explicitly allows direct patch.
```

### PR S6 — Fake model / fake tool simulator

Files:

```text
harness/build_soak/fixtures/fake_model.py
harness/build_soak/fixtures/fake_tools.py
packages/core/tests/test_build_loop_simulated_failures.py
```

Done when:

```text
wrong tool in planning, malformed plan, missing action observation, and verifier failure can be simulated without live model spend.
```

### PR S7 — Claude/Codex repair adapters

Files:

```text
harness/build_soak/repair_loop.py
harness/build_soak/adapters/claude_code.py
harness/build_soak/adapters/codex.py
harness/build_soak/prompts/claude_fix.md.j2
harness/build_soak/prompts/codex_rca.md.j2
```

Done when:

```text
repair_loop.py can generate prompts, call adapters if configured, or write prompt files for manual Claude/Codex sessions.
```

Adapters should support:

```text
mode=write_prompts_only
mode=invoke_claude
mode=invoke_codex_rca
mode=invoke_both
```

### PR S8 — Exact-seed replay and no-fluke policy

Files:

```text
harness/build_soak/replay.py
harness/build_soak/intermittent.py
harness/build_soak/tests/test_replay.py
harness/build_soak/tests/test_no_fluke_policy.py
```

Done when:

```text
A failed run can be replayed by scenario id, seed, model, assist/autonomous flags, and follow-up timing.
```

### PR S9 — Patch acceptance gate

Files:

```text
harness/build_soak/patch_gate.py
harness/build_soak/repair_loop.py
harness/build_soak/tests/test_patch_gate.py
```

Done when:

```text
Claude Code patches are accepted only after exact-seed replay, regression test, and neighborhood tests pass.
```

### PR S10 — Regression dashboard

Files:

```text
harness/build_soak/report.py
.github/workflows/build-soak-experimental.yml
```

Done when:

```text
The dashboard reports pass rate by scenario class, failure code, model, branch, and commit.
```

---

## 28. Promotion gate into AppKit/mainline

Do not merge AppKit or other higher-level Build primitives into the main product path until this gate passes:

```text
[ ] All oracle unit tests pass.
[ ] No INVALID_RUN in final soak.
[ ] No UNKNOWN_FAILURE in final soak.
[ ] API bare build: 100/100 PASS.
[ ] UI bare build: 50/50 PASS.
[ ] Revision scenarios: 25/25 PASS.
[ ] Complex revision scenarios: 10/10 PASS.
[ ] Tool-rejection simulator: 100% PASS.
[ ] Exact replay exists for every historical P0/P1 failure.
[ ] Regression test exists for every historical P0/P1 failure.
[ ] No accepted patch weakened assertions without migration.
[ ] Final report is generated from classification.json only, not prose summaries.
```

Ordering:

```text
Bare Build loop reliability
→ Multi-turn revision reliability
→ AppKit primitive reliability
→ UI AppKit reliability
→ main product promotion
```

---

## 29. Claude Code bootstrap prompt

Paste this into Claude Code to implement the harness:

```md
Implement the Disco Build Soak Guidelines.

Priority order:
1. Add evidence lock and oracle result schema.
2. Add deterministic classifier for Build event logs.
3. Add API runner for bare Build smoke and revision scenarios.
4. Add evidence folders with timeline.md, events, state, trace, workspace manifest, logs, and hashes.
5. Add fake-model/fake-tool tests for wrong-tool-in-planning and no-replan-after-followup.
6. Add replay.py and no-fluke intermittent policy.
7. Add repair_loop.py that writes Claude and Codex prompts from a failed dossier.
8. Add patch acceptance gate.
9. Add UI Playwright build-soak path after API runner is green.
10. Add dashboard and promotion gate.

Do not start by implementing more AppKit features.
The current target is the bare Build loop:
submit → plan → approve → execute → revise → re-plan → verify/output.

Every failure must get a known classifier code.
No generic "failed" bucket in promotion runs.
Every patch must add a regression test.
Do not weaken planning/tool gates to make tests pass.
Do not call a product failure a fluke.
The oracle adjudicates; Claude and Codex only repair or review.
```

---

## 30. One-sentence rule

The Build loop is not green because Claude, Codex, ChatGPT, or a human believes it is green.

It is green only because the same run contracts repeatedly pass against frozen evidence, deterministic oracles, exact-seed replay, output truth, and regression tests.
