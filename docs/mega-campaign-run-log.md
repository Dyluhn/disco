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

<!-- append below as work lands -->
