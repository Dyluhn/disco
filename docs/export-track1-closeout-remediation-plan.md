# Export Track-1 Closeout — Consolidated Remediation Plan (canonical)

Status: **NOT COMPLETE.** This is the canonical remediation guide produced after an
independent adversarial audit of the closeout candidate. It supersedes ad-hoc notes.

- **Audited candidate:** `581d1fbe` (the WO-C7 integration tip; C0–C7 all integrated).
- **Acceptance re-freeze lineage:** `acceptance-v1` (lightweight, G01) → **`acceptance-v2`**
  (annotated, commit `9abc43e2`, the R0 production-free re-freeze) → **`acceptance-v3`**
  (annotated, the R0-reopen: adds the G08/G11 URL-binding red + G02 evidence hygiene +
  ruff/format zero-out + this plan). Each vN tag is annotated, reviewer-created, and sits
  on a production-free acceptance-only commit; a new re-freeze never moves an older tag.
- **Owner disposition:** the C8 production fixes are PARKED. R0 (this document's re-freeze
  work) is production-free and authorized; **R1–R8 modify export production code and are
  NOT started** — they begin only when the owner explicitly lifts the production parking.

## Gap ledger (G01–G19)

| ID | Severity | Gap (verified) | Package |
|----|----------|----------------|---------|
| G01 | crit-gov | Acceptance tag was LIGHTWEIGHT, created/moved AFTER C1–C6; never froze an independently-reviewed pre-implementation harness. | R0 |
| G02 | crit-sec | A bare POSITIONAL literal credential passes `release_declare` → persists in the intent → ships in the emitted `Dockerfile` CMD + `release.json` (npm registry-token form; no closeout branch rejects it). | R1 |
| G03 | high | `ReleaseIntent` v2 CLAIMS runtime/install/package-manager/lockfile/output-dir/scoped-env semantics but LACKS those fields. | R2 |
| G04 | high | A typed Express/FastAPI candidate (deps, no build) emits NO runtime dependency-install layer → `Cannot find module 'express'` / missing `uvicorn`. (= C8 finding F1) | R2 |
| G05 | high | A typed `pnpm start` intent is accepted, but the emitted Node image provisions no pnpm. | R2 |
| G06 | med | Static intents cannot carry `output_dir` → both Vite fixtures fail the schema before Docker. (= C8 finding F2) | R2 |
| G07 | high | A filesystem-root persistent path (`/app.db`) is accepted, but Compose mounts the volume at `/data` → the file is not persistent. (= C7 residual O1) | R3 |
| G08 | high | Frontend `downloadProject` IGNORES `version_seq`+`spec_digest` → requests an UNBOUND `/download` URL, bypassing the C2 source-binding. | R4 |
| G09 | high-UI | Firefox cannot reach the candidate / needs-review `SelfHostPanel` in either mode; four browser reachability tests fail. | R4 |
| G10 | gov/UI | A frozen candidate browser test forbade the run command while WO-9 requires the exact command — a harness contradiction. | R0, R4 |
| G11 | med | Frontend `ReleaseResponse` treats source-binding fields as non-null though the backend can return null. | R4 |
| G12 | med | The AppKit heredoc `COPY` needs BuildKit/Buildx, but the bundle declares only Engine + Compose v2. (= C8 finding F3; fix in `local_compose._dev_server_dockerfile`, NOT the protected appkit generator) | R5 |
| G13 | crit-gov | The verifier skipped Playwright, omitted gates, and wrote PLACEHOLDER docker evidence labelled as live-produced. | R6 |
| G14 | crit | The first real frozen live run was 2 pass / 5 fail; the live assumptions had never been exercised on Docker. | R7 |
| G15 | crit-gov | No valid C9 evidence bundle / independent rerun / closeout report / signature. | R8 |
| G16 | gate | Changed-file Ruff `E501` + format drift. | R0/R8 |
| G17 | gate | Full non-integration Python suite: 3 LibreOffice/document failures (a frozen zero-fail gate). | R8 |
| G18 | gate | The anti-bypass scanner did not enforce no-new-suppression; ≥1 `# noqa` existed in closeout TEST code (ratified as the single baseline). | R6 |
| G19 | crit-harness | The frozen §3.3 frontend gate invoked a NONEXISTENT `e2e/export-track1-closeout.spec.ts`; the real specs live under `e2e/export-track1-closeout/`. | R0, R6 |

