# Export Track 1 Closeout — Adversarial, Non-Bypass Acceptance Plan

**Status:** PROPOSED; no criterion in this document is passed yet.

**Production baseline:** `2ec1ceba` (`export-track1`) on 2026-07-12.

**Original campaign baseline:** `5cfbc5c468b3bc5275cdb8e474acc4474732b468`.

**Historical plan:** `docs/export-track1-work-orders.md`.

**Purpose:** close the independent audit findings and prove that the self-host handoff is
honest, source-bound, secret-safe, and runnable—not merely green against friendly fixtures.

This document is stricter than the historical plan. Where the two disagree, this closeout
plan controls final sign-off. The two reconciliations already recorded in the historical
plan remain locked: the provider-neutral strategy is `dev_server`, and the `.dev.vars`
property is “the entrypoint is the sole writer,” not “the text occurs exactly once.” Those
decisions are not reopeners or gaps.

---

## 1. What “cannot be bypassed” means

No repository document can technically prevent an actor with unrestricted write access
from weakening both code and tests. The control that makes this plan non-bypassable is a
two-party acceptance protocol:

1. An acceptance-only commit is reviewed and frozen **before** production remediation.
2. The implementing agent may not alter the frozen acceptance files.
3. A clean-checkout run produces machine-readable evidence.
4. A reviewer other than the implementing agent reruns the gates and signs off the exact
   commit SHA.

An implementation agent's prose, checked boxes, screenshots of terminal text, or claim
that a criterion is “effectively” met is not evidence. If the frozen harness must change,
the prior freeze is void and the owner/reviewer must ratify a new harness before
implementation continues.

### 1.1 Frozen acceptance paths

WO-C0 will create and freeze these paths:

- `scripts/verify_export_track1_closeout.py`
- `packages/core/tests/export_track1_closeout/**`
- `packages/tools/tests/export_track1_closeout/**`
- `packages/agent-server/tests/export_track1_closeout/**`
- `packages/agent-server/tests/integration/test_export_track1_closeout_live.py`
- `packages/agent-server/tests/fixtures/export_track1_closeout/**`
- `frontend/src/test/export-track1-closeout/**`
- `frontend/e2e/export-track1-closeout/**`
- `.github/workflows/export-track1-closeout.yml`
- `docs/export-track1-closeout-work-orders.md`
- `docs/export-track1-closeout-acceptance.sha256`

After the acceptance tag is created, the final implementation diff against that tag must
be empty for every path above. Any difference invalidates all results until independently
reviewed and re-frozen. The SHA-256 manifest hashes every other frozen file; it does not
attempt to hash itself. The reviewer-created protected tag freezes the manifest itself.

### 1.2 Evidence that counts

Valid evidence has all of these properties:

- It is produced from a clean checkout of the exact candidate SHA.
- Every command exits `0`; exit code is captured, not inferred from summary text.
- Selected pytest lanes report zero failed, errors, skipped, xfailed, xpassed, or
  deselected tests. The verifier reads JUnit XML and rejects any nonzero count.
- The verifier invokes the frozen node IDs directly and compares `--collect-only` output
  with the frozen inventory, so changing pytest discovery/configuration cannot hide a test.
- The live lane uses a real Docker Engine and Docker Compose v2. A mock, YAML parser,
  Podman compatibility run, or `docker compose config` alone is supplemental—not live
  proof.
- Public route tests use the real FastAPI router, actual `ProjectStore`, real zip bytes,
  and real filesystem snapshots. They do not replace the target release functions with
  mocks.
- Bundle tests inspect and execute the actual bytes returned by `/download`; they do not
  call `emit_local_compose()` and pretend that is an end-to-end download test.
- Frontend tests render the real component through its real hook/API boundary. A direct
  component fixture may supplement but cannot replace the route/hook test.
- The evidence manifest contains the candidate SHA, clean-tree status, tool versions,
  command exit codes, JUnit counts, response/zip digests, image IDs, and cleanup result.

### 1.3 Evidence that never counts

The following are automatic failure, even if every remaining test is green:

- `skip`, `skipif`, `xfail`, an environment-controlled early return, or a swallowed
  assertion in any closeout test.
- `continue-on-error`, an advisory-only CI job, or a workflow step guarded so it does not
  run on the candidate commit.
- Catching `Exception` and falling back to a plain zip for a **bound self-host download**.
- Updating a golden file without a reviewer inspecting the semantic diff.
- Replacing a behavioral assertion with a source grep, mock call-count, or snapshot that
  never executes the public boundary.
- Adding `# type: ignore`, `# noqa`, `pyright: ignore`, coverage exclusion, lint exclusion,
  or a broader exception solely to make a gate pass.
- Reducing an existing test's assertions, deleting a fixture, relaxing a regex/schema, or
  changing expected behavior to match the implementation.
