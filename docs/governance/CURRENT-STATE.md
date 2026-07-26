# Current State

**Status: MUTABLE.** Concise, evidence-linked description of where this
repository actually is. For the running operational ledger see
[`CAMPAIGN-STATUS.md`](./CAMPAIGN-STATUS.md).

Last updated: **2026-07-25 21:53 CDT / 2026-07-26T02:53:03Z**

## Checkout

This campaign runs in exactly one checkout:

```text
/var/home/dylan/projects/build-platform-core-v1/disclaude
```

| Fact | Value |
|------|-------|
| Branch | `disclaude/build-platform-core-v1` |
| Committed HEAD | `271af2e60062988507984d6df6d64e05877a5bc8` |
| `disclaude/stable-main` | `f55efb03` |
| `disclaude/stabilization-integration` | `f55efb03` |
| `f55efb03` ancestor of HEAD? | **Yes** (verified with `git merge-base --is-ancestor`) |
| Clean candidate | **None yet** |
| Promotion credit on eventual final bytes | **0 / 100** |

Because `f55efb03` is an exact ancestor and remains frozen, final integration
can be `git merge --ff-only` without changing certified bytes.

### Other worktrees — do not touch

- `/var/home/dylan/projects/disclaude` — older shared-repository worktree with
  extensive unrelated dirty reliability work. **Do not reset, clean, stash,
  merge, or modify it.**
- The stabilization integration worktree has an unrelated dirty
  `.serena/project.yml`. **Preserve it.**

## Intentional dirty paths

### Active product work — Ruling-2, unfinished, must not be discarded

```text
M  harness/build_soak/adapters/disco_api.py
M  harness/build_soak/run.py
?? harness/build_soak/tests/test_freeze_before_kill.py
```

Committed `271af2e6` already added the provider-free process-backend
reproduction
`packages/agent-server/tests/test_progressing_hardcap_freeze_order.py`, which
proves:

- kill-first loses a progressing run's changed artifact and PNG;
- pause → durable PAUSED → `WorkspaceVersionEvent(PAUSED)` → exact immutable
  ProjectStore version → ordinary kill preserves both hashes;
- a bounded pause failure claims no snapshot.

The dirty wiring begins integrating that lifecycle into the real hard-cap path
but is **incomplete**:

- browser evidence is not yet clipped to the accepted `WorkspaceVersionEvent`
  horizon;
- the exact verified immutable version directory is not yet registered as the
  workspace source, so browser collection can fall back to the mutable
  ProjectStore head;
- resume and agent-view/run-authority supersession are not completely fenced;
- current tests are helper-heavy and do not yet prove the full real
  `drive_scenario` hard-cap path;
- the standing external ledger predates these dirty bytes.

### Context/governance reset — intentional, do not revert

A repository-context reset was performed immediately before this campaign
resumed. Stale live handoffs, dated "current status" files, superseded work
orders, root-level session findings/specs, and the nonfunctional ten-minute
heartbeat were removed from this worktree; historical archive headers now say
they are historical rather than current operating authority.

Deleted material is preserved **outside** the repository at:

```text
/var/home/dylan/disclaude-context-archive/2026-07-25-pre-reset/
```

Manifest verification: `sha256sum -c MANIFEST.sha256` → **33/33 OK**
(verified 2026-07-25).

**Do not restore these files from Git or from the archive.**

## Verified reset facts

| Claim | Verified |
|-------|----------|
| `.serena/memories/` is empty | Yes — no populated Serena memories |
| No `AGENTS.md` | Yes |
| External archive manifest verifies | Yes — 33/33 OK |
| Obsolete Fable prohibition removed from live directives | Yes — remaining mentions are confined to `archive/`, each carrying an explicit `SUPERSEDED / HISTORICAL … Not a source of current status or operating instructions` header |
| Removed stale handoff/status files not restored | Yes |

## Fingerprints

Computed with `scripts/source_fingerprint.py` (reads worktree contents; never
stages, stashes, or writes).

| Digest | Value | Scope |
|--------|-------|-------|
| source | `sha256:bd1b1bba209e6b9362df1e69e360b0dd6d8b56087b147bfa72eeae81a63c8b98` | `packages/ frontend/ harness/ scripts/` + build config (1929 files) |
| tree | `sha256:448ba2ed4925e3b0c61ff96daae882403bfd08be8833ef209872add8cd87836c` | every tracked/untracked non-ignored file (2531 files) |

A **source** change resets counted soak credit. A governance status write does
not.

## Accepted earlier phases

- **Phase 2** accepted at `f55efb03` — broad tests, frontend, Export Docker 8/8,
  live Freeform/AppKit/MCP, backup/restore.
- **Phase 3** accepted at `bca26cb4` — target-neutral platform contracts and
  synthetic non-web conformance.
- Historical main lane reached **86/86** at `a75f0b79`.
- All four restart shapes have passing historical/diagnostic evidence: counted
  historical `450000` plus diagnostic `450001`–`450003`.

**None of those runs count after governed-byte changes.**

## Known limitations, honestly stated

1. **There is no clean candidate.** The tree carries unfinished Ruling-2 product
   work.
2. **Promotion credit is 0/100.** The historical 98/100 is valuable evidence but
   is not credit after source changes.
3. **The external ledger is behind.** The authoritative campaign ledger
   `/var/home/dylan/build-platform-campaign-evidence/2026-07-21/CAMPAIGN-LEDGER.md`
   ends at **Entry 146** and predates the current dirty Ruling-2 wiring. Current
   status continues in [`CAMPAIGN-STATUS.md`](./CAMPAIGN-STATUS.md); detailed
   runtime evidence is written outside the checkout. Historical evidence is
   never rewritten green.
4. **The existing soak matrix is stale as a counting authority.**
   `…/phase4-codex/soak-matrix-final/SOAK-MATRIX.md` binds older source/scenario
   bytes and describes a local→Podman runtime, while current diagnostics force
   the process backend. A truthful final matrix/manifest must be authored for
   the final bytes before any run counts.
5. **Process backend is not production isolation.** It may count as the
   DEVELOPMENT build-loop stratum *only* if the manifest explicitly says it
   proves no production sandbox isolation. Real Podman/gVisor/release isolation
   proof stays separate and honest.

## Context-pressure configuration

The context-pressure configuration is exactly **24,000** for the two context
scenarios. A config diff proved this is the sole intended scalar change from the
normal 1,000,000 window. **Normal product configuration is not globally reduced
to 24k.**

### Latest context diagnostics

- `k460001` **passed** with 37 real durable condensations.
- `k460000` **failed** `TOOL_ERROR_THRASH` with 72 condensations.

Exact observed chain:

| seq | event |
|-----|-------|
| 258 | attempted raw host-process kill |
| 259 | process backend refused it — host signals are not permitted |
| 297/298 | condensation forgot the refusal span |
| 324 | repeated the same forbidden kill |
| — | the oracle correctly failed the repeated normalized error |

The replacement summaries also contained raw DSML/tool-call protocol markup.

A code-path audit found the same old event span can be condensed twice because
`ViewBuilder.build()` appends condensation/rebuilds while
`_materialize_current_view()` can return pre-build `consistent_events`.

These two families are Epic 2 and Epic 3 in
[`CAMPAIGN-PLAN.md`](./CAMPAIGN-PLAN.md).
