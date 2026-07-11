# Disco — security state (single source of truth, 2026-07-11)

Code snapshot at `disclaude/mega-campaign` @ `83c593a6`. This file records everything
security-related: what is DONE (committed), what remains separately DEFERRED, and the
gate that currently protects the tree. The W1–W6 close-out was recorded at `17869bdb`;
the later A2.2/A2.3 and F4.1 Stripe work are included below.

Companion detail: `sec-work-remaining/disco-security-fix-campaign.md` (the wave-by-wave campaign
log + close-out evidence).

---

## 1. Done — committed and gated

| Wave | Scope | Commit |
|------|-------|--------|
| S-W1 | Authentication + CORS + owner-scoping (the keystone) | `e028d2ac` |
| S-W2 | Secret-ref resolution + egress origin-approval chokepoint | `17c47761` (tree-identical replacement for archived `2408e40f`) |
| S-W-Pi | Removed the Pi integration (attack-surface reduction) | `76b4e397` |
| S-W3 | Host-execution cluster / gVisor-bypass floor (rm-root floor, in-sandbox DoD, backend allowlist, env hygiene, session hygiene) + 2 adversarial review rounds | `1b762e3f` |
| S-W4 | MCP pre-connect config approval, first-use/schema-drift approval, honest host scope, configured risk-tier gating | `212f6e89` |
| S-W5 | Per-surface isolated egress, real workspace/resource caps, bounded output/event/WS boundaries, fail-closed DoD, argv-safe preview | `10433330` |
| S-W6 | Immutable share projections, audited storage-root picker, live WS redaction, XLSX injection escaping, env hygiene, generic-provider origin pinning | `17869bdb` |
| — | Origin-approval ledger: surface silently-dropped entries + fail-closed/warn-once regression | `8157745b`, `d6651e7e` |
| A2.2 | Authenticated per-app host-service bus + narrow sandbox capability relay | `cd57f830`, `205d8c6b` |
| A2.3 | Hardened generated-Worker host-service client shim | `ef19bac6` |
| F4.1 | Stripe key custody, verified Worker webhooks, entitlement transaction, deploy lifecycle, mandatory live exploit proof | `8f356c1e` |

These are live on the branch and covered by full package/frontend suites, the
contract/fuzz/fault gates, and clean lint on every changed file. Repository-wide
Ruff still has 1,100 inherited findings; no rule or assertion was weakened.

The reusable building blocks S-W2 left behind (used by the whole platform, and
the intended substrate for generated-app outbound calls):
- `disco.core.host_egress` — `guarded_request` / `guarded_get` (public-IP-only,
  redirect revalidation, host allowlist).
- `disco.core.origin_approvals` — `OriginApprovalStore.is_approved(url, purpose, ref)`.
- `disco.core.llm.secret_refs` — `resolve_provider_secret`, `secret_ref_allowed_for_origin`.

---

## 2. Campaign implementation complete

There are no parked implementation waves in S-W1 through S-W6. The campaign
close-out re-read found no surviving path in the changed surfaces: public share
tokens remain bounded after later events/title changes; storage browsing is
admin+CSRF gated and confined to `$HOME`, `/mnt`, and `/media`; live WS frames
use the redaction seam; spreadsheet injection strings are inert; and a poisoned
generic-provider URL cannot receive the stored key.

The Codex half of the requested post-campaign round-pair is complete. An independent
Opus pass was not available in this execution environment, so that extra assurance
pass remains explicitly outstanding; it is not an unimplemented code wave. A2.2/A2.3
and F4.1 were completed afterward; their verification evidence is recorded in
`docs/HANDOFF-2026-07-11-stripe.md`.

---

## 3. Deferred during the primitive sprint (design written, code not built)

The primitive framework was built so security-critical code is Disco-authored and
gated — but the actual security fills below were intentionally NOT written this
sprint. Each has a design/spec doc so the later work is fill-in-the-blanks, not a
rebuild.

### 3a. Host-service bus — the auth surface
- **Complete:** A2.2 now supplies the per-app, conversation-bound bearer, host-side
  mint/revocation, authenticated bus endpoint, and narrow relay. A2.3 emits the hardened
  Worker client shim. The original design notes remain useful context, but this is no
  longer a deferred implementation wave.

### 3b. Scoped-credential / quota plane (was WO-A4)
- **Deferred entirely.** Per-app token minting/rotation/scope + per-app usage
  accounting, quota, and rate-limiting at the bus. Prerequisite input is 3a's v0
  bearer. No code written.

### 3c. Remaining fail-closed scaffold

F4.1 graduated from its original fail-closed scaffold into the real Stripe primitive at
`8f356c1e`. The generic F3.3 webhook scaffold remains intentionally unmerged and
unregistered with `verify=None`.

| Scaffold | Branch (commit) | Security-fill spec |
|----------|-----------------|--------------------|
| Webhook endpoint seam | `disclaude/f33-webhook-seam` (`ec622888`) | `docs/wo-f33-webhook-security-spec.md` (on that branch) |

The F3.3 spec enumerates the host-side secret custody, signature verification,
idempotency, SSRF/rebind rejection, and real adversarial harness required to graduate
it. Do not merge it as a false affordance or weaken the fail-closed gate to make it ship.

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

## 6. Packaging (Epic P) — deploy-path security close-out

- **Default: local rootless Podman.** Compose mounts the current user's Podman
  socket (`$XDG_RUNTIME_DIR/podman/podman.sock`, with the conventional
  `/run/user/1000` fallback), never `/var/run/docker.sock` implicitly. This local
  isolation tier works on Linux and WSL2 without granting the agent-server root
  control of the host container daemon.
- **Optional stronger tier: gVisor/runsc.** gVisor remains separately deployable
  and opt-in via `DISCO_LOCAL_RUNTIME=runsc`; it is intentionally not the default
  because it does not run cleanly in common WSL2 environments.
- Docker's root-equivalent `/var/run/docker.sock` is available only through an
  explicit `DISCO_SANDBOX_SOCKET` override. The unisolated `process` backend is
  likewise dev-only and already fail-closed unless
  `DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1` is explicit
  (`preflight_build_sandbox_backend`, S-W3).
- OSS defaults no longer contain a private homelab address or the host-specific
  `/opt/sandbox/workspaces` path; Podman and workspace defaults are local and
  portable.

---

## 7. One-line summary

Auth, secret custody, egress chokepoint, the host-execution floor, MCP approval
integrity, isolation/resource bounds, output-sink/share hardening, the authenticated
host-service bus, and the Stripe security fill are DONE and gated. The credential/quota
plane and generic F3.3 webhook fill remain deferred. The framework's fail-closed gate
keeps anything security-critical from shipping unverified. Packaging now defaults to
local rootless Podman, with gVisor available as an opt-in stronger tier.
