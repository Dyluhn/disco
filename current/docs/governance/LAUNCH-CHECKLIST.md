# Launch Checklist — release readiness

**Status: MUTABLE.** Updated 2026-09-09. The Deep Research closeout below is current. The remaining August 19 inventory is historical and has not been recertified by this work.

## Deep Research

- Engineering: deliberate source selection, discovery/evidence separation, draft-specific repair validation, retained-source inspection, compressed-response decoding, corrupt-text rejection, revised-brief handoff, local evidence context, review-signal disposition, turn accounting, recovery, authenticated deletion, native controls, and packaged offline NLI have regression and live-boundary evidence. Exact source versions and results are in the external closeout record.
- Provider compatibility: representative DeepSeek, GLM, and Qwen profiles use the common loop. No provider account or individual model is a project release prerequisite. Native search/extraction and local grounding have real execution evidence.
- Quality is measured by rate, not pass/fail, and measurement remains open: automated structure checks are separate from source-backed semantic review. Failed reports and infrastructure incidents remain in the attempt ledger. The broader 36-attempt campaign has not been completed.
- Governance approval is granted and applied: the reconciliation restored historical receipts, disclosed the exact retirements, and validated the inventories, API transitions and seal. The explicit owner instruction README.md requires was given in writing on 2026-09-08 — *"after that, you are authorized to merge."* — and the protected seal was rebaselined on it. The two sealed standards are unchanged.
- Landing: V48 is landed on main from candidate 71535d280fa0a364601b31353bbca7fd7d97aaeb, and V49 (PKG-36-PORTABILITY-V49), V50 (PKG-37-UI-FIXES-V50) and V51 (PKG-38-UI-FIXES-V51) have landed on top of it on the same written authorization. V51 also lands the rename authority, so a renamed test no longer has to be laundered through a single-use retirement to regenerate the inventory. The incoming main source is preserved on the branch main-dirty-snapshot-2026-09-08 and in /var/home/dylan/Archives/recovered/disco-main-dirty-2026-09-08; the canonical working tree was never reset. Existing-history database compatibility and a disposable forward/inverse patch rehearsal pass. No public release or deployment has occurred.

Current evidence: /var/home/dylan/AI-Work/disco-gap-closure-2026-09-05/CLOSEOUT-RECORD.json. See [Current State](CURRENT-STATE.md) for implementation limits. The dated items below describe their original evidence, not the current Deep Research status.

## August 19 launch inventory — DONE

- **Assist control hidden, prop retained.** Rendering removed on Build and
  Agent; the `assist` prop stays in the signature for API stability,
  deliberately unused.
  → `current/frontend/src/components/build/AgentStatusBar.tsx` (+
  `AgentStatusBar.assist.test.tsx` asserting absence under both framings).

- **MCP usable out of the box + diagnostics.** Stdio configs take an explicit
  `args` list or shlex-split a pasted command (stdio only, never a URL, argv
  exec, approval gates untouched); stdio failures report
  `{code, attempts, exception_type}` via `server_diagnostics()`.
  → `current/packages/app-server/src/disco/app_server/config/dtos.py`,
  `current/packages/tools/src/disco/tools/mcp/pool.py`; tests
  `test_mcp_pool.py`, `test_mcp_status_routes.py`.

- **Skill uploads.** Skills are real persistent `.md` instruction modules with
  full create/update/delete over the API and a Settings surface.
  → `current/packages/app-server/src/disco/app_server/routes/skills.py`,
  `current/frontend/src/components/settings/SkillsSection.tsx`; tests
  `test_skills.py`, `test_skill_mount.py`.

- **Provider probes hit real endpoints.** Brave (`api.search.brave.com`,
  `X-Subscription-Token`), Tavily (`api.tavily.com/search`), Firecrawl
  (`api.firecrawl.dev`, Bearer) each probe their real endpoint in their real
  auth shape; 401/403 reports as unauthorized.
  → `current/packages/retrieval/src/disco/retrieval/bundled_providers.py`.

- **Quickstart fixes.** `cd disclaude` is gone (`cd disco` in README and
  self-host); both container sockets documented rootless-first, with the
  root-equivalence of `/var/run/docker.sock` called out.
  → `README.md` (Quickstart + socket notes), `current/docs/self-host.md`.

