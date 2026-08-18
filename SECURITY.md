# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately, using GitHub's
[private security advisory form](https://github.com/Dyluhn/disco/security/advisories/new)
on this repository (Security tab -> Report a vulnerability). Do not open a
public issue with exploit detail, and do not disclose it anywhere public until
a fix has shipped.

Include what you'd want if you were fixing it:

- The affected surface or component (Search / Deep Research / Build / Agent,
  or which package under `current/packages/`).
- A reproduction path, ideally minimal.
- Your deployment mode (podman/docker compose vs. local dev) and, if it's
  relevant to the bug, your sandbox backend (`process` / `local` / `gvisor` /
  `podman`).
- What you think the impact is — what an attacker gains, and from what
  position (unauthenticated network caller, authenticated operator, code
  running inside the sandbox, etc).

Disco is a single-maintainer project. There's no formal SLA, but reports go
straight to the maintainer and get a response before anything else on the
queue.

## Scope

This project is single-tenant, self-hosted software with several deliberate,
already-documented trust boundaries and known gaps — for example: no TLS
termination out of the box, a loopback bind by default, and (on the default
`local` sandbox backend) a container socket mount that makes the agent-server
process root-equivalent on the host if it's ever compromised. Read
[`current/sec-work-remaining/SECURITY.md`](./current/sec-work-remaining/SECURITY.md)
before reporting — it's the full threat model: what each control actually
does, what it explicitly does *not* protect against, and which hardening work
is still open. If what you found matches something already listed there as a
known limitation, it's still worth a report if you have a way to make it
worse (e.g. reach it remotely, or without the access the doc assumes) — but
please check first so we're not duplicating a tracked issue.

In scope, roughly in order of what would concern us most:

- Anything that lets an unauthenticated caller reach an authenticated route,
  or one owner's session act on another owner's data.
- A sandbox escape, or a way to reach the host network/filesystem from inside
  a sandboxed Build/Agent run beyond what's already documented.
- A way to exfiltrate secrets (provider keys, the session/CSRF material, the
  Fernet key) from the store, the wire, or a running process.
- Prompt-injection-derived actions that bypass the confirmation gate or the
  hard-deny floor described in that document's §5-6.

Out of scope: the already-documented absence of TLS, the `local` backend's
socket tradeoff, and the single-operator/not-multi-tenant model — those are
known, tracked design points, not undiscovered bugs. Denial-of-service against
your own self-hosted instance is generally not useful to report either, unless
it's remotely triggerable without authentication.

## Supported versions

Disco is pre-1.0 with a single active line: `main` and the latest tagged
release. There's no long-term-support branch yet, so fixes land on `main` and
the next release rather than being backported.
