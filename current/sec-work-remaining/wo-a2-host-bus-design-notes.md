# WO-A2 host-service bus — design notes for the A2.2 security session

Input doc for the security-classed A2.2 slice (per plan §10.2: NOT Fable-authored,
gets codex adversarial review). A2.1 (this worktree, branch `disclaude/wo-a21-hostsvc`)
shipped the registry + dispatcher: `current/packages/core/src/disco/core/host_services.py`.
`call_host_service` performs NO auth by design — everything below is what the bus
endpoint must decide before exposing it to a sandbox.

## What exists after A2.1

- `HostServiceDefinition(name, handler, description, payload_schema)` registry with
  the primitives-registry discipline (duplicate hard error, same-object idempotent).
- `call_host_service(name, payload, ctx)` — strict resolve (`UnknownHostServiceError`
  → 404), pydantic payload validation (`HostServicePayloadError` → 422), then handler.
- `HostServiceContext(secret_store, approvals, allow_hosts)` — exactly the inputs the
  S-W2 chokepoints take (`resolve_provider_secret`, `OriginApprovalStore.is_approved`,
  `guarded_request(allow_hosts=…)`). Handlers compose those; no new enforcement.
- `svc.ping` — echo reference service, zero security surface, for end-to-end proof.

## Decisions the A2.2 session owns (deliberately NOT made in A2.1)

1. **Per-app bearer shape (v0 = conversation-bound).** Plan §10.2 locks the v0
   semantics: the token binds calls to the owning conversation; minting/scopes/
   rotation/quota generalize in WO-A4. Open: token format (opaque random vs signed
   claims), lifetime (conversation lifetime vs TTL+re-mint on wake), storage of the
   mint-side record (event log? runtime map?), and constant-time comparison at the
   endpoint. HMAC signing under the SecretStore master secret would mirror the
   `origin_approvals` ledger pattern, but that choice is A2.2's.
2. **Injection point: Worker env var, never the tree.** The generated Worker reads
   the token from its environment (wrangler `.dev.vars` in the sandbox preview);
   `.dev.vars` must be written by the HOST at sandbox provision/wake time and must
   never be emitted by the generator or readable via any tree-export path. A2.4's
   acceptance grep (whole tree + built bundle contains no secret) must also cover
   the token. Open: is `.dev.vars` visible to model-driven file reads inside the
   sandbox? If yes, decide whether conversation-binding makes that acceptable
   (the model already acts for that conversation) — write the answer down.
3. **Which surface hosts `POST /_disco/svc/{name}`.** Proposal: the agent-server
   ASGI app (`current/packages/agent-server/src/disco/agent_server/app.py`, :8000) — it
   already terminates sandbox-adjacent traffic (preview proxy in `host_proxy.py`)
   and owns per-conversation runtime state needed to check a conversation-bound
   token and build the right `HostServiceContext`. app-server (:8800) is the
   current/frontend/admin gateway and never sees the sandbox. A2.2 must also decide the
   bind address the sandbox uses (see reachability) and confirm the route is
   excluded from any auth middleware meant for browser sessions, replacing it with
   the bearer check.
4. **Sandbox → host reachability.** The bus hop is sandbox-egress → host-ingress,
   the REVERSE of everything existing (host_proxy is host → sandbox). Constraints
   from the gVisor/Docker networking findings (plan §10.2 A2.2 line; the egress-proxy
   memory recap in `archive/docs/release-execution-plan.md` ~line 238: "no NIC
   hot-plug, dead embedded DNS, reach-by-IP"): the runsc sandbox cannot resolve
   compose/docker service names (embedded DNS dead) and cannot gain interfaces
   post-start, so the Worker must be given a literal IP:port (or host-gateway IP)
   in an env var at provision time — e.g. `DISCO_SVC_BUS=http://<host-ip>:8000`.
   Open: which IP is stable per backend (local podman vs VM-201 gVisor), whether
   the sandbox egress allowlist/proxy must be widened to permit that one host:port,
   and whether the bus should require TLS or treat the sandbox-host link as a
   trusted segment (VM 201 is a separate machine — plaintext crosses a real wire).
5. **Rate limits / quota: deferred to WO-A4** (per plan §10.4, feature half:
   per-app accounting, 429 + retry-after at the bus, per-service limits). A2.2
   should only leave an obvious seam (e.g. a per-token counter hook) — not build it.

## Open questions (rolled up)

- Token format + TTL + storage + revocation on conversation end (Q1).
- `.dev.vars` visibility to the model inside the sandbox; is conversation-binding
  a sufficient answer? (Q2)
- Stable host IP per sandbox backend; allowlist changes needed for the reverse hop;
  TLS-or-trusted-segment for the VM-201 case (Q4).
- Does `/_disco/svc/{name}` need its own body-size cap + timeout budget distinct
  from the preview proxy's? (bus hardening, likely yes — decide numbers in A2.2)
- Error-shape contract for the Worker client shim (A2.3): map
  `UnknownHostServiceError`→404, `HostServicePayloadError`→422, auth failure→401/403,
  handler `{"ok": false}` results pass through as 200 — confirm or amend.
