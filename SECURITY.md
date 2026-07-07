# Security posture & threat model — Disco

This document describes what disco v1 does and does **not** protect against, the
trust boundaries it enforces, and the honest limits of each control. It is written to be
trusted literally: where a control is partial, aspirational, or off by default, that is
stated. Every substantive claim cites the file (and line, where load-bearing) that
implements it, so a maintainer can verify it.

The operator-facing security narrative lives in
[`docs/archive/self-host.md#security`](docs/archive/self-host.md) (archived — some of its
line-level claims predate the security waves below). The **current internal security
state** — what is done, parked, and deferred — is `docs/disco-security-state.md`; every
status claim in this file traces to it. If documents disagree, treat it as a bug and
reconcile.

---

## 1. Scope & threat model

**v1 is a single-operator, single-tenant, locally-bound application — with an
authentication layer since security wave S-W1 (commit `e028d2ac`).**

- **Authenticated sessions (S-W1).** Both servers require an HttpOnly `SameSite=Strict`
  session cookie (`disco_session`); state-changing requests additionally require a CSRF
  header (`X-Disco-CSRF`). The first session is minted by exchanging a **one-time pairing
  token** (logged at server startup) at a loopback-only mint endpoint
  (`POST /api/auth/mint`). CORS is pinned to an explicit frontend-origin allowlist, and
  WebSocket handshakes are Origin-checked and cookie-authenticated. Routes are classed
  **admin vs authenticated**: settings, secrets, MCP, skills and other install-wide state
  are admin-only. Ownership is derived from the session — client-supplied `owner_id`
  params are ignored — and every conversation route is owner-scoped.
  (`packages/app-server/src/disco/app_server/auth.py`,
  `packages/agent-server/src/disco/agent_server/auth.py`,
  `packages/core/src/disco/core/auth.py`.)
- **Still bind loopback.** The defaults bind every published port to `127.0.0.1`
  (`compose.yaml:42,79,106`; `.env.example:6-8`), and there is no TLS. Auth is a real
  gate against drive-by and cross-owner access, but the parked hardening waves (see
  "Hardening status" below) are prerequisites for any public/hardened deployment — do not
  set `PMX_BIND=0.0.0.0` without your own TLS (a reverse proxy) in front.
- **The operator is trusted.** v1 ships one operator, whose session is admin. Non-admin
  sessions cannot mutate install-wide state (provider config, secrets, MCP, skills), but
  the single local operator holds an admin session.
- **Not multi-tenant.** Sandbox instances carry an `owner_id`/`conversation_id` and are never
  shared across owners (`tool-sandbox-contract.md:79`), but v1 ships exactly one owner. Do
  not treat this as a tenant-isolation guarantee.

### Hardening status (done vs parked)

Transcribed from `docs/disco-security-state.md` (the single source of truth for security
status); wave-by-wave detail + resume playbook: `docs/disco-security-fix-campaign.md`.

| Wave | Scope | Status |
|---|---|---|
| S-W1 | Authentication + CORS + owner-scoping (the keystone) | DONE (`e028d2ac`) |
| S-W2 | Secret-ref resolution + egress origin-approval chokepoint | DONE (`2408e40f`) |
| S-W-Pi | Removed the Pi integration (attack-surface reduction) | DONE (`76b4e397`) |
| S-W3 | Host-execution cluster / gVisor-bypass floor (rm-root floor, in-sandbox DoD, backend allowlist, env hygiene, session hygiene) | DONE (`1b762e3f`) |
| S-W4 | MCP approval integrity | **PARKED** |
| S-W5 | Isolation + resource caps | **PARKED** |
| S-W6 | Output sinks + share + low-severity cluster | **PARKED** |

The parked waves are a prerequisite for any public/hardened release. One further open
item in the deploy path: the compose `agent-server` still mounts `/var/run/docker.sock`
(root-equivalent on the host) **by default** — fine for a trusted single-user box,
unacceptable as a default others inherit. Making the isolated gVisor (`runsc`) backend
the documented default, with the docker-socket/process path behind an explicit opt-in,
is open packaging work (`docs/disco-security-state.md` §6).

