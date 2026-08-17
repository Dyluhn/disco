> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era build-loop speed-fixes work-order plan, frozen mid-execution.
> Historical only. Not a source of current status or operating instructions.

# Build-loop fixes (#6 + 2 codex-found live bugs) — work-order plan

Source: a bias-free dual investigation of the slow-build trace `conv_f32adcb4` (36.7 min,
user KILLED it). My independent analysis found the slowness mechanism; a neutral codex review
(no problem stated) independently found two more LIVE system-mechanics bugs. Fold in all three,
run via the harness: plan → codex approval → parallel Sonnet fan-out. Disco's gates = completion.

## The three findings (root-caused)

### F-1 — read-before-rewrite (the slowness/thrash) [my analysis]
Trace `conv_f32adcb4`: 99 actions, **38 full `file_write` vs 1 targeted edit**, `js/main.js`
rewritten **9×** and NEVER `file_read` before any rewrite; content sizes fluctuate
(3564→1804→2886→2210→…) — the model regenerates the whole file from STALE context after each
`browser`-observed runtime error, which spawns new errors → non-converging thrash. Per-turn model
latency (median 17.4s) × ~99 turns = ~32 of the 36.7 min. The model's own `thought`s say "let me
read/check main.js" but the action is a write — it intends to ground itself and doesn't.
The F3 read-before-write tracker (`files.py` `_read_state`, `_conv_state`) exists but is
`ctx.assist`-gated (capable models bypass it, `files.py` FileReadTool.run ~line 234) AND only
guards writes to PRE-EXISTING (un-created) files — so it never catches a rewrite-thrash of files
the agent itself created. Constraint (Dylan): do NOT fix this by enabling `file_str_replace`
(anchored_edit) on Qwen — it does anchored edits poorly. Use a read-before-rewrite gate + nudge.

### F-2 — C18 `command` plan-step predicate runs in the WRONG filesystem [codex]
`check_command_for_plan_step` runs `subprocess.run(..., cwd=cwd)` on the HOST with `cwd=None`
when `workspace_path` is None (`plan_conditions.py:228`). Container backends intentionally return
`workspace_path=None` (`_container.py:266`), so a plan-step command predicate like
`test -d css && test -f index.html` is evaluated against the agent-server host FS, not the box —
a FALSE advisory. The sibling `check_file_exists_for_plan_step` already fixed this by asking the
sandbox (`plan_conditions.py:212`); the command path must do the same via `ctx.sandbox.exec_shell`.

### F-3 — "publish it / I'm done" forces a RE-PLAN [codex]
Trace B [1461.3s]: user says "stop troubleshooting, publish it and be done" → system enters
`RUNNING planning` + injects "RE-PLANNING" (`messages.py:53`). After FINISH, a user message routes
through `request_plan` (`control_ops.py:62`) → `enter_planning` (`engine.py:1516`). For a
ship-it/mark-done intent, forcing a fresh approval plan is the wrong UX — it should finish/publish,
not re-plan.

## Work orders (parallel after none — all three are disjoint files)