- Production behavior conditional on `PYTEST_CURRENT_TEST`, `CI`, fixture/project names,
  acceptance sentinels, closeout-only environment variables, or test-only endpoints.
- Treating `candidate` as `verified`, or changing user copy to hide uncertainty.
- Claiming the live lane is deferred. Live proof is a final closeout requirement.

### 1.4 Required evidence manifest

`scripts/verify_export_track1_closeout.py` must write an evidence directory outside the
checkout (CI uploads it as an artifact) containing:

```text
evidence.json
pytest-nonlive.xml
pytest-closeout.xml
pytest-live.xml
frontend-vitest.json
bundle-digests.json
docker-versions.txt
docker-inspect-sanitized.json
compose-ps.json
cleanup.json
```

`evidence.json` must record `passed: true` only after every required file was produced and
validated. It must refuse to run on a dirty checkout unless invoked in an explicit
acceptance-authoring mode that can never emit `passed: true`.

---

## 2. Locked product semantics

These are decisions, not implementation suggestions:

1. **`candidate` means statically plausible and unverified.** It may expose a complete
   bundle, but the UI label is exactly **“Bundle available”** and visibly says **“Not
   runtime-verified.”** It must never say “Ready,” “verified,” or an equivalent.
2. **“Ready to self-host” is reserved for `assessment == verified`.** Track 1 does not
   fabricate that state. A future verifier may produce it.
3. **`self_host == true` means a complete, internally consistent overlay is available.**
   Any blocker or overlay collision forces `self_host == false` and no partial overlay.
4. **Every non-null `(version_seq, tree_digest)` names a real immutable
   `VersionRecord`.** A hypothetical next sequence is forbidden.
5. **A self-host download is source-bound.** The zip source, `release.json`, response
   source fields, and `spec_digest` all describe the same immutable version.
6. **Runtime uncertainty fails closed.** If the detector cannot establish the entrypoint,
   port contract, health path, output directory, required environment, or supported
   toolchain, the result is `needs_review` with a typed repair instruction.
7. **No partial security overlay ships.** Workspace collisions are review blockers; the
   generated overlay is atomic, all-or-none.
8. **Secrets presented through the release subsystem are references, never bundle
   material.** A required secret may exist in the runtime container environment, but not
   in intent sidecars, API JSON, zip bytes, build context, image filesystem/history,
   generated docs, or logs. Arbitrary user source can contain semantically unknowable
   text; the enforceable guarantee covers known host/release secret values and every value
   the intent/env interface receives.
9. **Resource consumers are authoritative.** A service receives only the mounts and
   resource-bound environment it consumes.
10. **Live persistence applies to every stateful fixture.** Stateless fixtures must boot,
    serve, and restart; stateful fixtures must additionally write, restart, and read the
    same record. This replaces the historical plan's impossible wording that every
    stateless fixture must perform a DB write.

---

## 3. Campaign-wide gates

All work orders inherit these gates. A work order is not complete if its focused tests
pass but a global gate fails.

### 3.1 Repository and architecture gates

Run from repository root, in this order:

```bash
test -z "$(git status --porcelain)"
uv sync --all-packages
uv run basedpyright
uv run lint-imports
uv run python scripts/check_arch_budget.py
uv run python scripts/check_tool_schemas.py
uv run python scripts/gen_arch_diagram.py --check
git diff --check 2ec1ceba...HEAD
git diff --name-only --diff-filter=ACMR 2ec1ceba...HEAD -- '*.py' -z \
  | xargs -0 -r uv run ruff check
git diff --name-only --diff-filter=ACMR 2ec1ceba...HEAD -- '*.py' -z \
  | xargs -0 -r uv run ruff format --check
```

Required result: every command exits `0`; basedpyright reports zero errors, warnings, and
notes. The verifier also rejects newly introduced suppression directives in changed
Python/TypeScript source or closeout tests, and runs ESLint with zero errors on every
changed frontend source/test file.

### 3.2 Python behavioral gates

```bash
uv run pytest packages/core/tests packages/tools/tests packages/agent-server/tests \
  -o addopts='' -m "not integration" --junitxml=/tmp/export-track1-nonlive.xml
uv run pytest packages/core/tests/export_track1_closeout \
  packages/tools/tests/export_track1_closeout \
  packages/agent-server/tests/export_track1_closeout \
  -o addopts='' \
  -m "export_track1_closeout and not integration" \
  --junitxml=/tmp/export-track1-closeout.xml
```

Required result: exit `0`, with zero failed/errors/skipped/xfail/xpass/deselected in the
focused closeout lane. The full non-live lane may retain only skips/xfails already present
at the frozen acceptance tag; no new one may originate in campaign-touched tests.

### 3.3 Frontend gates

```bash
cd frontend
npm ci
npm run typecheck:build
npx vitest run src/test/export-track1-closeout \
  --reporter=json --outputFile=/tmp/export-track1-vitest.json
npm run test
npx vite build
npx playwright test e2e/export-track1-closeout --project=firefox --retries=0 --reporter=json
```

