# Changelog

## Unreleased

### Added

- Reference packs for full-stack builds: fifteen bundled integration recipes
  (`packages/core/src/disco/core/references/packs/`) the Build agent reads with
  the new `reference_pack` tool before wiring a capability — the api/web/compose
  stack on the trusted database/auth/RBAC kits, accounts and roles, realtime and
  presence, chat rooms and DMs, alert banners, an OpenAI-compatible AI assistant
  with web search and context injection, RAG over uploaded documents with Chroma
  and @-references, voice dictation, transactional email with an outbox and
  Mailpit, Stripe payments with webhooks, photo/video uploads, live market data
  and news with range-selectable charts, booking/inventory rules without double
  booking, a server-authoritative multiplayer game loop with replays, and how to
  prove each one inside the sandbox. The build prompts point at the packs the way
  they point at starter kits and trusted components.

## v0.2.0 - 2026-09-10

First published release; everything since the 2026-07-03 tag.

### Added

- Prebuilt images on GHCR for amd64 and arm64: `ghcr.io/dyluhn/disco-server`,
  `ghcr.io/dyluhn/disco-frontend` and `ghcr.io/dyluhn/disco-sandbox`, tagged
  with the release. `compose.yaml` pulls them by default, so a self-host is
  `podman compose up -d` with nothing built on the host; `compose.build.yaml`
  is the override for building from a checkout. The release workflow builds
  each arch on a native runner after every gate has passed and joins them into
  one multi-arch tag per image.
- Repository restructure: `packages/`, `frontend/`, `deploy/`, `docs/` and
  `development/` at the top level, with governance, harness and tests under
  `development/`.
- Logo and definition mark (`docs/assets/`, from
  `development/scripts/brand_logo.py`), screenshots of the surfaces, and a
  showcase: a deep-research report on the Black Death and labour markets with
  its PDF, deck and audio-overview exports, plus a compose-deployed app from
  the Build agent.
- `sota-scan` (peer benchmark skill + workflow under `development/sota-scan/`)
  and a DeepGit MCP server entry in `.mcp.json`.

- Design direction library grown from 9 to 22 directions, covering app/product
  verticals (fintech, healthcare, enterprise, ops dashboards, docs, scientific)
  plus new brand/editorial registers, with a numeric design-constraint lint.
- Build depth for generated apps — durable, multi-user software instead of
  static scaffolds: local Workers-runtime persistence (D1 writes survive a cold
  restart under workerd, proven by a runtime harness), a `records` primitive
  with related entities, foreign keys, and N-entity CRUD routes, per-user auth
  plus RBAC (PBKDF2 login, session cookies, role-gated reads/writes), and
  client reactivity (typed API client, optimistic updates).
- Tier-3 build guidance: a think-gate at loop transitions, a durable
  never-modify-tests rule, and design-first (tailor tokens before components)
  nudges in the build prompts.
- Primitive framework: a primitive is a `PrimitiveDefinition`
  (tier/host_contract/spec_schema/verify/apply_spec); the new
  `app_add_primitive` tool validates the agent-filled spec, folds it into the
  AppSpec, regenerates the app, and records provenance. Includes a
  host-service registry/dispatcher and per-primitive verify dispatch with a
  fail-closed finish gate: `template_only` primitives without a verify harness
  cannot ship.
- Catalog primitives addable via `app_add_primitive`: `form` (typed fields,
  server-side validation, D1 submissions table, owner inbox — currently
  attaches to lead-gen apps only and is refused at the deploy gate), `seo`
  (meta/OG/JSON-LD plus sitemap.xml and robots.txt), and `collection`
  (structured content collections such as team/menu/testimonials).

### Fixed

