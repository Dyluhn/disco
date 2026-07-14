# Export Track 1 Closeout — Consolidated Remediation Plan

- **Status:** canonical fix guide. R0 acceptance-baseline repair is IN PROGRESS; R1–R8
  production remediation is NOT STARTED and remains parked.
- **Candidate audited:** `581d1fbed8d804459ef3f0c2d43d290d1605363c` (the WO-C7 integration
  tip; C0–C7 integrated).
- **Acceptance worktree:** `/var/home/dylan/projects/closeout-r0/disclaude` (branch
  `closeout-r0`). The integration worktree `/var/home/dylan/projects/closeout/disclaude`
  holds the reference copy of this plan; do not implement remediation in
  `/var/home/dylan/projects/export-track1/disclaude` (that worktree stays at the historical
  `2ec1ceba` baseline). Reconfirm the canonical worktree and SHA before each package.

This document is the single ordered guide for fixing every independently verified gap in
the Export Track 1 closeout implementation. It supplements, but does not weaken,
`docs/export-track1-closeout-work-orders.md`. If the two disagree, the frozen locked
semantics and the stricter acceptance condition win unless the owner approves a documented
semantic change and an independent reviewer re-freezes the affected acceptance files.

The campaign is **NOT COMPLETE**. A parked finding is documented work, not proof that the
original closeout criterion passed. Machine and subagent verification is not human
ratification: no acceptance tag in this campaign has yet received an independent human
signature or protected authoritative-remote publication (see §3.1).

## 1. Non-bypass completion rule

A work package below is complete only when all of these are true:

1. A regression test that fails for the verified reason exists before the production fix.
2. The test exercises the real public boundary named by the work package. A unit test,
   source grep, mock, snapshot, or emitter call may supplement but never replace it.
3. Positive, negative, and adversarial cases pass. Merely making the reported example pass
   does not close the bug class.
4. No closeout test is skipped, xfailed, deselected, weakened, deleted, or made advisory.
5. No `# noqa`, type-ignore, lint exclusion, broad exception, fixture-name branch, test-only
   endpoint, sentinel-specific rule, or environment-controlled early return is introduced
   to obtain a pass.
6. Every required command exits zero from one clean checkout of one exact candidate SHA.
   Passing results assembled from different commits do not count.
7. The run leaves the checkout clean and the Docker host free of campaign containers,
   networks, volumes, and test-only images.
8. Machine-readable evidence records command, exit code, test counts, candidate SHA,
   artifact digests, Docker identities, and cleanup results. Narrative claims are not a
   substitute.
9. A reviewer other than the implementing agent inspects the semantic diff and reruns the
   focused and live lanes. The implementing agent cannot self-sign completion. For an
   acceptance tag, that reviewer must be an independent human (§3.1); subagent review is a
   pre-check, not the ratification.
10. Any frozen-harness change follows R0 below. Editing an expectation to match production
    without showing that the prior expectation contradicted the locked contract is failure.
    A frozen proving red must be able to turn green through a legitimate production fix
    ALONE — if it can only pass by editing the frozen test, it is an escape hatch and fails
    this rule.

## 2. Verified gap ledger

