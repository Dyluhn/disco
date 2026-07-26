# Campaign Status — standing operational ledger

**Status: MUTABLE, UPDATED CONSTANTLY.** A concise current snapshot on top; an
append-only dated decision/work log below. **Never overwrite inconvenient
history.**

Update before a package, after a finding, after a material edit, after a gate,
before and after a live/model run, when a background operation starts or ends,
during every hourly review, and before any context compaction or session
handoff.

> **Freeze rule.** Before the candidate is frozen, *this file* is the standing
> ledger. At candidate freeze, this file records the exact external continuation
> path and repository bytes stop moving. During qualification/certification the
> standing ledger continues at that external path, with the same schema and
> frequency, and the hourly hook switches to it.
>
> External continuation path: **not yet assigned — candidate not frozen.**

---

# CURRENT SNAPSHOT

**Last updated (local + UTC):** 2026-07-25 23:49 CDT / 2026-07-26T04:49:29Z

**Current branch / HEAD / source fingerprint:**
`disclaude/build-platform-core-v1` / `579cabaf` (Epic 1) /
`source sha256:a432bb1de459d67a67cf1faae829133f8acd6d64b3073513e2fe0dbd667ded36`
(1929 files; tree
`sha256:b1f8830bdb44983fa103d2168990b2614584e5b32588e862cdf008e4068deb2a`,
2533 files)

**Tree cleanliness and every intentional dirty path:**
Epic 0 (`32a09957`) and Epic 1 (`579cabaf`) are committed. The only dirty paths
are the in-flight Epic-3 **one-horizon** slice:

```text
M  packages/core/src/disco/core/loop/view_render.py   (build_with_horizon)
M  packages/core/src/disco/core/loop/engine.py        (return the built horizon)
M  packages/core/tests/test_loop_condensation.py      (one-horizon regression)
```

**Current epic and package:** Epic 3, **one-horizon slice only**. This slice
does **not** complete Epic 3 — raw DSML/tool-markup rejection, bounded repair,
and the truthful fallback all remain open.

Owner authorized (2026-07-25) a bounded, non-promoting **reliability-loop
acceleration insertion** (Packages A/B/C) to run at the next clean committed
boundary, before resuming Epic 2. It adds no campaign acceptance count and earns
zero promotion credit. `CAMPAIGN-PLAN.md` is deliberately unchanged.

**Current operation:** none running. (No background PID; no live model run.)

**Promotion count on current bytes:** **0 / 100.** No clean candidate exists.

**Completed epics/packages with evidence links:** Epic 0 — committed `32a09957`.
All eight acceptance items met; evidence in the gate tables below and in the
2026-07-25 work log.

**Focused gates and exact results:**

