# Vercel prebuilt contract

**Epic 15 / PKG-15-TARGETS — local contract recorded 2026-08-04**

This document records what the R-VERCEL work established before any Vercel
connector or Next.js target is frozen. It is a contract boundary, not live
provider qualification.

## Accepted contract

- Build the artifact locally (or in CI) and retain its immutable artifact
  identity.
- A later `vercel deploy --prebuilt` connector may upload that existing output;
  it must not silently substitute a source rebuild.
- Build-time environment values are resolved before the local build. Runtime
  environment values are a separate deployment concern. Vercel's `--build-env`
  and `--env` flags must not be conflated.
- The generic Build Platform deployment seam carries the target, environment,
  and exact `package_digest_ref`; it returns intent and confirmation state, not
  a deployment effect or a provider verdict.
- Provider-side rebuild status is currently **unobserved**. A connector must
  report that residual rather than claim that the uploaded artifact was or was
  not rebuilt.

The contract is exercised without a provider implementation in
`packages/core/tests/test_build_platform_vercel_contract.py`. The fixture is
not a registered connector and does not contact Vercel.

## Evidence

The bounded, secret-negative probe is recorded in
`/var/home/dylan/disclaude-campaigns/architecture-rework/2026-08-04/PKG-15-TARGETS/`:
`R-VERCEL-EVIDENCE-2026-08-04.md` and its raw logs. It found no local Vercel
executable, resolved ephemeral `npx vercel@latest` as CLI 58.5.1, entered login
for unauthenticated prebuilt attempts, and rejected the invalid token before a
deployment. No credential or deployment was used.

Primary documentation consulted by that evidence:

- [Vercel build](https://vercel.com/docs/cli/build)
- [Vercel deploy](https://vercel.com/docs/cli/deploy)
- [Deploying from the CLI](https://vercel.com/docs/cli/deploying-from-cli)
- [Vercel environment variables](https://vercel.com/docs/environment-variables)

## Explicit residual

An authenticated Vercel project and authorized token are required to observe
provider-side upload and rebuild behavior. That external qualification is
deferred. Vercel remains optional, self-hosting remains first-class, and no
Next.js/provider branch belongs in target-neutral Core.
