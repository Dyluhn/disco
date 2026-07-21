# Track-1 Export Campaign — Work Orders (Fable planner, authoritative acceptance criteria)

> **AUDIT RECONCILIATION (2026-07-12).** An independent adversarial audit compared this plan to the implementation. Two criteria below were **intentionally deviated from** because the plan text was self-contradictory or was superseded by a security improvement; the rest were implemented as written (and the audit's other findings were fixed in code — see commits `01be8cd8`, `c2b9c213`). The two deviations:
>
> 1. **Runtime strategy is `dev_server`, NOT `cloudflare_dev`** (affects WO-1's enum and WO-6). WO-1 criterion 6 forbids the literal string `cloudflare`/`wrangler`/`vercel`/`turso` anywhere in `spec.py`, so a strategy literally named `cloudflare_dev` is impossible — the plan contradicts itself. Resolved to the provider-neutral `dev_server`, which is the correct keystone behavior. This is the intended reading, not a gap.
> 2. **WO-6's "exactly one `.dev.vars` reference" is superseded by "the entrypoint is the sole WRITER of `.dev.vars`."** A later secret-hardening added `.dev.vars` / `.dev.vars.*` (keeping `!.dev.vars.example`) to the emitted `.dockerignore` so a stray host secret file can never enter an image layer — defense-in-depth. That adds textual references but preserves the real property (only the container entrypoint writes the file, from the injected `${ADMIN_TOKEN:?}`), which the test now asserts directly.
>
> The original criteria are left below unchanged as the historical plan of record.

AppKit wrinkle decision: **Option (a)** — interim AppKit local-run compose bundle (WO-6), workerd/`wrangler dev` + local D1, zero changes to generator/appkit_cloudflare.

## Global gates (every Python WO must pass all; exit codes are truth)
- **G1**: `uv sync --all-packages` (never `--frozen`), then basedpyright exits 0 with 0 errors; diff introduces no `# type: ignore`.
- **G2**: import-linter (`lint-imports`) exits 0 — layering `core ← retrieval ← tools ← {agent_server|app_server}` intact, no new upward edges.
- **G3**: arch budget check exits 0.
- **G4**: arch diagram `--check` exits 0 (regenerate if cross-package imports changed).
- **G5**: `pytest <changed packages> -m "not integration"` exits 0 (exit code, not summary text).
- **GF** (frontend WOs): `npm run typecheck:build`, `npm run test`, `vite build` all exit 0.

## Campaign-wide DO-NOT-TOUCH
No modification to `appkit_cloudflare/**`, `appkit/generator.py`, `appkit/local_verify.py`, `stripe_*`/`webhook_*`, or anything under `core/loop/finish/`. Track 1 adds a local export path ALONGSIDE Cloudflare; the finish path gains nothing.

`[LIVE]` = requires a docker-compose-capable host. This campaign host has none → all `[LIVE]` criteria are DEFERRED (proven on a docker host later), never marked passed here.

---

## WO-1 — ReleaseSpec v1 + assessment models (keystone)
Files (create): `packages/core/src/disco/core/release/__init__.py`, `.../release/spec.py`, `packages/core/tests/test_release_spec.py`. Deps: none.
Models: `ReleaseAssessment` (6 states not_web|candidate|needs_review|verifying|verified|failed); `ReleaseService` (id, ingress role, root, runtime strategy node|python|static|cloudflare_dev|container, pkg mgr+lockfile, argv-list install/build/migrate/start, port_env, health path, depends_on, output dir); `EnvVarDecl` (name, scope build|runtime, required, secret class, resource binding — NAMES ONLY, no value field); `ResourceDecl` (kind sqlite v1, consumers, migrate cmd, persistent path, profiles.local + profiles.cloud=None); `ReleaseSpec` (kind, name, version_seq+tree_digest, services, env, resources, detector provenance, targets=["local_compose"], generated-file refs); `ReleaseIntent` (typed release_declare payload); `spec_digest()` sha256 canonical.
Acceptance:
1. 0 or 2 ingress services → validation error; exactly 1 passes.
2. `EnvVarDecl` has no `value` field (`"value" not in EnvVarDecl.model_fields`); bad env names rejected.
3. `spec_digest` byte-stable across field order; changes on any field change.
4. `ResourceDecl.profiles.cloud` defaults None + round-trips; enum has verifying|verified|failed (forward-compat).
5. Canonical serialize round-trip byte-identical.
6. Grep: no `cloudflare|wrangler|vercel|turso` (case-insensitive) in spec.py.
7. G1–G5 pass.
Out of scope: no detection/emission/IO/routes; no `import disco.tools.*`.

## WO-2 — Secret-path predicate relocation + light validation plane
Files: create `.../release/validate.py`, `test_release_validate.py`; modify `tools/projects/archive.py` (move `is_runtime_secret_path` body to core, re-export). Deps: WO-1.
Acceptance:
1. `validate_release(spec, files) -> ValidationResult{ok, blockers[]}` — blockers are data, never raise for control flow.
2. Blocker tests: (a) missing referenced path; (b) 0/2 ingress; (c) undeclared `${VAR}`; (d) secret file present (.env/.dev.vars/sub/.env.local); (e) secret-classed env literal in spec field.
3. Relocated predicate identical; `from disco.tools.projects import is_runtime_secret_path` still resolves.
4. Grep: no `subprocess|create_subprocess|docker` in validate.py.
5. G1–G5 (esp G2/G4 — imports moved).
Out of scope: no `docker compose config` here; no `core/loop/finish/` changes.

## WO-3 — Detection precedence (Track-1 subset)
Files (create): `.../release/detect.py`, `test_release_detect.py`, fixtures `packages/core/tests/fixtures/release_detect/` (express-node, fastapi, static, vite-spa, appkit-shaped, existing-dockerfile, unknown-stack, non-web-doc, native-sqlite). Deps: WO-1.
`detect_release(files, *, intent, provenance) -> DetectionResult`. Precedence: (1) typed intent / AppKit contract files → candidate; (2) repo Dockerfile/compose → needs_review + repairable diagnostic; (3) deterministic detectors (package.json+lock→node; pyproject/requirements+entry→python; index.html no server→static); (4) imported w/o intent → needs_review; (5) no HTTP evidence → not_web + reason. DBs: sqlite evidence→resource local `file:/data/app.db`; unknown engine→needs_review (never guess).
Acceptance:
1. Table-driven: each fixture → exact (assessment, strategy, ingress, resources); conflicting evidence → needs_review with both evidences.
2. Unknown-stack + complete intent → candidate; without intent → needs_review naming every missing contract field.
3. non-web → not_web+reason; native-sqlite → local `file:/data/app.db`; `mysql://` → needs_review, no invented resource.
4. Grep: no `surface|build_mode|prompt` in detect.py.
5. Determinism: twice → equal DetectionResult.
6. G1–G5.
Out of scope: no clean-room probes/attestation/preview-evidence (document gap in docstring).

## WO-4 — LocalComposeAdapter (portable bundle emitter)
Files (create): `.../release/local_compose.py`, `test_release_local_compose.py` + goldens `fixtures/release_compose/`. Deps: WO-1, WO-2.
`emit_local_compose(spec) -> dict[path,str]`: compose.yaml, per-service Dockerfile, .dockerignore, .env.example (names+comments only), SELFHOST.md, release.json (canonical spec). Hand-emitted deterministic YAML. Rules: healthcheck per ingress on health_path; one-shot migrate/init w/ `service_completed_successfully`; named volumes per persistent path; required runtime env `${VAR:?msg}`; `restart: unless-stopped`; ports `127.0.0.1:${HOST_PORT:-8080}:<c>`; no host bind mounts; sqlite `DATABASE_URL=file:/data/app.db` vol at /data.
Acceptance:
1. Goldens byte-identical (node, python, static); deterministic (same spec twice identical).
2. compose.yaml: exactly one published port; `${NAME:?` for every required runtime env & no optional; named volume per persistent path; migrate one-shot w/ service_completed_successfully; healthcheck under ingress.
3. .env.example lists every required name, no `=value`; release.json round-trips to equal ReleaseSpec.
4. Secret proof: planted sentinel value absent from all overlay strings.
5. Path collision → typed `OverlayConflict` naming path (no silent overwrite).
6. `[LIVE]` emitted bundle `docker compose config` exit 0 (integration-marked).
7. G1–G5.
Out of scope: no appkit/cloudflare_dev strategy yet (WO-6); no zip/routes; no repo's own compose.yaml.

## WO-5 — `release_declare` host tool + host-owned intent record
Files: create `tools/builtin/release_declare.py`, `test_release_declare.py`; modify `tools/registry.py` (register build/agent scopes), `tools/projects/store.py` (write/read_release_intent → `release-intent.json` beside manifest.json, outside workspace/). Deps: WO-1.
Acceptance:
1. Valid args persist sidecar outside workspace; read round-trips to equal ReleaseIntent.
2. Env value present, or shell-string (non-argv) command → typed tool error, nothing persisted.
3. Result text confirms names-only; sidecar bytes scan shows no rejected value.
4. Available in agent/build scope, NOT in strict AppKit allowlist (`tools/appkit_scope.py`); tool-schema gate passes (typed fields, no blind object).
5. Re-declare overwrites atomically (temp+replace like `_write_json_atomic`).
6. G1–G5.
Out of scope: no verification claims; no UI.

## WO-6 — AppKit interim local-run compose strategy (`cloudflare_dev`)
Files: modify `.../release/detect.py` (AppKit contract → cloudflare_dev candidate), `.../release/local_compose.py` (cloudflare_dev service template); create `packages/core/tests/test_release_appkit_local.py` (drives real `disco.core.appkit.generate`). Deps: WO-3, WO-4.
Encoded: Dockerfile FROM node:22-slim, `npm ci`, `npm run build`; entrypoint writes `/app/.dev.vars` at start from `${ADMIN_TOKEN:?}`; init one-shot `npx wrangler d1 execute <db> --local --file=./schema.sql --persist-to /data/state`; app `npx wrangler dev --ip 0.0.0.0 --port 8787 --persist-to /data/state`; vol /data; healthcheck GET / on 8787. SELFHOST.md states interim workerd host.
Acceptance:
1. generate() default spec + detect → candidate, single ingress strategy cloudflare_dev, sqlite resource persistent path /data/state.
2. Bundle: Dockerfile has `npm ci`+`npm run build`; compose app cmd has `--ip 0.0.0.0`+`--persist-to /data/state`; init refs schema.sql+persist dir w/ ordering; ADMIN_TOKEN only as `${ADMIN_TOKEN:?` in compose + name in .env.example (bytes scan: no token value anywhere).
3. Bundle has no `.dev.vars` file; entrypoint is only writer (exactly one `.dev.vars` ref, in entrypoint script).
4. `git diff --name-only` touches nothing under `appkit/` or `appkit_cloudflare/`.
5. `[LIVE]` unpack + `ADMIN_TOKEN=test docker compose up -d --build`; GET / SPA; POST /api/leads ok; restart; auth GET /api/leads shows persisted row; down leaves volume.
6. G1–G5.
Out of scope: no generator changes, no wrangler.toml rewrite, no D1→libSQL, no AppKit-on-Next.

## WO-7 — Agent-server release API + upgraded Download source
Files: create `agent_server/routes/release.py`, `test_release_endpoints.py`; modify `routes/__init__.py`, `app.py`, `routes/projects.py` (download overlay injection), `api-endpoints.md`. Deps: WO-2,3,4 hard; WO-5,6 soft.
`GET /api/projects/{cid}/release` → {assessment, reasons, blockers, required_env[{name,scope,required,secret}], command:"docker compose up -d --build", ingress|null, self_host, spec_digest, version_seq, tree_digest}. `/download` unchanged path/auth; candidate+valid → zip also carries overlay (collision: workspace wins, blocker surfaced). Assessment computed on request; nothing at finish.
Acceptance:
1. `/release` on node→candidate self_host:true; unknown→needs_review + repairable blockers; docs→not_web self_host:false honest reason; appkit→candidate strategy evidence. Identical response key set across all four.
2. Download candidate → source + compose.yaml + Dockerfile + .dockerignore + .env.example + SELFHOST.md + release.json; not_web → byte-equivalent file-set to today's plain zip (no overlay).
3. Secret proof: planted `.env` sentinel `PLANTED-731-hunter2` + `.dev.vars` token → neither in zip; no sentinel in any entry or the /release JSON.
4. Auth: non-owner 403 project_forbidden; unknown 404 project_not_found; storage unset 404 storage_unavailable (mirror existing).
5. Grep: no `surface` in release.py; diff touches nothing under `core/loop/finish/` or `appkit_cloudflare/`.
6. Idempotent identical bodies twice; NO subprocess (assert monkeypatched create_subprocess_exec never invoked).
7. `api-endpoints.md` documents `/api/projects/{id}/release`.
8. G1–G5.
Out of scope: no deploy endpoints/jobs/attestations/prepare/review routes; no `/download` auth/URL change.

## WO-8 — Frontend data layer for release capabilities
Files: create `frontend/src/types/release.ts`; modify `frontend/src/api/projects.ts` (getProjectRelease + offline fixture), `frontend/src/hooks/useProjects.ts` (useProjectRelease). Deps: WO-7.
Acceptance:
1. types mirror WO-7 response (6-state union; required_env name/scope/required/secret — NO value).
2. Vitest: useProjectRelease offline returns candidate/needs_review/not_web fixtures w/o backend.
3. Vitest: API hits `GET /api/projects/{cid}/release` on agent base (agentHttpBase), not app server.
4. Components don't import api/projects.ts directly — only hooks (diff grep).
5. GF passes.
Out of scope: no rendering (WO-9).

## WO-9 — Capability-driven Self-host UI, identical across modes
Files: create `frontend/src/components/build/SelfHostPanel.tsx` + test; modify `BuildSurface.tsx` (~L428 "Export"→"Download source"; wire panel), `build/DeliverablePanel.tsx` (labels→"Download source"), `views/ProjectsView.tsx` ("Download a zip"→"Download source"; per-row Self-host from useProjectRelease). Deps: WO-8.
Acceptance:
1. candidate → exact command `docker compose up -d --build` + every required env NAME + working Download source; needs_review → each blocker msg + no command/self-host affordance; not_web → honest reason + only Download source. No dead buttons.
2. "Export" no longer labels workspace-zip action (assert accessible name "Download source").
3. Mode-identity: SelfHostPanel props only {release,onDownload}; grep no `surface|framing|appkit|agent`; AgentSurface.tsx unmodified.
4. Secret hygiene: secret:true env → names only rendered.
5. GF passes.
6. `[LIVE]` Firefox/Playwright screenshot candidate + not_web states.
Out of scope: no Deploy drawer/provider/Settings (Track 2).

## WO-10 — Fixtures, E2E bundle proof, finish-path non-regression guard
Files (create): `agent_server/tests/fixtures/release_e2e/` (build node/express, agent python fastapi, imported node-http, appkit in-test), `agent_server/tests/integration/test_selfhost_e2e.py` (integration), `packages/core/tests/test_release_finish_isolation.py` (unit guard). Deps: WO-4,6,7.
Acceptance:
1. Guard (unit): no module under `core/loop/finish/` imports `release.local_compose`/`release.detect` (AST/import scan); campaign diff has no path under `core/loop/finish/`.
2. Route E2E (unit, no docker): each of 4 fixtures `/release`→candidate + overlay in zip; identical response schema + action set across all four.
3. Unknown-stack no intent → needs_review + diagnostic; docs → not_web + reason; both still plain-source downloadable.
4. Secret sweep across all 4 bundles + /release bodies → nothing.
5. `[LIVE]` each fixture on docker host: unzip → `docker compose config` 0 → `up -d --build` → health 200 + body → DB write → restart → persisted → down; one command, no host paths.
6. `[LIVE]` migration idempotence: init twice → healthy.
7. G1–G5 (integration excluded from required lane).
Out of scope: no Vercel/Turso/attestation/browser-probe.

## Dependency graph
WO-1 → {WO-2, WO-3, WO-5}; {WO-2,WO-3} → WO-4 → {WO-6, WO-7}; WO-7 → WO-8 → WO-9; {WO-4,6,7} → WO-10.

## Execution order (orchestrator: SEQUENTIAL, one worktree — parallel implementers would collide on shared files store.py/registry.py/routes/detect.py/local_compose.py)
WO-1 → WO-2 → WO-3 → WO-5 → WO-4 → WO-6 → WO-7 → WO-8 → WO-9 → WO-10.
