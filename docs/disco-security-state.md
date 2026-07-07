# Disco — security state (single source of truth, 2026-07-07)

Snapshot at `disclaude/mega-campaign` @ `113f67c9`. This file records everything
security-related: what is DONE (committed), what is PARKED, what was DEFERRED
during the primitive sprint, and the gate that currently protects the tree.

Companion detail: `docs/disco-security-fix-campaign.md` (the wave-by-wave campaign
log + resume playbook).

---

## 1. Done — committed and gated

| Wave | Scope | Commit |
|------|-------|--------|
| S-W1 | Authentication + CORS + owner-scoping (the keystone) | `e028d2ac` |
| S-W2 | Secret-ref resolution + egress origin-approval chokepoint | `2408e40f` |
| S-W-Pi | Removed the Pi integration (attack-surface reduction) | (in campaign log) |
| S-W3 | Host-execution cluster / gVisor-bypass floor (rm-root floor, in-sandbox DoD, backend allowlist, env hygiene, session hygiene) + 2 adversarial review rounds | `1b762e3f` |
| — | Origin-approval ledger: surface the silently-dropped entries | `8157745b` |

These are live on the branch and covered by tests + the four fitness gates.

The reusable building blocks S-W2 left behind (used by the whole platform, and
the intended substrate for generated-app outbound calls):
- `disco.core.host_egress` — `guarded_request` / `guarded_get` (public-IP-only,
  redirect revalidation, host allowlist).
- `disco.core.origin_approvals` — `OriginApprovalStore.is_approved(url, purpose, ref)`.
- `disco.core.llm.secret_refs` — `resolve_provider_secret`, `secret_ref_allowed_for_origin`.

---

## 2. Parked waves (not started — the campaign's remaining hardening)

| Wave | Scope |
|------|-------|
| S-W4 | MCP approval integrity |
| S-W5 | Isolation + resource caps |
| S-W6 | Output sinks + share + low-severity cluster |

Resume playbook lives in `docs/disco-security-fix-campaign.md` (rebase notes +
the pi-kernel leftover). These are a prerequisite for any public/hardened release.

---

## 3. Deferred during the primitive sprint (design written, code not built)

The primitive framework was built so security-critical code is Disco-authored and
gated — but the actual security fills below were intentionally NOT written this
sprint. Each has a design/spec doc so the later work is fill-in-the-blanks, not a
rebuild.

### 3a. Host-service bus — the auth surface
- **What's built:** the registry + dispatcher (`disco.core.host_services`,
  commit `2c8e56fd`) with a zero-dependency `svc.ping` reference and NO auth.
- **What's deferred:** the bus endpoint that exposes it to generated apps needs a
  per-app, conversation-bound bearer (minted host-side, injected as a Worker env
  var, never in the app tree). `call_host_service` MUST NOT be exposed to the
  sandbox without that layer.
- **Design input:** `docs/wo-a2-host-bus-design-notes.md` (committed) — token
  shape/TTL/revocation, the agent-server host surface, the sandbox→host
  reachability constraints, the open questions.

### 3b. Scoped-credential / quota plane (was WO-A4)
- **Deferred entirely.** Per-app token minting/rotation/scope + per-app usage
  accounting, quota, and rate-limiting at the bus. Prerequisite input is 3a's v0
  bearer. No code written.

### 3c. Two fail-closed scaffolds (built, unmerged, security fill documented)
Built as `tier="template_only"` with `verify=None` so they are FAIL-CLOSED (see §4).
Left in worktree branches — NOT registered, NOT in the tree.

| Scaffold | Branch | Security-fill spec |
|----------|--------|--------------------|
| Payment checkout seam | `disclaude/f41-stripe-seam` | `docs/wo-f41-stripe-security-spec.md` (in that branch) |
| Webhook endpoint seam | `disclaude/f33-webhook-seam` | `docs/wo-f33-webhook-security-spec.md` (in that branch) |

The specs enumerate exactly what the security session must build to flip each from
`verify=None` to a real harness: host-side secret custody, signature verification,
idempotency-in-one-transaction, SSRF/rebind rejection (all composing the §1 S-W2
chokepoints), plus the adversarial harness each must pass.

---

## 4. The gate protecting the tree right now

WO-A3 (`4cbaa218`) wired a **fail-closed rule** into the finish gate: any primitive
applied to an app that is registered `tier="template_only"` with `verify=None`
FAILS verification — "cannot ship unverified." Provenance records in
`.disco/primitives/*.json` tell the gate which primitives an app uses.

Consequence: even if the two §3c scaffolds were merged today, an app that added
them could not pass the finish gate until their verify harness is written. This is
the mechanism that lets security-critical scaffolds exist in the catalog without
becoming a false affordance — they are enforced-incomplete, not silently broken.

The shipped primitives (`form`, `seo`, `collection`) are `tier="fillable"` — their
security-critical surface is a thin hardening layer, not the core value, so they
ship with real `verify` hooks (or `None`-and-fallback for the base scaffolds) and
are not gated closed. The hardening layers themselves (e.g. form spam/captcha) are
NOT built and are a separate security-classed item.

---

## 5. Per-primitive adversarial harnesses (the template_only set)

Every future `template_only` primitive ships a real exploit-style harness as its
build gate (from `docs/disco-builder-primitives-plan.md` §7). None of these
harnesses are built yet; the framework hook (§4) is what they plug into.

| Primitive | Failure = | Build-gate harness |
|-----------|-----------|--------------------|
| Multi-tenancy / RLS | cross-tenant breach | 2-tenant cross-read FAILS build |
| Payment webhooks | double-fulfill / spoof | replay + forged-sig + idempotency-in-txn |
| Uploads | RCE / malware / DoS | magic-byte + size + polyglot + /dev/zero |
| Outbound webhooks/connectors | SSRF | internal-IP + DNS-rebind rejection |
| Secrets vault | key leak | hardcoded-credential lint fails build |
| Egress (email/AI) | exfiltration | host-mediated + origin-approved (S-W2 done) |
| Auto-admin | privilege escalation | admin respects RLS/tenant isolation |
| Runtime | tenant escape | long-lived multi-tenant isolation under load |

---

## 6. Packaging (Epic P) — the one open security item in the deploy path

- The compose `agent-server` mounts `/var/run/docker.sock` (root-equivalent on the
  host). Fine for a trusted single-user box; **unacceptable as the default others
  inherit.** P3/P4 must make the isolated `runsc`/gVisor backend the documented
  default and gate the docker.sock/process path behind an explicit opt-in. This
  couples packaging to the parked isolation work (S-W3 done / S-W5 parked).
- The `process` sandbox backend is dev-only and already fail-closed in prod
  (`preflight_build_sandbox_backend`, S-W3).
- Host-specific defaults to scrub before publishing (P3): `workspace_root`,
  `podman_url` in `core/llm/config.py`.

---

## 7. One-line summary

Auth, secret custody, egress chokepoint, and the host-execution floor are DONE and
gated. The isolation/MCP-approval/output-sink waves (W4/W5/W6) are PARKED. The
credential/quota plane and the two payment/webhook scaffolds' security fills are
DEFERRED with specs written. The framework's fail-closed gate keeps anything
security-critical from shipping unverified. Packaging's one blocker is flipping the
sandbox default off the root-equivalent socket.
