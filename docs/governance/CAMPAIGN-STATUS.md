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

**Last updated (local + UTC):** 2026-07-25 21:53 CDT / 2026-07-26T02:53:03Z

**Current branch / HEAD / source fingerprint:**
`disclaude/build-platform-core-v1` / `271af2e60062988507984d6df6d64e05877a5bc8` /
`source sha256:bd1b1bba209e6b9362df1e69e360b0dd6d8b56087b147bfa72eeae81a63c8b98`
(tree `sha256:448ba2ed4925e3b0c61ff96daae882403bfd08be8833ef209872add8cd87836c`)

**Tree cleanliness and every intentional dirty path:**
Not clean. Every dirty path is intentional and preserved.

*Active product work (Ruling-2, unfinished — must not be discarded):*
```text
M  harness/build_soak/adapters/disco_api.py
M  harness/build_soak/run.py
?? harness/build_soak/tests/test_freeze_before_kill.py
```

*Context/governance reset (intentional — must not be reverted):* 34 modified /
33 deleted tracked paths, plus the new governance surface being added in Epic 0.

**Current epic and package:** Epic 0 — context/governance reset.
Package: governance surface + seal + hourly-review hooks + GLM launcher.

**Current operation:** none running. (No background PID; no live model run.)

**Promotion count on current bytes:** **0 / 100.** No clean candidate exists.

**Completed epics/packages with evidence links:** none yet.

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

**Broad/live gates and exact results:** none run yet on this tree.

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

**GLM delegations (provider/model receipt + output + verified value):** none
yet — launcher is being created in this package.

**Next three concrete actions:**
1. Prove the PreToolUse seal guard and the hourly-review hook functionally
   (blocked Edit/Write, blocked Bash write, 60 s due-injection and completed
   review acknowledgement), then set the interval to 3,600 s.
2. Create and fail-closed verify the Ollama-only GLM 5.2 OpenCode launcher.
3. Reconcile root `CLAUDE.md` into a bootstrap and commit **only**
   context/governance paths — never the three active product paths.

**Known deferred post-campaign work:** everything in
[`ARCHITECTURE-ROADMAP.md`](./ARCHITECTURE-ROADMAP.md) (Tier A/B/C and the
carried research/owner gates). None of it may begin on this branch before
closeout.

**Completion-contract checklist:**

| Epic | State |
|------|-------|
| 0 — context/governance reset | IN PROGRESS |
| 1 — finish freeze-before-kill | not started |
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