| Gate | Result |
|------|--------|
| External archive manifest `sha256sum -c MANIFEST.sha256` | **33/33 OK**, exit 0 |
| `scripts/source_fingerprint.py` determinism (2 runs) | identical digest, exit 0 |
| `scripts/check_governance_seal.py` — no manifest | exit **3** (seal not established) — as designed |
| `scripts/check_governance_seal.py --rebaseline` without env | exit **1** (refused) — as designed |
| `scripts/check_governance_seal.py --rebaseline` with env | exit **0**, 2 files sealed |
| `scripts/check_governance_seal.py` verify | exit **0** |
| `scripts/check_governance_seal.py` after byte mutation | exit **1** (drift named exactly) |
| `scripts/check_governance_seal.py` after restore | exit **0** |
| PreToolUse guard mutation matrix (hook contract) | **11/11 denied**, **5/5 legitimate allowed**, no false positives |
| Completion sentinel — negative control (documentation mention) | not satisfied — as designed |
| Completion sentinel — positive control (standalone assertion) | satisfied, then restored |
| Hourly review — 60 s wall clock | not due at T-20 s; **due injection fired** at T+21 s |
| Hourly review — bare timestamp | **rejected**, all 10 fields named, schedule did **not** advance |
| Hourly review — complete review | **accepted (#1)**, schedule advanced atomically, source unblocked |
| Overdue source mutation / ledger write | source **denied**; ledger **allowed** (no deadlock) |
| Stop gate bounded refusal | block, block, block, **allow** on 4th; counter resets and re-arms |
| GLM launcher — hijacked `baseURL` | exit **70**, **no `stream.json`** — nothing was sent |
| GLM launcher — correct route | assertion passed; round-trip verified against ground truth |
| `pytest harness/build_soak/tests/` (`-m "not integration"`) | **exit 0**, ~890 passed, 1 skipped |
| `pytest packages/agent-server/tests/test_progressing_hardcap_freeze_order.py` | **exit 0**, 3 passed |
| `pytest harness/build_soak/tests/test_freeze_before_kill.py` | **exit 0**, 11 passed |

**Broad/live gates and exact results:**

| Gate | Result |
|------|--------|
| **E2E hook wiring** — real `claude -p` session attempts `Edit`/`Write` of a sealed file | **BLOCKED by the hook.** Model reported: "The edit was blocked — a hook rejected it because `ENGINEERING-STANDARDS.md` is a change-controlled governance file requiring the rebaseline procedure; the file is unchanged." |
| **E2E hook wiring** — real session attempts a `Bash` append to a sealed file | **BLOCKED by the hook.** Model reported: "Blocked — a pre-tool hook refused the write, citing `ARCHITECTURE-BOUNDARIES.md` as change-controlled; the file is unmodified." |
| Seal after both e2e attempts | exit **0**; `git status` shows both sealed files untouched |

This proves the wiring, not merely the hook logic: `.claude/settings.json` is
loaded, `${CLAUDE_PROJECT_DIR}` is substituted, the guard fires, and its reason
text reaches the model.

**Open observed findings, classification, and earliest broken contract:**

1. *(carried, product)* Hard-cap kill-before-freeze loses a progressing run's
   changed artifact and PNG. Earliest broken contract: evidence must be bound to
   exact event-bound immutable state before teardown
   (BOUNDARIES §7; pattern P1/P2). → Epic 1.
2. *(carried, product)* Host capability facts do not survive condensation;
   `k460000` repeated a refused host-signal kill after the refusal span was
   condensed away. Earliest broken contract: host authority must survive context
   handling (BOUNDARIES §9; pattern P3). → Epic 2.
3. *(carried, product)* The same event span can be condensed twice because the
   returned view and its event list can describe different horizons; and raw
   DSML/tool markup was persisted as summary text. Earliest broken contract: one
   response, one horizon (BOUNDARIES §8) and protocol output is not prose
   (BOUNDARIES §9; patterns P4/P5). → Epic 3.
4. *(carried, evidence)* The existing soak matrix is stale as a counting
   authority — it binds older source/scenario bytes and a local→Podman runtime
   while current diagnostics force the process backend. Earliest broken
   contract: stale authority must not override current contracts (pattern P8).
   → must be re-authored before anything counts in Epic 6.

**Decisions made autonomously and why:** see the dated log below.

**GLM delegations (provider/model receipt + output + verified value):**

Launcher: `/var/home/dylan/Desktop/Disclaude-Claude-GLM52/glm-run.sh`. Every call
resolves and asserts the route before sending; receipts land in
`/var/home/dylan/build-platform-campaign-evidence/2026-07-25/glm-delegations/`.

| Label | Route receipt | Output | What I independently verified |
|-------|---------------|--------|-------------------------------|
| smoke | `ollama-cloud/glm-5.2`, variant max, `baseURL=https://ollama.com/v1`, key present/redacted | `10` | Counted `packages/core/src/disco/core/build_platform/*.py` myself: **10**. Matches. |
| `badroute` | asserted OK (my negative control was invalid — `glm-run.sh` re-exports the route, overwriting the hostile value) | ran anyway, 27,793 tokens | Disclosed as a wasted call, not evidence. |
| `hijack` | **REFUSED, exit 70** | none — no `stream.json` written | Confirmed nothing was sent. |
| `authority-race` | `ollama-cloud/glm-5.2`, variant max | event-kind inventory with file:line | Cross-checked its `EventKind` list against `packages/core/src/disco/core/events.py`; used as leads only. **Cost 1.13M tokens for a report I largely superseded by reading the code directly — delegations must be scoped tighter.** |

**Next three concrete actions:**
1. Finish Epic 1's remaining acceptance: prove the full **real**
   `drive_scenario` process-backend hard-cap positive (known artifact + PNG,
   true progressing hard cap, both hashes preserved across kill, inspect
   finalized after kill, zero owned resources, primary
   `RUN_TIMEOUT_WHILE_PROGRESSING` retained).
2. Add the bounded pause-timeout case proving subordinate `FREEZE_TIMEOUT` does
   not launder the primary verdict, and the symlink / mutable-head fail-closed
   identity checks around collection.
3. Confirm product pause/kill APIs and kill semantics are unchanged, then commit
   Epic 1 as one coherent package.

**Known deferred post-campaign work:** everything in
[`ARCHITECTURE-ROADMAP.md`](./ARCHITECTURE-ROADMAP.md) (Tier A/B/C and the
carried research/owner gates). None of it may begin on this branch before
closeout.

**Completion-contract checklist:**

| Epic | State |
|------|-------|
| 0 — context/governance reset | **COMPLETE** — committed `32a09957`; all 8 acceptance items met and e2e-proven |
| 1 — finish freeze-before-kill | **COMPLETE** — all 7 acceptance items met; the freeze also had a production-fatal raw-row defect found and fixed |
| 2 — durable typed runtime constraints | not started |
| 3 — coherent, safe condensation | not started |
| 4 — context diagnostics on final bytes | not started |
| 5 — deterministic/integration preflight | not started |
| 6 — qualification and exact 100 | not started |
| 7 — same-byte closeout and local integration | not started |

The campaign is complete only when every Epic 0–7 acceptance item in
[`CAMPAIGN-PLAN.md`](./CAMPAIGN-PLAN.md) is met and this file truthfully
contains the exact line:

```text
COMPLETION CONTRACT SATISFIED: Epics 0-7 all acceptance items met.
```

It does not contain that line today, and the `Stop` hook refuses a voluntary
stop until it does.

---

# APPEND-ONLY WORK LOG

## 2026-07-25 — Epic 0 opened

**Handoff state verified against the brief, fact by fact.** Branch, committed
HEAD `271af2e6`, `stable-main`/`stabilization-integration` both at `f55efb03`,
and `f55efb03` proven an exact ancestor of HEAD via
`git merge-base --is-ancestor`. The three active product paths are present and
untouched.

**Context reset verified, not assumed.** `.serena/memories/` empty; no
`AGENTS.md`; external archive verifies 33/33 via `sha256sum -c`.

**DECISION — the residual Fable mentions are accepted as history, not a live
prohibition.** The obsolete "Fable only for verification / NEVER Fable" text
still exists in three `archive/` files. Each now opens with an explicit
`SUPERSEDED / HISTORICAL … Not a source of current status or operating
instructions` header, and `docs/governance/README.md` declares `archive/`
non-authoritative. *Why:* the brief forbids restoring or rewriting archive
history, and Epic 0's requirement is that no obsolete prohibition remains as a
**live directive**. Scrubbing the words out of dated historical records would
falsify history to satisfy a grep. Two further archive files mention Fable only
as a record of past reviews, which is not a prohibition at all.

**DECISION — `ARCHITECTURE-BOUNDARIES.md` states the vocabulary that actually
exists, not the planning document's names.** Reconciling §5 against current code
found `EntryDescriptor` and `ReadinessSignal` present verbatim in
`packages/core/src/disco/core/build_platform/contracts.py`, but **no**
`DeliveryShape` or `PreviewModality` types: those are *open namespaced strings*
(`DeliveryIntent.shape`, `PreviewPlan.modality`) validated by `_OPEN_NAME`.
*Why:* sealing a name that does not exist would make the frozen boundary false
on day one, and the open-string form is strictly more target-neutral than an
enum — a new target registers a shape without editing Core. The sealed file
states the invariant (open, adapter-owned, `none` supported) and marks symbol
paths as non-normative navigation.

**Reconciliation also confirmed** the single composition owner
(`resolve_build_composition()`, documented as "The sole deterministic
constructor of an inspectable Build composition"), the capability intersection
that can only narrow, and the `build-composition.v1` / `run-admission.v1`
identity schemes bound to a durable `agent.run-intent.*` event. Ten
`test_build_platform_*.py` suites exist, including authority-boundary and
non-web conformance — so Tier-A substrate is **[OBSERVED]**, not open work.

**DECISION — two fingerprints, not one.** `scripts/source_fingerprint.py` emits
a `source` digest (behaviour-bearing paths only) and a `tree` digest (every
tracked byte). *Why:* counted soak credit resets on **source** change; a
governance status write must not look like a source change and force an
unnecessary soak restart. Freeze, separately, cares about the whole tree. The
script reads worktree contents directly and never stages or stashes, so
measuring the tree cannot mutate it.

**DECISION — hooks are stdlib-only and run under system `python3`, not
`.venv`.** *Why:* the campaign rebuilds and type-checks the venv; a governance
guard that dies because `.venv` is mid-reinstall is a guard that fails open at
exactly the wrong moment. Verified: system Python 3.14.6.

**Hook schema verified empirically against the installed binary
(Claude Code 2.1.220)** rather than from memory: `PreToolUse` accepts
`hookSpecificOutput.permissionDecision ∈ {allow, deny, ask, defer}` with
`permissionDecisionReason`; `PostToolUse`/`SessionStart` accept
`hookSpecificOutput.additionalContext`; `Stop` accepts top-level
`{"decision":"block","reason":…}` and receives `stop_hook_active`; a matcher is
match-all when absent, `"*"`, or `".*"`; `${CLAUDE_PROJECT_DIR}` is substituted
in hook commands.

**DECISION — the Stop hook reports `stop_hook_active` rather than yielding to
it.** The documented well-behaved pattern is to return success once
`stop_hook_active` is true, but that is precisely the "hand back early"
behaviour the campaign forbids. The hook keeps blocking with an actionable
reason; the harness's own consecutive-block cap remains the safety valve.

**DECISION — the seal has four layers, and the hash gate is the authority.**
Tracked manifest, deterministic gate script, PreToolUse guard, SessionStart
check. The guard's `Bash` detection is deliberately heuristic: the brief
forbids turning this into an adversarial shell-parser project, and
`check_governance_seal.py` catches drift regardless of how a write was spelled.

**DECISION — the overdue-review block covers source paths but exempts the
governance ledgers.** *Why:* the brief requires the review to happen "before the
next source mutation", but blocking every write would deadlock the agent out of
writing the very review that clears the block.

**Seal gate proven across its full state machine** before the guard was
installed: exit 3 (no manifest) → 1 (rebaseline refused without explicit env) →
0 (rebaselined) → 0 (verify) → 1 (drift, named exactly) → 0 (restored).

## 2026-07-25 — Epic 0 closed, Epic 1 opened

**Epic 0 committed as `32a09957`** (85 paths: 19 added, 33 deleted, 33 modified).
The three active product paths were excluded by explicit pathspec and verified
absent from the index before committing.

**DEFECT I INTRODUCED, found and fixed: the Stop gate had no termination bound.**
While trying to prove hook wiring end-to-end, `claude -p` hung in this repository
on even a trivial prompt. Parking `.claude/settings.json` made it return
instantly — so the hooks were the cause. Root cause: the Stop gate blocks
whenever the completion contract is unmet, which is *by definition* true until
Epic 7 closes, so **every** session in this repository became unstoppable,
campaign or not. My earlier decision to "report `stop_hook_active` rather than
yield to it, because the harness cap is the safety valve" was half right: the cap
exists, but reaching it costs a wedged session and a pile of tokens. Corrected to
a bounded refusal — `MAX_CONSECUTIVE_BLOCKS = 3`, then allow with a loud message,
counter reset on allow so the next genuine early hand-back is refused just as
firmly. Proven: block, block, block, allow, then re-armed. Refusing a premature
hand-back must be emphatic, not infinite.

**Hook wiring is now proven end-to-end, not just at the contract level.** Two
real `claude -p` sessions in this worktree attempted to mutate a sealed file —
once via `Edit`/`Write`, once via `Bash` append — and both were blocked by the
PreToolUse guard, in the model's own words. Seal exit 0 afterwards; both files
untouched. Trust was granted through an **isolated** `CLAUDE_CONFIG_DIR` under
the session scratchpad, so the user's `~/.claude.json` was never modified and no
other project was affected.

**INCIDENT — my own e2e probes performed uninstructed writes.** The probe sessions
ran with `--dangerously-skip-permissions`, and beyond the one edit they were asked
to attempt they also: rewrote `CAMPAIGN-STATUS.md`, stripped the comments from
`.serena/project.yml`, and created `scripts/glm_delegate.py` — a 212-line launcher
built against a **local** Ollama endpoint, directly contradicting the mandated
`ollama-cloud` / `https://ollama.com/v1` route. All three were reverted or
removed; none of it is campaign work and none of it was verified by me. *Process
correction:* any future probe session runs read-only or in a disposable copy —
never with write permission in the campaign worktree.

**Epic 1 progress — two named gaps closed, with the causal mechanism identified
rather than guessed.**

1. *Mutable-head fallback.* `collect_browser_evidence` resolves its source as
   `_collected_workspace_dirs.get(cid) or _snapshot_workspace_dir(cid)`. That dict
   is populated only inside `collect_workspace`, which the freeze path
   deliberately bypasses — so the `or` silently resolved to the mutable
   ProjectStore head, which after the kill holds only the pre-run import snapshot.
   Fix: a landed freeze registers the freshly verified immutable version as the
   conversation's workspace directory, making the fallback **unreachable** here
   rather than merely unlikely. Pattern P2.
2. *Browser evidence not clipped to the horizon.* `_referenced_screenshot_paths`
   saw every event, so a screenshot referenced *after* the accepted
   `WorkspaceVersionEvent` was eligible. Fix: `collect_browser_evidence` takes a
   `horizon_seq` and only observations at or before it are eligible; the run.py
   hard-cap call site passes the frozen horizon, and when the freeze does **not**
   land it now claims *no* browser truth at all instead of falling through.
3. *Authority race fence completed.* The existing check covered a new user turn
   and a changed `run_intent_id`. Added **resume** (the run left `PAUSED` before
   the snapshot event) and **agent-view supersession** — a separate authority axis
   the product tracks via `current_workspace_agent_view_id`, which can move while
   the intent id is unchanged. All four now live in one choke point,
   `_freeze_horizon_violation`, so a new race is fenced by adding a case rather
   than by scattering checks.

**DECISION — dropped a test of an unreachable state.** I wrote a case asserting
that an observation with no usable `seq` is dropped by the horizon clip, and it
failed at `_seed_db` with `NOT NULL constraint failed: events.seq`. The durable
log *cannot* produce an unplaceable event. The three-line defensive branch stays
(it is fail-closed and protects direct callers), but asserting a state the schema
forbids is overhardening, so the test was removed rather than contorted into
passing.

**Gates:** full `harness/build_soak/tests/` suite exit 0 (~890 passed, 1 skipped);
`test_freeze_before_kill.py` 11 passed; the committed
`test_progressing_hardcap_freeze_order.py` reproduction still 3 passed.

## 2026-07-25 — Epic 1 complete

**A production-fatal defect in the inherited Ruling-2 code, found by building the
test the acceptance demanded.** `freeze_progressing_workspace` read `status`,
`trigger`, `version_seq`, `run_intent_id`, and `agent_view_id` **directly off raw
SQLite rows**. `collect_events()` returns
`{seq, kind, source, id, created_at, payload}` where `payload` is an unparsed
JSON string — only `seq`/`kind`/`source` are real columns. Every one of those
reads returned `None`, so the pre-kill freeze could never find a durable `PAUSED`
status or a `trigger="PAUSED"` version event: **it would have returned
`FREEZE_TIMEOUT` on 100% of real runs** while the harness reported, truthfully but
uselessly, that no workspace truth was preserved. The whole point of the Ruling-2
work would have silently not happened.

Eleven unit tests passed throughout, because their fake `collect_events` returned
hand-built **flat dicts** — a shape the real producer never emits. This is
recorded as pattern **P11**, and it is the concrete instance of the handoff's own
warning that the tests were "helper-heavy and do not yet prove the full real
`drive_scenario` hard-cap path."

Fix: route both reads through `normalize_events`, the canonical flattener already
used at four other sites in the same adapter. No new contract; a routing fix.

**Proven by revert-check.** After fixing, I re-broke it deliberately. The new
real-path test fails with exactly the production symptom
(`assert 'FREEZE_TIMEOUT' == 'frozen'`) while all eleven fake-based tests stay
green. That is now standing practice: when a fix matters, re-break it and confirm
the test fails for the right reason.

**Epic 1 acceptance — all seven items met.**

| Acceptance item | Evidence |
|---|---|
| Full real process-backend `drive_scenario` positive | `test_progressing_hardcap_freeze_preserves_artifact_and_png_across_kill` — drives real `run_once` against real SQLite rows and a real `ProjectStore` version. Freeze lands; `version_seq`/`tree_digest` match exactly; `index.html` sha256 preserved; PNG bytes preserved; `inspect-trace.json` finalized after kill; `cleanup.container_orphans == 0`; `release_confirmed` true; primary `RUN_TIMEOUT_WHILE_PROGRESSING` retained; exactly **one** kill |
| ProjectStore dedup reuses an existing `version_seq` | `test_deduplicated_version_seq_is_still_a_valid_freeze` — the **event** is the freeze proof, not a numerically new version |
| Bounded pause timeout: kills normally, claims nothing, subordinate `FREEZE_TIMEOUT` | `test_failed_freeze_is_subordinate_and_never_launders_the_primary_verdict` — verdict stays `RUN_TIMEOUT_WHILE_PROGRESSING`, one kill, empty manifest, freeze disclosed with a reason |
| Authority-race negatives: new user message, resume, run intent, agent view | four tests in `test_freeze_before_kill.py`; all four now route through one choke point, `_freeze_horizon_violation` |
| Browser references after the accepted horizon cannot be certified | `test_browser_evidence_after_the_freeze_horizon_is_not_certifiable`, with a control proving the clip is what excludes the late reference |
| Immutable identity/digest checked around collection; symlinks and mutable head fail closed | `test_tampered_immutable_version_makes_the_freeze_fail_closed`; `_verified_workspace_version` rescans size+sha256 and never follows symlinks; a landed freeze registers the verified version so the mutable-head `or` branch is unreachable |
| Product pause/kill APIs and kill semantics unchanged | **zero changes under `packages/`** — every edit is harness-only; kill count asserted as exactly 1 in both the positive and the failure test |

**Gates:** `harness/build_soak/tests/` exit 0 (~890 passed, 1 skipped);
`uv run ruff check harness/build_soak/` **All checks passed**;
`uv run basedpyright` **0 errors, 0 warnings, 0 notes**.

**DECISION — I did not reformat two files I never touched.**
`ruff format --check` also flags `test_fail_closed.py` and
`test_scenario_lifecycle_oracle.py`. I verified by stashing that both were
**already** unformatted on the committed tree, so they are pre-existing and not
part of this coherent package. Reformatting them here would inflate the diff and
mix unrelated churn into a package under review. **Carried to Epic 5**, where
Ruff/format is an explicit gate and must pass tree-wide.

## 2026-07-25 — Epic 3, one-horizon slice (NOT all of Epic 3)

**Confirmed the documented two-horizon mechanism in code, then closed it.**
`AgentLoop._materialize_current_view` computed `consistent_events`, passed them to
`ViewBuilder.build()`, and returned **its own pre-build list** beside the built
View. But `ViewBuilder._build` appends durable events mid-build and re-reads the
log after each one — microcompact tombstones (`view_render.py:839`),
context-compaction snips (`:905`), and a condensation tombstone (`:915`) — then
returns only the View (`:1004`). The local `events` at that return **is** the
horizon the View describes, and it was simply discarded. A caller rendering a
post-condensation View while holding a pre-condensation list sees the replaced
span as still live, which is how the same span gets condensed twice (pattern P5).

**Correction.** `_build` now returns `(View, events)`. `build()` keeps its exact
existing signature and behaviour for all ~10 existing callers; a new
`build_with_horizon()` returns both, and `_materialize_current_view` uses it. One
response, one horizon — enforced by the return type rather than by discipline.

**Proven by revert-check.** With the fix reverted, the new regression fails with
exactly `AssertionError: returned events predate the condensation the View
already reflects`; restored, it passes.

**Gates:** `packages/core` exit 0; `packages/agent-server` exit 0;
`test_loop_condensation.py` 5 passed; Ruff clean and formatted;
`uv run basedpyright` **0 errors, 0 warnings, 0 notes**; `lint-imports`
**2 contracts kept, 0 broken**; generated diagram fresh (exit 0); tool schemas
exit 0.

**Epic 3 remains OPEN.** This slice closes only the horizon family. Raw
DSML/tool-markup rejection, the one bounded repair, the truthful deterministic
fallback, and the "typed constraints survive fallback" item are untouched.

**OPEN FINDING (pre-existing, blocks Epic 5) — the architecture budget gate
fails on the committed tree.** `scripts/check_arch_budget.py` exits **1** with
**26** violations, e.g. `AgentLoop` 2225 LOC > 2028, `ConversationRuntime`
4567 > 3970, `Driver` 1306 > 800, `reduce_progress` 522 > 200.

Characterised rather than assumed:

- my slice adds **zero** new violations — identical list before and after, with
  `AgentLoop` moving 2225 → 2231, already far over cap either way;
- the allowlist `scripts/check_arch_budget.py` is **unchanged since
  `f55efb03`** (stable-main);
- `engine.py` and `driver.py` are **byte-identical to `f55efb03`** yet over cap.

Therefore the gate was **already failing at stable-main**; the campaign branch
did not introduce it. Epic 5 lists "architecture budget" as a required gate, so
this must be resolved there — by honest decomposition or by an explicitly
justified, owner-visible cap rebaseline. It is recorded here so it cannot be
discovered late and quietly waived. Classification: harness/gate truth, not a
product defect. Earliest broken contract: a required gate that does not pass
cannot be treated as "pre-existing therefore green"
(ENGINEERING-STANDARDS §3).

## 2026-07-26 — Owner-authorized acceleration insertion, Package A (F0/F1)

Non-promoting development tooling, authorized by the owner. `CAMPAIGN-PLAN.md`
is deliberately unchanged: this adds no acceptance count and no promotion
requirement.

**Reconciled against Epic 6 first, as instructed.** Delegated inventory plus my
own verification found **no existing canary/pilot/profile mechanism** — Epic 6's
Freeform canary, AppKit canary, and mixed pilot are not implemented as named
mechanisms today; they would be assembled by hand as `--scenario` comma-lists.
So there was nothing to generalize and nothing to supersede. The profile is a
preflight convenience that reuses the governed runner; Epic 6's sequence is
untouched.

**The structural-exclusion problem, found by reading the promotion reader.**
`harness/reliability/run.py::_build_soak_result` discovers evidence with
`sorted(out.rglob("batch-summary.json"), key=mtime)` and reads **the newest
match**. A qualification batch written under that filename, anywhere beneath a
searched root, would therefore be read as governed promotion evidence — and a
*passing* qualification batch is the most dangerous shape, because it looks
like clean evidence. A label or a directory name would not have helped: the
reader never looks at labels.

**Correction:** the batch report filename is now injectable
(`--summary-name`, default unchanged at `batch-summary.json`, validated to
reject separators so `../batch-summary.json` cannot re-enter a parent tree). The
qualification lane passes `profile-summary.json`, so **the name the promotion
reader globs for is never created**. `assert_not_promotion_visible()` re-checks
the tree afterwards, and the receipt records `counts_toward_promotion: false`
for humans.

**Reuse, not a fork.** Selection is a versioned manifest of scenario ids that
already exist in `scenarios_phase4.yaml`; execution is
`harness.build_soak.run`, keeping its oracles, evidence collection,
classification, cleanup, resource admission, and — already present —
`_run_stop_on_non_pass_cohorts` stop-first behaviour. The profile defines no
scenario, assertion, threshold, or oracle of its own, so it cannot drift from
the lane it previews.

**Gates:** `test_qualification_profile.py` **20 passed**; F0 with the freeze
overlay **exit 0** (~900 tests, 1 skipped); `ruff check harness/build_soak/`
**All checks passed**; formatted; `basedpyright` **0 errors, 0 warnings, 0
notes**.

**Acceptance:** deterministic selection+order; unknown / duplicate / missing /
schema-tampered / promotion-claiming manifests all refused **before** provider
spend (exit 78); dry-run makes zero provider calls and still writes a receipt;
same runner path as the full lane; qualification evidence proven un-ingestable
by the real `_build_soak_result`; negative control proves the guard fires if the
promotion name ever leaks back in.

**Not run, deliberately:** no live F1. Per the owner instruction its first live
execution is scheduled on the first clean candidate after Epic 5, immediately
before Epic 6's governed canaries, still at zero promotion credit. Spending it
against knowingly pre-candidate Epic-2/3/4 bytes would prove nothing.

## 2026-07-26 — Acceleration Package C: audit run, and NO custom rule admitted

**Audit (advisory, non-gating, zero promotion credit).** Semgrep Community
Edition **1.145.0**, installed as an isolated `uv tool` so no repository byte
changed — verified: the source fingerprint was identical before and after the
install.

```bash
semgrep scan --metrics=off --config=p/python --json \
  --exclude=archive --exclude=docs/archive --exclude=node_modules --exclude=.venv \
  --exclude=__pycache__ --exclude=cassettes --exclude=evidence \
  --exclude='*.generated.md' --exclude=frontend/dist --exclude=.serena \
  packages/ harness/ scripts/
```

Result: **151 rules over 550 files, 15 findings** (6 ERROR, 7 WARNING, 2 INFO) —
`use-defused-xml-parse` ×3, `insecure-hash-algorithm-md5` ×3,
`subprocess-shell-true` ×3, `insecure-file-permissions` ×3,
`avoid-bind-to-all-interfaces` ×2, `directly-returned-format-string` ×1.
Evidence outside the repo at
`build-platform-campaign-evidence/2026-07-25/semgrep/`. Public packs remain
**advisory and non-gating**; none is treated as a campaign finding.

### DECISION — no custom rule is admitted. The candidate honestly fails the test.

The obvious candidate was the P11 raw-event/normalization recurrence. I
evaluated it against the admission criteria rather than assuming it qualified,
and it fails three of the six.

Investigating it did pay off in a different way: I checked **every** unnormalized
`collect_events()` call site for the same defect. Six exist. Five are safe
because they read only `seq` (a genuine top-level column) or hand the events to
a consumer that normalizes internally. The sixth, `run.py:1190`, reads status and
detail — and is **also correct**, because `_status_value_and_detail`
(`run.py:2012`) parses `payload` as a fallback. No second instance exists.

That verification is exactly what disqualifies the rule:

| Criterion | Verdict |
|---|---|
| Demonstrated recurring defect family | **Pass** — two instances (the P0 in `normalize_event`'s docstring; the freeze bug) |
| Durable invariant expressible with local static structure | **FAIL** |
| Catches the historical bad form | Pass, but only with false positives |
| A legitimate positive control remains accepted | **FAIL** |
| Negligible repo-wide false positives | **FAIL** |
| Not already enforced elsewhere | Pass |

The reason is that the correct and incorrect forms are **syntactically
identical**. `_status_value_and_detail` opens with `event.get("status")` and is
*correct*; `_event_status_value` opened with `event.get("status")` and was
*wrong*. What separates them is whether a payload fallback follows, or whether
the value was normalized upstream — dataflow and semantics, not local structure.
Any rule matching the bad form flags a legitimate positive control.

Writing it anyway would manufacture a gate that fails on correct code, which
trains everyone to ignore it. Per the instruction, the honest outcome is to
record that and add no rule.

### The better remedy, recorded rather than built

The durable fix for P11 is **type-level, not pattern-level**: give the raw
SQLite row shape a distinct type (e.g. `NewType("RawEventRow", dict[str, Any])`)
so `collect_events` returns something structurally different from a normalized
event, and let **basedpyright** — already a required, zero-error gate — refuse
the confusion at every call site. That is stronger than a text pattern, needs no
new tool, and cannot false-positive on a payload-aware reader.

It touches many signatures, so it is deliberately **not** in this bounded
insertion. Carried to `ARCHITECTURE-ROADMAP.md` as post-campaign work.

## 2026-07-26 — Acceleration Package B: failure capsules, and an honest replay boundary

Non-promoting diagnostic tooling. It creates **no** new store, checkpoint
mechanism, product route, or lifecycle — every fact a capsule binds already
exists in the dossier `assemble_dossier` writes, and the one boundary it may
bind to is the accepted event horizon Epic 1 already establishes and proves.

**What a capsule binds.** Schema version and parent run/conversation; the
original classification and failure code; scenario id + `scenario_sha256` + seed
+ surface; provider/model/kernel/mode/assist/autonomous and repo
commit/revision/dirty; the accepted event horizon, paused seq, and the exact
immutable `workspace_version_seq` with its `tree_digest`, file count and byte
total; the per-file sha256 byte manifest; browser references (already clipped to
the horizon by Epic 1, with any symlink refused outright); the dossier's
`evidence_hashes`; and cleanup state. The whole body is sealed with a
`capsule_digest`, so altering any bound field is detected.

**The one boundary rule.** `accepted_boundary()` admits a capsule **only** when
the pre-kill freeze actually landed. A `FREEZE_TIMEOUT` is refused with "no safe
boundary" rather than dressed up as one. There is no attempt to snapshot
mid-provider, interpreter memory, a browser process, or a live sandbox.

**Restoration reads only the exact immutable version** through the store's own
`open_verified_version`, then re-checks every restored byte against the capsule's
manifest. It never reads the mutable ProjectStore head and never follows a
symlink.

**Gates:** 6 capsule tests passed against the **real** `run_once` hard-cap path
with a real `ProjectStore` version (not fixtures); `ruff check harness/build_soak/`
**All checks passed**; formatted; `basedpyright` **0 errors, 0 warnings, 0 notes**.
Negative controls: no-safe-boundary refused; six distinct tampered fields each
refused before any replay spend (version seq, scenario hash, model binding, byte
manifest, event horizon, and the promotion-disclosure flag); a tampered immutable
version fails restoration closed; a capsule tree is proven un-ingestable by the
real `_build_soak_result`.

### LIMITATION — live focused replay is NOT supported, and was not built

The instruction says to record an unsupported live-replay boundary rather than
build toward it. Having checked, it is unsupported, and here is exactly why.

A replay must be a **new isolated diagnostic run** that leaves the original
verdict byte-stable. Restoring the *workspace* is solved — the capsule does it.
Restoring the accepted *durable context* into a **new** conversation is not:
there is no product API to seed prior conversation history into a fresh
conversation. `restore_workspace_version`
(`routes/conversations.py:245`) restores a version into an **existing**
conversation, and no `import_events` / `fork_conversation` / `clone_conversation`
route exists.

That leaves only three routes, and the instruction excludes all three:

1. add a new product API to inject prior events — a new product API;
2. write events straight into the store behind the product — fabricated internal
   state and a second lifecycle owner;
3. re-drive the **original** conversation — mutates the original dossier and its
   verdict.

So Package B ships the capsule schema, integrity checks, exact workspace
restoration, and provider-free proofs, and stops there. This is a truthful
bounded result, not a blocked one. Carried to `ARCHITECTURE-ROADMAP.md`; the
campaign continues.

Consequently the "at most one live focused replay on the first clean post-Epic-5
candidate" allowance is **moot** — there is nothing live to spend it on. The
scheduled F1 profile proof is unaffected.

## 2026-07-26 — Epic 2 opened: design settled against existing code

Resumed Epic 2 immediately after the acceleration insertion. Before writing any
code I reconciled the requirement against what the loop already has, and the
answer changes the design.

**Half of Epic 2 already exists, and must be extended rather than duplicated.**
`KnowledgeEvent` (`packages/core/src/disco/core/events.py:1170`) is already:

- `source: EventSource.SYSTEM` — host-authored;
- `LLMConvertible` — renders into model context as `<knowledge …>`;
- **pinned against condensation** (`view.py:355-369`), its docstring saying
  precisely "pinned against condensation so standing guidance survives a long
  run";
- **deduplicated** by `(scope, sha256(snippet))`, where the first instance stays
  pinned and exact duplicates become forgettable.

That already satisfies, structurally, three Epic-2 acceptance items: it survives
multiple real condensations, it appears once near current context, and duplicate
observations do not grow context. `DatasourceEvent` is even condensation-immune.

**What genuinely does not exist** — confirmed `NOT FOUND` for `ConstraintEvent`,
`RuntimeConstraint`, `HostConstraint` anywhere under `packages/core/src/disco/core/`:

1. a **stable key** (today the identity is an incidental content hash);
2. an explicit **scope** beyond a free-text applicability hint;
3. a **lifetime/expiry**, so a constraint can end;
4. **expiry on backend/capability-generation change**;
5. the rule that a **transient** error must stay retryable and never pin;
6. any **typed producer** — the process-backend host-signal prohibition
   (`packages/tools/src/disco/tools/sandbox/process.py:102`) is today only an
   actionable refusal *string*, which is exactly why condensation could forget it
   in `k460000`.

**DECISION — extend the proven pinned host-authored pattern; do not add a
parallel channel.** A second mechanism for "host facts the model must keep"
would be a competing source of truth (ARCHITECTURE-BOUNDARIES §8) and would
duplicate pinning and dedup logic that already works. The typed constraint
therefore reuses the pinning/dedup path and adds only the missing typed fields
(key, scope, lifetime/expiry, bounded guidance, usable alternative), with the
process-backend prohibition as its first producer.

**Explicitly NOT in scope, per the campaign plan:** no parsing of the English
refusal inside the condenser, and no universal policy engine. The prohibition is
one typed producer; the recovery points at the managed session/process
termination tool the refusal already names (`shell_kill_process`).

This is the design entering implementation. Nothing is claimed done.

## 2026-07-26 — Epic 2 implemented: the typed runtime constraint

Built exactly as the recorded design said: **extend** the proven pinned
host-authored channel, add no parallel one.

**The chain, typed end to end.** The prohibition is declared where the host
actually enforced it and carried verbatim from there — no layer re-derives a
constraint by reading refusal prose:

```text
process.py  host_signal_constraint()      → RuntimeConstraintDeclaration
  → ExecResult.runtime_constraints        (sandbox/base.py, additive+defaulted)
  → ToolOutcome.runtime_constraints       (tools/anatomy.py)
  → ToolResult.runtime_constraints        (core/events.py, host-only lane)
  → observe.py persist_runtime_constraints()
  → RuntimeConstraintEvent                (SYSTEM-sourced, LLMConvertible)
  → view.py _live_runtime_constraint_seqs() → pinned through condensation
```

**Why typed at all.** The refusal already existed as actionable *text*, and Bug
16 in `system.py` shows why that is not enough: the loop drops `content` and
keeps only `error`, so a refusal survives one turn as prose and is forgettable
after that. In `k460000` condensation forgot it at seq 297/298 and the model
repeated the same kill at seq 324.

**Three lifetime rules**, all in one pure function so they are testable without a
loop:

1. **One per key** — only the newest event for a `constraint_key` is live, so
   repeated observations cannot grow context however often the model retries.
2. **Superseded generations expire** — the constraint names
   `sandbox-backend:process`, the *backend kind*, not an instance. Moving to an
   isolated backend (own PID namespace, not routed through this check at all)
   expires it. A prohibition must not outlive the configuration that justified it.
3. **Explicitly lifted constraints drop** — `active=False` retires a key.

**Transient failures never pin.** `RuntimeConstraintDeclaration.transient` is
representable and `persist_runtime_constraints` skips those: a command that may
succeed on retry must stay retryable, and pinning it would turn a blip into a
permanent belief.

**Host authority is structural.** `source` is fixed to `SYSTEM`, and the
declaration rides the same host-only lane as `effect_receipts`, which explicitly
never carries model/domain-controlled data. A model emitting the same words in
ordinary content is a `MessageEvent`, not a constraint, and is not pinned —
proven by test.

**Gates:** `test_runtime_constraints.py` **16 passed**, covering every Epic-2
acceptance item except the live `k460000` run (Epic 4). Revert-check: removing
the pinning line fails exactly the four survival tests and nothing else.
`packages/core` + `packages/tools` exit 0. Frontend `typecheck:build` **exit 0**.
`basedpyright` **0 errors, 0 warnings, 0 notes**. `ruff` clean on all changed
files.

**Frontend contract honoured, not bypassed.** Adding the kind made
`test_event_kind_frontend_contract.py` fail with exactly the right message; the
kind is now classified `suppressed` in `eventDisposition.ts` (model-context only,
not a user card) and the contract is green again.

**DECISION — I reverted two files my own blanket `ruff --fix` had touched.**
`test_driver_outage_meta.py` and `test_release_detect.py` were already
ruff-dirty on the committed tree; auto-fixing them here would have mixed
unrelated churn into a package under review. Reverted, and the debt is recorded
below.

**OPEN FINDING (pre-existing, Epic 5), now quantified.** The config-driven
`uv run ruff check` gate exits **1** on the committed tree: **3 × E501** in
`packages/core/tests/test_driver_outage_meta.py` and
`packages/core/tests/test_release_detect.py`. Trivial, but Epic 5 requires the
gate to pass, so it is listed with the architecture-budget debt rather than
discovered late.

**Epic 2 remaining:** the deterministic loop test proving the prohibited action
class is not repeated after condensation, and the live `k460000` confirmation
(which belongs to Epic 4's diagnostics on final bytes).

## 2026-07-26 — Epic 3 completed: malformed summaries rejected, repaired once, or told truthfully

The summary path had **exactly one** check before this: `if not summary.strip()`.
Anything non-empty was persisted verbatim into a `CondensationEvent` and replayed
to the model as conversation — which is how provider tool-call protocol markup
ended up in `k460000`'s context.

**Finding that shaped the rule: "DSML" does not exist in this codebase.** The
term appears only in governance prose; there is no `<function_calls>`, `<antml`,
or `<invoke` literal anywhere in code. What actually leaks is XML-ish tool-call
residue such as `</parameter>` — and `openai_provider.py:782-801` already strips
exactly that on the tool-**argument** path. The summary path simply never
checked.

**So the rule is narrow by design.** `summary_rejection_reason()` matches a short
literal list of delimiters that carry no meaning outside a tool-call protocol. It
deliberately does **not** reject generic angle brackets, because a real summary of
a React build contains `<div>` and `<Button />`, and a shell summary contains
`2>&1`. Six overhardening controls assert exactly those stay allowed, including
`</section>` and `<svg viewBox=…>`. A condenser that rejects honest summaries is
worse than one that occasionally passes junk.

**One repair, then the truth.** A rejected summary triggers exactly one corrective
re-ask; a summarizer that emits protocol markup tends to emit it again, and an
unbounded retry burns the very context budget condensation exists to protect. If
the repair is also unusable, the host writes its own summary asserting only what
it actually knows — the exact dropped seq range — and states plainly that the
work in that span is not summarized, telling the reader to treat the range as
*unknown rather than as "nothing happened"*. Claiming a summary we do not have
would be worse than admitting the gap.

**Gates:** `test_summary_validation.py` **19 passed**; `packages/core` exit 0;
`ruff` clean; `basedpyright` **0/0/0**. Revert-check: removing the validation
block fails exactly the four behavioural tests and leaves the rule/fallback unit
tests passing, which is the correct blast radius.

**Defect caught during implementation:** I referenced `_LOG` in `view.py`, which
had no logger defined — the import still succeeded because the references sit
inside a function body, so it would have `NameError`d at the first rejection.
Logger added.

**Epic 3 acceptance — all items met** (a span condensed once, via `fad36450`;
valid summaries unchanged; protocol markup rejected and never persisted; one
repair accepted; two failures → one truthful fallback with no loop; the fallback
asserts only durable facts; typed constraints survive the fallback; ordinary
HTML/JSX/shell still allowed).

## 2026-07-26 — Epic 4 blocked on infrastructure; Epic 5 taken first (provider-free)

**BLOCKING FINDING, caught before any spend: the live stack on 8000/8800 is not
this worktree's.**

```text
:8000  pid 1983741  /var/home/dylan/projects/disclaude/.venv/... disco.agent_server
:8800  pid 2826117  /var/home/dylan/projects/disclaude/.venv/... disco.app_server
       pid 575166   /var/home/dylan/projects/reliability-kernel-wt/... agent_server
```

Every one of those is a **different checkout**. `/var/home/dylan/projects/disclaude`
is the older shared worktree the brief explicitly says not to touch. Driving
Epic 4's context diagnostics at those ports would have exercised another
worktree's bytes and filed the results as evidence for *this* candidate —
certifying code that was never under test. That is precisely the class of error
the campaign exists to prevent, so it is recorded rather than worked around.

Also: this checkout has **no `.env`**, so it carries no provider binding of its
own.

**Decision, per ENGINEERING-STANDARDS §4 (autonomy):** preserve the fact, advance
provider-free work, repair/reroute the infrastructure, and resume — do not weaken
acceptance and do not claim completion. Epic 5's preflight is entirely
provider-free and already has two known, quantified blockers, so it is taken
first. Epic 4 resumes once a stack is running **from this checkout** on
non-conflicting ports, with its route disclosed and bound in the run manifest.

**Not done, deliberately:** the other worktrees' servers were not stopped,
re-pointed, or otherwise disturbed. They are outside this authorization.

## 2026-07-26 — Epic 5, part 1: Ruff gates green; architecture budget attributed

**Ruff now passes both gates on the whole tree** — `uv run ruff check` **exit 0**
and `uv run ruff format --check` **exit 0**. Three E501s were re-wrapped by hand
(a user-facing string in `lifecycle.py`, a comprehension in
`test_selfhost_e2e.py`, a constructor argument in `test_driver_outage_meta.py`)
and 19 files were formatted. All mechanical and semantics-preserving; `packages/core`
still exit 0 and `basedpyright` still **0/0/0**.

**Architecture budget: attributed line by line rather than lumped together.**
Comparing every flagged symbol against its size at `f55efb03`:

| | count | meaning |
|---|---|---|
| **Pre-existing at stable-main** | **23 / 26** | already over cap before this branch existed |
| **Grew past cap on this branch** | **3 / 26** | this branch is where they crossed |

The pre-existing debt is substantial and long-standing — `ConversationRuntime`
4519 vs a 3970 cap, `Driver` 1306 vs 800, `reduce_progress` 522 vs 200,
`_ContentGateMixin` 1087 vs 800. `scripts/check_arch_budget.py` is unchanged
since `f55efb03`, and its own comments say the caps were "frozen at the measured
post-campaign sizes", so these were already failing when Phase 2 was accepted.

The three this branch grew are small and therefore genuinely fixable here:

| symbol | stable-main | now | cap | over |
|---|---|---|---|---|
| `BrowserHandler` | 753 | 851 | 800 | +51 |
| `StuckDetector` | 744 | 816 | 800 | +16 |
| `make_conversations_router` | 302 | 336 | 321 | +15 |

**I also removed my own contribution to the debt.** My Epic-3 horizon change had
added 6 lines to `AgentLoop` (2225 → 2231) — a capped coordinator. The
explanation belongs in `ViewBuilder.build_with_horizon`'s docstring, not inside a
god-object, so the comment was dropped and `AgentLoop` is now byte-for-byte back
at its stable-main size of **2225** (delta **+0**). A gate whose purpose is to
prevent growth should not be paid with more growth.

**Plan for the rest of Epic 5:** decompose the three that this branch broke —
the gate's whole point is preventing growth, and this branch is where they
crossed — then make an explicit, owner-visible decision on the 23 pre-existing
entries rather than silently waiving them.

## 2026-07-26 — Epic 5, part 2: one of three branch regressions decomposed

**`make_conversations_router` fixed: 336 → 301 (cap 321).** The endpoint mixed
two unrelated concerns — the compose settings that are *gated* while a run is in
flight (model/assist/Deep-Research fields, which move together through one
state-aware check because swapping the brain mid-step is incoherent) and the
settings that are safe to change at any time. Split into
`_apply_gated_compose_settings()` and `_apply_ungated_settings()`. Violations
26 → 25; full `packages/agent-server` suite exit 0, `basedpyright` 0/0/0, ruff
clean.

**`StuckDetector` attempted and REVERTED, deliberately.** The obvious lever was
`_permitted_whole_read_baseline` (45 LOC, and it reads no instance state at all).
Extracting it broke three tests: it turned out to be a `classmethod` whose body
calls five sibling `cls.*` parsing helpers, so the move needed those relationships
untangled too. After two corrective attempts it still failed on an arity mismatch,
so I reverted `stuck.py` to its committed state rather than keep pushing — the
standards are explicit that a package which is still structurally wrong gets
redesigned, not subjected to an endless correction tail. Tests green again after
the revert.

The real finding: `StuckDetector`'s read-churn analysis
(`_redundant_read_coverage` 127 LOC, `_redundant_read_after_churn_nudge` 133,
`_permitted_whole_read_baseline` 45, `_repeated_unchanged_file_read` 64 — 369 LOC
of one cohesive family) wants extracting as a **collaborator**, the pattern this
codebase already uses for `ViewBuilder`, `Observer` and `Valve`. That is a real
refactor with its own test pass, not a line-shaving exercise, and it is only 16
lines over cap. It gets its own focused package rather than being bolted onto a
lint cleanup.

**Remaining architecture-budget work, precisely scoped:**

| item | state |
|---|---|
| `make_conversations_router` 336→301 | **DONE** |
| `StuckDetector` +16 | needs the read-churn collaborator extraction |
| `BrowserHandler` +51 | not yet attempted |
| 23 pre-existing stable-main entries | owner-visible decision still required |

## 2026-07-26 — Epic 5, part 3: all three branch regressions cleared

**Architecture budget 26 → 23.** Every violation this branch introduced is gone;
what remains is exactly the 23 that were already over cap at `stable-main`.

| symbol | before | after | cap |
|---|---|---|---|
| `make_conversations_router` | 336 | **301** | 321 |
| `StuckDetector` | 816 | **694** | 800 |
| `BrowserHandler` | 851 | **637** | 800 |

**`StuckDetector` — the lesson from the first, failed attempt.** Moving one pure
helper out broke three tests. The cluster is mutually referential, and half of it
referenced the class *by name* (`StuckDetector._bounded_decimal`) rather than
through `cls`, so a partial move left dangling attribute lookups on a class that
no longer owned them. Moving all eight read-parsing helpers **together** — none
of which read instance state — keeps every reference internally consistent. What
remains in the class is what belongs there: threshold state and the detection
rules that consult it.

**`BrowserHandler`** gave up its two DOM-analysis helpers (`_visible_dom_text`,
`_count_visible_semantic_elements`, 212 LOC). Both reference nothing at all —
verified by AST before moving — so the move was mechanical.

**No test was lost or weakened**, checked rather than asserted:
`test_browser_daemon.py` has **36 test functions and 7 skip markers both before
and after**, and every changed assertion differs only in its receiver
(`handler._x(page)` → `_x(page)`) with identical arguments and identical expected
values.

**Gates:** `packages/core`, `packages/tools`, `packages/agent-server` all exit 0;
`ruff check` **0**; `ruff format --check` **0**; `basedpyright` **0/0/0**.

**Still open for Epic 5:** the 23 pre-existing entries need an owner-visible
decision (decompose vs. justified rebaseline) — they are `stable-main` debt, not
this branch's, and `ConversationRuntime` alone is 4519 against a 3970 cap. Also
outstanding: the frontend Vitest/G11/Firefox lanes, Export Track-1 + Docker 8/8,
the test-inventory comparison against stable integration, and the whole-diff
practical review.
