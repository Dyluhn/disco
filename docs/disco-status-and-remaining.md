# Disco — status & remaining items (2026-07-07)

Snapshot at branch `disclaude/mega-campaign` @ `f09411da`. Working tree clean.

## Where we are

The builder-primitive framework and a first batch of catalog primitives are shipped and merged, each independently re-verified (full core+tools suites + the four fitness gates green at every merge).

| Area | State | Commit |
|------|-------|--------|
| Primitive framework (`PrimitiveDefinition`: tier/host_contract/spec_schema/verify/apply_spec) | shipped | `68088170` (A0), `f6a56ec3` (A1) |
| `app_add_primitive` tool (validate spec → fold → regenerate → provenance) | shipped | `f6a56ec3`; visibility fix `4cbaa218` |
| Host-service registry + dispatcher + `svc.ping` | shipped | `2c8e56fd` |
| Verify dispatch + finish-gate hook | shipped | `4cbaa218` |
| `form` primitive — typed fields, server-validate, D1 table, owner inbox | shipped | `a656334c` |
| `seo` primitive — meta/OG/JSON-LD + sitemap.xml + robots.txt | shipped | `055fb99a` |
| `collection` primitive — team/menu/testimonials | shipped | `172ce70d` |

Registered primitives in the tree: `lead_gen`, `directory`, `records`, `hello`, `form`, `seo`, `collection`.

