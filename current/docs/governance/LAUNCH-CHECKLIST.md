# Launch Checklist — release readiness

**Status: MUTABLE.** The honest DONE / PARTIAL / OPEN state of the public
launch, item by item. Every claim below was verified against the tree at
`main` = `f724188a` on **2026-08-19**; each carries a one-line pointer to its
evidence. When an item moves, move it here — do not let this file lie.

For what each launch package changed, see
[`CURRENT-STATE.md`](./CURRENT-STATE.md).

## DONE

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

- **`.env.example` advertises knobs that go nowhere.** `AZURE_OPENAI_API_KEY`
  and `COMFYUI_API_KEY` sit in the root `.env.example` — remove them or build
  what they promise.

- **Only one install path is proven.** Debian, macOS and WSL2 installs are
  untested, as is the anonymous HTTPS clone path; the verified path is
  Ubuntu 24.04 + rootless Docker (PKG-22).