Required result: all commands exit `0`, no skipped/todo tests in the closeout suite, and
the JSON report contains every frozen closeout test file. The Firefox run has zero retries
and produces the frozen candidate/needs-review visual evidence from its own fixture-mode
server, never a reused developer server.

### 3.4 Live Docker gate

```bash
docker version
docker compose version
uv run pytest \
  packages/agent-server/tests/integration/test_export_track1_closeout_live.py \
  -o addopts='' \
  -m "export_track1_closeout and integration" -ra \
  --junitxml=/tmp/export-track1-live.xml
```

Required result: Docker daemon reachable, Compose v2 reachable, test exit `0`, and JUnit
shows zero skipped/xfail/xpass/deselected. The test owns a unique Compose project name,
uses clean builds, and proves cleanup in `finally`; leaked containers, networks, images
created solely for the test, or volumes fail the lane.

### 3.5 Scope and diff gates

The final diff from `2ec1ceba` must not modify:

- `packages/core/src/disco/core/appkit/generator.py`
- `packages/agent-server/src/disco/agent_server/appkit_cloudflare/**`
- `packages/core/src/disco/core/loop/finish/**`
- Stripe/webhook implementation paths

The historical work-order reconciliation may be linked but not rewritten to lower a
criterion. Any necessary scope expansion requires owner approval before implementation.

---

## 4. WO-C0 — Acceptance harness first, then freeze it

**Goal:** encode every criterion below as a failing public-boundary test before production
code changes begin.

**Allowed changes in this work order:** frozen acceptance paths, pytest marker
registration, this document, and no production behavior.

### Required red-test inventory

The acceptance commit must contain named tests covering at least:

- custom project root through the real tool executor and release route;
- missing intent-writer capability fails closed;
- real VersionRecord binding and nonexistent-sequence rejection;
- bound download remains byte-identical under concurrent live-workspace mutation;
- dirty/unversioned workspace fails closed;
- `candidate` UI copy is unverified and never “Ready”;
- undeclared source env, fixed port, nested Python entrypoint, Flask, Bun, `uv`, Poetry,
  and dynamic/static output-dir ambiguity;
- public build env reaches a built Vite asset;
- secret build env is rejected unless the secure build-secret implementation exists;
- argv `$()`/backtick/semicolon/newline injection corpus;
- Dockerfile path/newline/flag injection corpus;
- health-path quote/code injection corpus;
- credential-bearing resource URLs and secret CLI flags;
- collision matrix for every generated overlay path;
- per-consumer mounts and resource-bound env;
- duplicate mount target/persistent-path rejection;
- four real bundle lifecycle tests and AppKit persistence/migration idempotence;
- secret absence from zip, build context, image filesystem/history, and logs.

It must also preserve every already-remediated gap with explicit green regressions:

- Vite dependencies install before build;
- mixed Node/Python evidence fails closed with both evidence strings;
- an empty intent is `needs_review` for `start_cmd`;
- inline env assignment rejection is value-redacted;
- secret-shaped env names classify secret;
- generic `depends_on` coexists with migration ordering;
- duplicate declared volume names cannot collapse state;
- imported provenance survives manifest rewrites and imported unknown/no-intent reaches
  `needs_review` through the real route;
- invalid assembled specs return `release_spec_invalid`, never HTTP 500;
- runtime-secret paths stay absent from response and zip;
- finish-path isolation and all campaign DO-NOT-TOUCH guards remain intact.
- the release spec remains provider-neutral and contains no provider executable/vendor
  vocabulary.

### Acceptance criteria

1. On baseline `2ec1ceba`, every remediation work order C1–C7 has at least one failing
   public-boundary test for its intended gap. Preservation/regression tests may already be
   green. A syntax error, missing fixture, import error, or one early failure masking the
   rest does not count as a red test.
2. The reviewer records each red test's node ID and expected failure code, plus the full
   required node-ID inventory, in
   `docs/export-track1-closeout-acceptance.sha256` metadata alongside SHA-256 hashes of
   every other frozen file.
3. At least one test per work order exercises the public API/download/tool/UI boundary;
   pure-unit tests are supplemental.
4. Acceptance E2E tests contain no `unittest.mock`, `pytest.monkeypatch`, `patch`, or fake
   emitter/release-route replacement. The verifier checks their AST/imports.
5. The live test fails—not skips—when Docker or Compose is unavailable.
6. The acceptance-only commit is independently reviewed and given a reviewer-created,
   protected tag
   `export-track1-closeout-acceptance-v1` before WO-C1 begins.
7. After the tag, changing a frozen path invalidates the tag and requires a new review.
8. File/project names and harmless source formatting vary from a recorded random seed at
   test time; detector inputs do not receive fixture names. Hard-coding fixture paths,
   project IDs, or exact fixture bytes cannot satisfy the matrix.
