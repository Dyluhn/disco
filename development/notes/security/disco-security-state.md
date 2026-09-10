# Disco — security state (single source of truth, 2026-07-11)

Code snapshot at `disclaude/mega-campaign` @ `7614dc22`. This file records everything
security-related: what is DONE (committed), what remains separately DEFERRED, and the
gate that currently protects the tree. The W1–W6 close-out was recorded at `17869bdb`;
the later A2.2/A2.3, F4.1, F3.3, packaging, and A4 work are included below.

Companion detail: `current/sec-work-remaining/disco-security-fix-campaign.md` (the wave-by-wave campaign
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
| F3.3 | Generic inbound/outbound webhook security fill, readiness lifecycle, mandatory live exploit proof | `ce349096` |
| Epic P | Local rootless-Podman default, explicit Docker/process opt-in, portable OSS defaults | `f01a0635` |
| A4 | Versioned per-app credentials, monotonic rotation, exact scopes, request/token quotas, 429/Retry-After, metered `ai.chat` | `a0d5eabd` |

These are live on the branch and covered by full package/frontend suites and the
contract/fuzz/fault gates. Focused Ruff over the new and security-critical close-out
modules is clean. Repository-wide Ruff still has roughly 1,100 inherited findings;
no rule or assertion was weakened.

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
`current/docs/HANDOFF-2026-07-11-stripe.md`.

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

### 3b. Scoped-credential / quota plane (WO-A4)
- **Complete.** New credentials use the versioned `a4v1` selector/verifier format;
  persisted A2 credentials migrate without prefix confusion. Rotation allocates
  monotonically and can revoke only strictly older generations in the exact
  owner/conversation/app tuple. The bus accounts requests plus estimated/exact
  input/output tokens against app aggregate and exact-service fixed windows,
  returns `429` with `Retry-After`, retains conservative spend on cancellation,
  timeout, unknown provider usage, or post-dispatch crash, and prunes settled
  rows after the maximum accounting window. `ai.chat` is the first bounded,
  tool-free, model-selection-free metered service.

### 3c. Security scaffolds graduated

F4.1 graduated at `8f356c1e`; F3.3 graduated at `ce349096`. Both retain the
`template_only` ownership boundary and now have deterministic verification plus
mandatory live exploit runners. Neither gate was weakened and neither shipped as
a false affordance.

---

## 4. The gate protecting the tree right now

WO-A3 (`4cbaa218`) wired a **fail-closed rule** into the finish gate: any primitive
applied to an app that is registered `tier="template_only"` with `verify=None`
FAILS verification — "cannot ship unverified." Provenance records in
`.disco/primitives/*.json` tell the gate which primitives an app uses.

Consequence: a future security-critical scaffold still cannot pass the finish
gate until its verify harness is written. Stripe and generic webhooks pass because
their real deterministic and live exploit checks are now present; the rule itself
is unchanged.

The shipped primitives (`form`, `seo`, `collection`) are `tier="fillable"` — their
security-critical surface is a thin hardening layer, not the core value, so they
ship with real `verify` hooks (or `None`-and-fallback for the base scaffolds) and
are not gated closed. The hardening layers themselves (e.g. form spam/captcha) are
NOT built and are a separate security-classed item.

---

## 5. Per-primitive adversarial harnesses (the template_only set)

Every future `template_only` primitive ships a real exploit-style harness as its
build gate. Stripe payment
webhooks and generic outbound/inbound webhooks now supply the first two live
harnesses; the remaining roadmap stays separately deferred.

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
host-service bus, Stripe and generic webhook fills, the scoped credential/quota plane,
and secure packaging defaults are DONE and gated. The framework's fail-closed gate
keeps the separately deferred primitive roadmap from shipping unverified. Packaging
defaults to local rootless Podman, with gVisor available as an opt-in stronger tier.