| ID | Severity | Verified gap | Required work package |
|---|---|---|---|
| G01 | Critical governance | The original acceptance tag was lightweight, was created after C1–C6 work, and did not freeze an independently reviewed pre-implementation harness. Frozen files changed during implementation. | R0 |
| G02 | Critical security | A literal positional credential can pass `release_declare`, persist in the intent, and ship in `Dockerfile` and `release.json`. The reproduced npm registry-token form used no closeout-specific branch. | R1 |
| G03 | High | `ReleaseIntent` schema v2 claims runtime, install, package-manager, lockfile, output-dir, and scoped env support but does not contain those fields. The tool surface cannot declare the required C4 contract. | R2 |
| G04 | High | Typed Express and FastAPI intents with dependencies and no build step emit no runtime dependency installation. Express exits with `Cannot find module 'express'`; FastAPI cannot execute `uvicorn`. | R2 |
| G05 | High | A typed `pnpm start` contract can be accepted although the emitted Node image does not contain `pnpm`. | R2 |
| G06 | Medium | Static typed intents cannot carry `output_dir`; both frozen Vite fixtures fail schema validation before Docker. | R2 |
| G07 | High | A root-level persistent path such as `/app.db` is accepted while Compose mounts the named volume at `/data`, so the declared file is not persistent. | R3 |
| G08 | High | The frontend self-host download ignores `version_seq` and `spec_digest` and calls the unbound download URL, bypassing the immutable binding implemented by C2. | R4 |
| G09 | High UI | Firefox cannot reach candidate or needs-review SelfHostPanel states in either mounting mode. Four browser tests fail. | R4 |
| G10 | Governance/UI | The frozen candidate browser test forbids a run command, while historical WO-9 requires the exact command and the closeout locked semantics allow a complete candidate bundle. This is a harness-contract contradiction, not automatically a production-copy bug. | R0, R4 |
| G11 | Medium | Frontend `ReleaseResponse` types `version_seq` and `tree_digest` as non-null (and only `spec_digest` as nullable), although the backend schema permits null `version_seq`, `tree_digest`, and `spec_digest` for non-snapshotted outcomes. | R4 |
| G12 | Medium / C8 blocker | AppKit export uses Dockerfile heredoc `COPY`, which requires BuildKit/Buildx, but its documentation and the live guard declare only Docker Engine plus Compose v2. On the accepted rootless host, ordinary context copy and npm build succeed, then heredoc `COPY` fails. | R5 |
| G13 | Critical governance | The verifier skips Playwright, omits campaign-wide gates, writes placeholder Docker evidence, and labels it as live-lane-produced without proving a successful lifecycle. The workflow delegates its verdict to this incomplete verifier. | R6 |
| G14 | High / C8 blocker | The first real frozen live-lane execution produced 2 passed / 5 failed. Before that run, its schema and runtime assumptions had never been exercised on Docker. | R7 |
| G15 | Critical governance | No valid C9 evidence bundle, independent rerun, closeout report, evidence digest, or reviewer signature exists. | R8 |
| G16 | Gate blocker | Changed-file Ruff has an E501 failure and formatting drift across eleven files. | R8 |
| G17 | Gate blocker | Full non-integration Python has three failures in LibreOffice/document verification. “Unrelated” does not satisfy the frozen zero-failure global gate. | R8 |
| G18 | Gate blocker | The anti-bypass scanner reports zero violations but does not enforce the no-new-suppression rule; at least one newly added `# noqa` exists in closeout test code. | R6 |
| G19 | Critical harness | The frozen frontend gate invokes nonexistent `e2e/export-track1-closeout.spec.ts`; the actual Firefox specs are under `e2e/export-track1-closeout/`. A verifier can therefore omit or misaddress the required browser proof. | R0, R6 |

The ledger is additive. Fixing C8 findings does not close G02, G07, G08, or the governance
failures, and green focused unit tests do not override a red public or global lane.

### Current code and evidence anchors

These symbols are starting points, not permission to patch only the named line:

| Gaps | Primary anchors |
|---|---|
| G02 | `release/command_grammar.py::check_declaration_argv`; `release/detect.py::_command_grammar_blocker`; `tools/builtin/release_declare.py` |
| G03–G06 | `release/spec.py::ReleaseIntent`; `release/detect.py::_from_intent`; `release/local_compose.py::_effective_install_cmd`; `tools/builtin/release_declare.py` |
| G05 | `release/detect.py::_SUPPORTED_NODE_HEADS` and the emitted Node base-image contract |
| G07 | `release/spec.py::local_mount_target`; resource validators; `release/local_compose.py` volume lowering |
| G08, G11 | `frontend/src/api/projects.ts::downloadProject`; `frontend/src/types/release.ts`; Build/Agent SelfHostPanel callers |
| G09, G10 | `frontend/e2e/export-track1-closeout/selfhost-{candidate,needs-review}.spec.ts`; offline fixture routing; `SelfHostPanel.tsx` |
| G12 | `release/local_compose.py::_dev_server_dockerfile` and `_dev_server_selfhost_doc` |
| G13, G18 | `scripts/verify_export_track1_closeout.py`; `.github/workflows/export-track1-closeout.yml` |
| G14 | `tests/integration/test_export_track1_closeout_live.py`; `_closeout_live_support.py` |
| G19 | Closeout work-order §3.3, verifier frontend command inventory, and actual `frontend/e2e/export-track1-closeout/` files |

## 3. R0 — Restore a legitimate acceptance baseline

**State:** prerequisite; documentation and test-design work may proceed while C8/R1–R8 are
parked, but no production remediation begins until this package is approved by an
independent human. **R0 remains INCOMPLETE until an acceptance-v4 tag receives independent
human review, an annotated signature, and publication to the protected authoritative
remote (§3.1).**

**Closes:** G01 and the harness portions of G10 and G19.

### 3.1 Acceptance tag lineage and ratification status (accurate as of this revision)

| Tag | Commit | Object type | Verification to date | Human signature | Protected remote |
|---|---|---|---|---|---|
| `export-track1-closeout-acceptance-v1` | `07a0444f` | **lightweight** (the G01 defect) | none valid | no | no |
| `export-track1-closeout-acceptance-v2` | `9abc43e2` | annotated | machine + subagent only | no | no |
| `export-track1-closeout-acceptance-v3` | `4d55204c` | annotated | machine + subagent only | no | no |
| `export-track1-closeout-acceptance-v4` | *(candidate; unbuilt tag)* | annotated (proposed) | machine + subagent pre-check | **pending** | **pending** |

Accurate facts, stated plainly to prevent false completion claims:

- v1 was a **lightweight** tag, not annotated and not reviewer-created; it is the G01 root
  cause and is retained only as historical evidence.
- v2 and v3 are annotated, reviewer-created (by an independent verifier subagent whose role
  is a pre-check), and sit on production-free acceptance-only commits. They are **unsigned,
  local-only, and not protected**: no independent human has ratified them and they have not
  been published to a protected authoritative remote.
- v3 is **frozen, not "in progress."** This revision reopens R0 to produce an acceptance-v4
  candidate correcting three residual harness weaknesses (§3.4); it does not reopen v3.
- The acceptance-v4 **candidate** is prepared for independent human review only. This agent
  does not create, sign, or push the v4 tag. The human reviewer must independently inspect
  the semantic diff, rerun the gates, create a signed annotated `…-v4` tag, and publish it
  to the protected authoritative remote before R0 is complete.
- No statement in this campaign asserts that human ratification, a protected-ref, campaign
  completion, or authorization to begin R1 has occurred. It has not.

### 3.2 Required work

1. Build an acceptance change record mapping every G-ID to frozen node IDs and the exact
   failure it must expose on `581d1fbe`.
2. Correct only proven harness errors, including the candidate-command contradiction (G10).
   Preserve the `ReleaseIntent.output_dir` live fixtures (a real schema gap, not a mistaken
   test assumption) and every other legitimate red test.
3. Correct the frozen Firefox command/path inventory to select every file under the actual
   `frontend/e2e/export-track1-closeout/` directory and record exact collection output.
4. Decide candidate UI semantics explicitly. Default reconciliation: `candidate` remains
   “Bundle available” and “Not runtime-verified,” never “Ready,” while an internally
   complete bundle may show the exact local run command. Any different owner decision must
   update locked semantics, historical reconciliation, and browser assertions together.