9. The verifier rejects production references to closeout fixture names, acceptance
   sentinels, `PYTEST_CURRENT_TEST`, or a test-only success switch. The independent diff
   review checks equivalent indirect branches that a lexical scan cannot prove.

**Forbidden shortcut:** writing tests after seeing the final implementation.

---

## 5. WO-C1 — Correct host-owned intent persistence for every configured root

**Problem closed:** `release_declare` currently constructs `ProjectStore("")` and writes
to the default root even when runtime settings select a custom root.

**Required design:** `release_declare` becomes an `in_process` host tool and receives a
narrow host-owned intent-writer capability in `ToolContext`. The runtime closure resolves
the active configured `ProjectStore` at invocation time; the tool receives no raw root
path and has no fallback store. Missing capability fails closed with a typed error.

### Acceptance criteria

1. A real `ToolExecutor` configured with a custom projects root containing spaces writes
   exactly:
   `<custom-root>/<cid>/release-intent.json`.
2. The same invocation writes nothing under the default `DISCO_DATA_DIR`, sandbox
   workspace, process temporary directory, or live workspace tree.
3. `GET /api/projects/<cid>/release` through a real app instance consumes that sidecar and
   returns the declared candidate fields.
4. A matrix covers default root, custom absolute root, unavailable root, and a root changed
   between executor construction and tool invocation. The active runtime configuration is
   authoritative in every case.
5. Missing writer capability, invalid root, owner/conversation mismatch, and persistence
   failure return typed tool errors and persist zero bytes.
6. Re-declaration is atomic: a forced write failure leaves the prior valid sidecar
   byte-identical and no `.tmp` file remains.
7. `release_declare.definition.runs_in == "in_process"`; it requests no sandbox filesystem
   capability and never derives a host path from `workspace_path`.
8. Tool success output contains env names and non-sensitive structural metadata only. It
   does not echo full argv, resource URLs, filesystem roots, or sidecar paths.
9. Existing default-root behavior remains green through the same executor path, not by
   directly calling `ReleaseDeclareTool.run()`.

**Proof command:** focused closeout tests for `test_intent_store_*`, then all global gates.

---

## 6. WO-C2 — Immutable, real source binding and bound downloads

**Problems closed:** speculative `version_seq`, separate file/digest traversals, release
assessment against mutable bytes, and download reassessment/reread races.

### Required behavior

- Assessment reads a real version workspace, not the mutable live mirror.
- Before use, the version directory is hashed and must equal its `VersionRecord` digest.
- A current workspace with no matching committed version fails closed with blocker
  `source_not_snapshotted`; it does not invent a sequence.
- Source fields are nullable when no exact source can be named. Non-null fields always
  resolve to a real record.
- The self-host action downloads with an immutable binding, for example
  `/download?version_seq=N&spec_digest=D`. The exact URL shape may differ, but both values
  must be enforced server-side.
- A bound download never silently falls back to an unbound/plain zip.

### Acceptance criteria

1. For every candidate response, `ProjectStore.list_versions(cid)` contains exactly the
   returned `version_seq`, and that record's digest equals `tree_digest`.
2. Rehashing the corresponding version workspace yields the same `tree_digest`.
3. Hashing all non-overlay source entries in the downloaded zip yields that same digest.
4. Parsing `release.json` yields the same `version_seq`, `tree_digest`, and `spec_digest`
   as the response and bound download request.
5. With only version 1 stored and dirty live bytes present, the route returns
   `needs_review`, `self_host:false`, blocker `source_not_snapshotted`, and no speculative
   version 2.
6. With no version record, the route returns the same fail-closed state and null source
   fields; it does not mutate storage from a GET.
7. A corrupt or missing version workspace returns a typed source-integrity blocker/error,
   never a candidate and never a plain self-host fallback.
8. After receiving a response bound to version N, a concurrent thread repeatedly edits
   the live mirror and cuts N+1 while the bound zip streams. Across at least 50 iterations,
   every completed zip is wholly N or fails atomically with a typed 409/410; no mixed tree
   is accepted.
9. Supplying the wrong `spec_digest`, nonexistent sequence, another owner's sequence, or
   a sequence from another conversation fails with the documented 403/409/410 and emits
   no bytes.
10. Two unchanged assessments and two bound downloads are byte/digest deterministic.
11. Workspace hashing, version verification, and zip construction do not block the async
    server event loop. While repeatedly assessing/downloading a large bounded fixture,
    concurrent health requests continue to complete within the frozen test's generous
    timeout; moving only metadata work off-loop while streaming synchronously fails.

**Forbidden shortcut:** holding a lock only while computing metadata and then streaming
the mutable live directory after releasing it.

---

## 7. WO-C3 — Honest readiness and fail-closed deterministic detection