### What this DOES try to protect against

| Threat | Where it comes from | Primary control |
|---|---|---|
| Malicious / buggy **agent-generated code** | The agent writes and runs code, shell, and a browser | Sandbox isolation (§3) + hard-deny floor (§6) |
| **Prompt injection** via fetched web content | A hostile page tries to hijack the agent | Quarantine + untrusted-data channel (§5) |
| Untrusted **build output rendered in the browser** | A shared/imported run's HTML reaches your browser | `<iframe sandbox>` hardening (§5) |
| **Secret theft** by agent-run code | Code in the sandbox tries to read API keys | Secrets never enter the sandbox (§2, §7) |

### What it explicitly does NOT protect against

- Deliberate network exposure without TLS/reverse-proxy hardening. S-W1 auth gates the
  API, but the parked hardening waves (W4/W5/W6) are prerequisites for a hardened public
  deployment (§1).
- A compromise of the **agent-server process** itself: on the `local`/Docker backend the
  mounted container socket makes that process root-equivalent on the host (§2, §3,
  `docs/archive/self-host.md:73-78`).
- A malicious operator.

---

## 2. Trust boundaries

| Boundary | Trusted side | Untrusted side | What crosses & how it's controlled |
|---|---|---|---|
| **operator ↔ server** | authenticated session | any other caller | S-W1 auth: HttpOnly session cookie + CSRF header on state-changing requests, pinned CORS, Origin-checked WS, admin-only route class for install-wide state, owner-scoped conversation routes (§1). Defaults still bind loopback (`compose.yaml:42,79,106`); no TLS. |
| **server ↔ sandbox** | agent-server (holds the socket) | code running in the sandbox | The agent-server spawns sibling sandbox containers through the host socket. The **sandbox containers do not get the socket** — the run mount is the workspace only (`gvisor.py:307`, `local.py:84`). No host env crosses in (`environment={}` — `gvisor.py:310`, `local.py:85`; `process.py:55-61`). |
| **sandbox ↔ internet** | the egress proxy / network policy | the sandbox guest | Three postures: sealed / filtered / open (§4). Enforced *outside* the guest, not by trusting in-guest config (`tool-sandbox-contract.md:36`, `egress_proxy.py:1-12`). |
| **agent-output ↔ browser** | your own live runs | shared/imported runs | Rendered in a sandboxed `<iframe>`; untrusted runs drop `allow-scripts` (§5, `ExecutionCanvas.tsx:401-403`). |

---

## 3. Sandbox isolation tiers

Where the agent runs its tools is operator-selectable (Settings → Sandbox, or `PMX_SANDBOX`).
Each tier is honest about its strength, and a **weaker tier is wired to a tighter
confirmation default** so it is never silently as permissive as a strong one
(`isolation.py:1-13,40-79`). The **default backend is `local`** (`SandboxSettings.backend
= "local"`, `packages/core/src/disco/core/llm/config.py:77`; the compose `agent-server`
leaves `PMX_SANDBOX` unset so the persisted setting wins, `compose.yaml:68-69`).