5. Add public-boundary regressions for gaps the first harness missed: positional
   credentials, root-file persistence, frontend bound-download parameters, typed runtime
   installs, Buildx absence, verifier evidence truthfulness, and UI state reachability.
6. Generate a fresh manifest and prepare an acceptance-only commit that precedes all
   remediation commits. An **independent human** then creates an annotated reviewer-created
   tag on that commit and protects it in the authoritative remote.

### 3.3 Strict acceptance criteria

1. The acceptance-only commit changes no production behavior. Concretely, this must return
   zero paths on the acceptance branch:
   `git diff --name-only 581d1fbe..HEAD -- 'packages/*/src/**' 'frontend/src/**' ':!frontend/src/test/**'`.
2. Every G02–G19 gap has at least one named failing test or verifier assertion for the
   verified reason; syntax/import/setup failures do not count.
3. A machine-readable inventory records exact node IDs, expected failure codes/diagnostics,
   file hashes, acceptance commit, and reviewer identity. It contains no claim that human
   ratification already happened.
4. `git diff <acceptance-tag> -- <all-frozen-paths>` is empty before the first production
   remediation commit.
5. The reviewer is not the acceptance-harness author and records semantic review of every
   corrected frozen assertion. The final ratifying reviewer is an independent human.
6. A lightweight tag, a tag placed after production fixes, or another self-review is an
   automatic failure.
7. Every frozen proving red is **self-discriminating**: it turns green through a legitimate
   production fix alone and never requires editing the frozen test (non-bypass rule 10).

### 3.4 Acceptance-v4 corrections (this revision)

1. **G08 made non-rewriteable.** The bound-download red must SUPPLY the binding to the real
   download boundary through the predeclared future contract
   `downloadProject(conversationId, { version_seq, spec_digest })`, invoked against the
   current one-argument implementation via a test-side compatibility type. Today the extra
   runtime argument is ignored and the URL stays unbound (RED); at R4 the production
   signature adopts the binding and the exact supplied `version_seq`/`spec_digest` ride the
   URL (GREEN) with no test edit. It uses at least two materially different bound fixtures,
   asserts exact URL-encoded values (not mere presence), preserves the unbound null-binding
   negative case, reaches the real URL-construction and fetch boundary, renders no
   component, and emits no jsdom navigation error. All “may be updated at R4” language is
   removed.
2. **G11 given a real compiler proving red.** A dedicated TypeScript compile lane
   (`tsc -p frontend/tsconfig.closeout-g11.json --noEmit`; a dedicated config is required
   because `tsconfig.build.json` excludes tests and Vitest transpilation does not
   type-check) checks a frozen contract asserting a non-snapshotted `/release` may carry
   null `spec_digest`, `version_seq`, and `tree_digest`. It fails today only because the
   frontend `ReleaseResponse` rejects null `version_seq`/`tree_digest`, and turns green
   through the R4 production type correction without editing the frozen contract. The
   manifest records the exact command and expected diagnostics. Runtime guards are added for
   any consumer of nullable binding fields that a compile-only check cannot prove safe.
3. **Evidence-hygiene made regression-proof.** Committed mutation tests prove that planting
   the registered non-secret marker in each supported text-evidence artifact class makes the
   hygiene scan fail; that violations report only artifact name, line number, and sentinel
   label (never surrounding text or the credential value); that a hygiene violation forces
   the final verifier verdict to `passed: false`; that clean evidence is accepted; and that
   the real G02 proving-red console and JUnit outputs contain zero occurrences of both the
   marker and the full planted sentinel. The regression test never writes the full credential
   into its own output. The word “regression-proof” applies only because these committed
   mutation tests exist and pass.

## 4. R1 — Close the command and credential boundary

**Closes:** G02.

### Required design

Replace the assumption that every non-flag positional operand is harmless. Accepted command
shapes must be structurally typed or narrowly allowlisted by executable and operand role.
Credential-capable values must be whole references to declared secret environment names;
they may never be literal intent data. The same validator must protect tool declarations,
raw persisted intents, spec validation, and final emission.

