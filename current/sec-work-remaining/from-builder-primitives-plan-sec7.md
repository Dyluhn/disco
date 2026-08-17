# Security tiers — the template-only set + its adversarial harnesses

> Extracted from the now-archived builder-primitives plan §7 (segregation 2026-07-06).
> This is forward-looking security work:
> the build-gate harness each template-only primitive must ship before it can be
> registered. The historical catalog rows referred to RLS, payments, uploads,
> and webhooks; this file owns their
> **adversarial harness contracts**.

These primitives are **LLM-FORBIDDEN** (Disco generates the code) and each ships a real
exploit-style harness as its build gate, inheriting the security campaign's discipline:

| Primitive | Failure = | Build-gate harness |
|-----------|-----------|--------------------|
| 2.1 RLS / multi-tenancy | cross-tenant breach | 2-tenant cross-read FAILS build |
| 4.1 payment webhooks | double-fulfill / spoof | replay + forged-sig + idempotency-in-txn |
| 1.2/3.4 uploads | RCE / malware / DoS | magic-byte + size + polyglot + /dev/zero |
| 3.3/6.6 outbound webhooks/connectors | SSRF | internal-IP + DNS-rebind rejection |
| 2.2 secrets | key leak | hardcoded-credential lint fails build |
| 3.2/5.1 egress (email/AI) | exfiltration | host-mediated + origin-approved (F-D done) |
| 6.3 auto-admin | privilege escalation | admin respects RLS/tenant isolation |
| 7.1 runtime | tenant escape | long-lived multi-tenant isolation under load |

## Labor routing (from plan §10.4 / §10.5 — security-classed, NOT Fable)

The security half of the framework's credential plane and every template-only adversarial
harness above were historically treated as **security-classed work**. This file records
that completed campaign; it is not current model-routing policy:

- **WO-A2.2 auth slice** — the bus per-app bearer (conversation-bound v0); adversarial review (codex xhigh).
- **WO-A4 security half** — token format/minting/rotation/scope model for per-app credentials + threat model + codex adversarial pass. (The *feature* half — usage accounting, quota, 429/retry-after, per-service rate limits — is Fable-safe.)
- **Every template_only adversarial harness** in the §7 table above.
- **S-W4/S-W5/S-W6 done** (see `disco-security-fix-campaign.md`).
- **Epic P3/P4 `docker.sock` flip** (host-root-equivalent default → scoped).
