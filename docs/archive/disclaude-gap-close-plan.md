> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Gap-close plan marked “RATIFIED — EXECUTING” against an old baseline; its targets largely exist now.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims. 🚫 **Fable 5 (Anthropic) models are off-limits to view per the project owner** — viewing them will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.

# Disclaude gap-close + AppKit-depth campaign plan

**Written:** 2026-07-03 · **Status: RATIFIED 2026-07-03 ("go with your recommendations") — EXECUTING.**
Decisions locked: (1) Drizzle over D1 in the Worker only · (2) Epic O deploy ports NOW
(owner-gated, inert without token) · (3) hard-replace old `app_*` tools + persisted-appspec
adapter · (4) retention 20 unlabeled / 512MB per conversation · (5) filter-repo/fresh-export
acceptable if the history scan finds contamination.
**Baseline:** branch `disclaude/experimental-20260628T025508Z` @ `988858ef` (REL-1e flipped).
**Scope:** a solid fix plan for EVERY open gap in `docs/disclaude-pr-status.md`, plus two
new Dylan directives (2026-07-03): (1) **build versioning + user-selectable rollback in the
UI**, (2) **kit catalog goes appkit-deep** — port the disco-nightly AppKit ("real,
functional sites as the output; Drizzle databases; high quality; building all the way to
just before Cloudflare provisioning").

Grounding: two exhaustive code maps (disco-nightly AppKit stack; disclaude
versioning/rollback machinery) + per-gap source verification. Anchors cited inline.

---

## Ground-truth facts the plans build on

- **Shared lineage.** disclaude and disco-nightly share merge-base `fb60fc88`
  (disclaude +525 commits, nightly +113; **72 appkit commits** on nightly's side).
  Port = directed code adoption, not a blind merge.
- **Nightly AppKit ≈ 13.8K LOC prod + 14.7K LOC tests (703 test fns).**
  `core/appkit` 4,890 (pure, pydantic-only, no IO) · tools layer 3,387 ·
  `appkit_cloudflare` 4,801 · verify 684.
- **What it generates:** React 18 + Vite 5 + TypeScript SPA · Cloudflare Worker backend
  (`worker/index.ts`: POST `/api/leads` with parameterized **D1** insert, Bearer-gated
  `/admin`, fail-closed) · `schema.sql` · `wrangler.toml` (Static Assets + D1 binding) ·
  6 design recipes · 37 section variants · design-lint slop gate · **seven-check strict
  verifier** (`design_lint_clean, schema_sql_valid, worker_contract, lead_form_posts,
  local_api_roundtrip, route_coverage, section_coverage` + `cloudflare_export_ready`).
- **NOT Drizzle today** — raw parameterized SQL over D1. Drizzle is an upgrade epic (B4).
- **Deploy is real, not stubbed** — `deploy.py:_run_real_deploy` runs the full wrangler
  sequence (d1 create/execute, deploy, secret put) but ONLY via owner-only HTTP routes
  behind 7 hard gates (dry-run default, confirmation phrase bound to plan-hash, sandboxed
  build required, encrypted token). The MODEL never deploys. "Just before provisioning"
  is a designed boundary, not missing code.
- **disclaude has a 328-LOC appkit stub** (`core/appkit/models.py` + P8's
  `semantic_metadata.py`) powering today's `app_*` tools — the port must RECONCILE, not
  coexist (same class names, different shapes).
- **Versioning reality:** host mirror (`ProjectStore`) is **single-slot** (overwritten
  every snapshot; `tools/projects/store.py`, `archive.py:84/175`). Events alone canNOT
  reconstruct a workspace (`file_edit` is a diff; `run_script`/image/pptx bytes are never
  event-carried). Rollback must therefore be **tree-snapshot based**, and per the
  event-state contract it must land as an **appended event**, never history mutation.

---

## A. NEW FEATURE — Build versioning + rollback (UI-selectable)

**Goal:** every build accumulates restorable versions; the user picks one in the UI and
rolls back; preview can show any version.

**A1. Versioned capture (storage layer).**
Extend `ProjectStore` from single-slot to versioned:
`<projects_root>/<cid>/versions/<NNN>-<tree_digest12>/workspace/…` + per-version
`version.json` (seq, ts, label, trigger, file_count, bytes, tree_digest) + a rolling
`versions.json` index in the conversation dir. Content-address dedup: identical
`tree_digest` ⇒ skip (no new version). Hardlink unchanged files against the previous
version (`os.link`, fall back to copy) so N versions ≠ N× disk. Reuse
`snapshot_workspace()` unchanged for the tree walk; `tree_digest` already exists in
nightly's `appkit/snapshot.py` (port it in B, or lift the 30-line digest helper now).
**Triggers:** the existing `_maybe_snapshot` call sites (`lifecycle.py:675`, post-turn +
`runtime.py:3017/3173`) become "update live mirror + cut version if digest changed AND
(turn ended with a productive mutation | finish | user-requested save)". Keep the live
single-slot mirror as `current/` (resume path untouched).
**Retention:** keep all labeled + first + latest K unlabeled (default K=20), prune oldest
beyond a byte budget (default 512MB/conversation); pruning never deletes the version a
restore came from.

**A2. Rollback as an event (loop/core).**
New `EventKind`: `WorkspaceRestoredEvent {version_seq, tree_digest, label, source:"user"}`
+ projection + TS mirror (harness contract suite will flag drift — extend it). Restore
flow: (1) validate version exists, (2) `rehydrate_workspace(version_dir → sandbox)`
(primitive exists, `archive.py:175`), (3) append `WorkspaceRestoredEvent`, (4) immediately
cut a NEW version from the restored tree (restore = new head, history append-only — the
`ReplayScrubber` model, never a rewind), (5) invalidate the A8 live-workspace-snapshot
sentinel so the model's next turn sees the restored tree. Guard: refuse while status is
RUNNING mid-turn (loop holds `self._lock`); allow on PAUSED/terminal/idle.

**A3. API (agent-server).**
`GET /conversations/{cid}/versions` (list, newest-first) ·
`POST /conversations/{cid}/versions/{seq}/restore` (owner-only; 409 while running) ·
`GET /conversations/{cid}/preview-app/?version={seq}` — extend
`_serve_static_from_snapshot` (`routes/preview.py:19`) to resolve the versioned dir
(read-only historical preview; live proxy stays version-less) ·
optional `GET /versions/{seq}/download` via existing `zip_workspace`.

**A4. UI (frontend Build surface).**
Version picker in `ExecutionCanvas.tsx` tab-bar region / `PreviewPane.tsx` header
(the natural homes per the map): dropdown of versions (label/time/trigger badge),
"Preview this version" (iframe src gains `?version=`), "Roll back" with confirm dialog
("creates a new version from vNNN — nothing is deleted"). Timeline chip on the activity
feed when a `workspace_restored` event lands. Deck editor gets the same picker later
(same API; out of v1 scope).

**A5. Interplay + tests.**
`app_snapshot_version` (AppSpec-only, `builtin/appkit.py:264`) folds in: its label
propagates to the enclosing tree version; keep the tool as the model-facing "name this
version" affordance. Tests: store versioning/dedup/hardlink/pruning units; restore-event
projection; integration restore→preview serves old bytes; **live MiniMax build → edit →
rollback → verify preview shows v1** + Playwright screenshot of the picker (house rules:
live-model proof + visual evidence).
**Estimate:** 2.5–4 days. **Dependencies:** none (pre-appkit; digest helper liftable).

---

## B. P7→APPKIT — port the nightly AppKit, then Drizzle (the deep-catalog directive)

**Strategy: staged port with reconciliation, not a merge.** Both sides diverged too far
for `git merge`; adopt per-layer with `git diff fb60fc88..nightly -- <paths>` as the
review artifact so provenance is auditable.

**B1. Core engine port (verbatim-ish).**
Copy `core/appkit/{spec,generator,recipes,section_catalog,primitives,local_verify,
snapshot,build_brief}.py` + golden fixture (4,890 LOC; pure/pydantic-only — ports clean).
**Reconciliation (the real work):** disclaude's existing `appkit/models.py` `AppSpec`
(328-LOC stub feeding today's `app_*` tools + P8 `semantic_metadata.py`) collides with
nightly's 715-LOC strict `AppSpec`. Resolution: nightly's spec WINS; write a one-shot
adapter `legacy_appspec_to_v2()` for any persisted `.disco/appspec.json`; port P8's
semantic metadata as a generator pass (nightly's generator gains the `data-disco-*`
attribute emission P8's selection layer reads — keeps P8 live). Gates: basedpyright 0,
core tests ported (~5 dedicated files).

**B2. Tool layer adapt.**
Port `app_kit.py` (4 validated-patch mutators), `verify_appkit_app.py` (seven-check),
`design_lint.py`, `appkit_scope.py`, `appkit_exec.py` (3,387 LOC). disclaude shares the
`ToolDef/ToolContext/ctx.sandbox` anatomy (same lineage) so this is import-path work +
**retiring the old thin tools**: today's `app_create/app_add_section/…` in
`builtin/appkit.py` are REPLACED by nightly's richer implementations (keep
`app_set_tweak` + tweaks IO — P9 is disclaude-ahead; nightly lacks it). The strict phase
allowlist (`appkit_scope.py`) barring raw `shell`/`file_write` in appkit builds **is**
the P4 "blunt-tool removal" (see F1). Contract registry: register the appkit contract
kind under the P7 kits funnel so kit selection routes through one place.

**B3. Runtime wiring.**
`classify_build_brief` on the WS intake (`ws.py:62` pattern), per-conversation
`appkit_mode` + `AppKitToolExecutor` (`runtime.py:1355` pattern), projects-list
lint/verify status surfacing, `appkit_dashboard` + `appkit_live_golden` scenario into the
verify runner. Prove with the scenario matrix + one live MiniMax lead-gen build passing
all seven checks, 0 OpenRouter.

**B4. Drizzle upgrade (Dylan's explicit bar — nightly does NOT have this).**
Generator emits, instead of raw-SQL worker + hand `schema.sql`:
`src/db/schema.ts` (drizzle-orm sqlite-core table defs lowered from the same entity
model), `drizzle.config.ts`, worker using `drizzle(env.DB)` (drizzle-orm/d1) for the
insert/read paths, `package.json` gains `drizzle-orm` + dev `drizzle-kit`; migrations
generated via `drizzle-kit generate` (checked-in `drizzle/` dir) replacing hand
`schema.sql` as the D1 migration source. `local_verify` extends: `schema_sql_valid`
becomes "drizzle-kit generate succeeds in the build sandbox AND emitted SQL passes the
existing sqlite structural check" (keeps the pure-python fallback when node isn't
available: structural TS parse of schema.ts field↔entity correspondence).
`local_api_roundtrip` unchanged (models the wire contract, not the ORM). This raises the
output to the "other repo" bar: typed DB layer, real migrations, high-quality stack.

**B5. Deploy layer (Epic O) port — scope decision.**
Port `models.py` + `wrangler.py` verbatim; `deploy.py`/`routes.py`/`sandbox_build.py`
adapted (SecretStore, ProjectStore, sandbox backends all exist in disclaude; gVisor
build-sandbox verifies on VM 201). **Ship posture = exactly Dylan's line:** everything
through `cloudflare_export_ready` + owner `POST /connect` + dry-run plan is in; real
`execute_deploy` stays behind the same 7 gates (owner-only, never model-callable).
**Estimate:** B1 1.5d · B2 1.5–2d · B3 1d · B4 1.5–2d · B5 1.5d ⇒ **7–8 days** total,
parallelizable to ~4–5 with the harness (B1/B2 fan-out lanes; B4 after B1).
**Dependencies:** none hard; A's digest helper comes free from B1's `snapshot.py`.

---

## C. Tier-2 honesty pass

**C1. F6 rewrite-directive (computed-but-unwired).**
It's already in the weak-assist enumeration (`exec_policy.py:30`) and computed in
`stuck.py` but its directive never reaches the prompt. Plan: wire the computed directive
into the assist prompt injection **only when `ModelExecutionPolicy` resolves weak-tier**
(house rule: weak-model compensations gated off for capable models), one unit test per
tier, note in the pack docs. If live weak-tier probing shows it harms MiniMax, delete F6
entirely instead (honesty over scaffolding). **0.5d.**

**C2. P8×RC-O conflict (selection-edit reverted by the dictated-content floor).**
A user's click-edit that contradicts a build-prompt literal gets reverted at finish by
`dictated_content_gate_passed` (`finish.py:1529`). Fix: user intent outranks stale
dictation — when a `selection_edit` frame lands, append the host-owned marker (extend the
existing scoped-edit directive emission in `core/selection_edit.py`) recording
`{literal_superseded_by: event_id}`; the RC-O gate's literal harvest skips literals whose
covering selection-edit is newer. Regression test: build w/ dictated literal →
selection-edit that literal → finish must NOT revert. **0.5–1d.**

## D. REL-2a manifest reader promote (banked flip)

Same discipline as REL-1e: identify the two ad-hoc readers that should consume the
artifact manifest projection (`context/artifact_projection.py`) — finish-gate deliverable
resolution + export-path artifact lookup — put the manifest read behind
`DISCO_ARTIFACT_MANIFEST_READER` default ON with explicit-off, shadow-compare log line
for one soak, live probe, flip. Lesson applied: verify the shadow data EXISTS before
claiming agreement. **1d.**

## E. P2 completion — host-assembles-document + schema-validated parts

Scope to ONE contract kind first (`document/report`). Model writes **parts**
(`.disco/parts/<section>.md` via a `doc_set_section` tool with pydantic schema validation
at write time — the "single mandated format"); host assembles the final document at
export through the same read-back choke point P10 built (`SlidesGenerate.run` pattern),
stamping `ExportRenderFacts`. `ExportContract.pipeline` tuple (`contract/models.py:110`)
then becomes honest: `("preflight","bundle","validate","deliver")` each mapped to a real
step (validate already real via P10); any stage we don't implement gets REMOVED from the
tuple rather than left as inert data. **1.5–2d.** (AppKit apps already meet P2's spirit
via spec→generate; this closes it for documents.)

## F. P4 completion — streaming edit, hot-reload, blunt-tool removal

**F1. Blunt-tool removal:** adopted from B2's `appkit_scope.py` phase allowlist (raw
`shell`/`file_write`/`code_exec` barred in appkit build phase; `file_write` stays
repair-only in the generic contract, already scoped in `contract/registry.py:33-65`).
Done-by-port + one enforcement test. **Free with B2.**
**F2. Hot-reload:** generated apps are Vite; run `vite dev` in the sandbox for appkit
conversations and make the preview proxy pass WebSocket HMR frames through
(`routes/preview.py` live path + `wake_for_preview`). Fallback stays static serve.
**1d.**
**F3. Streaming edit:** the driver already streams tool args (streaming-driver
invariant); surface it — pipe in-flight `file_write/file_edit` arg deltas over the
existing WS as a `file_stream` frame; frontend files tab renders the growing file
("watch-it-write"). Cut if WS frame plumbing balloons; it's UX, not correctness. **1–1.5d.**

## G. P6 completion — builder/verifier context split

Verify currently runs in-band in the builder's context. Split: verification executes as a
**separate bounded context** — a `ModelRole.VERIFIER` invocation seeded with ONLY
{contract, deliverable paths, seven-check results, screenshot} that returns the typed
verdict; the builder's context receives just the `VerifierVerdictEvent` summary (never
the verify transcript). The host verifier (REL-1e) already runs model-free; this adds the
model-judged layer for checks that need reading (copy quality, brief-fit) without
polluting builder context. Wake-on-fail already exists. **1.5d, after B3.**

## H. P9 residue

Versioning-by-copy → **subsumed by A** (whole-tree versions supersede AppSpec-only
copies). `file_write` description over-nudging rewrites → reword to steer at
`file_edit`/appkit mutators first (`files.py` FileWriteArgs description). **0.25d.**

## I. P10 residuals — disposition (each one, explicitly)

Keep-as-documented (all are in the module threat-model note): marp ±1 slack (renderer
variance, refusing on it = false-blocks) · media-by-existence (image bytes present ⇒
content; entailment would need vision — revisit only if a blank-with-images deck escapes
in a soak) · single-CRC-bad-slide tolerance · marp-PDF byte-floor · bounded cap-release
(deliberate anti-livelock). **Action:** add a soak counter (export-gate refusals + release
events per run) to the verify dashboard so drift shows up in data, not anecdotes. **0.25d.**

## J. P11 release engineering (the ship gate)

1. **Secret scrub:** `gitleaks` over full history + runtime env audit; the B6 "real
   secret leak" flag from the surgical-swath memo gets root-caused first (if history is
   dirty: filter-repo BEFORE the repo is ever public; decision point for Dylan).
