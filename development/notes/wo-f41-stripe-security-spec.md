# WO-F4.1 — Stripe security fill: the spec for the deferred security session

Status: **SECURITY FILL COMPLETE** on `disclaude/mega-campaign` (merge
`8f356c1e`, hardened through `11d3d70f`; archived reference:
`refs/archive/disclaude/f41-stripe-seam`). The primitive
(`current/packages/core/src/disco/core/appkit/stripe_primitive.py`) remains Disco-owned
`tier="template_only"`, now with the two-stage gate live: `stripe_verify`
(deterministic provenance / trusted-tree / secret-absence checks) plus the
mandatory `stripe.security.v1` live exploit runner (§5's five checks). The
host side lives in `current/packages/core/src/disco/core/stripe_host_service.py`
(`payments.checkout` + `payments.ready`), worker emission in
`appkit/stripe_worker.py`, and the live runner in
`current/packages/agent-server/src/disco/agent_server/stripe_live_verifier.py`.
This work was security-classed (plan §10.5: NOT Fable; Opus/codex session with
adversarial review). The sections below are the specification of record for the
fill; none of it may be stubbed or regressed to pass the gate.

## What the seam already provides (do not rebuild)

- `StripeSpec` (display strings + `entitlement_flag` + `success_message`; NO price IDs/keys by design).
- `apply_stripe_spec`: pricing section (`stripe_pricing`, variant `pricing.single-plan-emphasis`) folded
  into the AppSpec + `entitlement_flag` recorded in `AppSpec.roles` (records RBAC gates entities on roles).
- Pending-state UI (disabled button + "Checkout activates after payment setup."; no checkout/webhook
  route, no fetch, no secret template) and provenance at `.disco/primitives/stripe.json` (A3.2 gate input).

## 1. Host-side Checkout Session creation (`payments.checkout`)

- A `HostServiceDefinition(name="payments.checkout", …)` in the host-service registry
  (`disco.core.host_services`, WO-A2.1), reached by the generated Worker over the WO-A2.2 bus
  (`/_disco/svc/payments.checkout`) using the A2.3 `disco-client.ts` shim and the A2.2 per-app
  conversation-bound bearer. The app NEVER holds a Stripe credential.
- The handler is a THIN ADAPTER composing the existing S-W2 chokepoints, exactly the pattern documented in
  `host_services.py`: `resolve_provider_secret("stripe", ctx.secret_store)` →
  `secret_ref_allowed_for_origin("stripe", url)` → `OriginApprovalStore.is_approved(url,
  "payments.checkout", "stripe")` → `guarded_request("POST", "https://api.stripe.com/v1/checkout/sessions", …)`.
  Egress pinned to `api.stripe.com` via `allow_hosts`; no other host may be reached with this ref.
- The operator configures HOST-SIDE: the restricted API key (see §4) and the real Stripe **price ID(s)**
  mapped to the app's plan. The spec's `price_display` is card copy only; the handler must NEVER accept a
  price/amount from the sandboxed app (a compromised app choosing its own price is the obvious abuse).
  Input: plan selector + success/cancel return URLs validated against the app's own origin. Response to
  the app: the Checkout Session URL only. `success_message` rendering keys off the verified webhook,
  never off the redirect (a user hitting the success URL manually must not be treated as paid).

## 2. Emitted Worker webhook route (Disco-owned, template_only output)

- The generator emits a Worker route (e.g. `POST /api/stripe/webhook`) — Disco-owned code the model never
  authors. `STRIPE_WEBHOOK_SECRET` is injected as a Worker env var host-side (A2.2/A2.3 discipline, like
  the bus bearer); it never appears in the emitted tree or bundle.
- **Signature verification:** verify the `Stripe-Signature` header (v1 scheme: HMAC-SHA256 over
  `timestamp.payload` with the webhook secret) with constant-time comparison + a timestamp tolerance
  window (Stripe default 5 min) before ANY parsing of the body as trusted. Failure → 400, no side effects,
  audit-logged.
- **Idempotency in ONE transaction:** fulfillment and the dedup marker commit atomically — a
  `stripe_events` table with `event_id` UNIQUE; `INSERT event_id` + the fulfillment write happen in the
  same D1 transaction (or D1 batch with the insert first, aborting on conflict). A replayed/duplicate
  event MUST NOT double-fulfill (double-grant, double-credit). Replay of a processed event → 200 (ack),
  zero new writes.
- Only handle the event types fulfillment needs (`checkout.session.completed` + subscription lifecycle);
  unknown types → 200 ack, no side effects. Amounts/line items come from the verified event payload only.

## 3. Entitlement grant on verified fulfillment

- On a verified + deduped `checkout.session.completed`: grant the `entitlement_flag` role (already in
  `AppSpec.roles`) to the paying user in the records auth model (users/sessions tables) — the role write is
  part of the §2 single transaction. The RBAC gates (`write_roles`/`read_roles`) then work unchanged.
  Revocation: subscription cancelled/expired events remove the role (also idempotent).
- Only after this exists may the pricing card flip from pending to a live checkout button (generator
  change: enabled button → A2.3 shim call to `payments.checkout`). Removing the disabled/pending state
  WITHOUT this fill is a false affordance and is forbidden.

## 4. Key custody / blast radius

- The Stripe credential is a **restricted key** (`rk_…`) scoped to Checkout Sessions + Customer Portal
  only — reject a full-access `sk_…` at configuration time (leak blast radius: no refunds/payouts).
- Secret lives in the host `SecretStore` under ref `"stripe"`, origin-pinned to `api.stripe.com`
  (`secret_ref_allowed_for_origin`); never in app code, tree, bundle, or logs (existing no-plaintext rule).

## 5. The `verify()` harness — what flips `verify` from None

Real-exploit discipline (no cassettes), against the running preview + a signing webhook simulator:

1. **Forged signature rejected:** valid-shape event with a wrong/tampered signature (and a stale-timestamp
   variant) → 400, zero fulfillment rows, zero role grants.
2. **Replay deduped:** the same correctly-signed event delivered twice → exactly one fulfillment row, one
   role grant, second delivery acked without writes.
3. **Secret absence:** grep the whole emitted tree AND the built bundle for the key/secret values and
   `sk_live|sk_test|whsec_` patterns (2.2's build-time-lint discipline) → zero hits.
4. **Blast radius:** configuration accepts only `rk_…`; an `sk_…` ref is refused with a loud error.
5. **No price injection:** a checkout request carrying a client-chosen amount/price is refused by the host
   handler.

All five pass → set `PrimitiveDefinition.verify` to the harness runner returning `PrimitiveVerifyResult`;
the WO-A3.2 finish gate then passes for stripe-bearing apps. Until then the gate stays closed.

## Compose points (existing, reuse wholesale)

`disco.core.host_egress.guarded_request` · `disco.core.llm.secret_refs.resolve_provider_secret` /
`secret_ref_allowed_for_origin` · `disco.core.origin_approvals.OriginApprovalStore` ·
`disco.core.host_services` registry (A2.1) · A2.2 bus + bearer · A2.3 `disco-client.ts` shim ·
`.disco/primitives/*.json` provenance (A3.2 gate input) · records auth schema (users/sessions/roles).
