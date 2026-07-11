# Disco Builder — Feature-Primitives + OSS-Packaging Campaign (v2, execution-ready)

**Status:** planning-locked 2026-07-06. Decisions ratified by Dylan (all 5 recs accepted). This is the tight,
WO-level execution plan: the primitive framework design, per-primitive work orders, the mega-soak gate, and the
OSS single-command-deploy epic. Companion: the briefing Artifact (ranked catalog + competitive matrix) and the
7 research streams summarized in `mega-campaign-run-log.md`.

---

## 0. Thesis (why this is a campaign, one paragraph)

Every AI builder ships backend features as **raw model-authored code per prompt** — so validation, RLS, spam
protection, webhook sig-checks and secret custody exist only if the model remembers them. Disco inverts this:
each capability is a **secure scaffold a weak open-weight model just fills in** — the security-critical code is
Disco's, the model supplies only the shape. The 38 primitives are not 38 builds; they snap onto ~6 shared
foundations, and the biggest one — host-mediated egress + secret custody — is **already built** (S-W2). Build the
framework first; then primitives are cheap *and* uniformly safe. End state: an open-source, self-hostable builder
whose owned primitives, local-model AI (no egress), and secure-by-construction outbound are things **no competitor
offers**, shippable via a single `docker compose up`.

---

## 1. Locked decisions (ratified 2026-07-06)

| # | Decision | LOCKED |
|---|----------|--------|
| 1 | Content model | **Native** Pydantic-backed store + RBAC-gated owner admin (NOT an embedded Node CMS) |
| 2 | Comms backbone | **Adopt Novu** (self-hosted) as the one backbone for email + in-app + push — *resolved otherwise in §9 (2026-07-07): Novu is too heavy for the single-command deploy; transactional email goes **plain SMTP first**, Novu revisited only as an optional profile* |
| 3 | Managed providers | **Operator-gated opt-in adapters** (Resend/Stripe/Twilio) alongside self-hosted defaults |
| 4 | Hosting scope | **Preview + self-host deploy + custom-domain TLS first; defer autoscale/scale-to-zero** |
| 5 | Approach | **Framework-first** (build F-A/F-E seam before primitives) |
| — | Sequence | features → **mega-soak (Epic Z)** → **OSS packaging (Epic P)** |

---

## 2. F-A — The Primitive Framework (the keystone WO cluster)

> **As-built: see §10.** §10.0 is the authoritative as-built record: the shipped tool is `app_add_primitive`
> (not `add_primitive`), it folds the spec into the AppSpec rather than overlaying `generate(spec)` artifacts,
> and A0/A1/A2.1/A3 are DONE. Where this section and §10 disagree, §10 wins.

Extends the existing generated-app scaffolding system (`disco.core.appkit` / the AppKit generator the build loop
already drives — **confirm exact seam at WO-A0**). A **Primitive** is a registered object:

```
Primitive:
  kind:          str                      # "form", "payments", "rag", "rls", ...
  spec_schema:   type[BaseModel]          # the declarative spec the AGENT fills (LLM-fillable part)
  tier:          "fillable" | "template_only"
  generate(spec) -> GeneratedArtifacts    # Disco-owned: emits routes/components/migrations/wiring.
                                          #   template_only tier => the model NEVER authors this output
  host_contract: list[HostService]        # runtime services the generated app calls (email.send, ai.chat,
                                          #   storage.put) — secrets resolved HOST-SIDE via S-W2's
                                          #   origin_approvals + secret_refs + host_egress
  verify(app) -> VerifyResult             # adversarial harness; MANDATORY build-gate for template_only tier
```

**Work orders:**
- **WO-A0** — locate + document the AppKit generation seam; define `Primitive` protocol + `PRIMITIVES` registry
  (`disco.appkit.primitives`). Acceptance: a trivial "hello" primitive generates + mounts in a sandbox app.
- **WO-A1** — the agent tool `add_primitive(kind, spec)` (builtin, `tools/builtin/`) = the "add X" UX. The model
  picks `kind`, fills `spec_schema`; `generate()` runs; for `template_only` the model's role is spec-only.
- **WO-A2** — the **host-service contract + registry**: a per-app service bus where generated apps call
  `email.send`/`ai.chat`/`storage.put`/`charge` and the HOST resolves the secret + egress (reuse S-W2 wholesale;
  the app never imports a provider SDK with a secret). Acceptance: a generated app sends mail with zero secret in
  its code/bundle; egress is origin-approved.