### Order F-1 — read-before-rewrite gate + nudge — owner: Sonnet #1
Files: `packages/tools/src/disco/tools/builtin/files.py`, `packages/core/src/disco/core/llm/prompts.py`,
`packages/core/src/disco/core/loop/view_render.py`, `packages/tools/tests/**`, `packages/core/tests/**`
(prompt/snapshot + view tests only — NOT plan_conditions/C18, which is F-2's).
Enforcement point: `FileWriteTool.run` BEFORE `_gated_write()` (files.py:325), extending `_read_state`
(files.py:57). Rules (per codex):
- **Rule:** existing path + NO successful `file_read` since the path's LAST successful MUTATION ⇒
  REFUSE with a structured ToolError: read it first, or use a targeted line-edit
  (`file_replace_lines`/`file_insert_lines`). **REMOVE the old F3 "second write forces replace"
  escape hatch** — it would let the model work around the refusal and keep thrashing. No bypass.
- **New-file creation stays EXEMPT** (no prior content to ground on). Same-created-file rewrites are
  NOT exempt — that's the trace's exact failure mode.
- A successful `file_write` CLEARS the path's read-since-write bit. ONLY `FileReadTool.run` SETS it —
  the internal `read_file()` used by the syntax gate must NOT count.
- ALL successful mutators invalidate the read bit for their path: `file_append`, `file_edit`,
  `file_replace_lines`, `file_insert_lines`, AND `file_str_replace` (so read→append→full-rewrite
  can't pass on stale context).
- **Canonicalize tracker keys** via `strip_redundant_workspace_prefix` so `/workspace/foo` and
  `workspace/foo` are ONE key (raw `args.path` aliases currently bypass the guard).
- Applies to ALL tiers (the thrash hits capable models too); supersede the assist-gated F3 path so
  they don't double-refuse or contradict.
- **Prompts:** update BOTH `prompts.py` execution prompts AND `view_render.py:166` (which currently
  tells the model whole-file `file_write` is the reliable path and to AVOID line edits — that
  directly conflicts; rewrite it to: read-before-rewrite, prefer targeted line-edits for small
  changes, don't regenerate whole files from memory).
Accept (codex gates): create→2nd `file_write` w/o read REFUSED; read→write SUCCEEDS; write→write
REFUSED again; append/edit/line-edit each invalidate the read bit; path aliases collapse to one key;
assist-OFF is now guarded; the old F3 second-attempt bypass is gone. Existing tools tests green. NO
`file_str_replace`/anchored_edit ENABLEMENT (it stays withheld on Qwen; we only invalidate its bit).

### Order F-2 — sandbox-aware C18 command predicate — owner: Sonnet #2
Files: `packages/core/src/disco/core/loop/plan_conditions.py`, `packages/core/tests/test_c18_*`.
- `check_command_for_plan_step`: when `ctx`/the loop has a sandbox with `exec_shell`, run the
  predicate command via `self._loop.executor.sandbox.exec_shell(cmd, timeout_s=...)` (mirror the
  file_exists fix at line 212 — duck-typed, no upward tools import), comparing the ExecResult exit
  code to `expect_exit`. Keep the host-`subprocess` branch ONLY for the sandbox-less/fake-executor
  test path (like the file_exists predicate does). 5s timeout preserved; advisory-only (never wedge).
- **Timeout caveat (codex):** if the sandbox `ExecResult.timed_out` is true, treat it as a
  FAILURE/non-pass even if `exit_code == expect_exit` (e.g. 124) — a host-subprocess timeout can't
  pass today, so a sandbox timeout must not accidentally pass.
Accept: a test — a command predicate on a fake container backend (`workspace_path=None`) is evaluated
via `sandbox.exec_shell` (NO host subprocess); a `timed_out` ExecResult fails even on a matching exit
code; the sandbox-less/fake-executor path still uses subprocess. Existing C18 tests green.

### Order F-3 — "publish/done" must not force a replan — owner: Sonnet #3
Files: `packages/agent-server/src/disco/agent_server/control_ops.py` ONLY (+ `packages/agent-server/
tests/**`). Do NOT touch engine.py — `request_plan` owns the routing (codex).
- In `ControlOps.request_plan` (control_ops.py:62), ONLY when the current state is already
  `FINISHED`, run a NARROW, conservative ship-it intent check on the user text. On a match: append
  the user message (so the optimistic UI echo resolves) but do NOT call `enter_planning()` and do
  NOT `kick()`. On any ambiguity, fall through to the EXISTING replan behavior.
- Match only SAFE phrases that clearly mean "stop, accept as-is": "publish it and be done",
  "leave it as is", "you're done", "that's done", "mark it (as) done/complete", "we're done".
  Do NOT match bare "publish it" (ambiguous — publish/deploy can be real work) and MUST still
  replan on real change/deploy intents: "publish to Netlify", "deploy it", "add dark mode",
  "fix X", "change Y". Default = replan.
- (Long-term note, NOT this pass: a cleaner signal is an explicit UI `accept_finished`/`mark_done`
  frame action rather than free text — leave a TODO.)
Accept (codex gates): a post-finish "publish it and be done" appends the user text but emits NO
`planning` status, NO "RE-PLANNING" framing, NO revision-2 PlanEvent, and schedules NO task; a
genuine "add a dark mode toggle" AND "publish to Netlify" STILL replan (no regression).

## Disjointness (codex-confirmed, revised)
- F-1: `tools/builtin/files.py` + `core/llm/prompts.py` + `core/loop/view_render.py` + tools tests +
  core prompt/snapshot/view tests.
- F-2: `core/loop/plan_conditions.py` + core C18 tests.
- F-3: `agent-server/control_ops.py` + agent-server tests (NO engine.py).
No shared SOURCE file across orders. Core test dirs differ by file (F-1 = prompt/snapshot/view;
F-2 = test_c18_*). Workers do edits-only against the spec; orchestrator integrates + runs the suite.

## Acceptance gates (all)
- Full pytest (core+tools+agent-server) + ruff green; frontend unaffected (no FE in scope).
- The per-order codex gates above (F-1: create→refuse-on-2nd-write-without-read, read lifts, all
  mutators invalidate, alias-collapse, assist-OFF guarded, F3 bypass removed; F-2: sandbox-eval +
  timeout-fails + sandboxless-fallback; F-3: publish-and-be-done → no planning/RE-PLANNING/rev-2/
  task, real change+deploy still replan).
- No `file_str_replace`/anchored_edit ENABLEMENT (Dylan constraint).
- Merge → full suite → codex re-review of the merged diff → live proof where feasible.
