# Campaign Plan — Build Platform Core reliability closeout

**Status: CHANGE-CONTROLLED.** This is the finite current campaign and the sole
work order. Do not build a parallel work-order bureaucracy on top of it.

## Scope boundary

> **No new scenario, threshold, provider, framework requirement, or test count
> may be added unless a demonstrated defect makes the existing contract
> impossible or dishonest.**

Tier A/B/C architecture work (see
[`ARCHITECTURE-ROADMAP.md`](./ARCHITECTURE-ROADMAP.md)) must **not** begin on
this branch before closeout. Pulling it in moves the target and resets the soak.

## The single voluntary stop

The campaign is complete only when **Epics 0–7 satisfy every acceptance item**
and [`CAMPAIGN-STATUS.md`](./CAMPAIGN-STATUS.md) truthfully says so.

A finding, a difficult decision, a failed test, a provider outage, the
completion of one package, an hourly review, or a status report **is not a
stop.** Decide, act, record the decision, continue.

---

## Epic 0 — context/governance reset

Create the canonical governance surface, install and prove the review/seal
hooks, create the fail-closed GLM delegation launcher, and commit **only**
context/governance paths.

**Acceptance**

- [ ] no populated Serena memories;
- [ ] no obsolete Fable prohibition remains as a live directive in tracked
      Markdown;
- [ ] no removed stale handoff/status file is restored;
- [ ] one canonical authority map;
- [ ] immutable standards/boundaries gate and hook are **mutation-proven**
      (one blocked Edit/Write, one blocked Bash write, test state restored);
- [ ] hourly review hook functionally proven at 60 s, then set to 3,600 s;
- [ ] Ollama-only GLM delegation is fail-closed and provider-proven;
- [ ] active product bytes remain preserved.

## Epic 1 — finish freeze-before-kill

Complete the lifecycle:

```text
pause
→ new durable PAUSED after the pre-pause watermark
→ new WorkspaceVersionEvent(trigger=PAUSED) after PAUSED
→ verify/read exactly that immutable version
→ bind browser evidence at or before that event horizon
→ unchanged ordinary /kill
→ final inspect/evidence
```

**Acceptance**

- [ ] full real process-backend `drive_scenario` positive: writes a known
      artifact and PNG, reaches the true progressing hard cap, preserves both
      exact hashes across kill, finalizes inspect after kill, proves zero owned
      resources, retains primary `RUN_TIMEOUT_WHILE_PROGRESSING`;
- [ ] genuine ProjectStore dedup case proves a new event may reuse an existing
      `version_seq`;
- [ ] bounded pause timeout kills normally, claims no workspace/browser truth,
      records subordinate `FREEZE_TIMEOUT` **without laundering the primary
      verdict**;
- [ ] authority-race negatives cover a new user message, resume, run intent, and
      agent-view/run-authority change;
- [ ] browser references **after** the accepted event horizon cannot be
      certified;
- [ ] immutable identity/digest checked around collection; symlinks and mutable
      head fallback **fail closed**;
- [ ] product pause/kill APIs and kill semantics remain unchanged.

## Epic 2 — durable typed runtime constraints

Implement the small **generic** host-authored constraint/recovery directive
contract. The process-backend host-signal prohibition is the first typed
producer; the recovery points toward the managed session/process termination
tool.

Do **not** parse the exact English refusal inside the condenser. Do **not**
build a universal policy engine.

**Acceptance**

- [ ] the process-backend signal prohibition produces exactly one typed
      directive;
- [ ] it survives multiple real condensations and appears once near current
      context;
- [ ] duplicate observations do not grow context;
- [ ] backend/capability-generation change expires it;
- [ ] transient command error remains retryable and unpinned;
- [ ] a model-authored lookalike has **no authority**;
- [ ] it names a usable managed alternative;
- [ ] deterministic loop test proves the prohibited action class is not repeated
      after condensation;
- [ ] live `k460000` no longer repeats the forgotten operation and reaches a
      truthful terminal result.

## Epic 3 — coherent, safe condensation

Fix both demonstrated families:

1. returned View/events share **one** post-condensation horizon, eliminating
   duplicate condensation of the same span;
2. malformed summarizer output is structurally rejected, with one bounded repair
   and a truthful deterministic fallback.

**Acceptance**

- [ ] a span is condensed once;
- [ ] valid existing summaries remain accepted;
- [ ] observed raw DSML/provider tool output is rejected and never persisted or
      shown as summary context;
- [ ] one valid repair is accepted;
- [ ] two invalid responses produce one truthful fallback, no loop;
- [ ] fallback asserts only durable facts and explicitly says unsummarized
      progress was omitted;