- **WO-A3** — the **build-gate hook**: wire each primitive's `verify()` into the DoD/finish gate so a
  `template_only` primitive can't ship unless its adversarial harness passes (reuses the security campaign's
  real-exploit-harness discipline — no cassettes).
- **WO-A4 (F-E)** — the **scoped-credential / quota plane**: per-app tokens minted host-side + quota + rate-limit
  at one gateway. Consumed by `ai.chat` (5.1), auto-API (6.3), connectors (6.6). Acceptance: an app's AI calls
  are metered + rate-limited per-app; a second app can't spend the first's quota.

---

## 3. The six foundations

> **As-built: see §10.** The Status column below is the planning-time snapshot; §10.0 is the authoritative
> as-built record (F-A's A0/A1/A2.1/A3 are DONE, not NEW).

| # | Foundation | Status | WOs |
|---|-----------|--------|-----|
| F-A | Primitive framework | NEW | WO-A0..A3 |
| F-B | Data substrate (Postgres-first) | NEW | migrations, pgvector, cache, jobs (see §4 Epic F1) |
| F-C | Trust plane | RBAC in flight | RLS, secrets vault, WAF, audit (Epic F2) |
| F-D | Egress + secret contract | **DONE (S-W2)** | expose via WO-A2 |
| F-E | Scoped-credential / quota plane | NEW | WO-A4 |
| F-F | Persistent runtime | NEW (largest) | deployment/realtime/workers (Epic F7 + 6.4) |

---

## 4. The primitive catalog — work orders by epic

Format per WO: **id — scaffold output · host-contract · tier · verify-harness (if template_only) · OSS block · effort · deps**

### Epic F1 — Data substrate (F-B)
- **1.1 Migrations/ORM** — declarative model classes → Alembic autogenerate wrapped in a gated diff-preview +
  sandboxed `db reset` dry-run before apply · fillable · SQLAlchemy+Alembic (MIT) · M · F-A.
- **1.2 File/blob storage** — presigned-POST upload flow + per-app bucket/key-prefix · host `storage.put` ·
  **template_only** (upload path) · harness: policy-condition size/type enforcement + no-transit-through-app ·
  SeaweedFS (Apache) · L · F-A.
- **1.3 Search** — tsvector+pg_trgm+pgvector indexes + query scaffold; Meilisearch sidecar escalation · fillable ·
  Postgres/pgvector; Meilisearch (MIT) → points at existing bge-m3 · M · 1.1.
- **1.4 Cache** — `cache.get/set/ttl` wrapper, per-tenant namespaced · fillable · Valkey (BSD-3) · S · F-A.
- **1.5 Jobs/cron** — decorated task fns + schedule entries; workers run gVisor-isolated · fillable ·
  procrastinate (MIT, no broker) · M · 1.1, F-F(worker iso).

### Epic F2 — Trust plane (F-C) — do early, gates every multi-user app
- **2.1 Multi-tenancy/RLS** — Disco generates tenant_id columns + Postgres RLS policies (USING+WITH CHECK) +
  covering index + `SET app.current_tenant` from auth ctx · **template_only** · harness: 2 tenants in sandbox,
  A's token CANNOT read B's rows → else FAIL build · XL · 1.1, RBAC. **Crown jewel.**
- **2.2 Secrets vault** — generated source writes `env('X')` refs; secret registered in vault; sandbox pulls via
  scoped per-app token at boot · **template_only** · harness: build-time lint FAILS on any hardcoded credential ·
  Infisical (MIT) · M · F-A.
- **2.3 Rate-limit/WAF-lite** — app token-bucket middleware default-on + shared edge WAF fronting deploys ·
  template_only · harness: unauthenticated flood on signup/upload is throttled · Coraza+CRS (Apache) · L · F-F.
- **2.4 Audit log** — append-only table + RBAC-wired trigger emitting {actor,action,tenant,before/after,ip,ts} ·
  fillable (triggers Disco-owned) · near-free on the event-sourced core · M · 2.1, RBAC.

### Epic F3 — App-real primitives (F-A + F-B + F-D)
- **3.1 Forms + spam** — one Pydantic schema → React form + server-validate (422) + submissions table + owner
  inbox; ALTCHA+honeypot+HMAC-timing+slowapi on by default · **template_only** (validation + upload) · harness:
  client-tampered payload rejected server-side; polyglot/oversize upload rejected; bot without captcha blocked ·
  ALTCHA/slowapi (MIT) · M · F-A, 1.2. **← the MVP reference vertical.**
- **3.2 Transactional email** — `email.send(template,vars)` host-mediated · template_only (egress) · harness:
  provider secret never in app code/bundle; egress origin-approved · Novu backbone; SMTP/Postal/Resend adapters ·
  M · WO-A2.
- **3.3 Webhooks (in+out)** — inbound: sig-verify+idempotency scaffold; outbound: `webhook.emit` via egress proxy ·
  **template_only** (sig-verify + SSRF) · harness: forged-sig rejected + replay deduped; outbound to
  internal/metadata IP blocked (DNS-rebind test) · Convoy/Svix · M · WO-A2.
- **3.4 Media library** — owner asset browser over 1.2 storage + imgproxy responsive srcset · template_only
  (upload) · reuses 1.2 harness + SVG-sanitize · imgproxy (Apache) · M · 1.2.

### Epic F4 — Monetize (F-D + inbound webhooks + F-C)
- **4.1 Stripe Checkout + Billing + paywalls** — host-proxied Checkout Session (secret host-side, egress pinned
  api.stripe.com) + Customer Portal + entitlement flag via RBAC · **template_only** (webhook + secret) · harness:
  webhook sig-verify + idempotency-in-one-transaction (replay does not double-fulfill); leaked-key blast radius
  bounded (restricted key) · SAQ-A by construction · M–L · 3.3, 2.4.
- **4.2 Usage metering** — usage events → aggregate/price → settle via Stripe · fillable · Lago (confirm license) ·
  L · 4.1, 1.5.
- **4.3 Tax/MoR** — Polar-first merchant-of-record adapter; loud "you are MoR" warning on direct-Stripe · fillable ·
  Polar (Apache) · L · 4.1.

### Epic F5 — The moats (F-E + F-B + existing router/encoders)
- **5.1 Embedded AI (chat/RAG/semantic-search)** — scoped internal `/_disco/ai/*` over the existing router +
  local embeddings + reranker; RAG = index→bge-m3→pgvector→rerank, server-side only · fillable · **THE
  differentiator** (keyless, free-at-inference, non-exfiltrating) · L · WO-A4, 1.3.
- **5.2 Content model + owner edit** — native Pydantic content collections + keyed copy store + RBAC-gated
  `/admin/content`; render via nh3/bleach allowlist (no raw HTML) · fillable · L · F-A, 2.1.
- **5.3 SEO/sitemaps/OG/blog** — managed per-route meta/OG/JSON-LD + auto sitemap/robots/RSS from the content
  graph; blog = a content-collection template · fillable · M · 5.2.
- **5.4 Error monitoring → Agent loop** — per-app DSN; wire captured errors BACK into Agent context so "fix this
  prod error" is a one-click build loop · fillable · GlitchTip (MIT) · M · F-F.

### Epic F6 — Operate & grow
- **6.1 Analytics** — per-app site id + embedded read-only dashboard (admin token proxied) · fillable · Umami
  (MIT) · S.
- **6.2 Feature flags** — SDK + small admin view; flags feed 2.4 audit · fillable · Unleash (Apache) · M.
- **6.3 Auto-admin panel** — scaffold React-Admin/Refine data-provider AS PART of the app codebase, RBAC+RLS+audit
  gated · **template_only** (privileged queries) · harness: admin respects tenant isolation (can't bypass RLS) ·
  React-Admin/Refine (MIT) · L · 2.1, 2.4. **Sequence last.**
- **6.4 Realtime** — host-run Centrifugo sidecar; per-channel JWT auth · **template_only** (channel authz) ·
  harness: app A's user can't subscribe to app B's channel/rows · Centrifugo · L · F-F, WO-A4.
- **6.5 Notification center + web push** — Novu `<Inbox>` + VAPID web-push · fillable · Novu (MIT) · M · 3.2.
- **6.6 MCP connector library** — vetted MCP servers behind egress-approval; OAuth tokens in vault, injected
  server-side; per-action scopes · **template_only** (OAuth custody + egress) · harness: compromised connector
  can't exfiltrate beyond approved origins; destructive action requires confirm · XL · WO-A2, A4.

### Epic F7 — Ship it (F-F persistent runtime)
- **7.1 Deployment/hosting** — extend gVisor sandbox to a persistent, resource-capped per-app runtime; preview →
  promote-to-prod gate; custom domains + ACME TLS (Caddy/Traefik); egress stays approval-gated · **template_only**
  (runtime isolation) · harness: long-lived multi-tenant isolation (no cross-app escape/noisy-neighbor); the
  known prod-only gVisor container-recreation-under-load bug MUST be exercised here · XL · F-C. Defer
  autoscale/scale-to-zero.

**Deferred:** CRDT multiplayer (Yjs+Hocuspocus, XL) · Stripe Connect marketplace (XL, funds-flow risk) ·
autoscale runtime · SMS (spend-capped opt-in) · i18n with live translation mgmt.

---

## 5. Epic Z — the mega-soak (gate BEFORE packaging)

Before we package for the public, the whole thing must survive a long autonomous soak (the campaign's original
closer). Run the existing soak harness across ALL surfaces (research/build/agent/deep_research) AND every new
primitive's build-and-run path, on real models, with the security waves' exploit harnesses in the gate set.
Target: reproduce the 193-run soak methodology, but now covering primitive scaffolding (add-a-form, add-payments,
add-RAG end-to-end) + the template-only adversarial harnesses. Debug to a clean bar before Epic P. Also the gate
where the PARKED security waves (W3/W4/W5/W6) get resumed + finished (they're prerequisites for a public release —
you don't open-source a builder with known host-exec / MCP-approval / isolation gaps).

---

## 6. Epic P — OSS packaging & single-command deploy (the distribution goal)

Goal: `docker compose up` brings the full stack; ship the gVisor sandbox in-repo; two image variants
(encoders-in / encoders-out). **More scaffolded than expected** — recon (2026-07-06) found a working compose +
three Dockerfiles + an entrypoint bootstrap already in-tree. Epic P is therefore mostly *hardening + closing
specific gaps*, not greenfield. Ground truth first, then WOs that target the gaps.

### 6.0 Current state (recon 2026-07-06) — exists vs. gap

| Piece | Exists | Gap to close |
|-------|--------|--------------|
| `compose.yaml` | app + agent (one `disco-server` image, two commands) + frontend + `sandbox-image` (build-only) + `disco-data` vol | **no `profiles:`** (can't split core / +encoders / +sandbox); no printed first-run URL/token |
| `deploy/compose/Dockerfile.server` | multi-stage, `uv sync --frozen --no-dev --all-packages`, `python:3.12-slim` + WeasyPrint/pango/cairo/libreoffice-impress/jemalloc; no torch | no `full`/`lite` split; encoder weights not baked |
| `frontend/Dockerfile` | node:22-alpine → nginx:alpine; runtime `/env.js` from container env (`window.__DISCO_ENV`) | OK; just verify prod-origin passthrough |
| `deploy/sandbox/Dockerfile` | debian-slim + Playwright/Chromium + Marp + LibreOffice + noVNC (largest image) | not wired into compose; `README.md` stale (`pmx-sandbox:base`, cites a missing CONTRACT.md); default backend is raw docker.sock, not runsc |
| `deploy/compose/entrypoint.sh` | mkdir `/data` subdirs, generate `DISCO_SECRET_KEY`→`/data/.secret_key`, run `seed_config.py` | seeds driver to Ollama `host.docker.internal:11434` → **no working model out of box** |
| Auth bootstrap | S-W1 single-use loopback pairing (**verified live 2026-07-06**: token logged, single-use, loopback mint → session cookie) | `AUTH_DEV_AUTO_PAIR` undocumented; container-origin (non-loopback) pairing UX unproven |
| Encoders | in-process ONNX via `fastembed` (`DISCO_ENCODERS=local` default); weights **lazy-download** to `/data/cache/fastembed` | `fastembed` is a **mandatory core dep** (`retrieval/pyproject.toml:12`) → `lite` must make it optional; weights not pre-baked → `full` isn't truly offline yet |
| OSS files | LICENSE Apache-2.0, README, CONTRIBUTING, SECURITY, thorough `.dockerignore` | compose cites `docs/self-html.md`; real file is `docs/archive/self-host.md` (**broken link**); `.env.example` still names CC-BY-NC jina reranker + wrong encoder-tier default |

**Security ground-truth (corrected 2026-07-11):** packaging defaults to the current user's rootless Podman socket,
which is suitable for Linux and WSL2 without granting host-root-equivalent daemon access. gVisor/runsc is an
optional stronger tier, not the default; `/var/run/docker.sock` and the process backend are explicit opt-ins.

- **P1 — one-command full stack + `profiles:`.** Add `profiles:` to `compose.yaml` (`core`, `encoders`, `sandbox`)
  so bare `docker compose up` = app+agent+frontend+data with sane defaults; `--profile sandbox` adds the isolated
  runtime. Surface the working UI URL + first-run admin token on boot (entrypoint already generates the pairing
  token — print it). Fix the `docs/self-html.md` → `docs/archive/self-host.md` broken reference. Acceptance: fresh
  box, `git clone` + `docker compose up` → UI at a printed URL + a copy-pasteable admin path, zero source edits.
- **P2 — two image variants (`full`/`lite`), corrected for the REAL encoder path.** The split is about
  `fastembed`'s ONNX weights, **not** the mini-PC sidecar models the earlier draft named. `disco:full`: keep
  `fastembed` in core **and pre-bake** its default ONNX weights into `/data/cache/fastembed` at build (final
  `COPY`/warm step) so RAG works offline with no first-run download. `disco:lite`: move `fastembed` to an optional
  dep group, default `DISCO_ENCODERS` to a remote endpoint (or graceful-degrade retrieval), smaller image. Toggle
  via build-arg + separate final stage; mirrors the in-process-ONNX vs remote seam in `retrieval/live.py` /
  `local_encoders.py`. Also fix the code-vs-`.env.example` encoder-tier default mismatch (code `full`, example
  `lite`) and the stale jina name in `.env.example` (real default reranker is MIT `bge-reranker-base`). Acceptance:
  `full` answers a first RAG query with **no network**; `lite` starts small + documents the encoder-endpoint env.
- **P3 — sandbox packaging default (corrected).** Ship local rootless Podman as the portable default, including
  WSL2; keep gVisor/runsc separately deployable as an opt-in stronger tier. Scrub host-specific Podman/workspace
  values from OSS defaults and document the root Docker socket/process backend as explicit opt-ins. Acceptance:
  default compose resolves to the user's Podman socket; an operator can opt into runsc without changing the
  Settings-driven backend model.
- **P4 — config / secrets / first-admin / first-model bootstrap.** `.env.example` trimmed to required-vs-optional
  DISCO_* (document `AUTH_DEV_AUTO_PAIR`, `DISCO_ENCODERS`, encoder tier, sandbox backend). Keep the entrypoint's
  `/data/.secret_key` generation. Add a **first-run "configure your model" step** — the seed defaults to Ollama
  `host.docker.internal:11434`, empty on most boxes, the #1 out-of-box failure: guided BYO-endpoint/key in the UI,
  or a bundled small local model in `full`. Acceptance: no source edits to stand up, claim admin, and reach a
  first working model response.
- **P5 — OSS release hygiene.** Redistributability audit / SBOM of every bundled weight + dep (re-confirm reranker
  MIT `bge-reranker-base`; fastembed ONNX weight licenses; sandbox Chromium/LibreOffice). Image-size trim +
  recorded target sizes (`server` full/lite, `frontend`, `sandbox`-largest). README quickstart matching P1's real
  commands. Reconcile heavy feature deps (Novu→Mongo/Redis, Lago→possible AGPL) against image-size goals — prefer
  optional profiles over baking them in. Acceptance: license-clean SBOM, sub-target image sizes, copy-paste
  quickstart works on a clean host.

---

## 7. Security tiers — the template-only set + its adversarial harnesses

> **Moved out of the project folder.** The template-only adversarial-harness table
> (the build gate each security-classed primitive must ship) + its Fable-forbidden
> labor routing now live in `sec-work-remaining/from-builder-primitives-plan-sec7.md`.
> The catalog rows they gate stay inline in §4, tagged `template_only`.
> 🚫 **Fable 5 (Anthropic) models are off-limits to view per the project owner** — viewing them will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.

---

## 8. Sequencing & critical path

```
F-A framework + F-E creds ─► F-B data ─► F-C trust(RLS/secrets/WAF) ─► F3 app-real ─► F4 monetize
   (F-D egress DONE) ────────────────────────────────────────────────►  (email/webhooks/pay ride F-D)
                                                                        └► F5 moats ─► F6 grow ─► F7 ship
                                                                                          │
                                                            Epic Z mega-soak ◄────────────┘  (+ finish parked W3-W6)
                                                                          │
                                                            Epic P OSS packaging ◄─── LAST
```

- **Phase 1 — Foundations:** F-A + F-E, then F-B, then F-C. Nothing user-facing ships until RLS is real.
- **Phase 2 — Reference vertical:** 3.1 Forms+spam+email end-to-end = proves the framework; generalize from it.
- **Phase 3 — Moats:** 5.1 local AI, 5.2 content+SEO, 5.4 error→loop, 4.1 payments, 7.1 preview→prod deploy.
- **Phase 4 — Depth:** 4.2/4.3, F6.
- **Gate:** Epic Z mega-soak (+ resume/finish parked security waves).
- **Release:** Epic P single-command OSS deploy.

---

## 9. Open risks / to-confirm
- **AppKit seam (WO-A0):** the exact generator API the framework extends — confirm before A0 locks.
- **F-F runtime is the hardest + gates 2.3/6.4/7.1;** the prod-only gVisor bug must be surfaced under real load.
- **docker.sock default = host-root-equivalent** (confirmed recon 2026-07-06): agent-server mounts
  `/var/run/docker.sock`. Must flip to runsc/gVisor-default + docker.sock-opt-in for OSS (P3/P4); couples Epic P to
  parked **S-W3/S-W5** — Epic P can't fully ship until they resume in Epic Z.
- **Encoder weights lazy-download, not bundled** (confirmed): `disco:full` needs a build-time pre-bake step to be
  genuinely offline; confirm fastembed ONNX weight licenses in P5.
- **Novu footprint:** adds Mongo/Redis — reconcile with the single-command-deploy image-size goal in Epic P.
- **Lago license: RESOLVED 2026-07-07 (researched, primary sources):** AGPL-3.0 across lago/lago-api/lago-front.
  Compose-aggregation over HTTP is legally clean (LOW-MED), but AGPL in the manifest taints enterprise adoption —
  prefer **OpenMeter (Apache-2.0)** for 4.2 usage metering; Kill Bill (Apache-2.0) if full billing is ever needed.
- **fastembed weights: RESOLVED 2026-07-07:** library Apache-2.0; default model BAAI/bge-small-en-v1.5 — runtime
  download is Qdrant/bge-small-en-v1.5-onnx-Q (Apache-2.0, converted from BAAI MIT). Pre-baking in `disco:full`
  images is LICENSE-OK (ship MIT+Apache texts + BAAI/Qdrant attribution). P2/P5 unblocked.
- **Novu footprint: RESOLVED 2026-07-07:** community compose = 6 containers (api/worker/ws/dashboard + mongo:8 +
  redis), ~1.1GB Novu images alone, 8GB-RAM single-VM recommendation; license = MIT core + PROPRIETARY EE dirs
  (enterprise/, apps/*/src/ee — no redistribution). Verdict: too heavy to bake into single-command deploy —
  3.2 goes **plain SMTP first** (as §10.2 A2.4 already sequences); revisit Novu as an optional compose profile
  only; ntfy (Apache-2.0, single binary) is the light push alternative. Flag: don't redistribute Novu ghcr images
  (possible EE code inside) — users pull from ghcr directly.
- **Parked security waves are a release prerequisite** — Epic Z must resume + finish W3/W4/W5/W6.

---

## 10. Execution scoping — post-WO-A1 ground truth (2026-07-07)

### 10.0 F-A status

| WO | Status | Commit | As-built notes (deviations from §2's sketch) |
|----|--------|--------|-----------------------------------------------|
| A0 | **DONE** | `68088170` | `PrimitiveDefinition` extended in place (not a new protocol): `tier` / `host_contract` / `spec_schema` / `verify`, all defaulted → the 3 existing primitives byte-identical. `HostService` + `PrimitiveVerifyResult` are frozen stdlib dataclasses (core stays stdlib-only at runtime; pydantic under TYPE_CHECKING). `hello` primitive = the mount proof. |
| A1 | **DONE** | `f6a56ec3` | Tool is `app_add_primitive` (house `app_*` naming, plan said `add_primitive`). **Fold-into-AppSpec, not `generate(spec)→artifacts`:** a new defaulted `apply_spec(app, validated_spec)→AppSpec` hook folds the validated spec into the AppSpec and the app's own base primitive regenerates the WHOLE tree. Rationale: sandbox protocol has no delete → per-addon file overlays strand stale files (same failure app_create's cross-primitive guard refuses). Addable ⇔ `spec_schema` AND `apply_spec` both set. Validation refusals carry the expected JSON schema (self-recovering). Provenance at `.disco/primitives/<id>.json`. Free-form `spec` arg = justified ALLOW_SCHEMA_HOLES entries (both `default:` and `appkit_v2:` labels). Residual: no live AGENT call yet — lands with the first real addable primitive (3.1). |
| A2 | slice 1 **DONE** | `2c8e56fd` | host-service registry + dispatcher + svc.ping (disco.core.host_services). Remaining slices: out of scope for now. |
| A3 | **DONE** | `4cbaa218` | verify dispatch live: worker_inspect.py + primitive_verify.py in core; verify_appkit_app dispatches prim.verify (lead_gen fallback); applied-primitive checks from .disco/primitives/*.json land in the W-45 verdict. Also fixed the WO-A1 tool-visibility bug (APPKIT_MUTATORS). |
| A4 | out of scope | — | — |

**Catalog shipped 2026-07-07 (sprint):** F3.1 `form` (`a656334c`), F5.3-lite `seo` (`055fb99a`), F5.2-lite `collection` (`172ce70d`) — all addable via app_add_primitive on the D1 shape, all merged with full suites + gates green.

**Consequence for catalog primitives:** an addon contributes SPEC (entities/pages/actions via `apply_spec`);
the generator's shared emitters learn to LOWER those spec shapes (exactly how `records` already lowers its
entities to D1 + Worker routes). A per-addon `generate_addon()` file-overlay seam is NOT planned; if a future
primitive truly needs one it must ship tree-GC first (delete support), else stale-file corruption.

### 10.1 ⚠ Platform decision required BEFORE Epic F1/F2: D1/Workers vs Postgres

Ground truth: every generated app today is a **Cloudflare Worker + D1 + React SPA** (wrangler.toml, workerd
preview, D1 schema.sql — see `generator.py` / `verify_appkit_app.py`). The catalog (§4) is **Postgres-first**;
the crown jewel 2.1 is *Postgres RLS* — **D1 has no RLS**, so 2.1 cannot exist on the current app shape, and
1.1 (Alembic), 1.3 (pgvector), 2.4 (triggers) don't map either. Options:

- **(a) Dual-track (recommended):** keep the CF shape for the existing verticals (lead_gen/directory/records
  stay as-is); add a SECOND app shape — "persistent runtime app" (server framework + Postgres) — as the F-F
  deliverable, and target the F1/F2/F4+ catalog at it. Mechanically: an `app_runtime` axis on AppSpec (e.g.
  `"cf_worker" | "persistent"`) + per-primitive supported-runtimes, enforced at `app_add_primitive` time.
  3.1 Forms can ship on the CF/D1 shape FIRST (D1 table + Worker validate route is enough for the reference
  vertical) and gain the Postgres variant when F-F lands.
- **(b) Postgres-only pivot:** replace the CF shape. Kills working, live-proven verticals + the deploy story
  (wrangler) for a runtime that doesn't exist yet. Not recommended.
- **(c) Stay D1-only:** caps the catalog at ~F3; forfeits RLS/pgvector/jobs. Not viable for the thesis.

### 10.2 WO-A2 — host-service contract (decomposed)

Transport ground truth: the generated Worker runs under wrangler/workerd INSIDE the sandbox; it cannot import
host SDKs or hold secrets. So a host service call is an HTTP hop from the sandbox to the host plane, and the
S-W2 chokepoints already implement everything security-critical about the outbound leg
(`host_egress.guarded_request`, `secret_refs.resolve_provider_secret` + `secret_ref_allowed_for_origin`,
`OriginApprovalStore`). Sub-orders:

- **A2.1 — service registry (core, feature):** `disco.core.host_services` — `HostService.name` → handler;
  handlers are thin adapters that ONLY compose existing S-W2 calls (resolve ref → check origin approval →
  `guarded_request`). No new security logic permitted in handlers; a handler that needs a new enforcement
  primitive is out of scope by definition and escalates.
- **A2.2 — the bus endpoint (agent-server, feature + ONE security surface):** `/_disco/svc/{service}` on the
  agent-server, reachable from the sandbox (mind the gVisor networking gotchas: reach-by-IP, no embedded DNS).
  AUTH IS THE ONE NEW SECURITY SURFACE: v0 binds calls to the owning conversation via a per-app bearer minted
  host-side and injected as a Worker env var (never in the tree); full minting/scopes/quota = WO-A4.
  → the auth slice gets adversarial review (codex xhigh) + is NOT Fable-authored.
- **A2.3 — the app-side client shim (Disco-owned, template_only-style):** generator emits `disco-client.ts`
  (`svc("email.send", payload)` → fetch to the bus with the injected env token). The model never authors it;
  add it to the emitted tree + `.dev.vars.example` documents the env.
- **A2.4 — first service = `email.send` (SMTP adapter first, Novu later per locked decision #2):** acceptance
  per plan: a generated app sends real mail with ZERO secret in code/bundle; egress origin-approved; the
  harness greps the whole emitted tree + built bundle for the secret (build-time lint, 2.2's discipline).

### 10.3 WO-A3 — verify build-gate (decomposed)

- **A3.1 — dispatch (feature):** replace the hardcoded branch at `verify_appkit_app.py` (`if primitive_id ==
  DIRECTORY... else lead_gen`, ~:1181) with `primitive.verify` dispatch; port the directory/lead_gen check
  bundles INTO their `PrimitiveDefinition.verify` fields (behavior-preserving refactor, verdicts byte-equal).
- **A3.2 — the gate (feature, ONE rule):** template_only primitives applied to an app ⇒ their `verify` MUST
  pass at finish. Wire it INSIDE the shared `run_finish_verify_gates()` (`core/loop/finish/`) — **never** on
  just one finish path (the notify/actionless valve skipped gates before; that's the finish-path-drift
  lesson). `.disco/primitives/*.json` provenance records tell the gate WHICH primitives are applied.
- **A3.3 — proof:** give `hello` a real `verify` (index.html exists + heading + subtitle match the folded
  spec) and a live finish-gate run.
- The ADVERSARIAL harness content for template_only primitives (2.1 cross-tenant, 4.1 replay, …) is per-
  primitive Epic F work and security-classed (NOT Fable) — A3 only builds the hook they plug into.

### 10.4 WO-A4 — scoped credentials / quota plane

Two halves, different labor classes:
- **Security half (NOT Fable):** token format/minting/rotation/scope model for per-app credentials; threat
  model + codex adversarial pass. Prereq input: A2.2's v0 conversation-bound bearer.
- **Feature half (Fable-safe):** per-app usage accounting (tokens/requests), quota config, 429 + retry-after
  at the bus, per-service rate limits; `ai.chat` as the first metered consumer (5.1's prereq).

### 10.5 Labor + sequencing map (who does what, what parallelizes)

- **Serial spine (framework):** 10.1 decision → A2 → A3 → A4-feature-half → 3.1 Forms (reference vertical,
  CF/D1 variant) → generalize. Framework WOs touch shared seams (generator, finish gates, agent-server) —
  keep them single-writer, no fan-out.
- **Security-classed (Opus/codex sessions, per standing rule — NOT Fable):** A2.2 auth slice, A4 security
  half, every template_only adversarial harness (§7 table), parked S-W4/5/6 at Epic Z, Epic P3/P4
  docker.sock flip.
- **Fan-out-safe once 3.1 proves the pattern (worktree kit, ledger-driven):** fillable catalog primitives
  with disjoint emitters — 1.4 cache, 6.1 analytics, 6.2 flags, 5.3 SEO, 2.4 audit-fillable-part. Each =
  spec_schema + apply_spec + emitter + tests; the framework makes them mechanical.
- **Big rocks needing their own campaigns:** F-F persistent runtime (7.1, XL — gates 2.3/6.4 and the
  Postgres track per 10.1); 2.1 RLS (XL, crown jewel, template_only + harness); 6.6 MCP connectors (XL).
- **Do-last (unchanged):** Epic Z mega-soak (resumes parked security waves) → Epic P packaging.