**Problems closed:** predictable false candidates and the UI's “Ready” claim for an
unverified static assessment.

### Positive fixture matrix

These canonical shapes must remain deterministic candidates with a complete overlay:

| Fixture | Required proven contract |
|---|---|
| Express/Node | root package lock, supported npm install, start entrypoint, exact `PORT` use, GET `/` |
| FastAPI | root module and `app`, declared dependency, exact `PORT` use, GET `/` |
| Static | root `index.html`, no server/build ambiguity |
| Vite | npm lock, Vite build, statically resolved output directory, public build-env contract |
| AppKit | generated contract, `dev_server`, D1 state path, admin secret name |

### Negative/adversarial fixture matrix

Each fixture must return `needs_review`, `self_host:false`, no overlay, and the exact
blocker field/code shown:

| Fixture | Required blocker |
|---|---|
| Node reads undeclared `process.env.SESSION_SECRET` | `required_env_unresolved` |
| Node listens only on literal 3000 | `port_contract_unresolved` |
| Node uses dynamic `process.env[name]` | `required_env_unresolved` |
| FastAPI app only at `src/acme/api.py` | `entrypoint_unresolved` |
| Flask app with no declared compatible start | `entrypoint_unresolved` |
| Python start executable absent from declared dependencies | `toolchain_unsupported` |
| `bun run`, `uv run`, or `poetry run` on a base that lacks it | `toolchain_unsupported` |
| Vite config computes `outDir` dynamically | `output_dir_unresolved` |
| Build tool with unknown output directory | `output_dir_unresolved` |
| No route corresponding to inferred health path | `health_path_unresolved` |
| Competing runtime evidence | `runtime_conflict` with both evidence strings |

### Acceptance criteria

1. The complete positive and negative matrices are table-driven and run twice with equal
   typed results and byte-identical positive overlays.
2. Common direct environment forms are recognized in JS/TS and Python. Requiredness that
   cannot be established is not guessed; typed intent must resolve it.
3. Nested/dynamic entrypoints are not replaced with `main:app` or another invented module.
4. Unsupported command heads do not fall back to Node.
5. Static output defaults to `dist` only for a provable default-Vite configuration;
   explicit literal `outDir` is honored; dynamic/unknown config fails closed.
6. The API never returns `self_host:true` when `blockers` is nonempty.
7. For `candidate`, every UI mounting point renders exact visible strings “Bundle
   available” and “Not runtime-verified”; case-insensitive DOM text contains no “ready.”
8. For `needs_review`, every blocker is rendered, no run command appears, and bound
   self-host download is unavailable. Plain source download remains available.
9. `verified` fixture data may render “Ready to self-host,” proving that wording is tied
   to verification rather than deleted indiscriminately.
10. SelfHostPanel remains capability-driven and mode-independent.
11. Detection is bounded: binary files, oversized text, excessive file counts, and dynamic
    syntax produce deterministic review blockers or are safely ignored according to a
    documented cap; the detector never performs an unbounded whole-tree text decode.
12. Frozen Firefox proofs cover candidate, needs-review, and not-web states in every UI
    mounting mode. DOM assertions prove the exact command/action set and screenshots prove
    blockers/uncertainty are visible without interaction.

**Forbidden shortcut:** renaming the API's `candidate` state to `verified` or hard-coding
fixture names in detection.

---

## 8. WO-C4 — Complete env, build, and toolchain lowering

**Problems closed:** missing application env discovery, Compose build args with no
Dockerfile `ARG`, and commands that name tools absent from their image.

### Required intent contract

Release intent must be able to express, as typed fields rather than a blind object:

- runtime strategy;
- install, build, and start argv lists;
- package manager/lockfile where applicable;
- static output directory where applicable;
- runtime and build env **names**, scope, requiredness, and secret classification;
- port env and health path;
- resources.

If backward-compatible v1 sidecars cannot be upgraded without guessing, they return
`needs_review` with `intent_upgrade_required`; they are not silently reinterpreted.
Secret classification is host-derived and fail-closed. Caller/model input may request
stricter treatment but may not downgrade a secret-shaped name to public.

### Acceptance criteria

1. Every statically discovered env name is either present once in `required_env` with the
   correct scope/requiredness/secret class or produces a blocker. No name disappears
   between source, intent, spec, API, `.env.example`, Compose, and UI.
2. A required public build variable appears as a Compose build argument and a Dockerfile
   `ARG` visible to install/build `RUN` steps. It is not persisted with `ENV` and is absent
   from the final runtime environment unless separately declared runtime scope.
3. A live Vite fixture uses `VITE_PUBLIC_BANNER` only at build time; the served asset
   contains the supplied public marker, proving the value reached the build.
4. Required secret build variables either use real Compose/BuildKit secret mounts with
   zero image/history leakage **or** fail closed with `secret_build_env_unsupported`.
   Ordinary `ARG` is not acceptable for a secret-classed build variable.
