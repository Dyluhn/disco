# WO-F3.3 — webhook security fill (spec for the security-classed session)

Status: **SECURITY FILL COMPLETE** on `disclaude/mega-campaign` (merge
`ce349096`). The primitive remains Disco-owned `tier="template_only"`, now with
deterministic trusted-tree verification plus the mandatory
`webhook.security.v1` live exploit runner. Inbound signature/idempotency,
outbound approved egress/signing, secret custody, Cloudflare readiness, and
failure rollback below are implemented and gated.

## What the seam already gives you

- `WebhookSpec` (endpoint_id / direction / event_types / description) validated
  through `app_add_primitive`; fold recorded as `custom` documentation Sections
  on the `webhooks` docs page + provenance at `.disco/primitives/webhook.json`.
- `host_contract=(HostService("webhook.emit"),)` — the declared (unregistered)
  outbound service name.
- The emitted surface is a static contract page carrying the
  `"Handler pending secure setup"` marker; NO route, NO handler, NO secret.
- Signing secret + outbound target URL are operator-configured host-side —
  deliberately absent from the spec. Keep it that way.

## Inbound leg (to build)

1. **Emitted Worker route** (`POST /api/webhooks/{endpoint_id}`), Disco-owned
   (template_only — the model never authors it), emitted only for
   `direction="inbound"` endpoints.
2. **Signature verification**: HMAC over the raw body (+ timestamp header inside
   the signed payload, bounded skew window), compared TIMING-SAFE
   (`crypto.subtle.verify` / constant-time compare — never `==`). The signing
   secret reaches the Worker as an injected env var via the A2.2/A2.3 pattern
   (minted host-side, never in the tree; see `docs/wo-a2-host-bus-design-notes.md`).
   Unsigned / forged / stale-timestamp deliveries → 401, body untouched.
3. **Idempotency dedup IN ONE TRANSACTION** with the effect: the delivery's
   idempotency key (provider event id) is inserted into a dedup table and the
   handler's side effect applied in the SAME transaction — a replayed delivery
   hits the unique constraint and is acknowledged WITHOUT re-applying (plan §7:
   "replay does not double-fulfill"; 4.1 Stripe webhooks inherit this exact shape).

## Outbound leg (to build)

1. Register the real `webhook.emit` host service in `disco.core.host_services`
   (A2.1 discipline: a THIN adapter composing existing S-W2 calls only).
2. All egress through `host_egress.guarded_request` (the S-W2 chokepoint:
   public-IP-only + redirect revalidation) — this is where **internal/metadata-IP
   rejection and DNS-rebind protection** live. Do not reimplement them in the
   handler; if guarded_request lacks a needed check, escalate (A2.1 rule).
3. Target origin requires a signed operator approval (`OriginApprovalStore`);
   outbound signing secret via `resolve_provider_secret` +
   `secret_ref_allowed_for_origin`. Deliveries signed so receivers can verify.
4. Dispatch rides the A2 bus (`/_disco/svc/webhook.emit`) behind the A2.2
   conversation-bound bearer — never a direct fetch from the generated app.

## The `verify()` hook — what flips `verify` from None to real

Replace `verify=None` in the `PrimitiveDefinition` registration ONLY when an
adversarial harness proves, against the real emitted app in the sandbox:

- forged-signature delivery → rejected (401), no side effect;
- valid delivery replayed (same idempotency key) → deduped, effect applied once;
- outbound emit to an internal / metadata IP (including a DNS-rebind host that
  resolves public then rebinds private) → blocked by the egress chokepoint;
- grep of the whole emitted tree + built bundle finds NO secret material
  (2.2's hardcoded-credential discipline).

The WO-A3 gate then keys on this verify at finish; until it exists the gate
fails closed for any app that applied the webhook primitive. That is intended.
