# Mega-Campaign Run Log

Chronological narrative of the autonomous run Dylan kicked off 2026-07-06 before bed.
State snapshot lives in `disco-mega-campaign.md`; this file is the append-only story.
Times are wall-clock local where noted; ordering is authoritative regardless.

---

## 2026-07-06 — Session start (Fable orchestrating)

- **Branch:** created `disclaude/mega-campaign` off `disclaude/design-loop-harvest` tip `10274058` (T1).
- **Recon (design prereqs):** DONE. No font allowlist (only Inter/Roboto/Arial banned as primaries) → any Google-Fonts family renders. `direction_tokens_css` emits only theme vars → structurally cannot self-flag design_lint. Mandatory edits: `DirectionId` Literal, `DIRECTIONS` tuple, `_DARK_DIRECTION_IDS`, and `test_design_directions.py:20-36` (EXPECTED_IDS + len==9). `#4F46E5` IS in the ai_purple hex list — literate-docs indigo swapped to teal `#1F5E52`.
- **Recon (deploy path):** DONE. Real Cloudflare deploy subsystem ALREADY EXISTS (`agent-server/appkit_cloudflare/deploy.py` 2911 LOC; generator emits wrangler.toml+D1+deploy scripts; `execute_deploy` shells real wrangler; hard-gated dry-run-default). Gap = the deferred "Epic I" local runtime proof: verifier is purely structural, nothing runs the worker; `wrangler` not installed. → B-W1 reframed to build the local workerd/miniflare persistence harness.
- **Campaign doc written:** `docs/disco-mega-campaign.md` — 6 epics (A designs, B build-depth, C tier-3, S security, Z soak+debug), execution discipline, live status ledger.
- **Tasks:** #61 EPIC A, #62 EPIC B, #63 EPIC C, #64 EPIC S, #65 EPIC Z. #60 (scoping) closed.
- **Next:** dispatch Epic A (design integration) to codex in an isolated worktree; then B-W1.

### Epic A dispatched (design integration)
- Worktree `disclaude-wt-A` (branch `wt-epic-a` off mega-campaign). PYTHONPATH override verified (imports worktree code, count=9 pre-change).
- Spec `scratchpad/spec-epic-a-directions.md` — 13 full drop-in records authored by Fable (taste-driven), codex does mechanical integration + Literal/dark-set/test edits + gates. codex bg id `beyvfa6ke`.
- All 13 palettes verified clean of the ai_purple hex list; fonts all real Google Fonts; 3 dark directions flagged for `_DARK_DIRECTION_IDS`.

### Epic B setup
- Worktree `disclaude-wt-B` (branch `wt-epic-b`). 
- `wrangler` global install kicked off (bg `b7hfazrg1`) — required for the B-W1 local Workers-runtime persistence proof (was not installed).
- B-W1 build-surface recon dispatched to map the minimal generate→boot→restart path before speccing.

### Epic A LANDED — commit `aef9c33d`
- codex integrated all 13 records + Literal + `_DARK_DIRECTION_IDS` + tests + prose list; no keyword collisions (all pick_direction assertions held, no keyword edits needed).
- Fable authoritative verification (not codex self-report): basedpyright 0; arch-budget = the standing 23-item baseline with ZERO new items (all pre-existing loop/runtime/turn-control god-objects, none in directions.py); design tests pass; **live-proof script**: count=22, all 13 ids present, dark-set = 5 (correct), every direction renders tokens.css + resolves correct light/dark mode + design_lint zero self-flags + pick_direction selects each by its own keywords.
- Patched to campaign branch via git-diff→git-apply (worktree commit-hook path). wt-A removed.
- Tasks #59, #61 → completed.

### B-W1 spike in progress
- Real lead-gen app materialized to `scratchpad/bw1-app` (21 files; db `acme-leads-d7fcf353`; wrangler.toml has `[assets] directory=./dist` + `run_worker_first=["/api/*","/admin"]`; POST /api/leads public, GET auth-gated by ADMIN_TOKEN).
- `npm install` in the app dir DONE (exit 0). Next: stub ./dist, write .dev.vars, init local D1 with --persist-to, boot `wrangler dev`, prove POST→kill→restart→GET survives.

### B-W1 spike PASSED (keystone proven) — real runtime, real cold restart
- Proven sequence against real `wrangler dev`/workerd + local D1: POST /api/leads → 201 {"ok":true}; GET (Bearer) → lead id 1; KILL process → HTTP 000 (down); RESTART (same cwd/.wrangler/state) → GET → SAME lead present. **State is durable across a Worker cold restart.**
- Working incantation: `wrangler dev --port P --ip 127.0.0.1 --local`, `CI=true WRANGLER_SEND_METRICS=false`, stub `./dist/index.html`, `.dev.vars` ADMIN_TOKEN, default cwd `.wrangler/state`. Ready ~2s/boot.
- codex dispatched to productionize into a reusable harness (`packages/core/tests/_workerd_harness.py`) + integration test (`test_workerd_persistence.py`) + retire the "Epic I deferred (local)" docstrings. bg `be1mwl9z6`. Task #62 in_progress.

