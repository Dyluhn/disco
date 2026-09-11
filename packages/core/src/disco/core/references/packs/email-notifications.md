# Transactional email notifications

> Send an email when a state changes (order status, booking confirmed), with a subject that says what changed, a proper sender, a retry queue, and Mailpit for verification without an external provider.

## Env
```
SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASS   # any SMTP provider; Mailpit in dev (host "mailpit", port 1025, no auth)
SMTP_SECURE                                  # "true" for port 465, else STARTTLS when offered
EMAIL_FROM                                   # e.g. "City Pharmacy <orders@pharmacy.example>"
PUBLIC_URL                                   # for links in the email
```
`npm i nodemailer@^7.0.0`. compose (dev/verification profile):
```yaml
  mailpit:
    image: axllent/mailpit:v1.20
    ports: [ "127.0.0.1:8025:8025" ]      # web inbox; SMTP on 1025 inside the network
```

## Outbox pattern (reliable + prompt)
Never send from inside the request that changed state; write an outbox row in the same transaction, then a worker sends it. That is what "reliably and promptly after the update is saved" means.
```sql
CREATE TABLE email_outbox(id INTEGER PRIMARY KEY, to_addr TEXT NOT NULL, subject TEXT NOT NULL,
  text_body TEXT NOT NULL, html_body TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  sent_at TEXT, next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL);
```
```js
// api/src/email.js
import nodemailer from "nodemailer";
const transport = nodemailer.createTransport({ host: process.env.SMTP_HOST, port: Number(process.env.SMTP_PORT ?? 587),
  secure: process.env.SMTP_SECURE === "true", auth: process.env.SMTP_USER ? { user: process.env.SMTP_USER, pass: process.env.SMTP_PASS } : undefined });
export function enqueueEmail(db, { to, subject, text, html }) {
  db.prepare("INSERT INTO email_outbox(to_addr,subject,text_body,html_body,next_attempt_at,created_at) VALUES(?,?,?,?,?,?)")
    .run(to, subject, text, html ?? null, new Date().toISOString(), new Date().toISOString());
  drain(db);                                                    // kick immediately; the interval below is the safety net
}
let draining = false;
export async function drain(db) {
  if (draining) return; draining = true;
  try {
    for (const row of db.prepare("SELECT * FROM email_outbox WHERE sent_at IS NULL AND next_attempt_at <= ? AND attempts < 8 ORDER BY id LIMIT 20").all(new Date().toISOString())) {
      try { await transport.sendMail({ from: process.env.EMAIL_FROM, to: row.to_addr, subject: row.subject, text: row.text_body, html: row.html_body ?? undefined });
        db.prepare("UPDATE email_outbox SET sent_at=?, attempts=attempts+1 WHERE id=?").run(new Date().toISOString(), row.id);
      } catch (err) { const backoffMin = 2 ** row.attempts;
        db.prepare("UPDATE email_outbox SET attempts=attempts+1, last_error=?, next_attempt_at=? WHERE id=?")
          .run(String(err).slice(0, 500), new Date(Date.now() + backoffMin * 60_000).toISOString(), row.id); }
    }
  } finally { draining = false; }
}
export function startEmailWorker(db) { setInterval(() => drain(db).catch(() => {}), 30_000).unref(); }
```
Startup: if `SMTP_HOST` is unset, log one warning `email disabled: SMTP_HOST not set` and make `enqueueEmail` still write the outbox row (so nothing is lost; the admin can see unsent rows).

## Content rules
- Subject states the change: `Order #1042: processing → ready for pickup`.
- Body: greeting by name, what changed (previous → new status), the order id and a one-line item summary, a link to the order (`${PUBLIC_URL}/orders/1042`), the sender's name, how to get help. Plain-text body always; HTML optional and simple.
- One email per transition; do not email for no-op updates (same status).

## Wiring a transition
In the order status handler: inside the transaction that updates the row, read the previous status, then `enqueueEmail(db, template.orderStatusChanged({ user, order, from, to }))`; then `broadcast("order:"+id, "order:updated", row)`.

## Prove it
With Mailpit: change an order's status → `curl http://mailpit:8025/api/v1/messages` (from the api container) lists a message to the patient with the expected subject; the Mailpit UI at :8025 shows it. Stop Mailpit, change a status, start it again → the message arrives on the next drain (retry works). With a real SMTP provider, send one test to yourself and note the provider in the delivery notes.
