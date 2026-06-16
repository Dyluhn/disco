# Disco — Remaining-Work Plan (compiled 2026-06-13)

Consolidated from: `north-star.md` (the guiding doc), the three reconciliation
ledgers (`fable-plan-reconciliation.md`, RP-pack, decomplexity-wave), the live task
list, and this session's adversarial-review findings. SmallCode (Doorman11991)
harvest items will be appended once the 3 reviews land.

**The anchor (North Star §8):** *a stranger installs Disco with one command on a
clean 8 GB box, keyless, and uses every surface without troubleshooting.* Tracks are
ordered by distance to that bar.

Effort key: **S** ≤ half-day · **M** 1–2 days · **L** 3+ days. Priority: **P0**
blocks the stated goal · **P1** product-quality moat · **P2** feature completeness ·
**P3** polish/debt.

---

## ⚠ Verification status (2026-06-13)
This plan was compiled from docs spanning Jun 7–12 of varying freshness. The
items from **today's** code-verified reconciliations (fable/RP/decomplexity) +
this session's reviews are trustworthy. The items bolted on from **older docs**
(pending-items 06-08, build-parity, release-execution, harvest-backlog) were
re-verified against the LIVE code and several were **already done, obsolete, or
deliberately rejected** — struck through below. **Pruned in this pass:** A7
(README/matrix shipped `fba3b81`/`359442e`), C19 (violates the no-automatic-nudge
invariant `c97c1b3` — would BREAK), BP-G1/G4 (`shell_sessions.py` done), BP-G2/G3
(DC-01 `host_proxy.py` superseded the keepalive model), HS-04 (`stuck.py` scenarios
done); BP-G5/G6→D8 and HS-05→C1 deduped. **Rule: any item not from today's fresh
reconciliation must be re-checked against current code before a brief is written**
(some remaining rows are tagged "VERIFY at impl"). Verified-still-real: D6, HS-01,
F1, BP-G10 (flip-only), C9.

## ✓ BY-HAND VERIFICATION (2026-06-13) — authoritative disposition
Every item below re-checked against LIVE code (file:line). This supersedes the
track tables where they disagree. **This is the list the harness plans from.**

**REAL & OPEN (verified, do these):**
- A1 (no `tsconfig.build.json`; **36** tsc errors today) · A2 · A3 · A4 · A5 (ruff debt confirmed) · A6 (`PMX_` still the prefix)
- B1 (encoders HARD-CODED to e5-large/jina-v2, no env knob) · B2 (ctx still 32K, no KV-quant) · B3 (no OOM guard) · B4 · B5 (**3,088** leaked dirs)
- C1 **B7 evaluator — ZERO code (headline)** · C2 · C3 · C6 (recitation fires every step) · C8 · C9 · C10 · C14 (shared router confirmed) · C15 · C16 · C20 · C21
- D2 · D3 · D4 · D6 (no DatasourceEvent producer) · D7 · **D8** (screenshot/vision gate = absorbs BP-G5/G6) · D9 (no image-gen) · D10 · D11 · D12
- E1 · E2 · E3 · E4 · E5 (no docker-wedge guard) · E6 · E7 · E8 (podman/local SEALED, not proxied)
- F1 (no tool-call text/`reasoning_content` fallback — the verified headline) · F2 · F3 (no read-before-write) · F4 (no bootstrap) · F5 · F6 · F7 · F8 — **all behind the F-gate**
- G HS-01 (no shell spill-to-file) · HS-03 (verify) · HS-07
- H BP-G9 (no multi-service) · BP-G10 (flip Build egress open→filtered; infra exists) · BP-G12

**PARTIAL — reduced to the named residual only:**
- C5 (spill-to-disk + MEMORY.md-style View recall EXIST → confirm residual) · C7 (temp-jitter DONE → only serialization-seed jitter + nudge-pool) · C11 (tombstones/microcompact exist → on-demand recovery) · C13 (prefill infra exists; full-lifecycle is a gated experiment — LOW) · C18 (model-authored `verify` exists → structured predicates) · D5 (**in-process Kokoro DONE** → only the Settings toggle) · F9 (remember-dedup exists → general read-dedup) · HS-02 (resume reality-block exists → compaction-as-update-template) · HS-08 (requery already reroutes unknown tools → only the invariant check)

**DONE — remove:** A7 (README/CONTRIBUTING/matrix shipped `fba3b81`/`359442e`) · C17 (cache markers `prompt_cache_key`+`cache_control` shipped `eb302cc`; only cost-tracking nuance) · BP-G1/BP-G4 (`shell_sessions.py`) · HS-04 (`stuck.py` scenarios) · HS-05 (→ C1).