**Honest edges on the shipped primitives:**
- `form`: attaches to `lead_gen` apps only; **form-folded apps are refused at deploy** (deploy.py's worker check predates the form emitter); `success_message` not editable via `app_update_content`.
- The live-model end-to-end proof is **CONFIRMED** — deepseek-v4-pro added `seo` + `form` to a real site via `app_add_primitive`, rendered correctly (see `docs/disco-project-state.md`).
- **NEW top-priority bug the proof surfaced:** an appkit autonomous **finish deadlock** (dictated-content false positive on quoted tool args + finish-verify shell-probe blocked in appkit scope + execution-nudge resetting refusal caps). Tool work succeeds but the build can't cleanly terminate. Engine/feature work — detail in project-state.
- Standing baseline: `check_arch_budget` fails (~19 pre-existing god-object violations) — not caused by this work, but it is a red required-CI gate.

Licenses cleared for packaging (primary-source research): fastembed default ONNX weights are redistributable in a published image; Lago is AGPL → use OpenMeter (Apache) for future usage metering; Novu is heavy + partly proprietary → SMTP-first, revisit as an optional profile.

## Remaining items (feature catalog, not started or deferred)

Full detail lives in `docs/disco-builder-primitives-plan.md` §4/§10. High level:

**Ships on today's D1 shape (fan-out-safe, mechanical — spec_schema + apply_spec + emitter + tests):**
- Analytics (per-app site id + read-only dashboard)
- Feature flags (SDK + small admin view)
- Blog / RSS (extends `seo` + `collection`)
- Media library over an upload path
- Owner-facing content edit UI (the rest of 5.2 — collections currently edit via re-apply)

**Needs the persistent-runtime / Postgres track first (deferred — the D1-vs-Postgres decision is dual-track; Postgres runtime is out of this sprint's scope):**
- Migrations/ORM, search (pgvector), cache, jobs/cron
- Multi-tenancy, audit log
- Embedded AI / RAG (the differentiator), error-monitoring → agent loop
- Usage metering (OpenMeter), deployment/hosting (preview→prod, custom domains)

**Built as fail-closed scaffolds, left unmerged in worktree branches** (`disclaude/f41-stripe-seam`, `disclaude/f33-webhook-seam`) — the runtime fill for these is documented and out of scope now. Not registered, not in the tree.

**Verification residual:** a live model driving `app_add_primitive` end-to-end (the scope fix `4cbaa218` unblocked it; a proof run is in flight).

## Next: packaging (Epic P — single-command self-host deploy)

Ground truth (recon): a working `compose.yaml` (app + agent on one `disco-server` image, frontend, sandbox-image build-only, one data volume), `deploy/compose/Dockerfile.server` (multi-stage uv, WeasyPrint/LibreOffice, no torch), `frontend/Dockerfile` (node→nginx, runtime env.js), `deploy/sandbox/Dockerfile`, `deploy/compose/entrypoint.sh`, `.env.example` all already in-tree. Epic P is hardening + gap-closing, not greenfield.

Ordered work (detail in plan §6):
- **P1** — `profiles:` (core / encoders / sandbox) so bare `docker compose up` = app+agent+frontend+data; print the working UI URL + first-run admin path on boot; fix the `docs/self-html.md` → `docs/archive/self-host.md` broken reference. Acceptance: `git clone` + `docker compose up` on a fresh box → UI at a printed URL, zero source edits.
- **P2** — `full`/`lite` image variants around fastembed's ONNX weights: `full` pre-bakes the default weights (offline RAG, no first-run download); `lite` moves fastembed to an optional dep + remote-endpoint default. Fix the `.env.example` encoder-tier default + stale reranker name.
- **P3** — promote `deploy/sandbox/` to a first-class deliverable; scrub host-specific defaults in `core/llm/config.py` (workspace_root, podman_url) to neutral container-local values; rewrite the stale sandbox README (`pmx-sandbox:base` → `disco-sandbox:base`).
- **P4** — trim `.env.example` to required-vs-optional; add a first-run "configure your model" step (the seed defaults to an Ollama endpoint that's empty on most boxes — the #1 out-of-box failure).
- **P5** — release hygiene: SBOM of bundled weights/deps, image-size trim + recorded targets, README quickstart matching P1's real commands.

## Parked for v0.2: Trusted Components tier

Dylan's verified vendored-component design (2026-07-10) — immutable hash-pinned
security cores (auth/RBAC/payments) + free periphery; verify = integrity hash +
requires-graph + per-component seam probe; eject = honest relabel. Full design +
market evidence: `docs/trusted-components-design.md`; **implementation-ready spec
(data contracts, tools, verify semantics, WO-TC1..6): `docs/trusted-components-spec.md`**.
Fail-closed seam pinned in
`packages/core/src/disco/core/trusted_components/` (manifest contract, tested,
NOTHING advertised — same parking discipline as the f41/f33 seams). Deliberately
NOT v0.1: no current surface claims auth/RBAC, so deferral breaks no promise.
v0.1 borrow only: the hand-rolled-auth honesty label (flag model-generated
auth/payment code as unverified in the deliverable).

## AppKit kill switch (2026-07-10)

`DISCO_APPKIT_ENABLED=0` on the agent-server fully separates the AppKit track
if it proves unreliable — one config change, restart, gone. Off means: the
`app_*` v2 mutators leave the default registry (unknown_tool everywhere), the
create route refuses `appkit_mode` with an honest 409 (never a silent
downgrade), the runtime never composes the strict `AppKitToolExecutor`
(existing appkit conversations degrade to normal free-form builds; their files
are ordinary Vite apps), and the `lead_form` starter refuses with the
free-form alternative. Re-enable + restart restores everything — no stored
state is touched (the gate is on the READ, `_effective_appkit_mode`, not the
setter). Flag helper: `disco.core.flags.appkit_enabled()`. Known caveat: the
`disco verify` AppKit acceptance scenario fails with the 409 detail while
disabled — expected and self-explaining. Live-proven 2026-07-10 (409 with flag
off, 200 both ways with flag on, on a throwaway :8010 instance).

> **2026-07-10 supersede:** the "verifier re-aim" queue item below is now
> **Wave 1 of `docs/appkit-campaign.md`** at a corrected, larger scope — the
> repro proved **5 of 6** fallback checks false-FAIL for records apps (not just
> `schema_sql_valid`), so the fix is a full `records_verify` bundle, not a
> representative-row tweak. Read the campaign doc first.

## HANDOVER — immediate queue (2026-07-10, post appkit-lane session)

1. **Fix `verify_appkit_app` `schema_sql_valid`** (packages/tools/src/disco/tools/
   builtin/verify_appkit_app.py): the check inserts "a representative lead row"
   (assumes a `name` column) into the app's ACTUAL entity table — a records app
   with entity `shift` false-FAILs ("table shift has no column named name") and
   the model repair-spirals (observed: app_create ×11). Fix: derive the
   representative row from the entity's real fields (or scope the check to
   lead_gen kinds). Do NOT weaken the check — re-aim it.
2. **One sequential lane proof run** (NOT parallel — concurrent npm installs
   contend and the 300s _VITE_BUILD_TIMEOUT_S is tight cold):
   `systemctl --user restart disco-app.service disco-agent.service` (picks up
   package edits), then
   `DISCO_RELAY_LOG=/tmp/disco-provider-ledger.jsonl .venv/bin/python -m
   harness.build_soak.run --scenario appkit_finish_autonomous --autonomous
   --hard-cap 3600 --out test-record/appkit-lane`.
   PASS = FINISHED/VERIFIED + .disco specs. Gotcha: killing the runner does NOT
   kill the build — kill the conversation via
   POST :8000/conversations/{cid}/kill (X-Disco-CSRF from /api/auth/mint).
3. **Funnel backlog** (evidence-ranked, apply AFTER a green run, measure with the
   lane): plan-time appkit sequence recipe > next-step guidance in app_create
   SUCCESS message > advertise ONLY verify_appkit_app in appkit scope (models
   picked verify_web_app) > variant_id catalog in app_add_section schema
   ('form.standard' was refused) > server_status poll damping (46 polls seen).
4. Context commits: 36bab3d1 (durability), 09a1f127 (gate order), 12abd8df
   (appkit vocab), e2b9f021 (lane), 70e5ad7a (gvisor PASS), e949dc83 (UI soak),
   617e07bb (trusted-components v0.2 parking). Evidence: test-record/appkit-lane/.