Do not solve this with a list of known sentinel values, registry hosts, or fixture commands.

### Strict acceptance criteria

1. The reproduced `npm config set <registry-auth-key> <literal>` declaration is rejected
   before sidecar persistence with a name-only, value-redacted typed error.
2. A table covers credential values in leading, middle, and trailing positional positions;
   `--flag value`; `--flag=value`; URL userinfo; npm/pip package-manager config forms;
   inline env assignments; newlines; shell metacharacters; `$()`; and backticks.
3. The same corpus is exercised through real `release_declare`, a directly written intent
   sidecar, `/release`, and bound `/download`.
4. For every rejected case the planted value is absent from sidecar bytes, API JSON, zip
   entries, `release.json`, Dockerfiles, Compose, logs, image history, and image filesystems.
5. Benign supported argv forms retain exact argv semantics; no shell re-parsing is added.
6. Mutation tests that remove each new validator or bypass one entry path make at least one
   acceptance test fail.

## 5. R2 — Complete typed intent and dependency/toolchain lowering

**Closes:** G03, G04, G05, G06 and the FastAPI/Express/Vite C8 failures.

### Required design

1. Introduce the complete typed declaration contract: runtime, install/build/start argv,
   package manager, lockfile, output directory, scoped env declarations with requiredness
   and secret class, port/health, and resources.
2. Because persisted shape changes are occurring, bump the intent schema version. Do not
   silently add fields to the existing v2 interpretation. Supply deterministic v1/v2 read
   policy or return the exact upgrade blocker.
3. Wire every field through tool schema, persistence, parse/serialize, `_from_intent`,
   `ReleaseService`, validation, response, bundle, and UI.
4. Runtime dependency installation must not depend on the presence of a build command.
   Explicit typed install metadata wins. Safe tree derivation is allowed only where the
   package manager and manifest are unambiguous; otherwise fail closed.
5. `pnpm`, Yarn, Bun, Poetry, and `uv` must either be pinned/provisioned in the emitted image
   and live-tested or rejected before a candidate verdict. Mapping them to npm/pip is not
   acceptable.
6. Add `output_dir` with the existing workspace-relative path validator and lower it into
   the static multi-stage copy. Dynamic/unknown output directories remain review blockers.

### Strict acceptance criteria

1. Canonical new-schema serialize/parse/serialize bytes are identical; old supported
   sidecars migrate deterministically and newer unknown versions fail closed.
2. Express with `express` and no build step emits a real install, boots, serves its body,
   and restarts in a clean no-cache Docker build.
3. FastAPI with `requirements.txt` and no build step installs both FastAPI and `uvicorn`,
   boots, serves its body, and restarts.
4. Imported Node continues to derive and execute `npm ci`; the fix cannot special-case the
   two named fixtures.
5. Vite intent with `output_dir=dist` validates, builds, serves the built asset, and proves
   public build env reaches the asset while secret build env is rejected or securely
   mounted without leakage.
6. An unsafe/traversing/newline output directory fails before emission.
7. `command -v` inside final images proves every accepted executable exists. Unsupported
   toolchains return `needs_review/toolchain_unsupported`, never a broken candidate.
8. Golden-byte changes receive semantic review and R0-compliant re-freeze; blindly updating
   snapshots is failure.

## 6. R3 — Make persistence topology truthful

**Closes:** G07.

### Required design

Every accepted persistent path must correspond exactly to the path backed by the emitted
volume. For a file path, either mount the file safely with initialized storage semantics or
reject file-root layouts that cannot be represented portably. Rewriting `/app.db` to a
different `/data/...` location without updating the canonical contract is forbidden.

### Strict acceptance criteria

1. A real bundle declaring `/app.db` either fails closed with a typed repair instruction or
   writes to volume-backed `/app.db`; it may not return `self_host:true` while mounting only
   `/data`.
