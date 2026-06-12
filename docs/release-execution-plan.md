# perpleximanus — Release Execution Plan (v0.1 public release)

**Written 2026-06-12.** Reconciles `release-roadmap.md` (compiled 2026-06-09) against the
~158 commits that landed since (BP-00..16, DC-01..07, RP-01..14, df-08, cs-01 — all
`committed` in `orders.yaml`). Supersedes the roadmap's Phase 2/3 sections as the
execution document; the strategy section of the roadmap (§1, the wedge) is unchanged and
still correct. Companion: `next-fix-set-plan.md` (RP pack — now complete),
`harvest-backlog.md` (HS- ledger), `decomplexity-wave-plan.md` (DC wave — complete).

**The wedge, restated:** Manus-grade task UX that is *reliable on self-hosted open
weights*, with a one-command deploy. After the RP pack, the product legs of that sentence
exist. What does NOT exist is the last clause — and the receipts. **This plan is almost
entirely Phase 3 (release engineering) plus the short list of Phase-2 residue that is
user-visible enough to gate a credible v0.1.**

---

## 1. Reconciliation — roadmap line items vs what actually shipped

Classification: **DONE** / **PARTIAL** / **NOT-STARTED**, each with a commit hash or file
path verified against the tree on 2026-06-12.

### Phase 0 + 0.5 (substrate + server cockpit) — DONE

| Item | Status | Evidence |
|---|---|---|
| B1 observation masking | DONE | bp-06 `3faf74a`, `f614556` (view.py) |
| B2/B3/B9 + weak-model FC kit | DONE | rp-12 `be5b4ff` (arg coercion, requery-outside-log, grammar-constrained calls, name sanitization, prefill) |
| B8 persistent IPython kernel | DONE | bp-08 `2b3321a`; `sandbox/kernel.py` |
| S1–S3 shell sessions / preview-as-session / env contract | DONE | bp-01/02/03 (`orders.yaml:22-29`); `sandbox/shell_sessions.py`, `port_owner.py` |
| S4/S5 eyes + verify gate | DONE | bp-00 `2c30a5e` (vision driver e2e), bp-05 `d2f3d1a` (finish-time browser verification), bp-04 `8df4311` (persistent Playwright daemon), df-08 `272f59e` (vision escalation to Gemini 3 Flash) |
| Marathon gate | DONE | bp-16 `1d9a364` Phase A/C PASS; Phase B failure → DC-05 series → live PASS `2b84c62`, `f3d2966`; Wave 0 closed `7a1c302` |
| Loop-discipline residue (livelock, valves, condensation) | DONE | `fcf50cf` (publish-gate + execution-nudge livelock), dc-05a `f129a27`, dc-05b `18b8c5e`, dc-05c `1513070` |

### Phase 1 (table stakes) — DONE

| Item | Status | Evidence |
|---|---|---|
| File upload, both surfaces | DONE | bp-11 `ca5d8dc`; dc-07 uploads re-materialization across sandbox recreate `8398573` + `0c5dbd6`, live acceptance `f3d2966` |
| Vision feedback for the build agent | DONE | bp-00/bp-05/df-08 (above) |
| MCP client | DONE | rp-05a `247b81b` (stdio, pool, approval gate), rp-05b-py `9dce032` (HTTP transport, egress allowlist, output fence, retrieval tier), rp-05b-ui `e021f2f` (live CRUD + approval store), rp-05c `7dc2bf4` (active-schema cap + tool_search split). Full stack: `packages/tools/src/perpleximanus/tools/mcp/` (approval, fence, http_egress, pool, retrieval_tier, tool_search) |
| Clarifying questions + report follow-ups | DONE | rp-13 `9b0658c` |
| Export PDF/DOCX | DONE | rp-07 `0d30b7f` + `accd7ee` (PDF live, capability-gated) + `415aee5` (DOCX in transient jailed sandbox); `agent_server/report_export.py` |
| Usability debt (theme, Cmd+K, history search, cost meter, isolation tier) | DONE | rp-14 `17b5ce0` |

### Phase 2 (differentiators) — MOSTLY DONE, two PARTIAL