5. Missing required build/runtime names make `docker compose config` or startup fail with
   a clear name-only message. Optional names do not use required guards.
6. Npm candidates install before build. No install/build layer depends on host
   `node_modules`.
7. Every accepted executable is available in the emitted image through the base image or
   declared install. An executable-availability unit table and live `command -v` proof
   cover every supported toolchain.
8. Bun, Poetry, `uv`, pnpm, and Yarn are either installed/pinned and live-tested or
   rejected with `toolchain_unsupported`. Merely mapping them to Node/Python is failure.
9. Lockfile/package-manager disagreement fails closed; it is not resolved by precedence.
10. `.env.example` remains names/comments only. Public test values used by live tests are
    supplied outside the bundle.
11. Any persisted intent/spec shape change increments its schema version. V1 input is
    either migrated by a deterministic tested adapter or rejected with the exact upgrade
    blocker; canonical v2 serialize/parse/serialize bytes are identical.

---

## 9. WO-C5 — Command, path, healthcheck, and secret injection boundary

**Problems closed:** incomplete secret-value rejection, arbitrary resource URLs, unsafe
shell lowering of argv containing `$`, and unescaped values in generated Dockerfile and
healthcheck code.

### Required safety design

- Arbitrary argv must never be concatenated into `sh -c` source.
- The only expandable command token form is an entire typed env reference such as
  `${PORT}` whose name is declared. `$`, backticks, command substitution, separators,
  redirections, control characters, or partial interpolation anywhere else are rejected.
- Accepted commands must parse through a runtime-specific grammar (supported executable,
  existing script/module/path, and known-safe options). Opaque custom argument shapes are
  `needs_review`; there is no catch-all “arbitrary argv is probably fine” path.
- If a shell wrapper is unavoidable, it is a generated constant program that constructs
  argv with safely quoted literals and expands only validated whole-token env references.
- Dockerfile paths use normalized workspace-relative POSIX paths and JSON-form
  instructions where possible. User/model text can never create a new Dockerfile line,
  flag, stage, or source outside the context.
- Health paths use a restricted HTTP-path grammar and are passed as data, not pasted
  unescaped into Python/JavaScript source.
- SQLite local URLs are credential-free `file:` URLs with an absolute POSIX path
  consistent with `persistent_path`.

### Acceptance criteria

1. Case-insensitive inline assignments (`name=value`, `Name=value`, uppercase variants)
   are rejected in every command field without echoing the token.
2. Secret CLI forms including `--token VALUE`, `--token=VALUE`, `--password`, `--secret`,
   `--api-key`, `--credential`, and URL userinfo are rejected unless the value is a typed,
   declared secret env reference.
3. Tool success/rejection output and structured data do not echo argv, URLs, roots, or
   rejected values. The persisted sidecar is absent on rejection.
4. Hypothesis/adversarial corpora cover `$()`, `${...}` misuse, backticks, `;`, `&&`, `||`,
   pipes, redirects, quotes, backslashes, CR/LF, Unicode separators, NUL, and leading
   command flags across argv/path/health/resource fields.
5. For every invalid input, validation fails before emission and the exception text omits
   the planted sentinel.
6. Malicious roots/output paths cannot add `RUN`, `COPY --from`, `ADD`, or a second line to
   any Dockerfile. Absolute paths, traversal, ambiguous normalization, and control chars
   are rejected.
7. Malicious health paths cannot alter the healthcheck program. The emitted health URL
   round-trips exactly as data for every accepted path.
8. Resource URLs with authority/userinfo, query, fragment, non-`file` scheme, relative
   path, or mismatch with `persistent_path` are rejected.
9. A real-container canary passes the full injection corpus and proves no sentinel file,
   process, network request, or log entry was created by rejected input.
10. Source inspection gate: release command rendering contains no helper equivalent to
    “if token contains `$`, paste it unquoted into shell source.”
11. Runtime-specific grammar tests reject unknown flags/positional values instead of
    relying only on the secret-flag blacklist; supported command matrices prove every
    accepted literal has a defined role.

**Forbidden shortcut:** expanding the blacklist while retaining arbitrary shell-string
construction. The representation/rendering boundary must be safe by construction.

---

## 10. WO-C6 — Atomic overlays and collision-honest API/UI

**Problems closed:** security files suppressed while `self_host:true`, partial overlays,
and blockers hidden in the UI's ready branch.

### Acceptance criteria

1. Generate the complete overlay in memory, validate it, then apply collision policy once.
   There is no state in which only a subset is considered releasable.
2. For a collision with each generated path (`compose.yaml`, every Dockerfile path,
   `.dockerignore`, `.env.example`, `SELFHOST.md`, `release.json`, AppKit entrypoint), the
   response is `needs_review`, `self_host:false`, `spec_digest:null`, and contains blocker
   `overlay_path_conflict` with the exact path.
