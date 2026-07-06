# Disco Mega-Campaign — the closing push

**Created:** 2026-07-06 · **Branch:** `disclaude/mega-campaign` (off `disclaude/design-loop-harvest` tip `10274058`) · **Owner-run:** Fable orchestrating; codex primary worker; parallel worktrees per epic.

**Mandate (Dylan, 2026-07-06, going-to-bed autonomous run):** "fold this whole [build-depth] plan, plus the new designs we found, and then finally the security audit into a mega-campaign with epics for each section and the associated WOs, then execute autonomously."

This is the last big push before v0.1 is "basically it." Four epics. Autonomous execution discipline below is **non-negotiable** — every landed WO is built + gated + **live-proven** (a real model / real runtime / real exploit, never a cassette) + committed in isolation. Anything not reached stays **QUEUED**, never mislabeled done. Dylan's #1 pain is code that *looks* finished but was never run — we do not produce that.

---

## Execution discipline (applies to EVERY WO)

1. **Isolated worktree** per parallel work-stream (`disco-wt-<epic>` on real disk, never `/tmp` — tmpfs usrquota wedge). `.disco-env` with `PYTHONPATH` override so each worker tests its OWN code. See `disco-parallel-worktree-kit`.
2. **Five fitness gates**, run authoritatively by the integrator (Fable) on the MAIN checkout before commit, not just the worker's green:
   - `uv run basedpyright` (0 errors tree-wide)
   - `uv run python scripts/check_arch_budget.py` (no NEW over-budget item)
   - `uv run lint-imports`
   - `uv run python scripts/gen_arch_diagram.py --check`
   - `.venv/bin/python3 -m pytest <touched packages> -m "not integration"`
3. **Live-proof** — the gate that matters. Cassettes/mocks prove "didn't break"; only a live model / real workerd / real HTTP / real exploit proves it WORKS. Every WO names its live-proof.
4. **Adversarial review** for security WOs: `codex exec -c model=gpt-5.5 -c model_reasoning_effort=xhigh` must reach SHIP (fix every BLOCK/major).
5. **Commit in isolation**, message trailer:
   `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_0148WNDs6Jw33bq3eTt8qgCm`.
   Worktree commit-hook refuses commits from worktree paths → integrate via `git add && git diff --cached > patch`, then `git apply` on MAIN and commit there (linear history).
6. **No false affordances, no cheap workarounds, preserve originals.** Flag any gap in red.

**Parallelism plan (disjoint file sets → safe concurrent worktrees):**
- Epic A → `core/design/*` + core design tests. 
- Epic B → `core/appkit/*` + `tools/.../verify_appkit_app.py` + `agent-server/appkit_cloudflare/*`.
- Epic C → `core/loop/*` guidance strings + prompts.
- Epic S → `agent-server/**` + `app-server/**` + `tools/**` runtime/egress/sandbox.
A, B, C are mutually disjoint and can run concurrently. S overlaps `tools/**` with B in a few files → S starts as B's appkit work quiesces, or in a worktree with careful file ownership.