- **Ubuntu + Docker README-verbatim install verified.** Real install on clean
  Ubuntu 24.04 with rootless Docker: 4m35s to three healthy services, both
  image-size columns measured, benign-looking failures documented.
  → PKG-22 (`140d211b`); measurement note in `current/docs/self-host.md`.

- **disco-verify secret resolution.** The documented verify command no longer
  fails on a working install; the CLI resolves the app secret the way the
  servers do and never mints a rival key.
  → PKG-25 (`ffc1b6b2`); four tests in
  `current/packages/core/tests/test_secrets.py`
  (`test_entrypoint_secret_is_adopted_rather_than_a_second_key_minted` is the
  one that matters).

- **Builder security seams (F41 Stripe / F33 webhook) merged and live.** The
  fills merged as `8f356c1e` / `ce349096`; the primitives register real
  deterministic verifiers plus mandatory live exploit runners, with host
  services, live verifiers and config routes across core/agent-server/
  app-server. All 14 seam test suites pass at `main` (185 passed, 1 skipped;
  verified 2026-08-19). The archived seam branches are ancestors of `main` —
  provenance, not pending work.
  → `current/packages/core/src/disco/core/appkit/stripe_primitive.py`,
  `webhook_primitive.py`; state of record
  `current/sec-work-remaining/README.md`.

- **Dead `.env.example` knobs removed (owner decision: remove, don't build).**
  `AZURE_OPENAI_API_KEY` and `COMFYUI_API_KEY` had no reader anywhere in the
  tree — neither appears in the `_LEGACY_IMPORTS` env-import table
  (`secret_refs.py`) and provider secrets resolve from SecretStore only, never
  `os.environ`. ComfyUI image-gen itself stays: it is a live, deliberately
  keyless backend (`_ComfyUIBackend` takes only a base URL). Removed from
  `.env.example`, both `compose.yaml` service blocks, and the compose
  environment test.
  → `.env.example`, `compose.yaml`,
  `current/packages/agent-server/tests/test_self_host_compose_environment.py`.

## PARTIAL

- **Mobile tap-target sweep.** First-run paths are done on shared primitives
  (`tapTarget`, `ScrollFade`); roughly **25 secondary Settings forms and some
  occasional Build panels** still carry sub-44px controls — a mechanical sweep
  with the existing helpers. A parallel branch may be closing this; as of
  `main` the gap stands.
  → `current/frontend/src/lib/tapTarget.ts`; remaining scope recorded in
  PKG-24 (`05b2484c`).

## OPEN — needs the owner

- **gVisor has never run on real hardware.** The engine now refuses a silent
  runtime downgrade (PKG-24) and the docs point at rootful Docker, but no
  environment here has exercised real runsc: it is not installed on the
  workstation and no rootful Docker daemon exists (checked 2026-08-19).
  Decision pending: provision a rootful-Docker host or drop the claim.
  → `current/docs/self-host.md` §Sandbox Image;
  `current/packages/tools/tests/test_gvisor.py` is unit-level only.

- **Chrome MCP live connection never verified.** No live-connection evidence
  exists in the tree; MCP correctness is proven against test servers only.

- **Live API keys never exercised.** Every provider/probe test runs against
  `httpx.MockTransport`. **Firecrawl additionally has zero contract
  coverage** — its provider accepts no injectable transport (constructor takes
  only `api_key`/`base_url`), so only the no-key safety path is tested.
  → `current/packages/retrieval/tests/test_bundled_providers.py`,
  `bundled_providers.py` (`FirecrawlExtractionProvider.__init__`).

- **References/citations output usability never addressed.** The citation
  plumbing exists (grounded types, passage chunking); how the output reads to
  a user has had no pass.

- **Independent second-model half of the final security assurance pass not
  run.** The campaign waves are done; the independent-model half remains
  outstanding.
  → `current/sec-work-remaining/README.md` (state of record).

- **Only one install path is proven.** Debian, macOS and WSL2 installs are
  untested, as is the anonymous HTTPS clone path; the verified path is
  Ubuntu 24.04 + rootless Docker (PKG-22).
