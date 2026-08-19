# Current State

**Status: MUTABLE.** Concise, evidence-linked description of where this
repository actually is. For the running operational ledger see
[`CAMPAIGN-STATUS.md`](./CAMPAIGN-STATUS.md); for release readiness see
[`LAUNCH-CHECKLIST.md`](./LAUNCH-CHECKLIST.md).

Last updated: **2026-08-19 04:26 CDT / 2026-08-19T09:26:52Z**

## Checkout

There is exactly one canonical checkout:

```text
/var/home/dylan/projects/disco
```

| Fact | Value |
|------|-------|
| Branch | `main` |
| Committed HEAD | `f724188aba2c2d591a273d6a1c13ae3bb43e5d0d` |
| `origin/main` | `f724188a` (identical) |
| Working tree | Clean |
| Clean candidate | **`main` itself** — every landed package is a source/candidate sibling pair |

The repository was renamed **disclaude → disco** and restructured on
2026-08-17. The old `projects/build-platform-core-v1/disclaude` checkout and
the `disclaude/build-platform-core-v1` branch no longer exist. Historical
branches (~495) were archived to `refs/archive/*` on origin, not deleted;
recover one with
`git fetch origin 'refs/archive/<name>:refs/heads/<local>'`.
Dated `prep/*` worktree branches may carry in-flight work; only `main` is
authoritative.

## Three-bucket layout

Since the 2026-08-17 restructure (source `67ef47be`, candidate `6a8cf723`) the
top level is three buckets:

| Bucket | Contents | Rule |
|--------|----------|------|
| `archive/` | Historical material, explicitly superseded. | Never current authority; never touch. |
| `current/` | Shipped code + docs: `packages/`, `frontend/`, `docs/`, `sec-work-remaining/`. | The product. |
| `development/` | Harness, tests, scripts, architecture authorities. | The machinery around the product. |

The public-API authority could not carry across the directory move (its
immutability rule reads a move as rename+delete), so it is **re-pinned at
`c2760f2a`** — the last accepted pre-move candidate — and surface comparison
now keys on the bucket-normalised path. Consequence: pre-restructure
certificates (V40C/V41) no longer verify from the repo bytes; the continuity
receipt lives in the external campaign ledger.

## The launch line — PKG-21 … PKG-25

Five owner packages landed after the restructure, each as a source commit plus
a derived-authority sibling candidate. The candidate is what `main` carries.

- **PKG-21-PUBLIC-RELEASE-PREP** (source `a2248195`, candidate `532343de`,
  2026-08-17). Made the repo survivable for a stranger and removed the
  maintainer's home directory from everything that executes. The quickstart was
  unrunnable (`cd disclaude` into a directory the rename retired) — it now
  clones the real URL and cds into `disco`; the Docker opt-in documents both
  socket paths, rootless first, with the root-equivalence of
  `/var/run/docker.sock` called out. Three live-e2e gauntlet specs, the
  offline Projects fixtures, two package suites, three harness scripts and
  `trace_conversation.py` stopped hard-coding absolute home paths (silently
  broken since the rename); what remains absolute is deliberate — hash-pinned
  acceptance bytes and prose records. Community health added: issue forms
  (model in use is a required field), PR template, Contributor Covenant 2.1,
  root `SECURITY.md` routing to private advisories. Null architecture advance.

- **PKG-22-DOCKER-QUICKSTART-DOCS** (source `140d211b`, candidate `90cb29e4`,
  2026-08-17). A real install on clean Ubuntu 24.04 with rootless Docker found
  the Quickstart block was entirely Podman — a Docker user hit three
  consecutive failures. Both quickstart docs now carry a copy-pasteable Docker
  block beside the Podman one, and no Linux user is told to run `open`. Two
  Docker behaviours that look like failures are documented as benign (registry
  pull-before-build noise; rootless DNS needing explicit resolvers). Image
  sizes now carry both columns — disco-server is 7.88 GB under Docker vs
  4.69 GB under Podman. Measured: 4m35s from `up --build` to three healthy
  services. Documentation-only; null architecture advance.

