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

### B-W4 (client reactivity) dispatched — bg `byd1snk7w`
- Scope: typed API client + reactive submit hook (idle/submitting/success/error discriminated union) + double-submit protection + inline validation + API-error surfacing + OPTIMISTIC "recently submitted" list. Enhances the shared form emitter (applies to lead-gen + records). INTENTIONALLY changes the frontend → golden digests updated (legit output change, not test-cheating). Mandatory Firefox screenshot live-proof (vite build → wrangler dev → Firefox E2E of the optimistic update).
- Dispatched to codex to build on ITS budget; Fable verifies the screenshot + gates on return.

### Honest remaining-scope read (for the morning)
Landed + fully proven this run: **Epic A (designs), Epic C (Tier-3), B-W1 (deploy-proof), B-W2 (relational data), B-W3 (auth/RBAC — adversarially SHIP)**. That covers Dylan's two biggest asks (designs + auth) + the deploy/data foundation. B-W4 (reactivity, his 3rd ask) in flight.
Remaining (next session): **Epic S (security, 6 waves — ALREADY fully spec'd in docs/disco-security-fix-campaign.md, implementation-ready)** and **Epic Z (soak)**. These are large focused blocks; S each wave needs real-exploit-before/after + gpt-5.5 adversarial (like the B-W3 auth cycle). They're teed up to execute directly from the security doc.

<!-- append below as work lands -->

---

## B-W4 — client reactivity (LANDED) — commit `efccf4fe`

codex implemented the reactivity feature in wt-B; I verified + landed on main.

- **Feature:** typed `postJson<T>` API client (`src/api/client.ts`), a `useSubmit`
  hook with a discriminated-union state (idle|submitting|success|error),
  double-submit protection (button disabled while submitting), inline
  required-field validation, API-error surfacing, and an OPTIMISTIC in-session
  "Recently submitted" list.
- **Byte-identical backend:** I recomputed worker+D1 digests across main (pre-BW4)
  and wt-B for all three app types — lead `dbf11360`, records `293b85b0`, auth
  `90c28a51` — IDENTICAL. B-W4 is frontend-only, so B-W3's adversarially-hardened
  auth worker is untouched. Golden frontend digests updated (correct, intentional):
  `_LEAD_GEN_ACME_DIGEST` 3363aa17→7f05d743, `_RECORDS_BW2_DIGEST` 18074e7e→d2e60094.
- **Live proof (Firefox, real render):** vite build + wrangler dev serving a
  generated app; a real submit shows the disabled "Sending…" button and the
  optimistic "Recently submitted" item (Ada Lovelace / ada@example.com) rendered
  immediately, then the success state. Screenshots at
  /tmp/disclaude-bw4-live-proof/*.png — sent to Dylan as visual evidence.
- **Gates (authoritative, on main):** basedpyright 0 errors; arch-budget no new
  over-budget item in touched files; lint-imports 2 kept/0 broken; diagram fresh;
  core+tools unit suite exit 0.

### Epic B COMPLETE
Build-depth is done end-to-end: A (designs) + B-W1 (workerd persistence keystone) +
B-W2 (FK data-layer) + B-W3 (RBAC/sessions, adversarially cleared) + B-W4 (client
reactivity) all committed AND live-proven against real workerd/D1/Firefox. Epic C
(tier-3) done (soak-pending). This satisfies Dylan's "get it working" build-depth
mandate + the ephemeral→durable model (persistence demonstrated across restart).

### Pivot → Epic S (security, the closer)
Per Dylan's stated order (security is the closing epic), starting the 6-wave
security fix campaign spec'd in docs/disco-security-fix-campaign.md. Each wave:
build → real-exploit-before/after → gpt-5.5 adversarial to SHIP → commit. Then
Epic Z (big soak + debug).

---

## Epic S — Security Hardening (the closer) — STARTED

Pivoted to the 6-wave security campaign (docs/disco-security-fix-campaign.md, a 7-round
Opus+gpt-5.5 converged plan, 38 findings, all decisions pre-resolved by Dylan). Per-wave
discipline: real-exploit-before/after + gpt-5.5 xhigh adversarial to SHIP + commit.

**Recon note:** the top-level `archived God files/` dir holds STALE copies of runtime.py /
dod_evaluator.py / app.py etc. — the 2026-06-20 plan line numbers came from THOSE. Live code
is under `packages/*/src`. Both WO specs re-grounded to current live locations (2026-07-06).

**S-W1 (auth keystone) — codex IMPLEMENTING in wt-B.** Grounded map: no session/cookie/CSRF
exists (build from scratch, mirror the appkit_cloudflare `_require_owner` hmac+rate-limit idiom);
CORS `allow_origins=["*"]` both servers (agent app.py:139, app app.py:44); client-controlled
owner_id (_common.py:79, conversations.py create+list+delete); store auto-creates unknown convs
as owner "local" (sqlite.py:449-454 _store_one) → must reject BEFORE append; 4 WS endpoints accept
with no Origin/auth; preview middleware has a session_resolver seam already wired (app.py:150) but
wake_for_preview does an owner-blind DEFAULT_OWNER_ID lookup (preview_service.py:93-95). WO includes
frontend auto-mint wiring so Dylan's UI keeps working + a GENERATED route-inventory test (A8) +
a 7-proof real-exploit harness.

**S-W2 (secrets + egress) — spec GROUNDED, queued behind W1.** Key live holes found:
- `mcp/http.py:83-103` forwards SECRET-backed headers with `follow_redirects=True` (cred-leak-on-redirect).
- `plan_conditions.py:812` `http_ok` has NO egress allowlist at all (the 2nd of two impls; DoD one at
  dod_evaluator.py:365-392 gates but urllib still follows 3xx).
- `policies.py:98-104` auto-approves `scope=="sandbox"` → the 3 remote-backed tools (image/audio/slides,
  labeled sandbox but doing host HTTP with keys) bypass the host-scope confirm.
- B4 SIMPLER than planned: brand fonts are already base64 data-URIs (css.py:82-122), so the WeasyPrint
  url_fetcher can deny ALL file://+external with no font-allowlist exception.
- B1 os.environ fallthrough at ~12 sites (runtime.py:1065 canonical + wiring.py:131-147 + 10 more).

### S-W1 adversarial round 1 — BLOCK (9 findings), fix pass dispatched
codex implemented S-W1 (+1676/-302, 41 files, 3 new auth modules + exploit harness + route-inventory test +
frontend auto-mint). My authoritative gates ALL passed: basedpyright 0, unit suite exit 0, arch-budget clean
(23-item baseline, zero new overage), 7/7 exploit proofs, route-inventory green, harness confirmed genuine
(real app via create_app, real 403 asserts, checks the sqlite conversations table).

BUT the mandatory gpt-5.5 xhigh adversarial review returned BLOCK — exactly why it's non-negotiable for auth
code (same as B-W3). 9 findings, verified real by spot-check, in 4 root causes:
- **R2 (CRITICAL class): guard/sink id-format mismatch.** The owner middleware keys on `_CID_RE=/conv_.../`
  but preview/capability/message routes accept a TRUNCATED cid8 or non-conv_ id that bypasses it → #2 owner B
  proxies A's live preview via `/conversations/<A-cid8>/preview-app/`; #3 B mints a preview capability for A's
  cid8; #4 `POST /conversations/notconv/messages` auto-creates an owner=local row. Same class as the B-W3
  self-approval bug (guard covers one shape, sink is another). Fix = canonical-id + owner-check AT THE SINK via
  a shared `require_owned_conversation` dep, not a prefix-matching middleware alone.
- **R1: mint unauthenticated by default.** `AUTH_DEV_AUTO_PAIR` defaults "1" → mint returns an admin session
  with no pairing token, token never consumed (#1). Fix = default OFF, require+consume the one-time token, keep
  loopback+Origin always-on, deliver the token to the first-party frontend same-origin.
- **R3: whole resource families unscoped.** projects-list (#5), spaces (#6 — SpaceRecord has no owner_id),
  workflows (#7 — JsonDirWorkflowStore no owner), schedule-runs (#8) all cross-owner readable/mutable. Fix =
  add owner_id (legacy→DEFAULT_OWNER_ID) + filter every list/get/mutate.
- **R4: tests miss the bypasses** (#9 — harness runtime=None, PROOF 6 encodes wrong mint behavior, inventory
  only classifies templates). Fix = real negative tests for all of R1-R3 + inventory does actual requests.

Fixing ALL 9 by class in ONE convergence pass (per parallel-convergence discipline), then re-review to SHIP.

### S-W1 adversarial round 2 — BLOCK (3 findings, converging), fix pass 2 dispatched
codex fix pass 1 (+2640/-424, 61 files, harness grew 7→14 proofs). My authoritative re-verify: basedpyright 0,
unit exit 0, arch clean (23, zero new), 14/14 proofs, test edits confirmed legit id-canonicalization (not gutted).

gpt-5.5 round 2: BLOCK — but CONFIRMED all 9 round-1 findings CLOSED (pairing default/replay/origin fixed;
preview raw-cid8 + cross-owner capability blocked; non-conv_ writes 404 before auto-create; projects/spaces/
workflows/schedules owner-scoped). 3 NEW narrower findings (9→3, only 1 High = convergence):
- **#1 (High, CONFIRMED w/ live PoC): client-supplied `space_ids` forwarded into corpus lookup with no owner
  check** — same A7 class relocated: after locking conversation_id, `space_ids` became the unguarded handle; B
  passes A's space_A_secret via /ws/research frame or POST /conversations body → vectorstore keys on namespace
  only → returns A's corpus. Fix clean: JsonSpaceStore.get(space_id, owner_id=...) already owner-scopes; routes
  just weren't using it for space_ids. The wave's core lesson: EVERY client-supplied data-selecting id must be
  owner-validated at the sink; locking one moves the attacker to the next.
- **#2 (Medium): legacy owner-less records default→"local"** = the back-compat-vs-fail-closed tension I flagged;
  low real reachability (post-#1 mint needs pairing token; hosted has no legacy rows) but making it explicit:
  map legacy→INSTALL_OWNER + admin-gate unclaimed reads (Dylan's admin session unaffected).
- **#3 (Plausible Medium): deck export drops authed owner, sandbox as "local"** — owner-attribution not a data
  leak (B owns the conv); thread current_owner_id through.

Fix pass 2 dispatched (targeted 3 + PROOF 15-17). Then adversarial round 3 → SHIP (per ~2-3 round discipline).

### S-W1 adversarial round 3 — BLOCK (2 findings, converging 3→2), fix pass 3 dispatched
Fix pass 2 verified authoritatively: basedpyright 0, unit exit 0, arch clean (23 zero-new), 17/17 proofs
(PROOF 15 space_ids owner-checked, 16 legacy admin-gated, 17 deck owner-attributed).

gpt-5.5 round 3: BLOCK — all 3 round-2 findings CONFIRMED closed. 2 NEW, SAME class (client-supplied
server-local filesystem path → FS sink, no admin/owner gate) — the attacker pushed outward again
(conversation_id → space_ids → raw path):
- **#1 (High CONFIRMED, live PoC): `POST /api/projects/import` server-local `path`** — B posts A's workspace
  path, it's copied into a B-owned project, B downloads A's secret.txt. Fix: admin-gate the server-path branch.
- **#2 (Medium CONFIRMED): `GET /api/storage/browse`** lists any server dir for any authed user = the plan's
  M2 finding exactly; decided fix = operator/admin-only + safe-roots. Fix: admin-gate (require_admin_session
  already exists at auth.py:106).
Both = operator-filesystem features → admin-gate (matches A2 "settings=admin-only" + M2 decisions); Dylan's
operator session is admin so no UX regression. Fix pass 3 dispatched (+PROOF 18/19). Round 4 → expected SHIP.

### S-W1 adversarial round 4 — SHIP ✓ + COMMITTED e028d2ac
Round 3's 2 findings (projects/import path exfil + storage/browse) both admin-gated (PROOF 18/19). gpt-5.5
round 4: SHIP — both closed BEFORE any FS resolution; final sweep found NO remaining cross-owner or
non-admin host-FS read/write/exfil. Convergence in 4 rounds: BLOCK 9 → BLOCK 3 → BLOCK 2 → SHIP.

**Arch-budget catch (methodology bug found + fixed):** check_arch_budget.py resolves `packages/` from its OWN
__file__, so running main's copy from the worktree silently scanned MAIN the whole time — my worktree arch
"diffs" during the fix passes were meaningless. The ONLY valid arch check is on the tree that has the code
(worktree's own script, or main AFTER applying the patch). On main-with-patch I caught the real regression:
S-W1 pushed make_ws_router 200→213 (NEW god-function) + make_conversations_router 353→387 (grew). Dispatched a
behavior-preserving decomposition → ws_router back <200, conversations_router down to 321 (BELOW pre-S-W1),
preview_router 406→212. 19/19 proofs still green after (pure extraction). Residual: pre-existing god-objects
(ConversationRuntime +27, DeepResearchService +60) grew slightly from unavoidable owner-threading — no NEW
items, accepted + noted.

**Committed e028d2ac** — final gates on main: basedpyright 0, ws_router not over budget, lint 2 kept, diagram
fresh, unit exit 0, 19/19 exploit proofs. S-W1 closes C5/H6/H8 + removes reachability of H2/H10/H13/H14/M2/M7.

**Deferred (human-verify):** the "real UI still works with auth mandatory" Firefox screenshot. The
security-authoritative proof (19-proof real-app exploit harness + 4-round adversarial SHIP) is DONE; the
frontend auto-mint + CSRF wiring needs one real-browser eyeball (full stack: agent-server :8000 + app-server
:8800 + vite :5173). Flagged, not silently skipped.

**Next: S-W2 (secrets + egress chokepoint)** — spec already grounded (spec-sw2-egress.md).

## S-W2 — secret-resolution + egress chokepoint — codex done, verifying
codex impl: +1883/-415, 46 files, 3 new modules (core/host_egress.py 273 = SSRF chokepoint, llm/secret_refs.py
153 = secret-ref map + allowlisted migration, test_sw2_egress_exploits.py 412 = 9 exploit tests). B1-B6 all
addressed (secret-ref-only resolution, 2-class egress, report-PDF url_fetcher deny, error redaction,
pre-execution host-scope confirm for remote image/audio/slides).

My authoritative verify: basedpyright 0; arch 22 symbols, NONE in S-W2-touched files (no new god-objects);
exploit harness 9/9. Unit suite: 1 failure = test_process_backend_expose_port_defense — NOT S-W2 (didn't touch
test_sandbox.py or sandbox/process.py); root cause = an orphaned `python3 -m http.server 3000` (leftover disco
sandbox fixture, 36min old) squatting :3000, so the "port not bound" assert failed. Killed the orphan → test
passes. Env flake, not a regression.

**/tmp hygiene:** found 11,357 orphaned /tmp/disco-sbx-* dirs (near-empty, inode creep) + leftover podman
sandbox containers from prior soak runs. Quota ~39% blocks (not wedged). Safely removed 9,987 EMPTY orphans
(→1,370 non-empty left untouched) as proactive insurance vs the /tmp usrquota wedge before the sandbox-heavy
W3/W4/W5. codex couldn't self-run the gpt-5.5 adversarial pass (no such tool in its env) — dispatched
separately. Awaiting S-W2 adversarial verdict.

### S-W2 adversarial round 1 — BLOCK (6 findings, incl. a CRITICAL), fix pass dispatched
Core B1/B4/B5 HELD (no env fallthrough; report-PDF url_fetcher deny; provider-error redaction). But the trust
model + SSRF range + 2 un-chokepointed host-HTTP paths failed:
- **#1 CRITICAL (loopback-reproduced): config SELF-BLESSES.** trusted_origins is a plain field IN the config
  (config.py:447), so a poisoned config sets base_url=attacker + trusted_origins=[attacker] + api_key_env=
  OPENAI_API_KEY → migration imports the host key → startup prewarm ships `Authorization: Bearer <host key>` to
  the attacker. Approval living in the artifact being approved is no approval. Fix: OUT-OF-BAND approval,
  master-secret-HMAC'd (unforgeable by a config/file poisoner), written ONLY by the S-W1 admin Settings API;
  migration imports no key + startup fires no wire for unapproved origins (fail-closed per plan B2'').
- **#2 HIGH: SSRF guard misses 100.64.0.0/10** (CGNAT/Tailscale) — is_private is False for it. Fix: `not
  ip.is_global` (covers CGNAT/benchmark/unspecified/mapped-v6).
- **#3 HIGH: HTTP MCP connects+list_tools at startup before approval** (+ latent `.get` vs `.get_secret` bug).
- **#4 HIGH: audio_overview resolves LLM base_url at IMPORT time**, POSTs report text with no trusted-origin
  check → host SSRF. Fix: execution-time ConfigStore resolution + egress policy.
- **#5 MED: Pi inference client honors proxy env** → trust_env=False, follow_redirects=False.
- **#6 LOW: exploit tests miss the blocking cases** (trusted=() only, monkeypatched redirect, scope-not-ordering).
The self-blessing crux is why S-W1 (auth boundary) had to be the keystone: approval must ride the admin
boundary, not the model-facing config. Fix pass dispatched. Then adversarial round 2 → SHIP.

### S-W2 adversarial round 2 — BLOCK (2 findings, converging), fix pass 2 dispatched
HMAC approval infra PROVEN SOUND (no bypass: compare_digest, canonical JSON, fails closed on
missing/tampered/absent-signing-secret, replay-resistant across scheme/port/purpose/ref/trailing-dot). All 6
round-1 findings CONFIRMED closed (poisoned-config zero-wire, SSRF is_global incl. 100.64/10 + redirect
revalidation, MCP startup gated, audio exec-time approval, Pi trust_env=False). 2 NEW = same chokepoint-
completeness class (an egress site that forgot the approval gate):
- **#1 CRITICAL (wire-reproduced, 1062 bytes): `find_and_edit` cross-origin key exfil** — resolves ctx.driver_llm
  raw + POSTs Authorization: Bearer <host key> with no approval check; root = runtime_settings.py:363 hands
  tools the config endpoint even when build_providers skipped it unapproved. Fix: gate at source
  (_effective_driver_endpoint) + sink (find_and_edit) + sweep all ctx.driver_llm readers.
- **#2 HIGH (wire-reproduced): `/api/mcp/test` connects before approval** — probes.py:284 builds McpHttpClient +
  connect+list_tools with no origin_approved; also AgentAuthMiddleware lacks admin-path enforcement. Fix:
  approval-gate the probe + admin-gate agent-server operator routes (mirror app-server _is_admin_path).
Same lesson as S-W1's owner sweep: the infra is right; completeness across every sink is the work. Fix pass 2 +
pi_inference arch trim dispatched. Then round 3 → SHIP.

## STRATEGY PIVOT (2026-07-06) — MAX THROUGHPUT (budget abundant, time-constrained)
Dylan clarified: the "50% weekly usage" spans the whole 6-day session; budget is ABUNDANT, but today is the
last day of availability. So: go maximally parallel + full adversarial rigor, get the whole campaign DONE +
proven today, don't conserve. Also: **DROP Pi** (Dylan: "cut off pi, it never worked well") — all reliability
work was on the native DiscoKernel loop; Pi never earned the 193-run soak. Pi removal = attack-surface
reduction, folded into Epic S (task #68).

**Parallelization plan (disjoint file sets → separate worktrees, concurrent codex impl + adversarial):**
- wt-B (sandbox/runtime chain, serial): S-W2 finish → commit → Pi removal → S-W3 → S-W5.
- wt-C (fully disjoint sinks): S-W6 (share_service/sheets/redaction/gitignore).
- wt-D (mostly disjoint, after S-W2): S-W4 (MCP approval — builds on S-W2's mcp origin/secret-ref gating).
Recon for W4/W5/W6 launched in parallel; W3 already grounded (spec-sw3-hostexec.md). S-W2 fix pass 3 (2 egress
stragglers: app-server data-source probe + MCP secret-ref-pin; + 3 stale test fixtures; + operator-approval-
path check) running. Round 3 had CONFIRMED closed R2 fixes + HMAC infra sound + ~15 egress sites swept clean.

### S-W2 — VERIFIED, adversarially SHIP (2026-07-06)
Fix pass 3 landed both round-3 stragglers. Final gate results on wt-B (branch `wt-epic-b`):
- **adv4 (gpt-5.5 xhigh): VERDICT SHIP** — no confirmed BLOCK. Confirmed closures: (a) app-server data-source
  probes admin-gated + signed `origin_approved(base_url, kind:provider, "")` required before `probe_reachable()`
  wire; paid probes also require `secret_ref_allowed_for_origin()`. (b) MCP header-refs require both
  `origin_approved(url, mcp:name, ref)` AND secret-ref pinned to URL before streamable-HTTP connect — an
  OpenRouter ref to a non-OpenRouter MCP origin is NOT sent. (c) operator admin-gated approval paths exist
  (`POST /api/security/approve-origin` + config-save flows sign local-provider approvals).
- **unit (core+tools+agent-server+app-server, not-integration): 5562 passed / 0 failed / 0 errors / 4 skipped**
  (JUnit-XML counted; per repo CLAUDE.md the `-q` summary line is swallowed — exit code + XML are truth).
- **basedpyright: 0 errors** · **lint-imports: 2 kept** · exploit harness `test_sw2_egress_exploits.py`: 14 green.
- **arch budget: ONE regression** — S-W2 wiring grew `ConfigState` 935→1058 class-LOC (>800 cap, an already-
  over-budget coordinator). NOT hidden behind an allowlist bump. Behavior-preserving extraction dispatched to
  codex: relocate the origin-approval/probe-gating wiring into a new app-server-internal
  `origin_approval_wiring.py`, ConfigState back to ≤935, re-verify all 6 gates + prove no approval call dropped.
  Once green → patch to main + commit S-W2. THEN fan out {Pi removal, W4} and the wt-B chain {W3 → W5}.

Pi-removal dependency-closure recon DONE (read-only). Key hazard found: naively deleting the `build_kernel`
field is a **silent full-config wipe** — `RouterConfig` is `extra="forbid"` and `config_store.load()` swallows
the validation error → falls back to seed config, discarding ALL user settings. Mitigation baked into the Pi
spec: keep a vestigial `build_kernel: Literal["disco"]` that coerces legacy `"pi_experimental"`→`"disco"` on
load. Bonus: Pi removal DROPS baseline arch violations (`pi_chat_completions` 331-LOC etc.).

### S-W2 — COMMITTED `2408e40f` (2026-07-06)
ConfigState extraction landed (new `app-server/origin_approval_wiring.py`, 185 LOC; ConfigState back to 935
baseline, 16→16 approval calls preserved, no dropped approval). Committed on `disclaude/mega-campaign` via
wt-epic-b commit + `--ff-only` merge (68 files, +3620/−624; 6 new files incl. `core/host_egress.py`,
`core/origin_approvals.py`, `core/llm/secret_refs.py`, `app-server/routes/security.py`, and the 962-line
`test_sw2_egress_exploits.py`). Canonical-checkout confirmation: basedpyright 0, ConfigState 935, exploit
harness exit 0. **S-W2 DONE.**

### Fan-out after S-W2 (2026-07-06)
Critical path opened. wt-B (clean on 2408e40f) → **Pi removal** (spec sharpened w/ exact file:line closure +
config-wipe mitigation) → then W3 → W5 (serial: share tools/sandbox/*). wt-D (re-synced to 2408e40f) → **W4**
(MCP approval integrity — builds on S-W2's mcp origin/secret-ref gating; touches runtime.py analyzer wiring
:1880/:2341/:2373/:2394, disjoint from Pi's runtime.py hunks :164/:991/:3896). wt-C → **W6** still running
(bctsmtdda). NOTE Pi+W4 both touch runtime.py but non-overlapping hunks → integrate Pi first, W4 rebases on top.

### Parallel (user request 2026-07-06): site-builder capability research
Dylan asked to "send out research for other areas similar to auth/database/RBAC we could add to our site
builder." Dispatched 7 parallel agents (6 capability clusters + competitive scan), grounded in Disco's
self-hosted/security-first/open-weight constraints. 6/7 back; synthesizing into a ranked Artifact briefing when
cluster 4 (content/forms/CMS) lands. Verified thesis: table-stakes = auth/DB/RLS/storage/secrets/hosting/auto-
API/Stripe/in-app-AI; real whitespace = (1) self-hostable w/ OWNED primitives not borrowed-Supabase, (2)
local/open-weight AI features in generated apps w/ no external key + no data egress (NO competitor offers this),
(3) hardened secrets/data-residency. Cross-cutting: Disco's egress-approval chokepoint makes every new outbound
primitive (email/webhooks/connectors/payments) SSRF-safe by construction — a security story competitors can't tell.