### B-W2 recon done (data-layer depth)
- AppSpec ALREADY holds N entities (`entities: tuple[Entity,...]` max 50) but Entity/EntityField are FLAT — no FK/relation field; generator collapses all to ONE hardcoded `leads` entity. Schema/Drizzle/worker fully lead-specialized.
- No migrations dir; drizzle-kit configured but never invoked (pure/deterministic generator can't run Node at generate-time). Least-invasive: hand-emitted numbered `migrations/NNNN_*.sql` + `wrangler d1 migrations apply`.
- **Decision:** B-W2 = a NEW `records` primitive (register_primitive alongside lead-gen/directory), NOT a lead-gen mutation — lead-gen output must stay byte-identical (tests assert exact shape) and the codebase's explicit anti-over-generalization stance (primitives.py:6-11) wants per-primitive modules. Adds a relation/FK concept to the spec model + multi-table schema + FK ordering + N-entity worker route table + migrations.

### Epic C LANDED — commit `1dfedb15` (done directly on main; disjoint from codex's B-W1/B-W2 files)
- Recon corrected the map: build prompts live in `llm/prompts.py` (not `loop/prompts.py`). 5 surgical additive edits by Fable (precise, low-latitude — safer than giving codex latitude in dense live prompts):
  - think-gate: `_EXECUTION_DRIVER_PROMPT` opener (plan→execute) + finish bullet; shared `_STUCK_ESCAPE_REMINDER_POOL[0]` (stuck→escape).
  - never-modify-tests: build `<file_rules>`.
  - design-first: `render_design_direction` DO-list (tailor tokens before components, no default shadcn).
- Scoped to capable exec prompt (small left leaner; preserves C21 capable≠small). Verified: 83 tripwire tests (stuck nonce/substrings, C21 inequality, render determinism, exec substrings), no new arch-budget item, full core suite green.
- HONEST: guidance text — behavioral effect is soak-verified (Epic Z), not claimed now.
- Tasks #63 → completed.

### Status: codex still building B-W1 harness (bg be1mwl9z6); B-W2 specced + queued.

### B-W1 LANDED — commit `d5db4e88` (the build-depth keystone)
- codex built `WorkerdApp` harness (`packages/core/tests/_workerd_harness.py`) + integration test + retired the "Epic I deferred (local)" verifier docstrings. Harness is robust: path-escape guard, process-group SIGTERM→SIGKILL + port-down check, db-name read from wrangler.toml, wrangler_available() skip, timeouts + capped output, fully typed.
- Fable authoritative verification (not codex paste): basedpyright 0; arch-budget baseline (no new); lint-imports 2 kept; diagram fresh; core unit suite green. **Own live-proof run** (unique marker `a785d1b2…`, distinct from codex's): POST lead → authed GET 200 → KILL (port 34361 down) → RESTART → authed GET still returns the lead; unauth GET 401 before AND after. Real wrangler 4.107 + workerd + local D1.
- Verifier scope notes correctly retired the stale wording while preserving the structural-vs-runtime honesty + "hosted Cloudflare deploy owner-gated."
- Patched to campaign branch; wt-B reset to new tip (harness present).

### B-W2 dispatched (records primitive — data-layer depth)
- codex building the new `records` primitive: optional FK on EntityField, AppSpec FK-target + cycle validation, multi-table FK-ordered schema.sql + migrations/0001_init.sql, FK-ordered Drizzle, N-entity worker route table (POST-insert + auth-gated GET-list per entity), registration. Live-proof reuses the B-W1 harness (FK'd shift survives cold restart). Hard constraint: lead-gen output stays byte-identical. bg `bu4a3da10`.
- Build-depth is now SEQUENTIAL (B-W2→B-W3 auth/RBAC→B-W4 reactivity); one deep workstream. Security (Epic S) held as the focused closer per Dylan's ordering.

### B-W2 LANDED — commit `fea94682` (records primitive — data-layer depth)
- codex built a genuinely real primitive (reviewed the full worker emitter — no stubs): FK-ordered multi-table schema + migrations/0001_init.sql, FK-ordered Drizzle `.references()`, an N-entity Worker ROUTES table (public POST-insert + Bearer-gated GET-list per entity, fail-closed like lead-gen), full request validation, collision-safe TS ids, cycle detection. Optional `EntityField.references` FK + AppSpec FK-target/cycle validators.
- Fable authoritative verification: basedpyright 0, arch-budget baseline, lint/diagram clean, core unit suite green (incl. FK-enforcement unit test: bad FK → IntegrityError). **Byte-identical lead-gen proven by MY cross-checkout digest** (44e78c8d… identical main-clean vs wt-B). **Own live-proof** (marker 05cc899d…): 2-entity app (team_member ← shift.member_id), boot workerd, POST member + FK'd shift, GET 200, KILL, RESTART, GET still returns shift w/ member_id intact.
- codex correctly left lead-gen emitters (`_emit_worker_ts`/`_emit_schema_sql`/`_emit_drizzle_schema_ts`) untouched.
- Task #62 metadata updated; wt-B synced to tip.

### Next: B-W3 (auth/RBAC) — the shift-calendar heart. Speccing real per-user RBAC (users+roles+sessions+role-gated routes) on the records primitive, with a gpt-5.5 adversarial review of the generated auth (it's auth code).

### B-W3 (auth/RBAC) — codex build + Fable verification + gpt-5.5 adversarial BLOCK → fix pass
- codex built real per-user RBAC: users+sessions tables, PBKDF2 (100k/SHA-256/16-byte salt), 32-byte session token stored as SHA-256, HttpOnly/SameSite=Strict/Secure cookie, register (ADMIN_TOKEN-gated), login/logout, role-gated routes. Opt-in on `AppSpec.roles` (inert when absent).
- Fable verification: byte-identical proven by MY cross-checkout digests (lead-gen 44e78c8d + no-auth records 737bd1b8 identical). Own live-proof: register approver+member, login, member creates team_member/time_off_request, approver POST /api/approval=201, member=403, unauth=401, no login enumeration, KILL, RESTART → session persisted, RBAC holds. basedpyright 0, arch baseline, core suite green. My own line-by-line read of the shipped worker: PBKDF2/session/expiry/parameterization all correct.
- **gpt-5.5 adversarial (xhigh) returned BLOCK — 6 findings.** The pass earned its keep: it caught that the bypass was in the DATA SHAPE, not the mechanism. Triage:
  - #1 CRIT (member self-approves via client-writable `time_off_request.status`, a route the guard doesn't cover) → FIX: redesign demo (drop `status`; approval row is the only approver-gated write).
  - #3 CSRF (cookie auth, no Origin/Content-Type check), #4 login-timing enumeration (PBKDF2 only on known email), #5 ADMIN_TOKEN `===`, #6 `/api/` falls through to ASSETS on method/path mismatch → FIX all (engine hardening).
  - #2 HIGH IDOR/object-level ownership → genuinely beyond route-level RBAC; DOCUMENT as the next depth increment (truth-in-affordance, not faked). 
- Fix pass dispatched to codex (bg `b9z75kumf`). Then: re-run live-proof + RE-RUN the adversary → must reach SHIP (or only the documented #2 remains) before commit. Commit held until then.

### B-W3 fix pass 1 verified + adversarial round 2 (BLOCK on defense-in-depth) → fix pass 2
- Fix pass 1: all engine fixes (#3 CSRF, #4 login-timing, #5 ADMIN_TOKEN, #6 /api fail-closed) + demo redesign (#1 dropped `status`) + #2 documented. My own live-proof of fixed version: 415/403/404 + RBAC cold-restart all hold. byte-identical preserved.
- Adversarial round 2 (gpt-5.5 xhigh) on the PATCHED worker: BLOCK, 4 findings — but ALL defense-in-depth (timing side-channels + consistency), NO auth bypass (adversary confirms "no direct DB read/write bypass"; core RBAC sound). Triage: #1 login-timing-via-password-size, #2 logout skips origin guard (logout-CSRF), #3 ADMIN_TOKEN length leak → FIX (cheap correctness); #4 percent-encoded /api serving SPA → cheap to close (not a handler bypass — ROUTES is exact-match). 
- CONVERGENCE DISCIPLINE: round 1 caught a real CRITICAL (worth it); round 2 is marginal hardening. Fixing all 4 by class, then ONE confirming pass — not an infinite adversarial loop (budget ~2-3 rounds). If round 3 leaves only marginal/theoretical nits, accept documented + commit. Fix pass 2 dispatched (bg `bmji9xlsp`).

### B-W3 LANDED — commit `d99c4147` (auth/RBAC — adversarial round 3 = SHIP)
- Round-3 gpt-5.5 adversarial: **SHIP** — no auth bypass, no privilege escalation, all prior fixes confirmed correct (it even ran a URL-parser test to verify the encoded-path classifier). Only residual = the documented object-ownership/IDOR limitation (accepted, in OWNER_GUIDE).
- Convergence: CRITICAL (round 1) → defense-in-depth (round 2) → SHIP (round 3). 2 fix rounds, as budgeted — no infinite adversarial loop.
- Final authoritative gates on main: basedpyright 0, arch baseline, lint 2 kept, diagram fresh, core suite green. My own final live-proof: full RBAC + session cold-restart. Byte-identical (my cross-checkout digests) for lead-gen + no-auth records.
- **Build-depth arc so far: deploy-proof (B-W1) + relational data (B-W2) + auth/RBAC (B-W3) — all live-proven against real workerd.** Next: B-W4 (client reactivity, the last explicitly-requested feature), then Epic S (security closer, already fully spec'd in disco-security-fix-campaign.md), then Epic Z (soak).

<!-- append below as work lands -->