**Sequencing (Dylan's stated order):** designs + build-depth ("get it working") first; **security is the closing epic**. Tier-3 (C) is tiny filler. Overnight priority: **A → B-W1 (keystone) → B-W2/3/4 → C → S-W1…W6**. Idle capacity is filled by bringing the next disjoint epic online early rather than leaving workers idle.

---

# EPIC A — Design Direction Expansion  (task #61)

**Goal:** grow the direction library 9 → 22 (+13), covering the app/product verticals Dylan builds (fintech, healthcare, enterprise, ops dashboards, docs, scientific) plus fresh brand/editorial registers. Additive, low-risk, self-contained to `core/design`.

**Verified constraints (from recon):**
- Add each new id to the `DirectionId` Literal (`directions.py:18-28`) or pydantic rejects it.
- Append records to `DIRECTIONS` (`directions.py:216`); `DIRECTION_IDS`/`DIRECTION_BY_ID` derive automatically.
- Register the **3 dark directions** (`signal-noir`, `noir-deco`, `controlled-maximalism`) in `_DARK_DIRECTION_IDS` (`directions.py:38`) or they render on a light bg.
- Every record's `art_guidance` MUST be `_SVG_FIRST_ART_GUIDANCE` or `_IMAGE_GEN_PREFERRED_ART_GUIDANCE` (test `:116` asserts one of the two prefixes).
- Fonts are all real Google Fonts families (no Optima/Didot-style proprietary; none are Inter/Roboto/Arial primaries).
- No hex may equal an `ai_purple` value (`#7c3aed #8b5cf6 #6d28d9 #5b21b6 #a855f7 #9333ea #7e22ce #c084fc #6366f1 #818cf8 #4f46e5 #4338ca #a78bfa`). Confirmed clean.

**WO-A1 — integrate the 13 records + wiring.** Insert the records (full Python in the Epic-A spec, `scratchpad/spec-epic-a-directions.md`), extend the Literal, register dark ids.
**WO-A2 — fix consumers/tests.** Update `test_design_directions.py:20-36` (`EXPECTED_IDS` → 22 ids, `len == 22`, rename `test_all_nine_...` → `test_all_directions_...`). Re-run `pick_direction` brief→id assertions (`:78-84`); if a new keyword outscores an existing pick, **adjust the NEW direction's keywords** (never weaken an existing assertion) and report. Update the prose list in `workflows/prompt_packs/build_static_site.md:13-15` (and drop the stale non-id "retro-print").
**WO-A3 — live-proof.** For each of the 13: `direction_tokens_css(d)` renders a valid CSS var block; `to_brand_tokens(d)` yields a Theme (dark ones get `mode="dark"`); `pick_direction(<its keywords>)` selects it; run `design_lint` over each emitted tokens.css and assert **zero** numeric/ai_purple/gradient findings (the no-self-flag guarantee). Capture output.

**The 13 directions (id · label · fonts · seed/accents · treatment · density · art):**
1. `console-dense` · Console Dense (ops/observability, light) · Geist/Geist/Geist Mono · seed `#2D6CDF` / green `#1F9D6B` amber `#D98A0B` red `#D24141` · outlined · dense · SVG
2. `enterprise-navy` · Institutional (B2B, light) · Archivo/Public Sans/Spline Sans Mono · seed `#21406B` / `#2C5A94` `#2A7D8C` `#C08A2E` · outlined · dense · SVG
3. `clinical-calm` · Clean Room (healthcare, light) · Lexend/Mulish/IBM Plex Mono · seed `#2C8BA0` / `#3FA787` `#6E8FD6` `#E0806B` · flat · spacious · SVG
4. `trust-fintech` · Ledger & Copper (fintech, light) · Hanken Grotesk/Figtree/Spline Sans Mono · seed `#1E5F52` / copper `#B0682F` slate `#3C4A5A` pine `#14463C` · soft-depth · balanced · SVG
5. `premium-consumer` · Porcelain & Vermilion (premium DTC, light) · Instrument Sans/Onest/DM Mono · seed graphite `#2A2622` / vermilion `#D8452B` brass `#C79A3E` stone `#8A8078` · flat · spacious · IMAGE-GEN
6. `signal-noir` · Signal Noir (cyber HUD, **dark**) · Space Grotesk/Chivo/JetBrains Mono · seed cyan `#2BB8D6` / magenta `#D6336C` amber `#E0A82E` lime `#8FBF3F` · outlined · compact · SVG
7. `lab-precise` · Instrument (scientific, light) · Libre Franklin/Libre Franklin/IBM Plex Mono · seed graphite `#2B2F33` / orange `#C75B2A` blue-grey `#48535E` slate `#6B7580` · flat · dense · **NO-MOTION** · SVG
8. `literate-docs` · Literate (dev docs, serif-body, light) · Fraunces/Source Serif 4/JetBrains Mono · seed teal `#1F5E52` / `#2F7A6F` amber `#B7822E` rose `#B5566B` · outlined · editorial · SVG
9. `noir-deco` · Noir Deco (art-deco luxe, **dark**) · Marcellus/Josefin Sans/Space Mono · seed gold `#B8963F` / emerald `#215E4C` oxblood `#7A2E2E` champagne `#D9C48A` · outlined · balanced · SVG
10. `inkline-sketch` · Inkline (hand-drawn, light) · Shantell Sans/Nunito Sans/Space Mono · seed ink `#26303A` / red `#C34B3E` ochre `#B98A3C` slate `#5A6470` · outlined · balanced · SVG
11. `controlled-maximalism` · Controlled Maximalism (jewel brand, **dark**) · Bricolage Grotesque/Albert Sans/Space Mono · seed jewel-teal `#0E7C7B` / magenta `#B5297E` gold `#D6A93B` coral `#E0654A` · soft-depth · balanced · IMAGE-GEN
12. `gradient-mesh-warm` · Warm Mesh (tasteful gradient, light) · Sora/Be Vietnam Pro/JetBrains Mono · seed terracotta `#C65D3B` / coral `#E0654A` marigold `#E5A93C` plum `#6B3A54` · soft-depth · spacious · IMAGE-GEN
13. `pressed-botanical` · Pressed Botanical (organic herbarium, light) · Spectral/Karla/IBM Plex Mono · seed sage `#4F6B47` / clay `#B26A4A` mustard `#C79A3E` berry `#8A3A4E` · flat · editorial · IMAGE-GEN

---

# EPIC B — Build Depth: deployed durable apps  (task #62)

**Reframed by the deploy recon.** The premise "deploy is a stub" was WRONG: a full real-Cloudflare deploy subsystem already exists (`agent-server/appkit_cloudflare/deploy.py`, 2911 LOC — generator emits `wrangler.toml`+D1 binding+`wrangler deploy`/`db:remote`/`db:local` scripts at `core/appkit/generator.py:918-953,1149-1183`; `execute_deploy` shells real `wrangler d1 create/execute`+`wrangler deploy`, REST-mounted at `routes.py:614`, hard-gated dry-run-default, refuses in autonomous mode). What's missing is what was deferred as **"Epic I": a runtime proof.** The verifier (`tools/.../verify_appkit_app.py`) is **purely structural** — it parses `worker/index.ts` as source and explicitly says "NOT a proof that the app works at runtime." Nothing ever *runs* the worker.

**Consequence:** "cross the deploy boundary" is built. The gap is proving durability — Dylan's "once it's deployed it's no longer ephemeral" made a *test gap*: we emit durable apps but never demonstrate state surviving a restart. That's B-W1.

**B-W1 — local workerd/miniflare persistence proof (the keystone).**
- Install a **trusted `wrangler`** binary (out-of-workspace, pinned) — the deploy subsystem's `_resolve_trusted_wrangler` (`deploy.py:160-211`) already expects `DISCO_WRANGLER_BIN`; the harness reuses that seam. `wrangler` is NOT currently installed.
- New runtime harness: take a generated app tree, `wrangler d1 execute --local --file=./schema.sql` against a **persisted** `.wrangler` dir, boot the worker (`wrangler dev` / `unstable_dev` / `getPlatformProxy`), POST a row, **kill the process**, restart, GET it back → assert the row survived the cold start. This is the emitted app's OWN `db:local`/`cf:dev` scripts driven programmatically.
- Wire as a **runtime verification tier** in `verify_appkit_app` (integration-marked — needs `wrangler`), complementing the structural pass. Honest scope note updated (retire the "Epic I deferred" TODO for the local case).
- **Live-proof:** the harness itself IS the proof — a real emitted app, real workerd, real D1, real restart, real row survives. Capture the transcript.
- Real-CF deploy stays the opt-in when Dylan connects a token (already built; needs creds + `DISCO_WRANGLER_BIN` + sandbox build backend — documented, not tonight).

**B-W2 — data-layer depth.** Emitted schema is a single `schema.sql` with no relations/migrations dir. Add: multi-entity **relations + foreign keys** (the `User→Role→Shift→TimeOffRequest→Approval` shape), a real **migrations** directory (drizzle-kit or ordered SQL), and generator support for related entities in `AppSpec`. Live-proof: generate a multi-entity app, run B-W1's harness, prove a FK-joined query returns correct rows post-restart.

**B-W3 — auth/RBAC depth.** `_emit_worker_ts` has a single `isAuthorized` admin-token guard + `wrangler secret put ADMIN_TOKEN`. Deepen to **sessions + roles + per-endpoint role checks** (the shift-calendar "only certain people can approve"). Live-proof: generate an app with an approver role; prove a non-approver POST to `/api/approve` is 403 and an approver's is 200, under the local runtime.

**B-W4 — client reactivity.** Client-side fetch/mutate + optimistic updates against the worker endpoints; **Durable Objects** only where genuine realtime (presence/live-collab) is requested. Live-proof: a generated app mutates and reflects without full reload; a realtime feature echoes across two clients.

---

# EPIC C — Design-Loop Tier-3 residual  (task #63)

Small harvest leftovers on the design-loop branch (guidance-weight, low risk). From `disco-design-loop-harvest`.
**C1 — `<think>`-gate at 3 loop transitions:** encourage a brief reasoning pass at the plan→execute, stuck→escape, and finish transitions (text/prompt-weight, no control-flow change; mirror T2's shape).
**C2 — codify never-modify-tests:** a durable rule in the build prompts that the model must not edit/delete tests to make a build "pass" (the root cause is in the code under test). 
**C3 — customize-shadcn-before-build:** guidance to tailor design tokens/shadcn theme to the committed direction *before* generating components, so output isn't default-shadcn slop.

---

# EPIC S — Security Hardening  (task #64)  — the closing epic

Executes the **already-implementation-ready** `docs/disco-security-fix-campaign.md` (7-round adversarial Opus+codex convergence; **38 findings = 8 Critical · 17 High · 10 Medium · 3 Low**; PAUSED awaiting go — this campaign IS the go). **Root cause:** 38 findings are ~5 problems; **ROOT-A (no-auth + wildcard CORS) is the keystone** — fixing it removes the reachability of ~9 findings. Full per-task detail + acceptance tests live in that doc; the waves are the WOs:

- **S-W1 — ROOT-A: auth + CORS + owner-scoping (keystone).** HttpOnly SameSite cookie + CSRF + strict WS Origin + scoped preview/artifact capabilities + generated route-inventory test + admin-only global app-server state. Closes C5,H6,H8; removes reachability of H2,H10,H13,H14,M2,M7. Tasks A1–A8 in the security doc.
- **S-W2 — ROOT-B: secret-resolution + egress chokepoint.** SecretStore-only provider map (kill the os.environ fallthrough, C8), two egress classes (untrusted hard-deny private vs operator-configured origin-pinned), WeasyPrint asset allowlist, `runs_in` honesty for remote image/audio/slides backends. Closes C4,C8,H1,H11,H12,M3,M5,M7,C7.
- **S-W3 — host-execution cluster (gVisor bypass).** Fail-closed backend Literal, plan/DoD command predicates run IN-sandbox, hardened deny floor, kernel-token hygiene. Closes C1,C2,C3,C6,H9,M1,H13.
- **S-W4 — MCP approval integrity.** Pre-CONNECT approval, first-use approval, stdio host mislabel, risk-tier wiring, inputSchema in the approval hash. Closes H3,H4,H5,H7.
- **S-W5 — availability / isolation.** Per-surface egress policy + host-enforced private+tailnet block + no sibling hairpin, real workspace quota, sandbox→host transfer caps, DoD no-auto-release, preview argv-not-shell. Closes H15,H16,H17,M8,M9,M10.
- **S-W6 — output sinks + share/storage + lows.** Share point-in-time snapshot, storage-browse jail, xlsx formula injection, dead redaction, .env untrack, KDF note. Closes M4,M2,M6,L1,L2,L3.
- **S-post — re-audit:** one more Opus+codex round-pair against the patched tree to confirm the tail collapsed.

Per-wave HARD GATES (from the security doc): build + real-sample harness → **exercise the real exploit pre-fix (prove it works) then post-fix (prove it's closed)** → gpt-5.5 adversarial to SHIP → commit. Line numbers in the security doc are from 2026-06-20 and WILL have drifted — **match on content/symbol, not line number** (use Serena).

---

# EPIC Z — Big soak + debug (final)  (task #65)

**Added by Dylan 2026-07-06:** after all epics land, run a **large soak** (many autonomous build/agent/research runs against live local models, per `disco-soak-verdict` methodology — the 193-run soak found the agent-surface workflow trap). Surface issues, then **debug them** — root-cause + fix + re-soak each failure class rather than logging and moving on. This is the whole-system integration proof: the epics landed individually green, but only a broad live soak proves they compose without regression. Feeds fixes back as new WOs.
- **Z1 — soak harness run:** N live runs across surfaces (build/agent/research/deep_research), record pass/fail + failure signatures.
- **Z2 — debug loop:** cluster failures by class, root-cause each (bias-free dual investigation), fix, re-soak that class until it holds. Never hardcode to pass.
- **Z3 — final re-audit + status report** to Dylan: what landed, what live-proved, what's still open.

---

## Run log

Everything that happens this run is appended to **`docs/mega-campaign-run-log.md`** (timestamped, per Dylan's "keep a log of everything"). That file is the chronological narrative; this ledger is the state snapshot.

## Live status ledger (updated as WOs land)

| WO | Epic | Status | Commit | Live-proof |
|----|------|--------|--------|-----------|
| A1–A3 | Design | **DONE** | `aef9c33d` | 22 dirs, zero self-flags, correct dark-mode, pick_direction selects each |
| B-W1 | Build | **DONE** (keystone) | `d5db4e88` | Fable's own run: POST→kill→restart→marker present; real wrangler+workerd+D1 |
| B-W2 | Build | **DONE** | `fea94682` | Fable's own run: FK'd shift survives cold restart; FK enforcement + byte-identical lead-gen verified |
| B-W3 | Build | **DONE** | `d99c4147` | Fable's own run: RBAC (approver-only) + session survive cold restart; gpt-5.5 adversarial SHIP after 2 fix rounds; IDOR documented |
| B-W4 | Build | **DONE** | `efccf4fe` | Fable's own run: real Firefox render of a generated app — disabled "Sending…" button + optimistic "Recently submitted" item mid-submit; backend worker+D1 byte-identical (B-W3 auth worker untouched) |
| C1–C3 | Tier-3 | **DONE** (soak-pending) | `1dfedb15` | 83 tripwire tests; behavioral effect deferred to Epic Z soak |
| S-W1 | Security | **DONE** (keystone) | `e028d2ac` | from-scratch auth/CSRF/owner-scoping; 19-proof real-exploit harness + route-inventory test; adversarially SHIP'd over 4 gpt-5.5 rounds (BLOCK 9→3→2→SHIP); UI-still-works Firefox screenshot = the one human-verify item |
| S-W1 | Security | QUEUED (keystone) | — | — |
| S-W2..W6 | Security | QUEUED | — | — |
| Z1–Z3 | Soak+debug | QUEUED (final) | — | — |

**Rule:** a row moves to DONE only when committed AND live-proven. Partial work stays IN-PROGRESS with a note. Nothing is ever marked done on a green unit-test count alone.
