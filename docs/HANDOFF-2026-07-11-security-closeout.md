# HANDOFF — F3.3, secure packaging defaults, and WO-A4 (2026-07-11)

## Integrated state

The three close-out items are implemented on local `disclaude/mega-campaign`.
Nothing in this close-out has been pushed yet; `origin/disclaude/mega-campaign`
still points to `426786d4` until the operator authorizes the outward step.

Principal integration commits:

| Scope | Commit |
| --- | --- |
| Rootless-Podman packaging default | `f01a0635` |
| F3.3 generic webhook fill | `ce349096` |
| WO-A4 credential/quota plane | `a0d5eabd` |
| Fake-runtime compatibility found by the full suite | `11ee5859` |
| Versioned `a4v1` Worker-shim compatibility found by live Stripe proof | `7614dc22` |

## F3.3 generic webhooks

- The primitive remains Disco-owned `template_only`; it now has a deterministic
  trusted-tree verifier and mandatory `webhook.security.v1` live verifier.
- Inbound routes verify a timestamped endpoint-bound HMAC with
  `crypto.subtle.verify` before parsing, then atomically deduplicate the provider
  event and durable generic effect in D1.
- Outbound delivery requires the token's exact app scope, an enabled host-owned
  endpoint/event configuration, a strong host secret, signed origin approval,
  and `guarded_request` public-IP/peer/redirect enforcement.
- Cloudflare deployment snapshots and rechecks configuration, secrets, approvals,
  event sets, trusted code, public bus wiring, and token-store availability. The
  Worker is latched at `WEBHOOK_RUNTIME_READY=0` until bindings and token rotation
  complete; failures restore `0` and revoke unfinished candidates.
- Combined Stripe/webhook apps receive one union-scoped token and enable webhooks
  only after Stripe readiness succeeds.
- The real emitted-Worker verifier proves forged/stale rejection, replay
  idempotency, metadata-IP and controlled DNS-rebind refusal, and zero secret
  material in the tree, bundle, logs, or local state.
- Firefox evidence: `docs/evidence/security/f33-webhook-firefox.png`.

## Packaging

- Compose defaults to the current user's local rootless Podman socket.
- Docker's root-equivalent socket and the process backend require explicit
  operator/dev opt-in.
- gVisor/runsc remains an optional stronger tier, not the WSL2-incompatible
  default.
- Private homelab and `/opt/sandbox/workspaces` defaults were removed from both
  configuration dataclasses.

## WO-A4 scoped credentials and quotas

- New bearers use `a4v1.<selector>.<verifier>`; only the verifier digest is
  persisted. Existing active `a2v0` rows migrate and remain valid without prefix
  substitution. Worker shims accept exactly those two versions.
- Rotation generations allocate transactionally. Finishing a candidate can revoke
  only strictly older generations in its exact owner/conversation/app tuple, so an
  older broad token cannot race a newer narrowed token and restore scope.
- App aggregate and exact-service fixed-window quotas cover requests, input tokens,
  output tokens, and combined tokens. Admission is an atomic SQLite reservation;
  denial is `429` with integer `Retry-After`.
- Authenticated malformed, unknown, and out-of-scope bus traffic also consumes the
  request quota. Schema rejection settles zero tokens.
- Dispatch is marked before downstream work. Exact successful usage reconciles the
  estimate; cancellation, timeout, handler failure, missing provider usage, or a
  post-dispatch crash retains the conservative estimate. Pre-dispatch crashes retain
  the request but release token capacity. Settled rows are incrementally pruned after
  the maximum accounting window.
- The owner-only app-server API configures aggregate or exact dotted-service limits
  and returns safe effective status/usage without credential material.
- `ai.chat` is the first metered service: bounded messages/output, no caller model,
  tools, provider fields, or system-role replacement; host routing is bound to the
  authenticated conversation. The bus integration is proven with host-minted exact
  `ai.chat` scope. No new generated-app UI/primitive was invented as part of A4.

## Adversarial review closures

The orchestrator and an independent subagent review produced and closed these
concrete findings:

- endpoint event-type injection on outbound webhooks;
- partially active webhook deployment after a binding/rotation failure;
- malformed persisted credential rows escaping authentication as server errors;
- older credential generations revoking newer narrowed candidates;
- cancellation/timeouts and unknown provider usage erasing token reservations;
- authenticated malformed traffic bypassing request limits;
- post-dispatch crash recovery reopening token spend;
- unbounded quota-row retention;
- `a4v1` credentials rejected by the existing generated Worker shims.

## Verification evidence

- Full Python non-integration suite: exit `0` on the integrated tree.
- F3.3 real emitted-Worker exploit verifier: pass, four required checks.
- F4.1 real Stripe verifier: pass, five required checks (rerun after the `a4v1`
  Worker-shim correction).
- Four fitness gates: architecture budget, import contracts, generated diagram,
  and basedpyright (`0 errors, 0 warnings, 0 notes`) pass.
- Harness gates: `make contract`, `make fuzz`, and `make fault` pass.
- Frontend: 144 files / 968 tests pass; TypeScript build check and Vite production
  build pass.
- Focused WO-A4 adversarial suites cover owner/app isolation, exact service plus
  aggregate limits, 429/Retry-After, malformed traffic, exact usage reconciliation,
  concurrency, cancellation/timeout/crash accounting, corrupt credential rows, and
  rotation races.
- Focused Ruff over the new and security-critical close-out modules, plus
  `git diff --check`, passes. `make lint` still invokes a
  repository-wide Ruff run with the pre-existing ~1,100-finding baseline documented
  in the security SSOT; it exits nonzero without any new changed-file finding. No
  lint rule or assertion was weakened and unrelated baseline files were not rewritten.

## Deliberately deferred

The separate per-primitive roadmap remains out of scope: multi-tenancy/RLS,
uploads/blob storage, secrets vault, transactional email, WAF/rate-limit primitive,
and auto-admin. WO-A3 continues to fail closed for any `template_only` primitive
without its real deterministic and live harness.