**DROP — deferred-by-design or OBSOLETE (building these would be WRONG):**
- C4 mode-boundary KV — DEFERRED low-value (`pending-items:86`, one-time-per-conversation)
- C19 2A–3B nudges — violate the no-automatic-nudge invariant (`c97c1b3`)
- BP-G2/BP-G3 — DC-01 `host_proxy.py` superseded the keepalive serving model
- D1 `deploy_preview` — intentionally absent (`builtin/__init__.py:64`); re-decide if wanted

**NEEDS A DYLAN DECISION before planning:**
- C12 B2 programmatic-tool-calling/CodeAct-default — feasibility-gated (`tool_choice`/grammar DEAD on the llama.cpp stack; prefill/template only) — big arch change
- HS-06 epochal observation masking — per-step-mask vs KV-stability conflict; pick one strategy

---

## Track A — Release honesty & CI (P0, the gate)
*Source: North Star §2. These make "green" mean something.*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| A1 | `tsconfig.build.json` excluding tests but KEEPING fixtures → drive shipping-file `tsc` errors to 0 | M | 28/39 `tsc -b` errors are in SHIPPING files; the Dockerfile "test-only" comment is false |
| A2 | "Demo data — no backend" badge when `/env.js` fails | S | today a failed env silently shows canned fixture data |
| A3 | Minimal honest CI (`make test` + build/vitest required; tsc/eslint labeled non-required first) | M | depends on A1 + de-flaking A4 |
| A4 | De-flake `make test` — the auto-preview TOCTOU race (#25 area) | S | `make test` is flaky-red today; a CI hazard |
| A5 | Ruff/eslint real errors (F821, B904; ignore cosmetic test E501) | S–M | eslint 67, ruff debt |
| A6 | `PMX_` → `DISCO_` rename (compat fallback for the secret key; keep `pmx-data` volume) | M | before any public release; coordinate with `.env`, compose, docs |
| ~~A7~~ | ~~Docs honesty~~ — **MOSTLY DONE** (verified): README updated (`fba3b81`, no "Phase 0"), CONTRIBUTING + provider/VRAM matrix shipped (`359442e`). Remainder: a demo gallery (optional, P3) | S | release-execution-plan was STALE |

---

## Track B — Make the keyless tier FIT 8 GB (P0, the install blocker)
*Source: North Star §4/§5/§6. The default keyless run peaks ~10.8 GB and OOMs 8 GB, silently.*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| B1 | Lite ONNX encoder tier (small e5/bge + tiny reranker); make `PMX_EMBED_MODEL`/`PMX_RERANK_MODEL` real env knobs | M | ~4 GB → ~0.8 GB; biggest single lever; knobs are hardcoded constants today |
| B2 | Default bundled ctx 32K → 8K + quantize KV (`-ctk q8_0 -ctv q8_0`) | S | −3.7 GB, one number + one flag; target ~10.8 → ~4 GB peak |
| B3 | Encoder OOM guard — check available RAM before lazy encoder load; stream an honest error frame instead of dying mid-WS (#26) | M | today the WS closes after one frame, query vanishes, `disco verify` exits 137 |
| B4 | Replace bundled `Qwen3-4B-Instruct-2507` (~11mo old) with a current small GGUF — only after it pulls keyless AND passes `disco verify` (config→completion→tool-calling) | M | the model is the first impression AND breaks the keyless RAM promise |
| B5 | Cleanup: ~1,795 leaked `/tmp/pmx-sbx-*` workspace dirs (process-sandbox `destroy` cleans tmux not workspaces) | S | |

---

## Track C — Agent-loop correctness (P1, the moat)
*Source: the FULL fable reconciliation tail (manus-gap-analysis + addendum + arch-rebuild B1–B9) + decomplexity carry-forwards + pending-items future-plans. This is where weak-model reliability is won.*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| C1 | **B7 fresh-context evaluator** — external DoD spec the agent can't edit + a separate read-only evaluator agent at finish | L | **highest leverage; zero code today.** The structural fix for false-"done"/verify-thrash (exactly the autonomous-120b thrash + the Gemma STUCK pattern). |
| C2 | First-hit wake race — `wake_for_preview` returns the URL before the in-container dev server binds → first proxy hit 503; add connect-retry on a just-woken upstream | M | decomplexity DC-02 carry-forward |
| C3 | Re-materialize agent-launched dev servers (vite/express) on wake | M | DC-02 only restarts the static http.server; real apps don't survive suspend/wake |
| C4 | 3.6-2 mode-boundary KV-cache break — single stable system prompt across mode flips | M | pure-local correctness win (prompt-cache stability) |
| C5 | Auto-spill large observations to disk + `.pmx/MEMORY.md` persistence across hard reset | M | GAP C / 3.4B; filesystem-as-memory residue |
| C6 | Recitation on cadence/drift, not every step (B6) | S | Manus: constant rewrite wastes ~⅓ of actions |
| C7 | Serialization jitter + nudge-pool variants (anti-self-imitation; temp jitter half-done) | S | 2.6/3.5 |
| C8 | propose_plan_update loop bound in autonomous (feed identical re-proposals into the bookkeeping streak cap) | S | this session's deferred engine review #1 |
| C9 | Condenser still caps at a 24k/32k working budget despite the window-fraction soft/hard thresholds — honor the live window (`view.py:494-499`) | S-M | GAP A PARTIAL |
| C10 | `keep_recent` counts raw EVENTS not tool-turns (`view.py`) — a long tool chain evicts too aggressively | S | GAP A caveat |
| C11 | Reversible-compaction tier — tombstoned spans recoverable on demand (the 3.1 S1–S5 cascade's missing tier) | M | addendum 3.1 |
| C12 | B2 programmatic tool-calling / CodeAct-as-default — keep big tool outputs OUT of context by running code. **Caveat [H]:** `tool_choice:required`/`grammar`/`json_schema` are DEAD on our llama.cpp stack → only assistant-prefill or a grammar-constrained tool template works (ties to G8/F1) | M-L | arch-rebuild B2, "largest controlled win" |
| C13 | B9 prefill masking — FULL lifecycle (only PLANNING prefill exists, behind `PMX_PLAN_PREFILL=1`, default OFF) | M | arch-rebuild B9 PARTIAL |
| C14 | DR isolated sub-contexts — concurrent gather uses a SHARED router, not true isolated sub-contexts | M | arch-rebuild §4 PARTIAL |
| C15 | B8 idle-kernel cull — the persistent CodeAct kernel is never culled (+ RLIMIT_AS not cgroup) | S | arch-rebuild B8 minor |
| C16 | 3.1-S5 pointer-only flush — `hard_reset` is a normal summarize, NOT a filesystem-pointer-only flush | M | addendum 3.1-S5 |
| C17 | 3.6-1 rolling transcript cache markers (#3/#4) + 3.6-4 cache-write cost tracking | S | addendum 3.6 |
| C18 | §4 plan-step verify predicates — per-step done-conditions checked, not just the model's assertion | M | addendum §4; ties to C1/G5 |
| ~~C19~~ | ~~System-reminder patterns 2A/2B/2C/3A/3B~~ — **DROP (verified OBSOLETE):** these violate the deliberate **no-automatic-nudge invariant** (`c97c1b3`: stuck-escape was kept reminder-free ON PURPOSE). Rejected ideas, not gaps — introducing them would break the design. | — | pending-items was STALE |
| C20 | Subagent fan-out (Explore/Plan) inside the build/agent loop | M | pending-items follow-up — VERIFY at impl |
| C21 | Execution-prompt tuning for small open models | S | pending-items follow-up |

---

## Track D — Feature completion (P2)
*Source: RP-pack + task list + addendum §2.*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| D1 | deploy_preview / detached `serve` tool (GAP E / 3.3) — the agent can build a site but can't show it | M | with a pre-expose self-test |
| D2 | RP-10 dedicated slides deck viewer (nav + PDF/PPTX download surface) | M | backend done; only the generic srcdoc preview today |
| D3 | RP-11 Univer read-only grid embed | M | blocked on `@univerjs/* 0.25.x` (collab-only); re-check upstream |
| D4 | RP-09 live audio acceptance + mixer robustness (#16) | M | |
| D5 | RP-09 in-process Kokoro TTS + Settings toggle (#21) | M | Fable design |
| D6 | DatasourceEvent emission path (events exist + pinned, no producer) | S | GAP G |
| D7 | Egress allowlist LIVE VM test (close the HARDWARE-UNVERIFIED gap, 2.3B) | S | security |
| D8 | 2.4 headless-browser/screenshot verify gate — `verify="app"` does an HTTP-200 check, but a PIXEL screenshot needs a browser in the sandbox image (absent on the process backend) | M | addendum 2.4 PARTIAL |
| D9 | 2.5 image generation + binary-safe file write | M | addendum 2.5 OPEN |
| D10 | Share-bundle ↔ harness-cassette unification (one projection, two consumers) — share bundle feeds replay_runner but the service-call cassette is still a separate format | S-M | RP-00 PARTIAL |
| D11 | Background-task **running-tasks dashboard** — "dashboard lite" exists (status write-through + History chips + read-repair), but NO dedicated running-tasks view, no global "N tasks running" indicator, no schedule-run history surface ("the one Phase-2 item still user-visibly open") | M | release-execution-plan PARTIAL |
| D12 | In-block artifact download (sheet/Univer) — needs cid threading into the block | S | RP-11 deferred |

---

## Track E — Polish / debt (P3)
*Source: this session's MiniMax reviews + standing process debt.*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| E1 | `_save_autonomous` atomic write (tempfile + `os.replace`) — and the other sidecars share the pattern | S | lane-3 M3 |
| E2 | Probe cache TTL/invalidation (hot-swap serves stale label/ctx — hit live swapping Gemma↔Qwen) | S | lane-3 #11 |
| E3 | Bookkeeping cap vs legit long plans (≥7 batched `plan_step`) — split set / raise plan_step cap | S | lane-1 M2 |
| E4 | Snapshot binary-file skip (null-byte check) + deleted-file note in the omitted notice | S | lane-1 L1/M4 |
| E5 | #25 root fix — sandbox Settings hot-apply + wrap docker client so a failing client can't wedge the loop | M | also an A4/North-Star item |
| E6 | #10 ApprovalDiff real new-hash from the agent-server probe | S | task list |
| E7 | Rebuild synthetic-fake harnesses (streaming / providers / DR lifecycle) from REAL captures — they used `_ScriptedRouter`/hand-made docs, violating the real-captured-samples rule | M | pending-items process debt |
| E8 | Egress podman/local — proxied allowlist instead of sealed deny-all (gVisor already proxies; podman/local just deny-all) | M | Cluster-3 PARTIAL |

---

## Track F — SmallCode harvest (from 2 Sonnet reviews; verified vs Disco's code)
Doorman11991/smallcode — a TS coding agent for 8B–35B local models. Reviewers
catalogued 21 small-model adaptations; below is the steal-list TRIAGED against what
Disco actually has (verified by reading our source, since the reviewers didn't have
it). MiniMax's independent review will be merged when it lands.

> **GATING IS THE ARCHITECTURE (Dylan, 2026-06-13).** Every item below is a
> *weak-model COMPENSATION*. NONE ships as a standard always-on addition — each is a
> **toggle-gated "weak-model assist" tier, DEFAULT-OFF for capable models.** Forcing
> these on a good model makes it WORSE: a read-before-write gate annoys a model that
> already knows the file, quality-monitor "corrections" and forced patch-rewrites
> override better judgment, thinking-truncation cuts a strong model's reasoning. The
> harvest's value is conditional on the model being weak. Implementation: one
> assist-tier toggle (per-conversation, like autonomous mode) that defaults from the
> probed model — small/local → on, capable/frontier → off — and is overridable.
> **Build the gate FIRST; add features behind it.** The ONLY near-exception is F1
> (pure empty-`tool_calls` fallback — it can't fire on a capable model whose calls
> are always structured), but it still lives in the tier for consistency.

**Tier 1 — port soon (verified gaps that hurt weak models):**
| ID | Item | Why it matters to Disco | Sev |
|----|------|--------------------------|-----|
| F1 | **Multi-format tool-call recovery** — when the structured `tool_calls` array is empty, scan `content` + `reasoning_content` for Hermes `<tool_call>` tags / fenced JSON / bare JSON / Liquid `[func(kw=val)]`, with trailing-comma repair and a `write_file` path+content regex last-resort | **VERIFIED GAP:** `openai_provider.py:385 _tool_calls` reads ONLY the structured array — no text/reasoning fallback. This is exactly what dropped Gemma's calls under the wrong chat template → the STUCK we just saw. A text-fallback would have recovered them. | **HIGH** |
| F2 | Quality monitor — hallucinated tool name → Levenshtein closest-match ("did you mean `file_edit`?"), cross-turn exact-repeat detection, capped at 2 corrections | cheap, high-leverage; weak models misname tools constantly | MED-HIGH |
| F3 | Read-before-write guard — refuse the FIRST `file_write` to an unread existing file (allow the 2nd, for legit full-replace) | complements the live snapshot; stops blind overwrites | MED |
| F4 | Bootstrap detection — project-type one-liner on turn 1 (build/test/entry cmds) | saves 3–5 discovery tool calls per session | MED |

**Tier 2 — enhance what Disco already has:**
| ID | Item | Disco status |
|----|------|--------------|
| F5 | Thinking-budget mgmt — emergency head+tail truncation of `<think>`, disable thinking on repair attempt ≥2 | Disco HAS `enable_thinking` on/off (`openai_provider.py:213`) but no truncation/repair-policy |
| F6 | Patch-spiral detector (failures + total attempts per file → force full rewrite) | fold into the stuck-detector; the no-op half shipped this session |
| F7 | Context-aware read trim — head-only + actionable "search then read a line range" directive under pressure | Disco likely fixed-cap; make it pressure-aware |
| F8 | Mid-turn arg truncation — shrink old `file_write` args to a prefix once the result is confirmed | free, lossless compaction Disco's condenser doesn't do mid-turn |
| F9 | Tool-call dedup — read-only sliding window + idempotent-write per-turn | evaluate vs observed behavior |
| F10 | **Contract / Definition-of-Done guard** — external assertions the agent can't fake; block "done" until they pass | **directly informs Track C1 (B7 fresh-context evaluator)** — a concrete design to copy |

**Do NOT copy (reviewer-flagged anti-patterns):** validation that EXECUTES user code (keep syntax-only — security); the "MarrowScript compilation" fiction (hand-written JS with a generated-by header); off-by-default safety (shell containment, auto-rollback); an over-aggressive compression target (~400 tokens on 8k); an adaptive router that can't distinguish a slow/dead server from an incapable model.

**Disco is already ahead on:** the always-on live workspace snapshot (SmallCode's file-state diff tracker is OFF by default → its model must re-read to re-anchor and can read-loop); event-sourced provenance (supersedes SmallCode's manual evidence store); gVisor sandboxing (vs optional cwd-containment).

**Porting caveats (from MiniMax's independent review, which found 19 bugs in SmallCode):**
- **F1**: SmallCode's own `reasoning_content` recovery has a bug — it extracts the
  tool call AND then promotes the reasoning prose into `content`, so the model sees
  its own thinking as assistant text next turn. When we port F1, recover the call
  but DROP the reasoning (don't leak it as content).
- Don't copy SmallCode's text-regex completion ("step N done" matches negations like
  "step 3 isn't done") — Disco's affirmative `finish` tool is already better; keep it.
- If we port trust-decay (F-tier), distinguish "tool returned no results" from "tool
  errored" — SmallCode demotes `search` after 3 empty results, exactly when it's
  needed to confirm absence.
- Avoid SmallCode's structural smells generally: module-global mutable retry state
  with mixed keyspaces, duplicate dead-code paths, and an SSRF guard the main loop
  bypasses (our gVisor egress proxy is the right layer instead).

**Routing:** F1 is a new small subsystem (its own item, near Track C). F2–F4, F6 → Track C/E. F5,F7,F8 → Track E. F10 → merge into C1's design.

---

## Track G — OSS-harvest backlog (HS-01…08, `docs/harvest-backlog.md`)
*A SEPARATE harvest from Track F's SmallCode — these are pi / OpenHands / SWE-agent /
OpenCode / smolagents / OpenManus steals that got an `HS-` id but no order. Several
overlap Tracks C/F; the brief author MUST diff against current code first. Per the
weak-model-assist gate, the model-compensation ones (G4/G5/G8) also belong behind the
Track-F toggle.*

| ID | Item (source) | Effort | Status | Overlap |
|----|---------------|--------|--------|---------|
| G1 (HS-01) | **Shell spill-to-file** — rolling tail buffer + full-output temp file + path in result (50KB threshold; tail-keep shell, head-keep reads); prescriptive truncation marker (say WHAT to do next) — pi `executeShellWithCapture` | S | backlog | the canonical version of C5/GAP-C auto-spill — **do this one** |
| G2 (HS-02) | Anchored-checkpoint compaction template (Goal/Constraints/Progress/Decisions/Next/Files as an UPDATE target) — pi `compaction.ts` + OpenCode | M | verify-overlap | the BP reality-block builder may already template some |
| G3 (HS-03) | Scheduled facts re-grounding every N steps + once post-restart — smolagents `planning_interval` | S | verify-overlap | composes with G2; check the resume path |
| ~~G4 (HS-04)~~ | ~~Stuck-detector n-gram + scenarios~~ | S | ✅ **mostly DONE (verified)** — `stuck.py` already uses `event_content_eq` (semantic, not byte) + A-B-A-B alternating + repeated action-obs scenarios. Only the n-gram/format-similarity nuance might remain (LOW). |
| ~~G5 (HS-05)~~ | ~~Critic finish-gate~~ | M | → **DEDUP into C1** — the independent critic-at-finish IS the B7 fresh-context evaluator |
| G6 (HS-06) | Epochal observation masking — batched keep/remove at polling boundaries to keep the KV prefix stable | S/M | **DESIGN-BLOCKED** | **needs a Dylan decision:** per-step masking vs KV stability conflict — pick ONE; never mask per-step |
| G7 (HS-07) | ThinkTool — no-op reasoning-dump tool (cheap prose-degeneration mitigation) | XS | backlog | fill-in-when-idle; composes with degeneracy-condensation |
| G8 (HS-08) | Reroute-to-hidden-`invalid`-tool repair — malformed/unknown tool call becomes an ordinary error tool_result via a registered-but-unoffered `invalid` tool; the turn never aborts, message invariants hold — OpenCode | S | verify-overlap | composes with **F1**; check rp-12's requery path doesn't already preserve invariants |

---

## Track H — Build & serve parity (`docs/build-parity-cluster.md`, G1–G12)
*The build-loop's serving/shell/verification coherence gaps. `BP-G` ids = this doc's
native G-ids (distinct from Track G's HS-ids). Some overlap C2/C3/D8/E8 — dedup at
brief time. G7/G8/G11 are DONE (persistent kernel `edf0185`; DC-05 loop survival +
resume) and listed only for closure.*

| ID | Item | Effort | Status |
|----|------|--------|--------|
| ~~BP-G1~~ | ~~Persistent inspectable shell sessions~~ | — | ✅ **DONE (verified)** — `shell_sessions.py` has named `session` + `shell_view` + `shell_write_to_process` (DC-04) |
| ~~BP-G2~~ | ~~Single-owner serving (keepalive race)~~ | — | ❌ **OBSOLETE (verified)** — the `while true http.server` keepalive is GONE; DC-01's `host_proxy.py` replaced the preview model. Fixing this would fight a solved problem. |
| ~~BP-G3~~ | ~~Truthful server status (bind-only)~~ | — | ❌ **OBSOLETE (verified)** — superseded by DC-01 hostname-routed preview |
| ~~BP-G4~~ | ~~Kill / ownership model~~ | — | ✅ mostly DONE via named shell sessions (verify the kill targeting at impl) |
| ~~BP-G5/G6~~ | ~~Browser verification / vision feedback~~ | M | → **DEDUP into D8** (one screenshot/vision verify gate; needs a browser in the sandbox image) |
| BP-G7/G8/G11 | Flat-latency exec / loop survival / resumable | — | ✅ DONE (persistent kernel `edf0185`; DC-05) |
| BP-G9 | **Multi-service builds** — run > 1 service (API + frontend) | M | OPEN/PARTIAL — overlaps C3; VERIFY at impl |
| BP-G10 | **Move Build egress "open" → "filtered"** + live-verify in gVisor | M | PARTIAL — filtered-proxy infra EXISTS (`_container.py` `egress_mode`/8888); just flip Build's default + the D7 live test |
| BP-G12 | **Cockpit UX** — build operator surface (shell view, server status, process list) — note `shell_view`/sessions now exist, so this is the UI surfacing | M | OPEN/PARTIAL — VERIFY scope at impl |

---

## Recommended sequencing
- **Wave 1 (release gate, parallelizable):** A1+A2+A4 → A3 (CI); B2 (the one-number RAM wins) + B5. These unblock an honest green build and a box that doesn't OOM on the easy levers.
- **Wave 2 (keyless-fit):** B1 + B3 + B4 — the encoder tier + OOM guard + current bundled model. This is what actually closes the North Star DoD.
- **Wave 3 (moat):** C1 (B7 evaluator) as the headline, with C2/C3 (preview survives wake) alongside — these are the reliability wins that differentiate.
- **Then:** A6 rename (pre-public), Track D features, Track E debt, Track F harvest folded in.

**Gating discipline (unchanged):** every item ends with a real-app run on the
gauntlet/8 GB box + visual evidence; "verified" requires the assembled app, not
green unit suites.
