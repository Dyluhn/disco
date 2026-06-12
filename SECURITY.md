# Security posture & threat model — Disco

This document describes what disco v1 does and does **not** protect against, the
trust boundaries it enforces, and the honest limits of each control. It is written to be
trusted literally: where a control is partial, aspirational, or off by default, that is
stated. Every substantive claim cites the file (and line, where load-bearing) that
implements it, so a maintainer can verify it.

The authoritative operator-facing security narrative is
[`docs/self-host.md#security`](docs/self-host.md). This file does not contradict it — it
summarizes the same model from a threat/code-citation angle and links back. If the two ever
disagree, treat it as a bug and reconcile.

---

## 1. Scope & threat model

**v1 is a single-owner, single-tenant, locally-bound application with no authentication.**

- **No auth.** There is no login, session, or authorization layer in v1. Every conversation
  is owner `"local"` (`docs/self-host.md:7-9`). The defaults bind every published port to
  `127.0.0.1` (`compose.yaml:42,79,106`; `.env.example:6-8`). Network exposure is the
  operator's responsibility: do not set `PMX_BIND=0.0.0.0` without your own TLS + auth
  (a reverse proxy) in front.
- **The operator is trusted.** Anyone who can reach the bound ports has full control of the
  app, the model config, the stored secrets, and the agent. There is no privilege boundary
  between "user" and "admin".
- **Not multi-tenant.** Sandbox instances carry an `owner_id`/`conversation_id` and are never
  shared across owners (`tool-sandbox-contract.md:79`), but v1 ships exactly one owner. Do
  not treat this as a tenant-isolation guarantee.

### What this DOES try to protect against

| Threat | Where it comes from | Primary control |
|---|---|---|
| Malicious / buggy **agent-generated code** | The agent writes and runs code, shell, and a browser | Sandbox isolation (§3) + hard-deny floor (§6) |
| **Prompt injection** via fetched web content | A hostile page tries to hijack the agent | Quarantine + untrusted-data channel (§5) |
| Untrusted **build output rendered in the browser** | A shared/imported run's HTML reaches your browser | `<iframe sandbox>` hardening (§5) |
| **Secret theft** by agent-run code | Code in the sandbox tries to read API keys | Secrets never enter the sandbox (§2, §7) |

### What it explicitly does NOT protect against

- A network attacker reaching an exposed port (there is no auth — §1).
- A compromise of the **agent-server process** itself: on the `local`/Docker backend the
  mounted container socket makes that process root-equivalent on the host (§2, §3,
  `docs/self-host.md:73-78`).
- A malicious operator.

---

## 2. Trust boundaries

| Boundary | Trusted side | Untrusted side | What crosses & how it's controlled |
|---|---|---|---|
| **operator ↔ server** | — | — | No auth in v1. Loopback bind is the only barrier (`compose.yaml:42,79,106`). |
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
| `process` | **NONE.** Tools run as subprocesses in the agent-server's own container — no kernel/VM boundary. It *does* uphold two things: a clean minimal env (no host secret leaks in) and a workspace path-jail for file ops (`process.py:1-16,48-61`). Network egress is **not** actually blocked (`process.py:11-16`). Labeled "no isolation, dev only" in the UI (`docs/self-host.md:99-101`). | No | weakest (fallback gates at LOW / on UNKNOWN, `isolation.py:83-90`) |
| `local` (Docker/OCI, `runc`) | Container-grade only — **shared host kernel** (`local.py:1-18,35-38`). Plus the **socket tradeoff**: the agent-server mounts the host container socket, making that process **root-equivalent on the host** (`docs/self-host.md:73-78`, `compose.yaml:72-75`). The agent's own code never touches the socket; this is a blast-radius concern for an *agent-server RCE*. | **No** | tighter — gate at MEDIUM, gate UNKNOWN (`isolation.py:68-78`) |
| `gvisor` (`runsc`, separate VM) | Strong: a user-space kernel intercepting syscalls, on a separate Docker host reached over (keyless Tailscale) SSH (`gvisor.py:1-12,46-53`). The only tier that enforces a **selective** egress allowlist (§4). | Yes | most permissive earned default — gate HIGH only, trust UNKNOWN (`isolation.py:49-56`) |
| `podman` (rootless, remote) | Container-grade (shared kernel) behind a host boundary; **real backend code but a stub in this environment** (the test VM was destroyed) — constructs but is not live-verified here (`runtime.py:256-260`). | No | gate HIGH, gate UNKNOWN (`isolation.py:57-67`) |

**Mitigations for the socket tradeoff** (from `docs/self-host.md:80-95`, not duplicated here):
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
> Build/Agent sandbox has **full outbound internet**. `docs/self-host.md` states this same
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
imported content cannot reach this instance's open-CORS APIs (`ExecutionCanvas.tsx:392-403`,
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

---

## 7. Secrets at rest

- **Encrypted at rest with Fernet** (AES-128-CBC + HMAC). The OpenRouter API key is the only
  stored secret today (`secrets.py:1-12,64-97`).
- **Key derivation.** The Fernet key derives from `PMX_SECRET_KEY` (SHA-256 → urlsafe-b64).
  Use a high-entropy value (`openssl rand -base64 32`) — it is not a slow KDF
  (`secrets.py:57-61`, `.env.example:20-24`).
- **Plaintext never on disk.** Only ciphertext is persisted, to a file separate from the
  config catalogue so the config stays credential-free (`secrets.py:1-12,96-153`). At build
  time the decrypted key is overlaid into the provider env in memory, never written
  (`secrets.py:51-54`).
- **Fails closed.** With no/wrong `PMX_SECRET_KEY` the store reports `locked` and callers get
  `None` rather than a broken key (`secrets.py:6-8,117-127`).
- **Out of the project tree.** New installs write `~/.config/disco/secrets.json`, not
  the repo root, so anything granted read of the working dir cannot copy the ciphertext
  (`secrets.py:27-49`). In compose it lives in the `pmx-data` volume (`compose.yaml:39-40,61`).
- **Pin your key.** If `PMX_SECRET_KEY` is blank it is auto-generated into the data volume;
  pin it in `.env` so your encrypted keys survive a volume rebuild
  (`.env.example:20-24`, `docs/self-host.md:134-135`).
- **Never inside the sandbox.** No secret, credential, or host env is present anywhere
  agent-run code can read it (`tool-sandbox-contract.md:35`; enforced by the clean-env
  container/subprocess construction cited in §2).

**The no-auth corollary:** because v1 has no authentication, the only thing standing between
these secrets and the network is the loopback bind. **Keep `PMX_BIND=127.0.0.1`** unless you
put TLS + auth in front (`.env.example:5-8`).

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

- **No authentication / authorization.** Loopback bind is the only access control
  (`.env.example:5-8`). Not multi-user.
- **The `local`/Docker socket mount is root-equivalent** on the host; a full agent-server
  compromise can own the machine. Mitigate with a rootless Podman socket
  (`docs/self-host.md:73-95`).
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
