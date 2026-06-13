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
*Source: fable-plan-reconciliation top-OPEN + decomplexity carry-forwards. This is where weak-model reliability is won.*

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

---

## Track D — Feature completion (P2)
*Source: RP-pack + task list.*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| D1 | deploy_preview / detached `serve` tool (GAP E / 3.3) — the agent can build a site but can't show it | M | with a pre-expose self-test |
| D2 | RP-10 dedicated slides deck viewer (nav + PDF/PPTX download surface) | M | backend done; only the generic srcdoc preview today |
| D3 | RP-11 Univer read-only grid embed | M | blocked on `@univerjs/* 0.25.x` (collab-only); re-check upstream |
| D4 | RP-09 live audio acceptance + mixer robustness (#16) | M | |
| D5 | RP-09 in-process Kokoro TTS + Settings toggle (#21) | M | Fable design |
| D6 | DatasourceEvent emission path (events exist + pinned, no producer) | S | GAP G |
| D7 | Egress allowlist LIVE VM test (close the HARDWARE-UNVERIFIED gap, 2.3B) | S | security |

---

## Track E — Polish / debt (P3, deferred review findings)
*Source: this session's MiniMax reviews (the LOW/MED items not yet done).*

| ID | Item | Effort | Notes |
|----|------|--------|-------|
| E1 | `_save_autonomous` atomic write (tempfile + `os.replace`) — and the other sidecars share the pattern | S | lane-3 M3 |
| E2 | Probe cache TTL/invalidation (hot-swap serves stale label/ctx — hit live swapping Gemma↔Qwen) | S | lane-3 #11 |
| E3 | Bookkeeping cap vs legit long plans (≥7 batched `plan_step`) — split set / raise plan_step cap | S | lane-1 M2 |
| E4 | Snapshot binary-file skip (null-byte check) + deleted-file note in the omitted notice | S | lane-1 L1/M4 |
| E5 | #25 root fix — sandbox Settings hot-apply + wrap docker client so a failing client can't wedge the loop | M | also an A4/North-Star item |
| E6 | #10 ApprovalDiff real new-hash from the agent-server probe | S | task list |

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

## Recommended sequencing
- **Wave 1 (release gate, parallelizable):** A1+A2+A4 → A3 (CI); B2 (the one-number RAM wins) + B5. These unblock an honest green build and a box that doesn't OOM on the easy levers.
- **Wave 2 (keyless-fit):** B1 + B3 + B4 — the encoder tier + OOM guard + current bundled model. This is what actually closes the North Star DoD.
- **Wave 3 (moat):** C1 (B7 evaluator) as the headline, with C2/C3 (preview survives wake) alongside — these are the reliability wins that differentiate.
- **Then:** A6 rename (pre-public), Track D features, Track E debt, Track F harvest folded in.

**Gating discipline (unchanged):** every item ends with a real-app run on the
gauntlet/8 GB box + visual evidence; "verified" requires the assembled app, not
green unit suites.