- **PKG-23-PRELAUNCH-FIXES** (source `e5ecc3b8`, candidate `330c622e`,
  2026-08-18). Five pre-launch findings, four from driving the product. MCP
  was unusable out of the box: the stdio config stuffed a whole command line
  into one argv token — the DTO now carries an optional `args` list, a pasted
  command is shlex-split (stdio only, never a URL, execution stays argv-list,
  approval gates untouched), and stdio failures report the same
  `{code, attempts, exception_type}` diagnostics as streamable_http
  (`McpPool.server_diagnostics()` is new public surface). The Settings "Test"
  buttons lied — Brave/Tavily/Firecrawl probes now call their real endpoints in
  their real auth shape and 401/403 reports as unauthorized; image generation
  stops sending `response_format` to gpt-image-1. The deprecated Assist
  control is hidden on Build and Agent — the `assist` prop and reducer state
  are retained for API stability, only rendering is gone. Two mobile layout
  bugs (fixed-width inspector, snap-toggle overlap) are `lg:`-scoped. Also
  fixed: a global gitignore `build/` rule was silently swallowing new files in
  `current/frontend/src/components/build/`.

- **PKG-24-MOBILE-AND-RUNTIME-HONESTY** (source `05b2484c`, candidate
  `23dc6784`, 2026-08-18). Two efforts, one a security fix. **Sandbox runtime
  honesty:** on the documented rootless-Podman default,
  `DISCO_LOCAL_RUNTIME=runsc` was parsed and never used — the compose line
  advertising gVisor produced an ordinary crun container with no error
  (verified on a fresh Fedora VM: host kernel visible inside the "sandbox").
  The runtime is now requested AND the assigned runtime is inspected
  afterwards; a mismatch is a typed refusal. Docs stop advertising gVisor on
  this path and point at rootful Docker. **Mobile:** below-`lg:` is mobile,
  44px minimum targets, one scroll dimension, desktop pixel-identical. Search
  pins its composer to the viewport bottom; Build/Agent moves the inspector
  into a full-screen sheet; the plan-approval card no longer paints its badge
  over its heading (bounding-box Playwright test, proven by revert). Shared
  primitives `tapTarget`, `useScrollFade`, `ScrollFade` are new public
  surface. Known remaining: ~25 secondary Settings forms and some Build panels
  still carry sub-44px controls — see the checklist.

- **PKG-25-VERIFY-SECRET-RESOLUTION** (source `ffc1b6b2`, candidate
  `f724188a`, 2026-08-19). The documented verification step
  (`compose exec agent-server disco-verify --quick`) failed every check on a
  working install: `compose exec` skips the entrypoint that loads the app
  secret, so the CLI ran with no master key. Naively resolving the secret
  would have been worse — the entrypoint bootstraps `$DATA/.secret_key` while
  the library default is `$DATA/secret-key`, so the CLI would have minted a
  rival second key. Both halves fixed: `ensure_process_secret_key` adopts the
  entrypoint's key file when its own canonical path is absent, and
  `disco-verify` resolves the secret the way the servers do. Explicit path,
  operator env var and canonical file all still win. Four tests pin it,
  including no-rival-key-minted; verified against a live stack. No public
  surface moved.

## Fingerprints

Computed with `development/scripts/source_fingerprint.py` on a clean checkout
of `f724188a` (reads worktree contents; never stages, stashes, or writes).

| Digest | Value | Scope |
|--------|-------|-------|
| source | `sha256:0238f4f958b301e0b09f8f7f72816fa565dd102fb5954e645699cf889174bc9f` | `current/packages/ current/frontend/ development/harness/ development/scripts/` + build config (3068 files) |
| tree | `sha256:803f7085de401b6f9d33a82f41762eac4151887cf792e92ffcf472030972e394` | every tracked/untracked non-ignored file (3710 files) |

## Known limitations, honestly stated

1. **Release readiness is not uniform.** The DONE / PARTIAL / OPEN state per
   item — including everything that still needs the owner — is recorded in
   [`LAUNCH-CHECKLIST.md`](./LAUNCH-CHECKLIST.md). Do not infer readiness from
   this file.
2. **Pre-restructure certificates do not verify from the repo.** The
   public-API authority re-pin (above) is why. This is recorded, not broken.
3. **The mobile tap-target sweep is partial** (first-run paths done; dense
   secondary forms remain). The gap is described in the checklist as of
   `main`; parallel work may be closing it but has not landed.
