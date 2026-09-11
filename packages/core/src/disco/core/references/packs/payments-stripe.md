# Payments with Stripe (Payment Element + webhooks)

> Capture card details in the booking flow, charge on confirmation, record every transaction with a status, verify webhooks, and handle failure/retry — Stripe test mode end to end.

## Env
```
STRIPE_SECRET_KEY        # sk_test_… (server only)
STRIPE_PUBLISHABLE_KEY   # pk_test_… (served to the browser via GET /api/payments/config)
STRIPE_WEBHOOK_SECRET    # whsec_… from `stripe listen` or the dashboard endpoint
STRIPE_CURRENCY          # default "usd"
```
`npm i stripe@^18.0.0` in `api/`; `npm i @stripe/stripe-js@^7.0.0 @stripe/react-stripe-js@^3.0.0` in `web/`. Amounts are integers in the smallest unit (cents); compute them on the server from the booking, never from the client.

## Schema
```sql
CREATE TABLE payments(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  provider TEXT NOT NULL DEFAULT 'stripe', intent_id TEXT UNIQUE, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed','refunded')), last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
```
Bookings carry `payment_status` mirrored from the latest payment; a booking is `confirmed` only when a payment is `succeeded`.

## Flow
1. `POST /api/bookings` creates the booking as `pending_payment` (availability check per booking-and-inventory pack) and returns `{ bookingId, totalCents }`.
2. `POST /api/payments/intents` `{ bookingId }` → server recomputes the total, creates `stripe.paymentIntents.create({ amount, currency, automatic_payment_methods: { enabled: true }, metadata: { bookingId, userId } }, { idempotencyKey: `booking-${bookingId}-v${booking.version}` })`, stores a `pending` payment row with `intent_id`, returns `{ clientSecret }`. Re-calling for the same booking returns the same intent (idempotency key).
3. Browser: `<Elements stripe={loadStripe(pk)} options={{ clientSecret }}>` → `<PaymentElement/>` → `stripe.confirmPayment({ elements, redirect: "if_required", confirmParams: { return_url: `${location.origin}/bookings/${id}` } })`. On `error`, show `error.message` and keep the form so the user can retry or pick another method; on `paymentIntent.status === "succeeded"` show "Processing confirmation…" and poll `GET /api/bookings/:id` until the webhook flips it (max ~20 s), then show the receipt.
4. Webhook `POST /api/payments/webhook` (public route, RAW body):
```js
app.post("/api/payments/webhook", express.raw({ type: "application/json" }), (req, res) => {
  let event;
  try { event = stripe.webhooks.constructEvent(req.body, req.headers["stripe-signature"], process.env.STRIPE_WEBHOOK_SECRET); }
  catch (err) { return res.status(400).send(`signature: ${err.message}`); }
  const pi = event.data.object;
  const map = { "payment_intent.succeeded": "succeeded", "payment_intent.payment_failed": "failed", "charge.refunded": "refunded" };
  const status = map[event.type]; if (!status) return res.json({ received: true });
  const intentId = event.type === "charge.refunded" ? pi.payment_intent : pi.id;
  db.transaction(() => {
    db.prepare("UPDATE payments SET status=?, last_error=?, updated_at=? WHERE intent_id=?")
      .run(status, pi.last_payment_error?.message ?? null, new Date().toISOString(), intentId);
    const p = db.prepare("SELECT booking_id FROM payments WHERE intent_id=?").get(intentId);
    if (p) db.prepare("UPDATE bookings SET payment_status=?, status=CASE WHEN ?='succeeded' THEN 'confirmed' ELSE status END WHERE id=?").run(status, status, p.booking_id);
  })();
  broadcast("booking:" + p?.booking_id, "booking:updated", …);
  res.json({ received: true });
});
```
Register the raw-body route BEFORE `express.json()` or mount it on its own path; add `/api/payments/webhook` to auth-kit's `publicAllowlist` (signature is the auth). Webhooks may arrive twice — the UPDATE is idempotent by design.
5. Refunds (host/admin): `stripe.refunds.create({ payment_intent })` → the `charge.refunded` webhook records it.

## User-facing views
Booking page: total (line items: nights × rate, fees), payment status badge, receipt link; dashboard lists trips with payment status; failed → "Payment failed: <reason> — Try again" button that creates a fresh intent when the old one is `canceled`/`requires_payment_method`.

## Prove it
- Test card `4242 4242 4242 4242`, any future date/CVC; decline `4000 0000 0000 0002`; 3DS `4000 0027 6000 3184`.
- Webhooks locally: `stripe listen --forward-to http://localhost:3000/api/payments/webhook` prints the `whsec_`; or `stripe trigger payment_intent.succeeded`. Without the CLI, POST a hand-signed test event: compute `t=<unix>` and `v1=HMAC_SHA256(secret, "<t>.<rawBody>")` and send `Stripe-Signature: t=<t>,v1=<v1>` — the same function `constructEvent` verifies it.
- Inside the build sandbox the default egress blocks `api.stripe.com`; the operator sets `DISCO_BUILD_EGRESS=public` for a real test run, otherwise verify the webhook path with the signed test event and note that live card confirmation was not exercised.

## Security
Secret key server-only; amounts server-computed; verify signatures; never store card data (Stripe does); log intent ids, not customer details.
