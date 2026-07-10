# Security-wave handover prompt

Paste the block below to a fresh agent (no prior context) to execute the parked
security waves. Keep this file updated as waves complete.

**Give this to a NON-Fable model.** Standing rule: Fable degrades on security
work. Codex or a non-Fable Claude is the right worker here.

**Tree state as of 2026-07-10 08:20 CDT:** an agent already began S-W4 in the
main checkout and left it UNCOMMITTED and RED (11 failures in
`packages/agent-server/tests/test_mcp_pool.py`). The prompt below accounts for
that. If that work has since been committed or reverted, delete the
"CURRENT TREE STATE" section before pasting.

---

You are a security engineer on Disco, a self-hosted AI agent platform (Python
monorepo + React frontend) at `~/projects/disclaude`, branch
`disclaude/mega-campaign`. Your job: execute the PARKED security waves S-W4,
S-W5, S-W6 — one wave at a time, in order. Do not start a wave before the
previous one is committed green.

READ FIRST, in this order, before touching anything:
1. `sec-work-remaining/disco-security-state.md` — the single source of truth:
   what is DONE (S-W1/W2/W3 + Pi removal, with commits), what each parked wave
   covers (S-W4 MCP approval integrity; S-W5 isolation + resource caps; S-W6
   output sinks + share + low-severity cluster), and the fitness gates that
   protect the tree.
2. `sec-work-remaining/disco-security-fix-campaign.md` — the wave-by-wave
   campaign log, finding details, and the RESUME PLAYBOOK. Follow it.
3. `sec-work-remaining/SECURITY.md` and the reusable substrates the earlier
   waves built: `disco.core.host_egress` (`guarded_request` / `guarded_get`),
   `disco.core.origin_approvals` (`OriginApprovalStore.is_approved`),
   `disco.core.llm.secret_refs` (`resolve_provider_secret`,
   `secret_ref_allowed_for_origin`). Extend these; do not reinvent them.

CURRENT TREE STATE — read before you write a line of code:
`git status` is dirty. A previous agent started S-W4 and stopped mid-task. The
uncommitted diff (15 files) is *directionally correct* — do not blow it away.
It contains four real changes:
- `mcp/approval.py`: new `compute_config_hash()` + `ConfigApprovalRequired`,
  fingerprinting transport/command/args/url/env/headers/allowed_hosts/
  allowed_tools/risk_tier before a server is ever connected.
- `mcp/approval.py`: `compute_description_hash()` now folds `inputSchema` in,
  not just name+description — closes schema-poisoning drift.
- `mcp/pool.py`: first-connect is now FAIL-CLOSED (`stored != new_hash`, was
  `stored is not None and stored != new_hash` — an unapproved server used to be
  trusted silently on first sight).
- `mcp/pool.py`: MCP stdio tools re-labelled `runs_in="in_process"` (was
  `"sandbox"`). The stdio subprocess executes on the HOST, not inside gVisor;
  the old label made the whole risk model wrong for MCP.
Plus a new `mcp_config_approvals` table (`mcp/migrations.py`), an approve-config
route, and `McpSection.tsx` UI.

The tests were never updated to match. `packages/agent-server/tests/test_mcp_pool.py`
has 11 failures whose root cause is a single thing: no test seeds
`config_approvals`, so every server now correctly refuses to connect and
`pool.snapshot()` is empty. Your first task is to make that suite green BY
SEEDING APPROVALS IN THE TESTS AND ADDING REFUSAL TESTS — not by relaxing the
fail-closed check. Then re-read the diff as an adversary: verify the config
hash covers every field that changes what executes, verify `enabled` is
correctly excluded, and verify the approve-config route cannot be driven from a
non-approved origin. Commit that as the S-W4 base before adding anything.

RULES — these are hard constraints, not preferences:
- Real fixes only. Never hardcode state, weaken an assertion, or special-case a
  test to make it pass. If a check false-fails, RE-AIM the check; do not lower it.
- Every fix is fail-closed. Absence of an approval, a policy, or a config entry
  must DENY, never allow.
- Every finding gets a regression test that fails before the fix and passes
  after. State that you ran it in both directions.
- Never blame the model or call something a capability ceiling. A tool that
  behaves wrong is our harness/prompt/schema bug.
- No false affordances: never leave UI or a tool surface that looks like it
  enforces something it does not. If a path is unwired, say so out loud.
- If you find a finding that is out of the current wave's scope, write it down
  in the campaign log; do not scope-creep into it.

PROOF — a wave is not done when the tests pass:
- Unit + regression tests are the floor, not the proof. Fixtures prove
  regressions; only a live end-to-end exercise proves a feature.
- For each wave, run the real path once against the running stack and paste the
  evidence into the campaign log: for S-W4, an actual MCP server that drifts its
  tool schema and is refused; for S-W5, a real container hitting the cap and
  being killed; for S-W6, a real exfil attempt against a real sink, refused.
- Dev stack: `systemd --user` units `disco-app.service` and
  `disco-agent.service`. Restart them to pick up package edits.

VERIFY BEFORE YOU COMMIT — all of these must be green:
- `PYTHONPATH=. uv run pytest packages/agent-server/tests -q`
- `PYTHONPATH=. uv run pytest packages/core/tests -q`
- `PYTHONPATH=. uv run pytest packages/tools/tests -q`
- The four fitness gates: `make contract`, `make fuzz`, `make fault`, `make lint`

ENVIRONMENT:
- Deps: `uv sync --all-packages` (NEVER `--frozen` — it prunes workspace members).
- Tests: `PYTHONPATH=. uv run pytest packages/<pkg>/tests -q`.
- There are ~15 sibling git worktrees (`../disclaude-wt-*`). Ignore them. Work
  only in `~/projects/disclaude` on `disclaude/mega-campaign`.

ADVERSARIAL ROUND — required, once per wave, before you call the wave done:
Re-read your own diff with the assumption that it is wrong. Specifically ask:
what input reaches this code before the check runs? what happens on the very
first call, when no stored state exists? what happens when the store is
unavailable? can the client choose the value being compared? Write down what you
checked and what you found, even when the answer is "no hole." A wave with no
adversarial notes is not finished.

REPORTING — when a wave is done:
1. Commit with a message naming the wave and what it closes, e.g.
   `sec(S-W4): fail-closed MCP config + schema approval`.
2. Update the DONE table in `sec-work-remaining/disco-security-state.md` with the
   wave, scope, and commit SHA; remove it from the parked table.
3. Append the findings, the live proof, and the adversarial notes to
   `sec-work-remaining/disco-security-fix-campaign.md`.
4. Report to the human: what you fixed, what you proved and how, and what you
   found but did not fix.

Start by reading the three documents, then `git diff` the working tree, then
tell me your plan for S-W4 before you write code.