3. A collided bound download returns typed 409 and no body bytes. Plain source download
   remains available and retains the workspace file.
4. The UI renders every collision blocker and no self-host command/bound bundle action.
5. A workspace `.dockerignore` that omits `.env` can never coexist with a self-host-ready
   partial bundle.
6. An uncollided candidate zip has exactly the frozen overlay path set—no missing or extra
   generated files—and parses under Compose.
7. The zip writer rejects duplicate archive names, case-fold collisions relevant to
   Windows/macOS, slash-normalization collisions, symlinks, and traversal.
8. Assessment and bound download use the same collision result; the download does not
   silently reassess into a different action set.
9. No broad `except Exception` converts a bound release failure to a successful plain zip.
10. Existing not-web/needs-review unbound source downloads remain behavior-compatible.

---

## 11. WO-C7 — Coherent multi-service resource and env topology

**Problems closed:** every resource mounted into every service, ignored `consumers`, and
multiple volumes targeting the same container path.

### Required model invariants

- `persistent_path` is an absolute normalized POSIX path.
- Distinct resources may not claim the same persistent path or mount target in v1.
- Every resource has at least one valid consumer.
- Resource-bound env is injected only into those consumers and the relevant migration
  service.
- In a multi-service spec, unbound env must declare consumers; implicit fan-out is
  forbidden. A single-service spec may default to its sole ingress.

### Acceptance criteria

1. In a `web + worker` fixture where only `web` consumes `db`, only `web` receives the DB
   mount and bound `DATABASE_URL`; `worker` receives neither.
2. A worker-only SQLite/state resource at a distinct path is mounted/injected only into
   `worker`.
3. Migration receives only the resource it migrates and no unrelated secret/runtime env.
4. Duplicate persistent paths, duplicate mount targets, URL/path disagreement, empty
   consumers, or consumer/env references to unknown services fail schema validation.
5. Exactly one named volume is emitted per accepted resource persistent path, with no two
   service volume entries sharing a target unless they intentionally reference the same
   resource and service semantics permit it.
6. Generic `depends_on` and migration ordering coexist without overwriting each other;
   cycles and self-dependencies fail validation.
7. The emitted Compose document round-trips through `docker compose config --format json`;
   parsed mounts/env/dependencies equal the spec topology exactly.
8. A secret required only by one consumer is absent from every other service's resolved
   environment in the parsed Compose model.
9. Resource order changes do not change semantic output or assigned volume identity.
10. AppKit's state volume and migration behavior remain byte-deterministic and pass its
    focused/live tests.
11. Adding env-consumer topology bumps the release schema version and preserves an
    explicit, tested v1 read policy; silently treating missing consumers as global in a
    multi-service v1 spec is forbidden.

---

## 12. WO-C8 — Real clean-room bundle lifecycle and persistence proof

**Problems closed:** Docker lane never run, Docker-specific skip gate, and persistence
coverage weaker than the stated campaign outcome.

### Live fixture matrix

| Fixture | Clean build | HTTP | Restart | State write/read | Migration twice |
|---|---:|---:|---:|---:|---:|
| Express/Node | required | required | required | N/A | N/A |
| FastAPI | required | required | required | N/A | N/A |
| Imported Node | required | required | required | N/A | N/A |
| Vite/static | required | required | required | N/A | N/A |
| AppKit | required | required | required | required | required |
| public-build-env Vite | required | required | required | N/A | N/A |

### Acceptance criteria

1. Each bundle is obtained through the real authenticated `/release` then bound
   `/download` flow and extracted into a fresh temporary directory.
2. `docker compose config --quiet` exits `0`, followed by a no-cache build. The test does
   not use host `node_modules`, Python environments, source bind mounts, or prebuilt local
   project images.
3. `docker compose up -d` exits `0`; Compose reports every long-running service healthy
   and every one-shot migration successful.
4. The ingress is reachable only on `127.0.0.1` at a test-assigned host port, returns 200
   at its declared health path, and returns a fixture-specific meaningful body.
5. `docker compose restart` returns the fixture to healthy state and the meaningful body
   remains correct.
6. AppKit creates a unique DB record through its public API, restarts, and reads the exact
   record through its authenticated API. An in-memory substitute cannot pass.
7. AppKit is brought down without `-v`, brought up again, and remains healthy with the
   record present; migration initialization succeeds twice against existing state.
8. A missing required runtime env makes Compose/start fail before healthy; supplying it
   succeeds. No secret value appears in captured process output.
9. Secret sentinel scans cover every zip entry, extracted build context before `.env` is
   supplied, generated file, image history, final image filesystem, application logs, and
   evidence file. Runtime container inspection is sanitized because the secret is
   intentionally present in the container environment.
