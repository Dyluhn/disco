# HANDOFF — security campaign and WO-F4.1 Stripe fill (2026-07-11)

## Completed and published

`disclaude/mega-campaign` is clean and pushed to local `origin` at
`8f356c1eed19872ce45ed2cfb0a4dcace8026a22` (`merge: complete Stripe security fill`).
At the time this handoff was written, local and remote were synchronized.

The preserved `disclaude/f33-webhook-seam` scaffold was deliberately **not** merged:
F4.1 owns the Stripe-specific inbound Worker route, while F3.3 remains its separate,
unfilled generic webhook primitive.

## Broader security work-order board

The Stripe fill was **not** the whole security delivery. The following work was already
complete, integrated, and remains in `mega-campaign`:

| Work order | Status | Principal commit |
| --- | --- | --- |
| S-W1 — auth, CORS, owner scoping | complete | `e028d2ac` |
| S-W2 — secret-ref resolution and approved-origin egress | complete | `17c47761` |
| S-W-Pi — remove Pi integration attack surface | complete | `76b4e397` |
| S-W3 — host-execution/gVisor-bypass floor | complete | `1b762e3f` |
| S-W4 — MCP pre-connect and schema-drift approval | complete | `212f6e89` |
| S-W5 — isolated egress, resource/output/event bounds | complete | `10433330` |
| S-W6 — immutable shares and output-sink hardening | complete | `17869bdb` |
| A2.2 — authenticated host-service bus and capability relay | complete | `cd57f830`, `205d8c6b` |
| A2.3 — hardened Worker host-service client | complete | `ef19bac6` |
| F4.1 — Stripe checkout/webhook security fill | complete | `8f356c1e` |

The trusted-components campaign was also already integrated at `daa306b3`; its later
adversarial fixes close auth/RBAC normalization, encoded-path, XFF/413, and lockfile
integrity regressions. It is a separate campaign, but is security-relevant context for
any future component work.

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

## Remaining security-classed work

- **WO-A4 remains deferred:** per-app credential rotation, usage accounting, quotas, and
  rate limiting above the now-complete A2 bearer layer.
- **F3.3 remains deferred and fail-closed:** `disclaude/f33-webhook-seam` is not merged or
  registered. Its generic webhook security fill must have its own real exploit harness.
- **Packaging follow-up (subsequently corrected):** local rootless Podman is the intended
  default; gVisor/runsc remains an optional stronger tier for compatible hosts.
- **Independent assurance remains outstanding:** the historical W1–W6 campaign received the
  Codex close-out pass; an independent Opus-class review was unavailable. Run that review
  without reopening already-completed implementation waves.
