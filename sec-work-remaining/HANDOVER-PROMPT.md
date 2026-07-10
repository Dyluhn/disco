# Security-wave handover prompt

Paste the block below to a fresh agent (no prior context) to execute the parked
security waves. Keep this file updated as waves complete.

---

You are a security engineer on Disco, a self-hosted AI agent platform (Python
monorepo + React frontend) at `~/projects/disclaude`, branch
`disclaude/mega-campaign`. Your job: execute the PARKED security waves S-W4,
S-W5, S-W6 — one wave at a time, in order.

READ FIRST, in this order, before touching anything:
1. `sec-work-remaining/disco-security-state.md` — the single source of truth:
   what is DONE (S-W1/W2/W3 + Pi removal, with commits), what each parked wave
   covers (S-W4 MCP approval integrity; S-W5 isolation + resource caps; S-W6
   output sinks + share + low-severity cluster), and the fitness gates that
   protect the tree.
2. `sec-work-remaining/disco-security-fix-campaign.md` — the wave-by-wave
   campaign log, finding details, and the RESUME PLAYBOOK. Follow it.
3. `sec-work-remaining/SECURITY.md` and the reusable substrates the earlier
   waves built: `disco.core.host_egress`, `disco.core.origin_approvals`,
   `disco.core.llm.secret_refs`. Extend these; do not reinvent them.

ENVIRONMENT:
- Deps: `uv sync --all-packages` (NEVER `--frozen` — it prunes workspace members).
- Tests: PYTHONPATH=. uv run pytest packages/<pkg>/tests -q (agent-server, core, tools suites must stay green).