10. Public build marker appears in the built Vite asset; secret build sentinel is absent
    from layers/history/files, or the bundle was correctly rejected as unsupported.
11. Cleanup removes the unique project containers, networks, test-only images, and volumes
    even on assertion failure. A post-test Docker query proves zero resources with the
    test label remain.
12. CI runs this as a required, non-advisory job with `timeout-minutes`, uploads evidence
    on success/failure, and has no skip/continue-on-error path.

**Supplemental only:** the same config/lifecycle may be run under rootless Podman to
measure portability, but it cannot replace Docker proof for this closeout.

---

## 13. WO-C9 — Final reconciliation, independent rerun, and sign-off

**Goal:** prove the integrated tree, not a sequence of individually green commits.

### Final acceptance procedure

1. Rebase/merge all C1–C8 changes onto one candidate commit.
2. Confirm the candidate diff does not change frozen acceptance paths compared with
   `export-track1-closeout-acceptance-v1`.
3. From the canonical repository root, run
   `.venv/bin/python3 -I -S -P -B -X pycache_prefix=/dev/null
   scripts/verify_export_track1_closeout.py --all --evidence-dir <outside-repo>` from a
   clean checkout on a Docker-capable host. Manifest generation/checking uses the same
   Python flags. The verifier records and rechecks the exact interpreter, fixed system
   Git binary, canonical worktree top level, and linked-worktree-aware absolute Git dir.
4. Confirm every global, non-live, frontend, and live gate passed for the same SHA.
5. Confirm the worktree remains clean after the run.
6. Have a reviewer other than the implementing agent inspect:
   - production diff;
   - frozen-test hash comparison;
   - JUnit/Vitest counts;
   - six downloaded bundle digests and parsed `release.json` bindings;
   - Docker cleanup evidence;
   - secret/injection scan results;
   - UI wording and blocker behavior.
7. The reviewer reruns at least the focused closeout lane and full Docker lane rather than
   accepting uploaded logs alone.
8. Only the reviewer records final status in a separate closeout report containing the
   exact candidate SHA and evidence manifest SHA-256.

This certificate assumes a trusted operator and an uncompromised pre-launch host,
repository metadata, interpreter/dependency tree, and external toolchain. It proves that
the governed lanes executed truthfully at the recorded identities; it is not a security
boundary against a user who controls those prerequisites or can continuously race the
same-UID process. Ordinary Python cache loading/writing is disabled by the required
`-B -X pycache_prefix=/dev/null` launch. Deliberately planted sourceless bytecode or
malicious Git metadata is therefore an operator-trust violation, not an adversarial claim
made by this closeout harness.

### Final pass conditions

The campaign is complete only when all statements below are true simultaneously:

- The frozen acceptance harness is unchanged.
- Every required command exits `0` on one exact clean candidate SHA.
- No closeout test is skipped, xfailed, xpassed, deselected, mocked at the public boundary,
  or made advisory.
- Custom-root declarations are consumed end to end.
- Every source binding names a real immutable version and bound downloads cannot race.
- Candidate UI is explicitly unverified; “Ready” is verification-only.
- Predictably broken runtime contracts fail closed.
- Build/runtime env and supported toolchains work in clean images.
- Command/path/health/resource input cannot inject generated shell/Dockerfile/code.
- No security-critical collision yields a partial or self-host-capable overlay.
- Resource topology matches consumers exactly.
- Every clean-room fixture boots and restarts; AppKit persists and migrates twice.
- Secret sentinels are absent from every prohibited surface.
- The Docker host is clean after the run.
- An independent reviewer, not the implementing agent, signs the evidence SHA.

If any statement is false, the status remains **NOT COMPLETE**. There is no “mostly,”
“functionally equivalent,” “covered elsewhere,” or “deferred” final state.

---

## 14. Execution order

The implementation order is mandatory because later work depends on earlier contracts:

```text
C0 freeze acceptance
  -> C1 host intent injection
  -> C2 immutable source binding
  -> C3 honest readiness/detection
  -> C4 env/build/toolchains
  -> C5 injection + secret boundary
  -> C6 atomic collisions
  -> C7 resource topology
  -> C8 real Docker proof
  -> C9 independent integrated sign-off
```

C3–C5 may be developed in separate temporary branches only after C0 is frozen, but they
must be integrated sequentially and rerun against the entire frozen harness. C1/C2/C6
share the release/download boundary and must not be implemented concurrently in one
worktree.

## 15. Explicit non-goals

- No provider deploy API or cloud target.
- No finish-path execution or verification.
- No claim that arbitrary imported software can be automatically understood.
- No weakening of host ownership, source-download auth, or secret-path filtering.
- No support for a toolchain merely because its executable name can be guessed.
- No `verified` assessment without a separately designed per-project verifier.

Failing closed with a precise repair instruction is a successful Track-1 outcome. Shipping
an attractive but predictably broken “Ready” bundle is not.
