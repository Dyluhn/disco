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
- The live-model end-to-end proof that a real model calls `app_add_primitive` is **not yet confirmed** (a run is in flight; first run failed and found the now-fixed scope bug).
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