- [ ] typed runtime constraints survive fallback;
- [ ] ordinary HTML, JSX, and shell/code snippets remain allowed when they are
      legitimate content.

## Epic 4 — context diagnostics on final correction bytes

From a clean tree:

1. run `460000` and `460001` at exact 24k;
2. **both must pass**;
3. run `460002`–`460009` once each, stopping on first non-pass.

All ten require:

- [ ] `driver_context_window == 24000`;
- [ ] at least one genuine durable condensation;
- [ ] exact config binding with only the intended context-window difference;
- [ ] three bounded ranged reads, offsets, and disclosure hints;
- [ ] bounded rereads/repair/thrash;
- [ ] no raw protocol markup in persisted summaries;
- [ ] exact required workspace/browser evidence;
- [ ] zero resource leaks.

On failure: classify from evidence, fix the earliest general contract, restart
the diagnostic set on new bytes. **Never rerun an unchanged product failure for
luck.**

## Epic 5 — deterministic/integration preflight

On the clean candidate:

- [ ] complete Core tests;
- [ ] complete Agent Server tests;
- [ ] complete Tools tests, including applicable real sandbox integrations;
- [ ] complete Build-soak harness tests;
- [ ] architecture budget, import boundaries, generated diagram, tool schemas,
      Ruff/format, basedpyright;
- [ ] frontend Vitest, shipping TypeScript typecheck, Vite production build,
      G11, frozen Firefox UI lane;
- [ ] Export Track-1 focused gates and real Docker 8/8 lifecycle lane on the
      **same** candidate;
- [ ] test-inventory comparison against stable integration: no removed test, no
      pass→skip, no weakened assertion, no unexplained new skip;
- [ ] one whole-diff practical review over `stable-main..candidate` for
      seed/scenario/model/error-string special cases, weakened oracles, hidden
      web assumptions, isolation/auth/cleanup/AppKit regression, and
      future-target compatibility.

One practical reviewer and **one** correction pass maximum per coherent package.
A materially wrong package is redesigned, not subjected to endless edge attacks.

## Epic 6 — qualification and exact 100

On unchanged final candidate bytes, in order:

1. fresh Freeform canary: **1/1**;
2. fresh AppKit canary: **1/1**;
3. fresh mixed pilot: **10/10**;
4. only then begin promotion at **0/100**.

**Exact promotion**

- main: 86 trials, maximum four concurrent, cohort stop-first;
- restart: 4 trials, serial;
- context: 10 trials, two workers, exact 24k config;
- exact manifest-bound product driver/model/provider; fallback disabled;
- **100 PASS / 0 non-PASS on one unchanged candidate.**

**The 86 main allocation**

```text
static_basic 8      react_basic 8       node_basic 10     python_basic 10
static_steer 2      react_steer 2       node_pause 3      python_pause 3
static_continue 3   react_continue 3                      python_cancel 1
static_cancel 1
import_basic 2      import_rollback 2
appkit_create 12    appkit_semantic 8   appkit_rollback 4  appkit_strict 4
```

Restart seeds: `450000` React, `450001` Node, `450002`–`450003` AppKit.
Context seeds: `460000`–`460009` alternating catalog/ledger.

Before starting, prove provider capacity and disk/RAM headroom. **A provider
outage does not become a product failure and does not authorize weakening the
gate.** The historical 98/100 is valuable evidence but not final credit after
source changes.

GLM/Ollama delegation is **separate** from the product-under-test driver. Do not
silently change the soak driver merely because subagents use GLM. The final
matrix must name the exact product driver route. If a provider route changes
before the run: disclose it, bind it, run fresh qualification, and start
promotion at zero.

## Epic 7 — same-byte closeout and local integration

After genuine 100/100:

- [ ] make **no** repository changes;
- [ ] offline-revalidate every counted dossier and manifest;
- [ ] prove source/config stability;
- [ ] prove all exact 100 cells occur once and diagnostics were not counted;
- [ ] preserve all historical non-pass attempts honestly;
- [ ] write the final report **outside** the checkout;
- [ ] if `stable-main` is still the exact ancestor, fast-forward it locally with
      `git merge --ff-only` to the certified commit;
- [ ] run only nonmutating startup/health/evidence checks after the
      fast-forward.

If `stable-main` moved, or a content-changing merge is required: reconcile
first, then rerun preflight **plus** exact 100 on the actual integrated bytes.

---

## Authorization boundary

**Authorized:** local edits, tests, commits, evidence, provider-free
reproductions, soaks, and a local fast-forward of `stable-main` after same-byte
certification.

**Not authorized:** push, publish, sign, tag, destroying external state, or
overwriting another worktree.