2. **README + docs pass:** quickstart (uv sync → dev-up → first build), provider matrix
   (keyless defaults), architecture pointer, hardware guidance; scrub internal
   codenames/paths.
3. **Self-host verification:** clean-machine install on LXC 199 (standing 8GB test box)
   following ONLY the README; fix what breaks; repeat until clean twice.
4. **Packaging:** compose.yaml parity (nightly has one — adopt), pinned uv lock, `.env.example`.
5. **CI:** ci.yml exists (4 gates + units); add release workflow (tag → build artifacts
   → changelog); nightly e2e-live stays self-hosted-runner-only.
6. **License:** verify the MIT reranker default actually landed in disclaude (audit said
   swap `jina-reranker-v2` CC-BY-NC → `bge-reranker-v2-m3` MIT); ACKNOWLEDGEMENTS pass.
7. **v0.1 tag + CHANGELOG.**
**Estimate: 3–4d.** Last in sequence — everything above merges before the scrub/tag.

## K. questions_v2 (small)

Per playbook §8: a structured pre-plan intake tool — in interactive mode the model may
ask ONE batched clarification round (typed `questions_v2` frame: ≤4 questions, options +
free-text) before `submit_plan`; in autonomous mode it's skipped and assumptions are
logged into the plan preamble instead (defer-don't-block encoded). Frontend renders the
frame as a form. **1d.**

## L. Arch-budget red (25 pre-existing items)

**FinishGate (2,058 LOC > 800) is the one that matters** — decompose by cohesion into
`finish/` package: `verify_gates.py` (browser/host/export + delegation),
`content_gates.py` (DoD/dictated/execution-nudge), `finalize.py` (normalize/finalize/
notify valve), thin `FinishGate` facade preserving the public API + the no-relock
invariant (extracted helpers never re-acquire `self._lock` — CLAUDE.md). Mechanical,
test-covered by the existing suites. **1.5d.**
The other 24: route router factories + `pi_chat_completions` are flat endpoint
registrations — whitelist as capped dispatchers (`ALLOW_FUNCS` with justification), which
is the budget script's sanctioned path; decompose `view.of` (238) and
`synthesize_section` (258) only when next touched (boy-scout rule), whitelist-capped
until then. **0.5d.**

## M. M3 actionless stall (dominant real-build failure)

Two structural fixes, one measurement:
1. **Auto-resume ladder for autonomous builds:** PAUSED-actionless in autonomous mode
   currently waits for a human (today's stalled probe needed manual `/resume`). Add:
   pause #1 → host auto-resumes ONCE with a concrete continue-nudge; pause #2 → the
   REL-RC-P synthetic-finish valve fires (already built; today the counter resets on
   resume — persist the count in events, not loop memory, so the valve survives
   resume/recreate). Live-prove on a scenario that forces the stall.
2. **Context pressure relief:** the stall correlates with long context; wire the
   compaction cadence to fire BEFORE the stall zone (measure token depth at historical
   stall points from soak event logs; set the condenser threshold under it).
3. **Measurement:** stall-rate per 20-build soak becomes a tracked number on the verify
   dashboard (it was 1/5 today informally — get a real baseline, then prove the ladder
   drops it). **2d + soak time.**

## N. Doc staleness (immediate)

`disclaude-pr-status.md`: P6 row still says "advisory by default" (REL-1e flipped it) ·
Tier-2 item 3 still lists P8 wire + `ExportContract.validate` as open (both landed) ·
P11 row says "not started" while README/ci.yml exist (reword to "not release-grade").
**15 min, do first.**

---

## Sequencing (dependency-honest)

```
N (15min) → C1+C2+H (≈2d, parallel lanes) → A (versioning/rollback, 2.5–4d)
→ B1..B5 (appkit port + Drizzle, 7–8d serial / ~5d harness-parallel)
→ F2+F3 + G + D + E (post-port wave, ≈5d parallel lanes)
→ K + L + M (≈4d) → I dashboard counter → J release engineering (3–4d) → v0.1
```
Total ≈ **20–24 working days serial; realistically ~14–17 with harness fan-out** (the
labor-policy cascade: MiniMax/Gemini workers on mechanical lanes, Codex implementing
reviewed briefs, Fable gating + live proofs).

## Decisions needed from Dylan before execution

1. **Drizzle scope (B4):** Drizzle over D1 in the Worker only (recommended, matches the
   generated stack), or also a local-sqlite dev harness with drizzle-kit studio?
2. **Deploy epic timing (B5):** port Epic O with the appkit now, or defer real-deploy
   routes to post-v0.1 (export-ready + OWNER_GUIDE only)? Recommendation: port now —
   it's the proven, audited path and the gates keep it inert without an owner token.
3. **Old app_* tools:** hard-replace with nightly's (recommended; adapter migrates
   persisted appspecs), or keep both behind contract kinds?
4. **Version retention defaults (A1):** 20 unlabeled / 512MB per conversation OK?
5. **Secret-scrub posture (J1):** if gitleaks finds history contamination, is
   filter-repo/fresh-export acceptable? (Determines whether the public repo keeps this
   branch's history.)