2. A live test writes a unique record, restarts, performs `down` without `-v`, recreates the
   containers, and reads the exact record back.
3. Parsed Compose mounts, runtime URL, `release.json`, and service environment all identify
   the same normalized persistent location.
4. Directory resources, multiple consumers, duplicate mount targets, and migration services
   retain C7 isolation invariants.
5. Mutation tests that restore the `/data` fallback make the persistence proof fail.

## 7. R4 — Bind the real UI flow and make every state reachable

**Closes:** G08, G09, G10, G11.

### Required design

1. The frontend download API must accept and send the release response's `version_seq` and
   `spec_digest` for self-host bundles. Plain-source downloads remain explicitly unbound.
2. Build and Agent surfaces must expose candidate, needs-review, and not-web states through
   real reachable application state, not a component-only fixture.
3. Resolve candidate command semantics in R0. Do not relabel a candidate as verified and do
   not hide uncertainty.
4. Align frontend nullable fields exactly with backend response types (`version_seq`,
   `tree_digest`, `spec_digest` all nullable for non-snapshotted outcomes) and force callers
   to handle absent bindings.

### Strict acceptance criteria

1. Browser-observed candidate download requests contain the exact `version_seq` and
   `spec_digest` returned by `/release`; the downloaded `release.json` matches both. The
   frozen G08 URL-boundary contract turns green through this production change alone.
2. Mutating the live workspace after release assessment does not change bound zip bytes.
   A nonexistent/mismatched sequence or digest returns the typed failure and no zip body.
3. Firefox reaches all three states in both Build and Agent surfaces without test-only
   production branches. Candidate and needs-review panels are visible without interaction.
4. Candidate shows exact locked uncertainty copy and never “Ready.” Needs-review renders
   every blocker and no run command. Not-web renders its honest reason and source download.
5. The candidate command action set matches the adjudicated frozen contract in both modes.
6. The frozen G11 compile lane turns green through the production type correction alone, and
   typecheck fails if nullable binding values are passed to a bound download without an
   explicit guard.
7. All six frozen Firefox tests pass and their screenshots are uploaded as evidence; the
   verifier checks the Playwright JSON result rather than trusting process text.

## 8. R5 — Remove or declare AppKit's hidden builder dependency

**Closes:** G12. **PARKED under the 2026-07-13 C8 disposition.**

### Required design when resumed

The failing source is `release/local_compose.py`, not the protected AppKit generator or
`appkit_cloudflare` paths. The normal `COPY ./ ./` already succeeds. The incompatible
instruction is the Dockerfile heredoc used to create the entrypoint.

The default resolution under the current contract is to emit an entrypoint in a form that
works with Docker Engine plus Compose v2 without an undeclared Buildx plugin. If the owner
instead expands the supported-host prerequisite to BuildKit/Buildx, the live guard,
workflow runner labels/setup, `SELFHOST.md`, evidence manifest, and acceptance contract must
all change together through R0.

### Strict acceptance criteria

1. The AppKit bundle builds with `--no-cache` on the declared minimum host from a fresh
   extracted bound download.
2. No production change touches protected AppKit generator or `appkit_cloudflare` paths.
3. The entrypoint remains secret-free in bundle/image layers and writes runtime secrets only
   inside the starting container.
4. AppKit boots, becomes healthy, writes a unique record, restarts, reads the exact record,
   goes down without volume deletion, comes up again, and migrates idempotently twice.
5. A host missing any newly declared mandatory builder capability fails the preflight with
   an exact actionable diagnostic before fixture execution; it cannot pass the availability
   guard and fail later with an opaque `COPY` error.

## 9. R6 — Make the verifier and CI evidence truthful

**Closes:** G13, G18 and the execution portion of G19.

### Required work

1. The verifier must run every campaign-wide static, Python, frontend, Firefox, and live
   gate required by the frozen contract, including the dedicated G11 compile lane.
