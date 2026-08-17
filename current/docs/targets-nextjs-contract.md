# Next.js target contract

**Epic 15 / PKG-15-TARGETS — slice 15-T1 (tier R) frozen 2026-08-04**

This is the dated, documentation-only design freeze for the static and server
Next.js targets. It names the literal production and test requirements later
implementation slices must satisfy. It is a contract boundary over the
existing Build Platform Core, not live provider qualification.

Source of truth for the accepted local Vercel boundary:
[`current/docs/targets-vercel-prebuilt-contract.md`](./targets-vercel-prebuilt-contract.md).
Self-hosting is first-class; Vercel remains an optional strict-prebuilt
follow-on that this slice explicitly defers.

## Scope of this freeze

- Freezes the **public profile IDs**, the **engine** both profiles resolve to,
  and the literal **command / output / entry / port / preview / readiness /
  delivery / package / exporter** values each target emits.
- Freezes the **mutation assertions** that later focused tests must contain.
- Freezes the **source-locality rule**: the exact Core lifecycle directories in
  which Next.js / provider names are prohibited.
- Explicitly **defers** connector implementation (15-C1 / 15-C2) and keeps
  provider rebuild `unobserved`. No authenticated provider behavior is claimed
  or proven by this slice.

This slice changes no Python or TypeScript file. It only adds this document.

## Frozen public profile IDs

Both targets are expressed as built-in `disco`-namespace profiles that resolve
through the existing Build Platform registry/composition path. The **public
profile IDs** are frozen as:

| target | public profile ID (canonical) | component name | engine |
|---|---|---|---|
| Next.js static | `disco.nextjs_static@1` | `nextjs_static` | existing Freeform engine |
| Next.js server | `disco.nextjs_server@1` | `nextjs_server` | existing Freeform engine |

Rules that make these literal:

- Namespace is `disco`; component names are lowercase identifiers and obey the
  existing `ComponentId` validation (`^[a-z][a-z0-9_-]{0,62}$`), so the profile
  names are `nextjs_static` and `nextjs_server` (underscore, not dot).
- Both profiles select the **existing Freeform engine** — the same
  `FREEFORM_ENGINE_ID` already registered, **not** a new Next.js-specific engine
  and **not** AppKit. Neither target adds Next.js or Vercel vocabulary to the
  construction/engine layer.
- Each profile carries its own target, delivery, preview, package and exporter
  identity. The two targets are **distinct typed components**; a static profile
  must never resolve the server target or vice-versa.

## Static target: frozen literals

| attribute | frozen literal |
|---|---|
| build command | `next build` |
| output directory | `out` |
| entry | `out/index.html` |
| server start command | **none** (no start command; static export only) |
| preview policy | optional / **degrading** file-readiness preview |
| readiness | file-based readiness that **degrades** when unavailable (never blocks finish on a live server) |
| delivery shape | a static-only namespaced delivery identity |
| package shape | a static-only namespaced package identity |
| exporter | a static-only exporter identity |

Static build semantics:

- The target emits the exact build command `next build` and the exact output
  directory `out`.
- The delivery entry is `out/index.html`.
- There is **no** server start command and **no** port for the static target.
  A server profile must never be attached to the static target.
- Preview is optional/degrading: file-readiness is best-effort and its absence
  degrades the preview, it does not block or require a live HTTP server.
- The static target's delivery, package and exporter identities are distinct
  from the server target's.

## Server target: frozen literals

| attribute | frozen literal |
|---|---|
| build command | `next build` |
| start command | `next start` |
| output directory | `.next` |
| port | `3000` |
| readiness | **required / blocking** HTTP readiness at `/` |
| delivery shape | a server-only namespaced delivery identity |
| package shape | a server-only namespaced package identity |
| exporter | a server-only exporter identity |

Server build semantics:

- The target emits the exact build command `next build` and the exact start
  command `next start`.
- Output directory is `.next`; the served entry is the running server, exposed
  on the literal port `3000`.
- Readiness is **required and blocking**: an HTTP readiness check at `/` must
  succeed for finish; if the server is unavailable, the readiness blocks rather
  than degrades. This is the opposite of the static target's optional/degrading
  file-readiness preview.
- The server target's delivery, package and exporter identities are distinct
  from the static target's and never reused by it.

## Distinct identity / shape table

The two targets keep separate identities at every adapter-owned surface. Later
tests must assert these stay disjoint:

| surface | static | server |
|---|---|---|
| profile ID | `disco.nextjs_static@1` | `disco.nextjs_server@1` |
| build command | `next build` | `next build` |
| output directory | `out` | `.next` |
| entry | `out/index.html` | served server |
| start command | **none** | `next start` |
| port | **none** | `3000` |
| preview/readiness | optional, degrading, file-readiness | required, blocking, HTTP `/` |
| delivery shape | distinct static delivery | distinct server delivery |
| package shape | distinct static package | distinct server package |
| exporter | distinct static exporter | distinct server exporter |

## Adapter-owned target vocabulary and Core locality

- Next.js / Vercel command, output, entry, port, preview and readiness
  vocabulary is **adapter-owned**: it lives in the target/export profile
  declarations, not in Core, not in the engine, and not in the resolver.
- The following exact Core directories are **prohibited** from containing
  Next.js- or provider-specific vocabulary (including `nextjs`, `.next`,
  `next build`, `next start`, `vercel`, and target-specific `next` command
  handling):
  - `current/packages/core/src/disco/core/loop/` (the Core loop/lifecycle runtime);
  - `current/packages/core/src/disco/core/loop/finish/` (finish and verification);
  - `current/packages/core/src/disco/core/store/` (the Core store surface); and
  - `current/packages/core/src/disco/core/build_platform/` (target-neutral contracts,
    engines, resolver and registry).
  Generic Python `next(...)` iteration is not target vocabulary. The later
  locality slice must scan these actual directories and separately classify any
  pre-existing target-specific command recognition.
- The existing Freeform engine must keep emitting only its current
  target-neutral operations; it must not grow a Next.js branch.
- A source-locality check (later slice `15-L1`-style) must scan those actual
  prohibited modules and assert no Next.js / provider string is present.

## Mutation assertions later focused tests must contain

Each later implementation slice's focused tests must include mutation controls
that fail if the frozen value is removed or substituted:

1. **Command mutation:** removing or changing `next build` for either target
   must fail the test.
2. **Port mutation:** changing the server port away from `3000`, or adding a
   port to the static target, must fail the test.
3. **Entry mutation:** changing the static entry away from `out/index.html`
   must fail the test.
4. **Readiness mutation:** weakening the server target's required/blocking HTTP
   readiness to optional/degrading, or hardening the static target's
   optional/degrading readiness to blocking, must fail the test.
5. **Profile connector attachment mutation:** removing the first-class connector
   from the resolved profile (i.e. the profile resolves without a connector
   attached) must fail the test. The connector attachment is observed through a
   resolved profile/composition, not only in prose.
6. **Digest substitution mutation:** substituting a different
   `package_digest_ref` in the deployment/export request must be rejected (a
   silent rebuild or digest replacement is forbidden), in the same fail-closed
   shape the accepted Vercel contract already requires.
7. **Target identity mutation:** making the static profile resolve the server
   target (or vice-versa) must fail the test.

These controls must inspect the **resolved production plan**, not a
serialization-only proxy, per the slice contract (no serialization proxy proves
source locality).

## Self-host-first and deferred Vercel follow-on boundaries

- Self-hosting is first-class. The static and server targets exist to run on a
  self-hosted host before any external provider is considered.
- An optional **strict-prebuilt Vercel** follow-on connector is a separate
  boundary (`15-C1` self-host connector, `15-C2` optional strict-prebuilt Vercel
  connector) and is **explicitly deferred** from this slice. This slice freezes
  only the Next.js target contracts, not a connector.
- `provider_rebuild` remains **`unobserved`**. This slice makes no authenticated
  provider qualification claim: no credential, no deployment, no provider-side
  rebuild observation was performed.

## Explicit residual

Authenticated Next.js/Vercel provider behavior is outside this documentation
freeze and remains a qualification residual. Self-hosting remains first-class
and no Next.js/provider branch belongs in target-neutral Core.

## Source-locality and diff controls for this slice

- This slice added exactly one file: `current/docs/targets-nextjs-contract.md`.
- `git diff --check` is clean.
- No Python or TypeScript file was created or modified by this slice. The
  newly added Next.js / Vercel contract vocabulary appears only in this
  documentation. This is a changed-file locality proof, not a claim that the
  pre-existing tree already satisfies the future Core-locality prohibition:
  `current/packages/core/src/disco/core/loop/plan_command_validation.py:405` still
  recognizes the bare `next` server-start command and is intentionally left for
  the later locality/integration boundary.