- MCP was unusable in the shipped Compose deployment, in both transports.
  Remote (`streamable_http`) servers were routed at an egress proxy on
  127.0.0.1:8888 that the stack never runs, so every connection died with
  ConnectionRefused; an operator-approved origin now connects directly, while
  an unapproved one is still refused before a client exists
  (`DISCO_MCP_EGRESS_PROXY_REQUIRED=1` restores proxying for operators who run
  their own). Local (`stdio`) servers could not launch at all because the
  server image had no JavaScript runtime; it now ships Node 22 + npm/npx, so
  `npx -y @modelcontextprotocol/server-...` works — see the security tradeoff
  noted in `docs/self-host.md`.
- Host-address guidance for services running on the host machine: under
  rootless Docker `host.docker.internal` is a dead address and the host's LAN
  IP is required, the exact reverse of rootless Podman. Documented, with the
  Ollama preset hint corrected.
- OpenRouter model selection and image generation, broken by an approval-ref
  canonicalization gap.
- Research surface: Notify highlight, "New research" wired up, and source
  configuration moved to Settings only.
- Export and preview honesty: the non-truncating "bounded by rounds" notice is
  suppressed, and podman previews report their real state.
- Exported sites now load (web-asset MIME types are served) and a site .zip
  download was added.
- Generated sites and apps ship real visuals: always-on art direction with an
  SVG fallback.
- The plain-deck fallback is reported honestly instead of as a silent success.
- Breaker pause/question surfaces instantly rather than after a
  model-call-long wait.
- `slides_generate` has a real timeout instead of dying at the generic 300s
  cap.

### Security

- Wave 1: authentication, CORS, and owner-scoping keystone — HttpOnly session
  cookies with CSRF protection, strict WebSocket Origin checks, owner-scoped
  conversations/projects/spaces/workflows, and admin-gated operator routes.
- Wave 2: secret-ref-only provider resolution and an egress origin-approval
  chokepoint — SSRF-guarded host egress (public-IP-only with redirect
  revalidation), out-of-band HMAC-signed origin approvals, and secret refs
  pinned to their origins.
- Pi integration removed entirely (attack-surface reduction); the native
  DiscoKernel build loop is the only kernel.
- Wave 3: host-execution cluster closed (gVisor-bypass floor) — rm-root floor,
  in-sandbox DoD probes, sandbox-backend allowlist, and env/session hygiene.
- The silently-dropped origin-approval ledger is now surfaced.
- Waves 4-6 (MCP approval integrity, isolation/resource caps, output sinks +
  share) are parked, not shipped; see `current/sec-work-remaining/disco-security-state.md`.

## v0.1.0 - 2026-07-03

### Added

- Reliability engine: the REL ladder is complete, with REL-1e making host
  verification authoritative by default. `DISCO_HOST_VERIFY_AUTHORITATIVE=off`
  restores the earlier shadow posture.
- Build workspace versioning: Build projects now keep restorable workspace
  versions, expose list/restore APIs, serve historical preview bytes with
  `?version=`, and show rollback controls in the preview UI.
- AppKit engine: generated apps use React, Vite, TypeScript, Cloudflare Worker,
  D1, and Drizzle schema output. The strict AppKit verifier covers the original
  seven app checks plus Drizzle schema validation and Cloudflare export readiness.
- Owner-gated Cloudflare deploy: AppKit deploy routes are owner-only, dry-run by
  default, and require explicit confirmation before real Cloudflare mutations.
- `questions_v2` structured intake: planning can ask one bounded pre-plan
  clarification round, while autonomous runs skip it and record assumptions.
- Watch-it-write: streaming file-write/edit deltas now reach the frontend as
  `file_stream` frames so users can see generated file bodies assemble live.
- HMR preview proxying: live preview WebSockets, including Vite HMR frames, pass
  through the agent-server preview routes.
- Verifier context split: model verification now runs through the `VERIFIER`
  role with a bounded seed of contract, deliverable paths, check results, and
  screenshot; the builder context receives only the typed verdict summary.

### Changed

- Release packaging now includes a tag-triggered GitHub Actions workflow that
  runs the four deterministic fitness gates, unit suites, and frontend build
  before creating or updating a draft-only GitHub release from this changelog.