2. Remove generic Docker placeholder artifacts. Evidence files must be produced from the
   actual lifecycle or be explicitly marked absent/failed; they may never carry a success-
   sounding status without successful underlying commands.
3. Parse JUnit, Vitest/Playwright JSON, and Docker evidence structurally and reject failed,
   skipped, xfailed, xpassed, or deselected tests.
4. Enforce the no-new-suppression rule across the campaign diff, with an independently
   reviewed baseline for any pre-existing suppression. Retain and extend the evidence-hygiene
   gate and its committed mutation tests (§3.4.3).
5. CI must invoke the corrected verifier on the exact checked-out SHA and upload evidence on
   success and failure.

### Strict acceptance criteria

1. Deliberately failing one static gate, one Firefox test, and one live test each makes the
   verifier exit nonzero in independent mutation checks.
2. Deleting a required evidence file, changing its candidate SHA, or replacing it with the
   current placeholder text makes verification fail.
3. The command inventory in the evidence manifest exactly equals the frozen required
   inventory; omitted and additional substitute commands are rejected.
4. A new `# noqa`, type-ignore, lint exclusion, skip, xfail, or `continue-on-error` in the
   campaign diff fails the anti-bypass gate unless separately owner-approved and recorded.
5. Workflow status cannot be green if any required verifier lane is absent or red.
6. A planted credential marker in any scanned evidence artifact forces `passed: false`
   (evidence-hygiene mutation criterion, §3.4.3).

## 10. R7 — Re-run the complete live matrix

**Closes:** G14. **PARKED until R1–R6 are implemented and the owner resumes C8 fixes.**

### Strict acceptance criteria

1. All seven live-lane tests pass: availability plus six fixture lifecycles. Required
   result is `7 passed`, with zero failures, errors, skips, xfails, xpasses, or deselections.
2. Express, FastAPI, Imported Node, Vite/static, AppKit, and public-build-env Vite each come
   from real authenticated `/release` and bound `/download` bytes.
3. Every fixture performs config validation, a no-cache build, healthy startup, meaningful
   loopback HTTP response, restart, and cleanup. AppKit also satisfies R5 persistence and
   migration criteria.
4. Runtime/build secret sentinels are absent from every prohibited surface; sanitized
   inspection proves a runtime secret exists only where intentionally injected.
5. Per-fixture finalizers execute on failure, and a post-lane query proves zero labelled
   containers, networks, volumes, or test-only images.
6. Disk-use evidence records before/after state and shows no campaign leak.

## 11. R8 — Global reconciliation and independent C9 sign-off

**Closes:** G15, G16, G17 and the campaign.

### Required work

1. Fix changed-file lint and formatting without broad suppressions.
2. Reproduce and resolve the three full-suite LibreOffice/document failures on the declared
   acceptance host. If a host dependency is required, provision and verify it; do not skip or
   call the failures unrelated.
3. Run the corrected verifier and every explicit frozen command from a clean checkout of one
   integrated SHA.
4. Produce the final closeout report and obtain an independent human review/signature.

### Strict acceptance criteria

1. Ruff check and format check pass on the exact campaign changed-file set.
2. Basedpyright, import boundaries, architecture budget, tool schema, and architecture
   diagram freshness all pass.
3. Focused closeout Python, full non-live Python, focused frontend, full Vitest, typecheck,
   the G11 compile lane, production build, changed-file ESLint, all Firefox E2E, and R7 live
   Docker all exit zero (proving reds having been turned green by their production fixes).
4. The worktree is clean before and after verification.
5. The evidence directory is outside the checkout and contains real response/zip digests,
   six bundle digests, parsed bindings, image IDs, sanitized inspect data, Compose status,
   cleanup proof, tool versions, and command/test counts for the exact candidate SHA.
6. An independent human reviewer reruns the focused and full live lanes, reviews production
   and frozen-test diffs, and signs the evidence-manifest SHA-256 in a separate closeout
   report.