| Tier | Isolation actually provided | Adversarial-safe? | Confirmation default |
|---|---|---|---|
| `process` | **NONE.** Tools run as subprocesses in the agent-server's own container — no kernel/VM boundary. It *does* uphold two things: a clean minimal env (no host secret leaks in) and a workspace path-jail for file ops (`process.py:1-16,48-61`). Network egress is **not** actually blocked (`process.py:11-16`). Labeled "no isolation, dev only" in the UI (`docs/archive/self-host.md:99-101`). Since S-W3 the backend selector is a fail-closed allowlist and `process` is dev-only + fail-closed in production (`preflight_build_sandbox_backend`). | No | weakest (fallback gates at LOW / on UNKNOWN, `isolation.py:83-90`) |
| `local` (Docker/OCI, `runc`) | Container-grade only — **shared host kernel** (`local.py:1-18,35-38`). Plus the **socket tradeoff**: the agent-server mounts the host container socket, making that process **root-equivalent on the host** (`docs/archive/self-host.md:73-78`, `compose.yaml:72-75`). The agent's own code never touches the socket; this is a blast-radius concern for an *agent-server RCE*. | **No** | tighter — gate at MEDIUM, gate UNKNOWN (`isolation.py:68-78`) |
| `gvisor` (`runsc`, separate VM) | Strong: a user-space kernel intercepting syscalls, on a separate Docker host reached over (keyless Tailscale) SSH (`gvisor.py:1-12,46-53`). The only tier that enforces a **selective** egress allowlist (§4). | Yes | most permissive earned default — gate HIGH only, trust UNKNOWN (`isolation.py:49-56`) |
| `podman` (rootless, remote) | Container-grade (shared kernel) behind a host boundary; **real backend code but a stub in this environment** (the test VM was destroyed) — constructs but is not live-verified here (`runtime.py:256-260`). | No | gate HIGH, gate UNKNOWN (`isolation.py:57-67`) |

**Mitigations for the socket tradeoff** (from `docs/archive/self-host.md:80-95`, not duplicated here):
prefer a **rootless Podman socket** (an escape lands as your unprivileged user, not root);
keep ports on `127.0.0.1`; use `gvisor` on a separate VM for adversarial workloads. A
read-only socket mount is theater and a socket-proxy filters accidents, not attackers — the
docs say so and so does this file.

> **Do not over-read "sandbox".** On the default `local` tier the boundary is a shared-kernel
> container, and the agent-server that drives it is root-equivalent on the host. That is
> appropriate for *trusted local use*, not for running deliberately adversarial code. For
> adversarial code, use `gvisor` on a throwaway VM.

---

## 4. Network egress

Egress is resolved per-sandbox into one of three modes (`_container.py:38-67`):

| Mode | Meaning | Enforcement |
|---|---|---|
| `sealed` | No allowlist and no NETWORK capability → **no network at all** | `network_mode=none` (`gvisor.py:297`, `local.py:79`). The `SandboxSpec` default is sealed (`_container.py:38-41`). |
| `filtered` | Non-empty `egress_allow` → a per-connection **allowlisting proxy** | gVisor only: an internal no-NAT network whose only route out is a proxy sidecar that 403s any host the allowlist doesn't name (`gvisor.py:186-258`, `egress_proxy.py:1-24,39-56`). |
| `open` | NETWORK capability, no allowlist → deliberate raw egress (the whole internet) | `network_mode=bridge` (`gvisor.py:295`, `local.py:79`). |

**Honest caveats — the guarantee is tier-dependent (`tool-sandbox-contract.md:85-93`):**

- The **selective allowlist is a gVisor-only guarantee.** On `local`/`podman`, a `filtered`
  request **cannot** be enforced per-host, so it **fails safe to `sealed` (deny-all)** rather
  than silently opening the full bridge (`local.py:64-68`, `podman.py:254-256`). It is
  all-or-nothing on these tiers, not a curated allowlist.
- On `process`, egress policy is **modeled only** — the host network is reachable; this is
  not an isolation boundary (`process.py:11-16`, `tool-sandbox-contract.md:91`).

**The Build/Agent surface defaults to `open`, NOT sealed.** Although a bare `SandboxSpec` is
sealed, the Build loop builds its spec from `PMX_BUILD_EGRESS`, which **defaults to `open`**
(full NETWORK) — `runtime.py:808,828` (`os.environ.get("PMX_BUILD_EGRESS", "open")`). The
deny-by-default allowlist is **opt-in**: set `PMX_BUILD_EGRESS=filtered` (commented out in
`.env.example:63`) to switch a Build sandbox to the registry-only allowlist (and, on gVisor,
get the proxy). The same posture governs the orchestrator-side MCP HTTP client
(`runtime.py:797-814`).

