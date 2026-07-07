# Disco — full project state (master status, 2026-07-07)

Branch `disclaude/mega-campaign` @ `d04340c4`. Working tree clean.
This is the master index across every workstream. Detail docs:
- `docs/disco-status-and-remaining.md` — primitive catalog + remaining features
- `docs/disco-security-state.md` — security done/parked/deferred
- `docs/disco-builder-primitives-plan.md` — the primitive campaign plan (§4 catalog, §6 packaging, §10 execution scoping)

---

## The mega-campaign — epic scoreboard

| Epic | Scope | State |
|------|-------|-------|
| A — Design directions | Expand the direction library + numeric design lint | **DONE** — 9→22 directions (`aef9c33d`), constraint lint (`10274058`) |
| C — Tier-3 build guidance | think-gate, never-modify-tests, design-first nudges | **DONE** (`1dfedb15`) |
| B — Build depth | Durable generated apps (persistence, records, auth, reactivity) | **DONE** (see below) |
| Security waves | Auth, secrets, egress, host-exec, isolation | **W1–W3 DONE; W4/W5/W6 PARKED** — see `docs/disco-security-state.md` |
| Walkthrough/runthru fixes | The live-bug batch Dylan surfaced | **DONE** (see below) |
| F-A — Primitive framework | The "add X" scaffolding system | **DONE** (see below) |
| F catalog | 38-primitive catalog | **3 shipped this sprint; rest deferred** |
| Z — Mega-soak | Long autonomous soak gate | **NOT STARTED** |
| P — OSS packaging | Single-command self-host deploy | **NOT STARTED (recon done)** — next up |

---

## Build depth (Epic B) — DONE

Generated apps went from static scaffolds to durable, multi-user software:
- **B-W1** — local Workers-runtime persistence proof (`d5db4e88`): D1 writes survive a cold restart under workerd.
- **B-W2** — the `records` primitive (`fea94682`): related entities, foreign keys, N-entity CRUD list/insert routes.
- **B-W3** — per-user auth + RBAC for records (`d99c4147`): PBKDF2 login, session cookies, role-gated reads/writes.
- **B-W4** — client reactivity for generated apps (`efccf4fe`).

## Walkthrough / runthru fixes — DONE

The batch of live bugs Dylan surfaced, all shipped:
- OpenRouter model + image gen (approval-ref canonicalization gap) — `90828654`
- Research surface: Notify highlight, wire "New research", sources Settings-only — `bbf4be46`
- Export/preview: suppress false "bounded by rounds", honest podman previews — `ba9f5c4b`
- Exported sites load (web-asset MIME types) + site .zip download — `3b5e5417`
- Sites/apps ship real visuals — always-on art direction + SVG fallback — `93442553`
- Plain-deck fallback is now honest, not a silent success — `34fa9041`
- Breaker pause/question surfaces instantly, not after a model-call wait — `fed4703f`
- slides_generate got a real timeout (was dying at the generic 300s cap) — `ac5b4b21`

Also verified at session start: the three original requests (artisan woodworking site with real SVGs, 2D parallax platformer without idle-thrash, 22KB research-report → 13-slide styled deck) all render correctly in the running app.

## Primitive framework (Epic F-A) — DONE

The "add X" system: a primitive is a `PrimitiveDefinition` (id / tier / host_contract / spec_schema / verify / apply_spec); the agent picks a primitive and fills its spec, Disco validates + folds + regenerates.
- **WO-A0** (`68088170`) — the framework + a `hello` proof primitive.
- **WO-A1** (`f6a56ec3`) — the `app_add_primitive` tool (validate spec → fold into AppSpec → regenerate → provenance record). Visibility bug fixed in `4cbaa218`.
- **WO-A2.1** (`2c8e56fd`) — host-service registry + dispatcher + `svc.ping` reference.
- **WO-A3** (`4cbaa218`) — per-primitive verify dispatch + the fail-closed finish gate.

## Primitive catalog — 3 shipped this sprint

All addable via `app_add_primitive` on the current Cloudflare-Worker/D1 app shape:
- **`form`** (`a656334c`) — typed fields, server-side validation (422), D1 submissions table, owner inbox.
- **`seo`** (`055fb99a`) — meta/OG/JSON-LD + sitemap.xml + robots.txt.
- **`collection`** (`172ce70d`) — structured content collections (team/menu/testimonials).

Registered primitives in the tree: `lead_gen`, `directory`, `records`, `hello`, `form`, `seo`, `collection`.

---

## Remaining feature work

**Ships on today's D1 shape (mechanical, fan-out-safe):** analytics, feature flags, blog/RSS, media library, owner-facing content edit UI. Detail in `docs/disco-status-and-remaining.md`.

**Needs the persistent-runtime / Postgres track first (deferred — dual-track decided, Postgres runtime out of scope this sprint):** migrations/ORM, search (pgvector), cache, jobs/cron, multi-tenancy, audit, embedded AI/RAG, usage metering (OpenMeter), deployment/hosting.

**Built as fail-closed scaffolds, left unmerged in worktree branches** (`disclaude/f41-stripe-seam`, `disclaude/f33-webhook-seam`) — not registered, not in the tree; their fills are documented per `docs/disco-security-state.md`.

---

## In flight / loose ends

- **Live end-to-end proof** — a real model driving `app_add_primitive` through the running app. The scope fix (`4cbaa218`) unblocked it; a proof run was launched and its evidence is being written to `scratchpad/live-final/` (no verdict captured here yet). This closes the last verification residual on the primitive sprint.
- **Two unmerged worktree branches** preserved (the fail-closed scaffolds above).
- **Servers** on the box were restarted onto current HEAD during the proof run (systemd --user units, DISCO_INSPECT=1).

## Next planned workstream: packaging (Epic P)

Single-command self-host deploy. Recon done — `compose.yaml`, `deploy/compose/Dockerfile.server` + `entrypoint.sh`, `frontend/Dockerfile`, `deploy/sandbox/Dockerfile`, `.env.example` all exist; Epic P is hardening + gap-closing, not greenfield. Ordered work P1–P5 in `docs/disco-status-and-remaining.md` (compose profiles + printed first-run URL; full/lite image variants around fastembed weights; sandbox promotion + host-default scrub; first-run model config; release hygiene/SBOM).