7. Only after all conditions pass may status change from **NOT COMPLETE** to **COMPLETE**.

## 12. Mandatory direct command inventory

The corrected verifier must run these commands, but the final human reviewer also runs them
directly. A single verifier summary does not replace their individual exit codes and
machine-readable reports.

From repository root:

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
uv run pytest packages/core/tests packages/tools/tests packages/agent-server/tests \
  -o addopts='' -m "not integration" --junitxml=/tmp/export-track1-nonlive.xml
uv run pytest packages/core/tests/export_track1_closeout \
  packages/tools/tests/export_track1_closeout \
  packages/agent-server/tests/export_track1_closeout \
  -o addopts='' -m "export_track1_closeout and not integration" \
  --junitxml=/tmp/export-track1-closeout.xml
```

R0 acceptance-baseline additional check (must return zero paths — production-free proof):

```bash
git diff --name-only 581d1fbe..HEAD -- \
  'packages/*/src/**' 'frontend/src/**' ':!frontend/src/test/**'
```

From `frontend/`:

```bash
npm ci
npm run typecheck:build
npx tsc -p tsconfig.closeout-g11.json --noEmit   # G11 proving red: nonzero at R0, zero at R4
npx vitest run src/test/export-track1-closeout \
  --reporter=json --outputFile=/tmp/export-track1-vitest.json
npm run test
npx vite build
npx playwright test e2e/export-track1-closeout --project=firefox --retries=0 \
  --reporter=json
```

Back at repository root on the declared Docker host:

```bash
docker version
docker compose version
uv run pytest \
  packages/agent-server/tests/integration/test_export_track1_closeout_live.py \
  -o addopts='' -m "export_track1_closeout and integration" -ra \
  --junitxml=/tmp/export-track1-live.xml
uv run python scripts/verify_export_track1_closeout.py \
  --all --evidence-dir /absolute/path/outside/checkout
test -z "$(git status --porcelain)"
```

R0 must freeze the exact corrected browser invocation and collected test inventory, and the
exact G11 compile command and expected diagnostics. If the repository's Playwright CLI
requires an explicit JSON output file, that path must be fixed in the frozen command and
parsed by R6; relying on console output is insufficient.

## 13. Execution order and stop conditions

```text
R0 legitimate acceptance freeze (v4 candidate → independent human sign + protect)
  -> R1 command/credential boundary
  -> R2 intent + dependency/toolchain lowering
  -> R3 persistence truth
  -> R4 immutable UI flow + browser reachability
  -> R5 AppKit builder portability
  -> R6 verifier/evidence truthfulness
  -> R7 full live Docker matrix
  -> R8 global reconciliation + independent C9 sign-off
```

R1–R6 may be split into separately reviewed commits, but a later package cannot be declared
complete while an earlier dependency is red. R7 must run on the integrated result, not on
isolated work-package tips. R8 must use the same SHA as R7.

Stop and return to owner/reviewer adjudication if:

- a required fix needs a protected-path change;
- a locked product semantic must change;
- a frozen test is internally contradictory or cannot fail for the intended reason;
- a schema migration would guess missing security/runtime meaning;
- a proposed fix weakens a blocker, secret boundary, immutable binding, or live proof;
- the Docker host no longer matches the declared minimum prerequisites.

## 14. Current authorized next action

The authorized state is acceptance-harness repair only: prepare the acceptance-v4 candidate
(the §3.4 corrections), keep the change production-free, and stop for independent human
review. Do not alter export production code and do not begin R1–R8. Do not create, sign, or
push the acceptance-v4 tag. The human reviewer independently inspects the semantic diff,
reruns the gates in §12, creates a signed annotated `export-track1-closeout-acceptance-v4`
tag, and publishes it to the protected authoritative remote. Only then is R0 complete, and
only then may lifting the R1–R6 production parking boundary be considered.