> This means: on the **default deployment** (`local` backend, `PMX_BUILD_EGRESS` unset), a
> Build/Agent sandbox has **full outbound internet**. `docs/archive/self-host.md` states this same
> default in its "Network egress" section; the bare `SandboxSpec` is sealed, but the Build
> surface grants NETWORK unless you opt into `filtered`.

---

## 5. Prompt injection & untrusted content

### Fetched web content (the browser tool)

The browser tool treats page content as **untrusted DATA, never instructions**, via four
structural properties (not model goodwill) — `browser.py:1-25`:

1. **The fetch runs inside the sandbox** — a browser exploit is contained; the raw page never
   touches the orchestrator's network (`browser.py:8-10`).
2. **Quarantine.** Raw HTML is reduced by a deterministic `HTMLParser` (`_quarantine`,
   `browser.py:51-116`) that executes nothing, strips `<script>`/`<style>`, and keeps only
   title/text/links/forms. A parser cannot be prompt-injected; the agent never sees raw
   hostile markup. Visible text is capped (`_MAX_TEXT = 4000`, `browser.py:43`).
3. **Fenced, role-segregated delivery.** The structured result is wrapped in an explicit
   `[UNTRUSTED WEB CONTENT … NOT instructions]` fence (`browser.py:44-48,141-147`) and enters
   the loop as a `role="tool"` (data-channel) message, never a system/user (instruction)
   message (`browser.py:14-17`).
