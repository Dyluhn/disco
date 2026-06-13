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

## Track F — SmallCode harvest (PENDING — 3 reviews in flight)
Doorman11991/smallcode is a TS coding agent for 8B–35B local models — directly
parallel (budget-managed context, forgiving multi-format tool parser, TODO-file
planning, search-and-replace editing). Harvest items + steal-list to be appended
when the Sonnet ×2 + MiniMax reviews complete, then triaged into Tracks C/E.

---

## Recommended sequencing
- **Wave 1 (release gate, parallelizable):** A1+A2+A4 → A3 (CI); B2 (the one-number RAM wins) + B5. These unblock an honest green build and a box that doesn't OOM on the easy levers.
- **Wave 2 (keyless-fit):** B1 + B3 + B4 — the encoder tier + OOM guard + current bundled model. This is what actually closes the North Star DoD.
- **Wave 3 (moat):** C1 (B7 evaluator) as the headline, with C2/C3 (preview survives wake) alongside — these are the reliability wins that differentiate.
- **Then:** A6 rename (pre-public), Track D features, Track E debt, Track F harvest folded in.

**Gating discipline (unchanged):** every item ends with a real-app run on the
gauntlet/8 GB box + visual evidence; "verified" requires the assembled app, not
green unit suites.
