# HANDOFF — WO-F4.1 Stripe security fill (2026-07-11)

## Completed and published

`disclaude/mega-campaign` is clean and pushed to local `origin` at
`8f356c1eed19872ce45ed2cfb0a4dcace8026a22` (`merge: complete Stripe security fill`).
At the time this handoff was written, local and remote were synchronized.

The preserved `disclaude/f33-webhook-seam` scaffold was deliberately **not** merged:
F4.1 owns the Stripe-specific inbound Worker route, while F3.3 remains its separate,
unfilled generic webhook primitive.

## What F4.1 now provides

- Authenticated A2.2 host-service bus and A2.3 Worker client shim.
- Restricted `rk_` Stripe key custody, origin approval/pinning, host-side plan-to-price
  mapping, and client price/amount rejection.
- Worker-side raw-body Stripe HMAC verification, five-minute timestamp tolerance,
  atomic D1 fulfillment, idempotent replay acknowledgement, entitlement grants and
  revocation.
- Runtime readiness binding: a valid webhook signature alone cannot cause side effects
  until the Worker proves it has the current host-managed secret generation through
  `payments.ready`.
- Cloudflare deployment/rotation lifecycle with readiness probes and fail-closed
  rollback behaviour.
- Mandatory local live verifier using the generated dry-run Worker bundle, Miniflare
  D1, and the authenticated HTTPS host bus. It proves the five required exploit checks
  plus the full grant/revoke/stale-event/async-success lifecycle.
- Reviewed generated `package-lock.json`; verification refuses a mutable `npm install`
  fallback when the lockfile is absent.

## Key commits

| Commit | Purpose |
| --- | --- |
| `8f356c1e` | Merge completed F4.1 into `mega-campaign` |
| `11d3d70f` | Current-secret webhook readiness, real Miniflare lifecycle proof, deterministic build enforcement |
| `8117daf8` | Mandatory live exploit verifier |
| `dd11e23f` | Cloudflare Stripe deployment lifecycle |
| `5e30a15c` | Zero-baseline pyright fixes |

## Verification completed

- Stripe focused suites: green, including the real live verifier integration run.
- `uv run python scripts/check_arch_budget.py`: pass.
- `uv run lint-imports`: pass.
- `uv run python scripts/gen_arch_diagram.py --check`: pass.
- `uv run basedpyright`: `0 errors, 0 warnings, 0 notes`.
- `.venv/bin/python3 -m pytest -m "not integration" -q`: pass on the integrated
  `mega-campaign` tree.
- Frontend: `npm run test` (144 files / 968 tests), `npm run typecheck:build`, and
  `npx vite build`: pass.

Existing test warning output (React `act(...)`, jsdom navigation, and unrelated
dependency deprecations) did not affect exit status.

## Operational follow-up

No production Stripe key, webhook secret, Cloudflare account, or real deployment was
configured by this work. Before production activation, an operator must configure only
the restricted `rk_` credential and the host-managed webhook secret, approve the exact
Stripe origin, and use the deployment lifecycle rather than manually injecting secrets.

The requested Firefox screenshot could not be captured: the in-app browser was not
available in this environment. The generated UI did pass the frontend build and live
Worker verifier paths, but visual evidence should be captured in an environment where
the Firefox browser surface is available before a UI release.