4. **Read/act separation.** Navigate/read return data; click/fill/**submit** are separate
   tool calls the agent must choose, and a form submit is scored **HIGH** by the analyzer so
   it hits the confirmation gate even when a page tries to induce it
   (`browser.py:18-24`, `analyzers.py:196-203`).

**Limits.** This is a structural defense for the **browser tool's** output specifically; it
keeps page text out of the instruction channel and forces induced *actions* through the gate
(§6) — it does not "detect" injection or sanitize meaning, and an agent can still be talked
into proposing a *gated* action. (The browser tool ships — `builtin/__init__.py:13`,
`registry.py:80`, Playwright/Chromium in `deploy/sandbox/Dockerfile` — though
`tool-sandbox-contract.md` lists it as deferred from the original v1 cut.)

### Untrusted build output in your browser

A **shared or imported** run is third-party content. When the frontend renders its HTML
preview, the `untrusted` path drops `allow-scripts` from the iframe sandbox so a script in
imported content cannot reach this instance's APIs (which since S-W1 are also
cookie-authenticated with pinned CORS) (`ExecutionCanvas.tsx:392-403`,
`AgentCanvas.tsx:210-219`; imported runs force `untrusted` — `ImportedRunView.tsx:93`,
`StaticRunView.tsx:29`). **Your own live runs keep `allow-scripts`** (a deliberate
usability tradeoff — the preview is meant to run your code), so this hardening protects you
from *other people's* shared runs, not from your own agent's output.

---

## 6. Action guards

Three layers gate what the agent may do, before execution (`engine.py:2869-2932`):

1. **Hard deny (non-negotiable floor).** A small set of catastrophic shell shapes —
   `mkfs`, raw-device writes, fork bombs, `rm -rf /` — are refused **outright, before** the
   confirmation gate. No approval, policy, or LLM can run them (`analyzers.py:92-111`;
   refused at `engine.py:2872-2888`, and again on the finish-verify path
   `engine.py:1668-1686`).
2. **Risk gate.** Every action is scored by a rule-based analyzer (a shell-command parser
   plus per-tool heuristics; over-flags by design — `analyzers.py:38-208`), optionally raised
   (never lowered) by an advisory LLM analyzer via a most-cautious ensemble
   (`analyzers.py:246-292`). The score is stamped into the action's audit meta
   (`engine.py:2896-2904`).
3. **Confirmation policy** decides whether a scored action pauses for human approval:
   - **Research surface → `NeverConfirm`** (read-only scope, no world-affecting tools —
     `runtime.py:759,999`).
   - **Build/Agent surface → `BlastRadiusConfirm`** (`runtime.py:963`). This policy
     **auto-approves in-sandbox actions** — the sandbox confinement is the argument, so even
     UNKNOWN/HIGH risk *in the box* runs without a prompt — and gates only **host-scope /
     unknown-scope** actions (via `ConfirmRisky` semantics) plus **any tool whose name
     contains `deploy`/`publish`/`release`**, which always gates because publishing leaves
     the blast radius (`policies.py:84-111`).

> **Be honest about what the gate does NOT do.** On the Build surface, an ordinary in-sandbox
> shell command — anything short of the hard-deny list — runs **without** a confirmation
> prompt. The protection there is the sandbox + the hard-deny floor + egress posture, not
> per-command human review. Human confirmation mainly fires for publish/deploy-class tools
> and for actions that escape the sandbox scope.

**S-W3 hardened this layer** (commit `1b762e3f`; `docs/disco-security-state.md` §1): the
hard-deny floor was rewritten around command-position analysis (catching `\rm`,
`command rm`, `sudo rm`, `bash -lc`, `find <root> -delete`, and `$(…)`-wrapped variants
without false-positiving on e.g. `echo rm -rf /`); plan/DoD `command` predicates now
execute **in the sandbox** instead of host-side `subprocess`; the sandbox backend selector
is a fail-closed allowlist (the `process` backend is dev-only and fail-closed in
production); and env/session hygiene closed the kernel-token argv leak. Details + honest
residuals: `docs/disco-security-fix-campaign.md` (Wave 3).

---

## 7. Secrets at rest

- **Encrypted at rest with Fernet** (AES-128-CBC + HMAC). The OpenRouter API key is the only
  stored secret today (`secrets.py:1-12,64-97`).
- **Key derivation.** The Fernet key derives from `DISCO_SECRET_KEY` (SHA-256 → urlsafe-b64).
  This is **not** a slow/salted KDF, so it is secure only with a **high-entropy** secret —
  use `openssl rand -base64 32`. A low-entropy secret is brute-forceable offline against the
  ciphertext; the store logs a one-time warning if `DISCO_SECRET_KEY` looks weak. (A
  work-factor KDF — Argon2id behind a versioned ciphertext format — is a planned upgrade.)
- **Plaintext never on disk (via the store).** Only ciphertext is persisted, to a file
  separate from the config catalogue so the config stays credential-free. At build time the
  decrypted key is overlaid into a *per-request copy* of the provider env in memory, never
  written and never mutating the live process env.
- **Owner-only file perms.** The secrets file is written `0600` and its directory `0700`
  (created via `os.open(..., 0o600)` + explicit `chmod`), so a co-tenant on a shared host
  cannot copy even the ciphertext.
- **Pick ONE key path.** A provider key can be supplied *either* through the encrypted store
  (UI-set) *or* as a plaintext env var (`DISCO_OPENROUTER_API_KEY` in your `0600` `.env` /
  `agent.env`). Both are fine on their own; keeping the SAME key in BOTH places means the
  plaintext copy defeats the encryption — prefer one.
- **Fails closed.** With no/wrong `DISCO_SECRET_KEY` the store reports `locked` and callers
  get `None` rather than a broken key.
- **Out of the project tree.** New installs write `~/.config/disco/secrets.json`, not the
  repo root, so anything granted read of the working dir cannot copy the ciphertext. In
  compose it lives in the `disco-data` volume (`compose.yaml:39-40,81`).
- **Kernel gateway is authenticated.** The in-sandbox Jupyter kernel gateway (arbitrary code
  execution) requires a per-sandbox auth token derived as
  `hmac_sha256(DISCO_SECRET_KEY, "kernel-gateway:{sandbox_id}")`, so even where its port is
  published it cannot be driven by an unauthenticated caller (`kernel.py`).
- **Pin your key.** If `PMX_SECRET_KEY` is blank it is auto-generated into the data volume;
  pin it in `.env` so your encrypted keys survive a volume rebuild
  (`.env.example:20-24`, `docs/archive/self-host.md:134-135`).
- **Secret-refs, not env names (S-W2, commit `2408e40f`).** Provider secrets resolve by
  secret-ref through `disco.core.llm.secret_refs` (`resolve_provider_secret`,
  `secret_ref_allowed_for_origin`) rather than by reading arbitrary host env-var names,
  and a secret-ref is only released to an origin it is approved for.
- **One guarded egress chokepoint (S-W2).** Host-side outbound fetches route through
  `disco.core.host_egress` (`guarded_request`/`guarded_get`: public-IP-only, redirect
  revalidation, host allowlist), gated by an operator origin-approval ledger
  (`disco.core.origin_approvals` — `OriginApprovalStore.is_approved(url, purpose, ref)`;
  approvals are minted via the admin-only `POST /api/security/approve-origin`). These are
  reusable building blocks used by the whole platform and the intended substrate for
  generated-app outbound calls (`docs/disco-security-state.md` §1).
- **Never inside the sandbox.** No secret, credential, or host env is present anywhere
  agent-run code can read it (`tool-sandbox-contract.md:35`; enforced by the clean-env
  container/subprocess construction cited in §2).

**Exposure corollary:** since S-W1 the API in front of these secrets is authenticated
(secrets routes are admin-only, state changes require CSRF), but there is still no TLS and
the parked hardening waves are incomplete. **Keep `DISCO_BIND=127.0.0.1`** unless you put
TLS (a reverse proxy) in front (`.env.example:5-8`).

---

## 8. Reporting a vulnerability

Disco is an early single-owner project and **has no formal vulnerability-disclosure
process, security contact, or SLA yet.** If you find a security issue, please report it
privately to the maintainer (e.g. via a private GitHub Security Advisory on the repository, or
direct contact) rather than opening a public issue with exploit detail. This section is a
placeholder and should be replaced with a real contact + policy before any public/multi-user
deployment.

---

## 9. Known limitations & non-goals (v1)

- **Auth shipped (S-W1), but the hardening campaign is incomplete.** Waves W4 (MCP
  approval integrity), W5 (isolation + resource caps), and W6 (output sinks + share +
  low-severity cluster) are **PARKED** — prerequisites for any public/hardened release
  (`docs/disco-security-state.md` §2). No TLS; single-operator; keep the loopback bind
  (`.env.example:5-8`).
- **The `local`/Docker socket mount is root-equivalent** on the host; a full agent-server
  compromise can own the machine — and it is still the compose **default**. Mitigate with
  a rootless Podman socket (`docs/archive/self-host.md:73-95`); making the isolated gVisor
  backend the documented default is open packaging work (`docs/disco-security-state.md` §6).
- **`process` backend = no isolation** and no real egress block — dev/try-out only
  (`process.py:1-16`).
- **Egress is `open` by default on the Build surface** (`PMX_BUILD_EGRESS` defaults to
  `open`, `runtime.py:808,828`). Selective allowlisting is gVisor-only and opt-in.
- **Single-owner; not tenant-isolated.** Every conversation is owner `"local"`.
- **`podman` remote backend is a stub in this environment** — not live-verified here
  (`runtime.py:256-260`).
- **Your own live previews run scripts** (`allow-scripts`); only shared/imported runs are
  script-hardened (`ExecutionCanvas.tsx:401-403`).
- **The risk gate auto-approves in-sandbox actions** on the Build surface; it is not
  per-command human review (§6, `policies.py:84-104`).
- **No formal vulnerability-disclosure process** (§8).