## Remediation packages (order is mandatory; a later package cannot be green while an earlier one is red)

- **R0 — Restore a legitimate acceptance baseline.** Map every G-ID → frozen node IDs +
  expected red on `581d1fbe`; fix only PROVEN harness errors (G19 ghost path, G10
  candidate-command contradiction; keep the `output_dir` fixtures — a real gap); land an
  ANNOTATED, reviewer-created tag on a production-free acceptance-only commit BEFORE any
  remediation, with `git diff <tag> -- <frozen>` empty. Reviewer ≠ harness author.
  **R0 reopen (acceptance-v3):** add the G08/G11 red at the real download URL-construction
  boundary (fails because binding is omitted, independent of panel reachability); make the
  G02 planted credential absent from console + JUnit evidence (source redaction + a
  verifier-enforced evidence-hygiene gate); zero out Ruff/format on the changed set; commit
  this plan. **Status: DONE (v2) / v3 in progress.**
- **R1 — Command/credential boundary (G02).** Structurally-typed / allowlisted argv;
  credential VALUES must be whole references to declared secret env names, never literals;
  guard at the tool + raw-intent + spec + emission. Not a sentinel/host denylist.
- **R2 — Complete typed intent + dependency/toolchain lowering (G03–G06).** Full typed
  contract; schema-version bump + a v1/v2 read policy; runtime install NOT gated on
  `build_cmd`; pnpm/yarn/bun/poetry/uv pinned + live-tested or rejected; `output_dir` +
  validator into the static multi-stage build.
- **R3 — Persistence truth (G07).** An accepted `persistent_path` equals the emitted
  volume-backed path, or fail closed; prove with live write → restart → down (no `-v`) →
  up → read-back.
- **R4 — Bound UI flow + browser reachability (G08–G11).** The frontend sends
  `version_seq`+`spec_digest` for self-host; all three states reachable in Build + Agent;
  nullable fields aligned + guarded; the six Firefox tests pass with screenshots.
- **R5 — AppKit builder portability (G12).** Emit an entrypoint that works on Engine +
  Compose v2 (no undeclared Buildx) OR expand the declared prerequisite via R0; no
  protected-path change; preflight fails with an actionable diagnostic if the builder
  capability is missing.
- **R6 — Verifier / evidence truthfulness (G13, G18, G19).** Run ALL gates incl.
  Playwright; no placeholder evidence; parse JUnit/Vitest/Playwright JSON structurally;
  enforce no-new-suppression; CI on the exact SHA; upload on pass AND fail.
- **R7 — Re-run the full live matrix (G14).** 7 passed, zero skip/xfail/deselect; every
  fixture real `/release` → bound `/download` → config → no-cache build → healthy →
  loopback body → restart → cleanup; AppKit persist+migrate ×2; secret sentinel sweep;
  zero labelled leftovers.
- **R8 — Global reconcile + independent C9 sign-off (G15–G17).** Fix ruff/format without
  suppressions; resolve the 3 LibreOffice failures on the declared host (provision, don't
  skip); run the corrected verifier + every frozen command from one clean SHA; produce the
  final closeout report; an independent reviewer signs the evidence-manifest SHA-256. Only
  then does NOT COMPLETE become COMPLETE.

## Stop conditions (return to the owner)

Return to the owner if: a protected-path change is needed; a locked semantic must change;
a frozen test is internally contradictory; a schema migration would guess a security /
runtime meaning; a fix would weaken a blocker / secret / binding / live-proof; or the
Docker host is below the declared minimum.