| Item | Status | Evidence |
|---|---|---|
| Session replay + share links | DONE (v1) | rp-06 `85cf645`: `useReplay.ts` + `ReplayScrubber.tsx` + `ShareView.tsx`, `share_export` versioned scrubbed bundle (`bundle_version 1` = future cassette format), revocable base62 tokens, `redaction.py`. **Tailnet-only by ratified decision.** Fork/take-over = explicitly v2 |
| Contradiction surfacing UI | DONE (v1) | cs-01 `9475c93` (frontend-only NLI trust-signal surfacing). Credibility-weighted source scoring (roadmap bar item #6) still open — post-v0.1 |
| Wide-research fan-out | DONE (phase 1) | rp-04 `545f7b7` smart-client pipeline (concurrent retrieval, serial MTP synthesis, budget partition, cancel-all). N-item horizontal mode ("compare 20 X") NOT-STARTED — deliberately post-v0.1 (rp-04b profiling rejected at ratification) |
| Artifact engine — charts | DONE | rp-03 `8217e8e` (Chart.js 4, typed schemas, retry-then-degrade-to-table, fuzz suites) |
| Artifact engine — slides | DONE | rp-10 `ef651b4` + `02c156d` (marp runs **inside** the sandbox; Chromium + marp-cli baked into `deploy/sandbox/Dockerfile:21-35`) |
| Artifact engine — sheets | PARTIAL | rp-11 `56c35a0`: live-formula .xlsx via sandbox jail, honest preview card. Univer read-only viewer DEFERRED (packages locked, not wired — `STATUS-2026-06-11-1757.md`), in-block download deferred (needs cid threading) |
| Audio overviews | DONE | rp-09 `db2d0c8` + `9f4f106`/`cac8d90` (**in-process Kokoro ONNX**, lazy/idle-unload — upgraded from the planned Speaches dependency; `agent_server/tts_local.py`), live-verified |
| Scheduled tasks | DONE | rp-08 `f40c949` + `cb86a6e` (`schedule.py`, cronsim, coalesced catch-up, re-injection fix caught in review) |
| Background-task dashboard | PARTIAL | rp-01 `30f09c2` = "dashboard lite": status write-through + History chips + read-repair. No dedicated running-tasks view, no global "N tasks running" indicator, no schedule-run history surface. **The one Phase-2 item still user-visibly open.** |

### Phase 3 (release engineering) — ESSENTIALLY NOT-STARTED

| Item | Status | Evidence |
|---|---|---|
| One-command deploy | NOT-STARTED | `deploy/` contains ONLY `sandbox/Dockerfile`. No compose, no installer. The runbook is the tmux + env-var recipe in `HANDOFF-2026-06-10.md` — tribal knowledge |
| Eval-as-a-feature | PARTIAL (substrate only) | `harness/` (eval_runner.py, cassette.py, faults.py, canary.py, 4 cassettes), `evals/baselines/scorecard.json`, `make eval`/`make canary`. The rp-06 share bundle is the designed cassette format. **Nothing user-facing; nothing scores an arbitrary user model** |
| Security posture doc | PARTIAL | Strong primitives shipped: 4-tier sandbox (`sandbox/{gvisor,podman,local,process}.py`), egress allowlist proxy (`egress_proxy.py`, real on gVisor), MCP security canon (hash-pinned approvals, output fencing, egress-routed HTTP — rp-05b), export redaction (`redaction.py`), jailed renderers (`02c156d`, `415aee5`), secrets at rest moved to XDG (`c1a9345`). But the only doc is `tool-sandbox-contract.md` §7 — no consolidated threat model a stranger can read |
| Docs + gallery | NOT-STARTED | **README.md still says "Status: Phase 0 — the Event & State spine"; `project-status.md` still says "the brain isn't plugged in."** Both are months stale and actively misrepresent the product. No install guide, no provider/VRAM matrix, no demo gallery |
| Versioning / release artifacts | NOT-STARTED | All packages at `0.1.0`, **zero git tags**, no LICENSE file, no CONTRIBUTING, no `.github/` (no CI — `Makefile` header says "no CI; this IS the runner") |
| Mobile pass | NOT-STARTED | — |
| Windows packaging | NOT-STARTED | Zero Windows-targeted code. Hard POSIX deps: tmux-backed kernel/sessions (`sandbox/kernel.py`, `process.py`), gVisor/podman backends are Linux-only, ssh-socket sandbox transport |

### Standing debt (from `pending-items-status.md`, memory, and the HS ledger)

| Item | Status | Evidence |
|---|---|---|
| E3 `</parameter>` leak root cause | PARTIAL | Mitigated (`openai_provider.py` `_raw` fallback) + rp-04's stray-XML strip (`545f7b7`); root cause never isolated |
| Real-sample harness backfill | PARTIAL | Streaming/provider/DR-lifecycle harnesses still use `_ScriptedRouter`/synthetic docs (pending-items "Process debt"); rp-06 bundle format exists to fix this but no backfill order ran |
| HS- harvest backlog | NOT-STARTED | `harvest-backlog.md`: HS-01 spill-to-file, HS-02/03/05 reality-block cluster, HS-04 stuck-detector upgrade, HS-06 (design-blocked), HS-07, HS-08 |
| First-hit wake race; ruff/flake debt | OPEN | memory `perpleximanus-open-gaps` (post-BP residue list) |
| Stale strategy docs | OPEN | README, project-status.md, and the now-superseded roadmap phases need a truth pass |

**Bottom line of the reconciliation:** the roadmap's Phase 0/0.5/1 are complete, Phase 2
is complete except the background-task dashboard and two small artifact-polish deferrals,
and Phase 3 — the phase the RP plan *deliberately excluded* ("packaging a product that
still lacks the artifact/replay/MCP legs would ship the wrong thing") — is now unblocked
and is the entire remaining road. The product exists; the release does not.

---

## 2. The plan — four waves

Sequencing principle: **truth → proof → package → polish.** Within a wave, items are
parallel-dispatchable to the worker fleet under the existing orchestrate.sh + ROUTING.md
discipline. Effort: S ≤ 1 order-day, M = 2-4, L = a multi-rung campaign. Risk axis named
per item.

---

### Wave A — Truth & residue (make the tree honest before packaging it)

#### A1. Doc truth pass (README, project-status, roadmap supersession)
**What & why:** README announces Phase 0; project-status says no model is wired. A public
repo shipping those docs torpedoes credibility on first contact — worse than no docs.
Rewrite README around the wedge identity (what it is, what it runs on, honest status),
retire/`docs/archive/` the stale snapshots, mark the roadmap's Phase 2/3 as superseded by
this plan. This is also where the v0.1 scope line gets written down publicly (what's in,
what's explicitly post-release).
**Class:** NOT-STARTED (README.md:12, project-status.md:9-16 stale). **Effort:** S.
**Risk:** none (docs). **Deps:** none.
**First step:** rewrite `README.md` top section from the reconciliation table in §1 of
this plan; `git mv project-status.md docs/archive/`.

#### A2. Reliability residue batch (E3 root cause, wake race, lint debt)
**What & why:** Three known opens that are cheap now and embarrassing in a public issue
tracker later: isolate the E3 `</parameter>` leak root cause (the mitigations work but
the wedge story is "we root-cause weak-model failure modes"), fix the first-hit wake
race, clear the ruff/flake debt so CI (D4) can land green from day one.
**Class:** PARTIAL (mitigations in `openai_provider.py`; memory `perpleximanus-open-gaps`).
**Effort:** S-M. **Risk:** engine-loop (E3 touches the provider parse path — full
regression suite mandatory). **Deps:** none.
**First step:** reproduce E3 with the captured leak samples under the rp-12 requery
harness; bisect whether it is template echo vs stop-token truncation.

#### A3. Harvest steals that strengthen the wedge (HS-01, HS-04, HS-08)
**What & why:** Three S-effort, source-verified mechanisms that compose with the shipped
rp-12 FC kit and directly harden the "reliable on open weights" claim the eval harness
(Wave C) will publicly measure: HS-01 shell spill-to-file with prescriptive truncation
markers, HS-04 stuck-detector scenario upgrade (n-gram similarity + OpenHands scenarios
feeding OUR graduated valve), HS-08 hidden-`invalid`-tool repair (verify-overlap with
rp-12 first per the ledger). HS-02/03/05 (reality-block cluster) stay post-v0.1 unless
Wave C's eval runs expose them.
**Class:** NOT-STARTED (`harvest-backlog.md`). **Effort:** S each. **Risk:** engine-loop.
**Deps:** none; parallel with A1/A2.
**First step:** the ledger's own instruction — diff HS-04/HS-08 against current
`core/loop/stuck.py` and the rp-12 requery path before writing briefs.

---

### Wave B — Phase-2 closure (the user-visible gaps that gate "credible")

#### B1. Background-task dashboard (the real one)
**What & why:** The last open Phase-2 differentiator. The backend is fully ready:
status write-through (rp-01), task-as-resource lifecycle, idle-suspend badges (bp-13/
dc-02), schedule runs (rp-08). What's missing is the surface: a Tasks view listing
running/scheduled/suspended/finished tasks across both surfaces, a global "N running"
indicator in the shell, schedule-run history, and jump-to-task. Without it, scheduled
tasks (shipped) are half-invisible — a schedule fires and nothing in the shell tells you.
This is the "parallel-task awareness" gap the roadmap ranked #3 in user-felt pain.
**Class:** PARTIAL (rp-01 `30f09c2` = chips only). **Effort:** M. **Risk:** UX (pure
projection over existing APIs — no engine changes; false-affordance rule applies to any
action buttons). **Deps:** none (backend done).
**First step:** inventory what `GET /api/conversations` + the status column + the
`schedule_runs` table already expose; spec the Tasks view as projection-only, then a
DeepSeek-drafted frontend order with a Playwright live spec.

#### B2. Artifact polish: sheet viewer + in-block downloads
**What & why:** Closes rp-11's two honest deferrals so the artifact story demos clean in
the gallery: a read-only grid preview for .xlsx (Univer if its embed slimmed down,
else a ~100-line static grid renderer of openpyxl-extracted values — flagged
"formulas not evaluated" honestly), and thread `cid` through `BlockView` so artifact
blocks can offer real downloads instead of pointing at the deliverable panel.
**Class:** PARTIAL (`STATUS-2026-06-11-1757.md` deferrals). **Effort:** S-M. **Risk:** UX.
**Deps:** none.
**First step:** time-box a Univer 0.25 read-only embed spike to half a day; if it fights
back, build the static grid.

#### B3. Session portability: bundle import (replay polish)
**What & why:** rp-06 shipped export + ShareView; the import half — open a `.pmx-bundle`
from someone else's instance in your own UI — is what makes "session portability" true
(roadmap field-gap #3) and turns shared bundles into the demo-gallery format AND the
eval-cassette format (one projection, three consumers — the RP-00 locked synergy). v1
stays read-only; fork/take-over remains v2 post-release.
**Class:** PARTIAL (export DONE `85cf645`; import NOT-STARTED). **Effort:** S-M.
**Risk:** security (bundle is untrusted input — version-guard + schema-validate + treat
all content as fenced; no event replay into a live runtime). **Deps:** none; feeds C1, D5.
**First step:** add `bundle_version` validation + a static import route that feeds
ShareView from a local file instead of a share token.

---

### Wave C — Eval-as-a-feature (prove the wedge; nobody else can ship this)

#### C1. Cassette consolidation + real-sample backfill
**What & why:** Unify the harness's cassette layer on the rp-06 bundle format (it was
designed as "bundle_version 1 = future cassette format" — collect on that decision),
and retire the synthetic `_ScriptedRouter` harnesses flagged in pending-items by
re-capturing from live runs. This is the substrate the user-facing verifier replays, and
it pays the standing real-sample-harness debt in the same motion.
**Class:** PARTIAL (`harness/cassette.py` + 4 cassettes exist on the old format).
**Effort:** M. **Risk:** infra (capture needs the live stack; schedule around model
serving). **Deps:** B3 (import/validation code is shared).
**First step:** write the bundle→cassette adapter and re-run `make eval` against the
existing `research_demo` capture re-exported through `share_export`.

#### C2. "Verify your setup" — the user-runnable reliability scorecard
**What & why:** The wedge is "reliable on open weights"; the field study found NO project
lets a self-hoster verify their model/provider combo (field-gap #6). We have everything
needed: eval_runner, fault injection, the marathon harness, baselines/scorecard.json, and
now a portable cassette format. Ship `pmx verify` (CLI first, Settings panel second):
runs a graded battery against the user's configured driver — tool-call correctness under
the FC kit, loop discipline (no actionless spirals), grounding faithfulness vs baseline,
restart/resume survival (the marathon Phase-B scenario, time-boxed), artifact generation
smoke — and emits a per-capability pass/degrade/fail scorecard with the measured numbers.
The same scorecards, run against Qwen 27B/35B/the usual suspects, become the
recommended-model table in the docs (D5) — receipts, not vibes. This is the single
feature that converts the engineering campaign of the last month into a public claim.
**Class:** NOT-STARTED as a feature (substrate PARTIAL: `harness/eval_runner.py`,
`harness/faults.py`, `evals/baselines/scorecard.json`, `Makefile` eval/canary targets).
**Effort:** L. **Risk:** engine-loop adjacent (read-only against the driver, but the
battery must be honest about variance — pin seeds/sampling where the server allows; a
flaky verifier destroys the trust story it exists to build). **Deps:** C1.
**First step:** define the capability taxonomy + scorecard JSON schema (extend
`evals/baselines/scorecard.json`), then wrap the existing eval_runner + 2 cassettes
behind a `pmx verify` entry point and run it against the local 27B as the reference
baseline.

---

### Wave D — Release engineering (the last mile)

#### D1. One-command deploy (compose) — **the keystone**
**What & why:** `docker compose up` → app-server + agent-server + built frontend +
sandbox image + keyless defaults working with zero accounts. The keyless tier already
exists in code (`bundled_providers.py`: ddgs search / local extraction / bundled
encoders — built for exactly this); the blocker is that the runtime recipe lives in
HANDOFF-2026-06-10.md as tmux + env vars. Compose forces the config consolidation
(PMX_* env surface → one documented `.env`), gives healthchecks (the observability
floor), and is the gating dependency for the eval feature's distribution, the gallery,
the security doc's claims, and the entire Windows answer. Sandbox tiering in compose:
default = podman/docker-in-docker `process`→`podman` tier with the honest-labeling UI we
already have; gVisor documented as the recommended hardened tier (host-level runsc
install, can't be composed in). Footprint target: the R9700 workstation reference
deploy — services are CPU-side (encoders bundled or remote, Kokoro is in-process ONNX
CPU), so the budget is RAM/disk, not VRAM; measure and publish it.
**Class:** NOT-STARTED (`deploy/` = sandbox Dockerfile only). **Effort:** L.
**Risk:** infra + security (compose networking must preserve the egress-proxy and
loopback-only guarantees; remember the three gVisor/Docker networking gotchas from the
egress-proxy memory: no NIC hot-plug, dead embedded DNS, reach-by-IP). **Deps:** A1
(README install section), feeds D2/D5/D6.
**First step:** write the inventory: every PMX_* env var, port, volume, and external
service the two servers currently require (grep `os.environ`/`PMX_` across packages);
that inventory IS the compose spec.

#### D2. Release artifacts: sandbox image publish + version pinning
**What & why:** The sandbox image (Node 22 + Playwright/Chromium + marp + pandoc) is now
a multi-GB build that took multiple VM-201 rebuild rungs to stabilize — strangers must
pull it, not build it. Publish to GHCR with digest pinning from compose; consider a
`-lite` variant (no Chromium/pandoc → no slides/PDF, capability-gated honestly — the
capability-gating code already exists from rp-07/rp-10). Tag v0.1.0 across the five
packages, generate the changelog from the order history.
**Class:** NOT-STARTED (no tags, no registry). **Effort:** M. **Risk:** infra.
**Deps:** D1. **First step:** `docker build` the current Dockerfile, record size, and
draft the full-vs-lite layer split.

#### D3. Security posture doc (SECURITY.md + threat model)
**What & why:** The primitives are genuinely ahead of the field — 4-tier sandbox, real
egress allowlisting on gVisor, MCP approval canon (hash-pinned descriptions, output
fencing, egress-routed HTTP), jailed renderers, export redaction, XDG secrets — but the
only written artifact is a contract doc §7. Consolidate into a reader-facing threat
model: what each sandbox tier does/doesn't guarantee (verbatim honest, including
podman/local = sealed deny-all not proxied allowlist), prompt-injection posture
(quarantined browser content, MCP fencing), what tailnet-only share links mean, secrets
handling, the loopback-only server rule, port 8899 internality, and a disclosure policy.
Leading with a real security story is differentiation, not paperwork — no OSS competitor
has one.
**Class:** PARTIAL (primitives DONE, doc NOT-STARTED). **Effort:** S-M (writing +
verification of every claim against code — every sentence needs a file:line).
**Risk:** security (an overclaiming security doc is worse than none). **Deps:** D1
(the doc describes the deployed topology).
**First step:** outline from `tool-sandbox-contract.md` §7 + the rp-05b security canon
list in `next-fix-set-plan.md`, then claim-by-claim code verification.

#### D4. Repo hygiene: LICENSE, CI, CONTRIBUTING
**What & why:** No LICENSE = legally not open source = nothing else matters; pick it
(license choice is a Dylan decision — flag AGPL-vs-Apache tradeoff explicitly given the
self-host wedge). CI: GitHub Actions running `make test` (hermetic by design — the
Makefile's fast path needs no model) + frontend vitest/build/lint; the live/eval rungs
stay local-only, documented as such. CONTRIBUTING distills the house verification
discipline (real-sample harnesses, visual evidence, no cheap workarounds) — it's a
genuine differentiator in contributor quality.
**Class:** NOT-STARTED. **Effort:** S. **Risk:** none. **Deps:** A2 (lint debt cleared
so CI lands green).
**First step:** Dylan picks the license; scaffold `.github/workflows/test.yml` around
`make test` + `cd frontend && npm test`.

#### D5. Docs + demo gallery
**What & why:** The stranger's first 15 minutes: install guide (compose), provider
matrix with VRAM tiers (AgenticSeek-style honesty), the recommended-model table fed by
C2's measured scorecards, and a demo gallery of exported share bundles (B3 format)
showing a real build replay, a deep report with charts/contradiction callouts, slides,
a sheet, an audio overview. The gallery is the marketing artifact and it's free — the
bundles are just saved real runs.
**Class:** NOT-STARTED. **Effort:** M. **Risk:** UX. **Deps:** D1 (install), C2
(model table), B3 (bundle import for gallery viewing).
**First step:** capture three flagship runs as share bundles while doing C1's capture
work (same sessions, two consumers).

#### D6. Windows + mobile story (documented, not engineered)
**What & why:** Windows native is **not viable for v0.1 and shouldn't be attempted**:
the runtime is structurally POSIX (tmux-backed kernel/shell sessions in
`sandbox/kernel.py`/`process.py`, gVisor/podman Linux-only, ssh sandbox transport).
The honest, near-free answer is the one D1 produces: **Docker Desktop / WSL2 runs the
compose stack unchanged** — verify once on a Windows box or VM, document it as the
supported path, state native as a non-goal. Mobile: one responsive pass on
research/history/tasks surfaces; build inspector stays desktop-first, labeled.
**Class:** NOT-STARTED. **Effort:** S (docs + one verification pass each).
**Risk:** UX. **Deps:** D1.
**First step:** boot the compose stack under WSL2, run the C2 verifier inside it, record
what breaks (expected suspects: ddgs egress through Docker Desktop's proxy, volume
performance).

---

## 3. Explicitly post-v0.1 (cut from this release, on the record)

- Wide-research **N-item horizontal mode** (rp-04 phase 2) — tiers already honest about it.
- Replay **fork/take-over** (rp-06 v2) and internet-public share links (CF Access pattern).
- Credibility-weighted source scoring (bar item #6 beyond cs-01).
- Design-reference pack (next-fix-set §5) — strong fast-follow; tier 1 (style-preset
  skills) is a good first post-release order.
- HS-02/03/05 reality-block cluster + HS-06 (design-blocked on the masking-epoch call) +
  HS-07.
- Messaging triggers (Telegram/mail), GitHub two-way sync, chat-vs-agent cheap routing,
  cross-session memory (still gated on the 27B A/B evidence rule), native apps.

---

## 4. Highest-leverage next move

**D1 — the one-command compose deploy.** Reasoning: every other remaining item is either
blocked by it or amplified by it. The eval feature (C2) only becomes the public proof of
the wedge if a stranger can stand the stack up to run it; the security doc (D3) describes
a deployment that must exist reproducibly; the gallery (D5) and the Windows answer (D6)
are direct dependents; and the work itself flushes the config sprawl that is currently
the single biggest gap between "works in Dylan's tmux" and "works anywhere" — which is
precisely the failure mode the field study holds against Suna. The product differentiators
are done; the adoption determinant is not. Start with the PMX_* inventory (D1 first step)
while Wave A runs in parallel on the worker fleet — they don't collide.

## 5. Critical path to a credible v0.1

1. **D1 + D2** — compose up on a clean machine, pulling a published sandbox image,
   keyless defaults answering a real query with zero accounts.
2. **C1 + C2** — `pmx verify` produces an honest per-capability scorecard against the
   user's own model; our reference scorecards (27B Q5, 35B-A3B) published.
3. **B1** — the background-task dashboard, so the shipped lifecycle/scheduler machinery
   is visible product, not latent backend.
4. **D3 + A1** — SECURITY.md + the doc truth pass; the repo's words match the code.
5. **D4 + D5** — LICENSE/CI/CONTRIBUTING + install docs and a share-bundle demo gallery.

Gate for calling it v0.1: a stranger on a clean Linux box (or WSL2) runs two commands,
asks one research question and one build task with no API keys, watches the build verify
itself, runs `pmx verify` against their own model, and reads honest docs about what the
sandbox does and doesn't guarantee. Everything in that sentence except the compose file
and the verifier already exists in the tree.
