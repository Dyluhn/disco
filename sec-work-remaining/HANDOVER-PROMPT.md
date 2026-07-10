# Security-wave handover prompt

Paste the block below to a fresh agent (no prior context) to execute the remaining
parked security waves. Keep this file updated as waves complete.

**Give this to a NON-Fable model.** Standing rule: Fable degrades on security
work. Codex or a non-Fable Claude is the right worker here.

**Tree state as of 2026-07-10 09:05 CDT:** S-W4 is complete at `212f6e89`; the
signed-origin regression audit is `d6651e7e`. The remaining sequence is S-W5 then
S-W6. The checkout was clean immediately after the S-W4 code commit; ledger edits
may be the only newer changes if this handover itself has not yet been committed.

---

You are a security engineer on Disco, a self-hosted AI agent platform (Python
monorepo + React frontend) at `~/projects/disclaude`, branch
`disclaude/mega-campaign`. Your job: execute the PARKED security waves S-W5
(isolation + resource caps) and S-W6 (output sinks + share + the low-severity
cluster) — one wave at a time, in order. Do not start a wave before the previous
one is committed and verified.

S-W4 is completed and adversarially reviewed in the campaign log. S-W6 has an
unreviewed draft on an archive branch. Assume that draft is wrong until you have
proven otherwise yourself.

READ FIRST, in this order, before touching anything:
1. `sec-work-remaining/disco-security-state.md` — the single source of truth:
   what is DONE (S-W1/W2/W3/W4 + Pi removal, with commits), what each parked wave
   covers (S-W5 isolation + resource caps; S-W6 output sinks + share +
   low-severity cluster), and the fitness gates that
   protect the tree.
2. `sec-work-remaining/disco-security-fix-campaign.md` — the wave-by-wave
   campaign log, finding details, and the RESUME PLAYBOOK. Follow it.
3. `sec-work-remaining/SECURITY.md` and the reusable substrates the earlier
   waves built: `disco.core.host_egress` (`guarded_request` / `guarded_get`),
   `disco.core.origin_approvals` (`OriginApprovalStore.is_approved`),
   `disco.core.llm.secret_refs` (`resolve_provider_secret`,
   `secret_ref_allowed_for_origin`). Extend these; do not reinvent them.

COMPLETED S-W4 BASE — do not regress it:
`212f6e89` adds a separate pre-connect config approval, authoritative first-use
and drifted tool-schema approval, host-honest MCP tool scope, and configured risk
wiring. Its exploit harness is
`packages/agent-server/tests/test_sw4_mcp_approval_integrity.py`. The campaign log
records both-direction live proof. Preserve the invariant that no subprocess or
HTTP connection occurs before config approval and no discovered tool registers
before its exact live schema hash is approved.

PRIOR WORK EXISTS — do not rebuild these from scratch:
Two waves were partly executed in July 2026 and preserved as WIP archive commits
before their worktrees were culled. Read both diffs before planning:
- `archive/wt-epic-d` (`b9c26a14`) — the original S-W4 MCP-approval attempt,
  superseded by reviewed commit `212f6e89`. Keep it only as provenance.
- `archive/wt-epic-c` (`d627d287`) — S-W6, reportedly FINISHED and never
  reviewed: `share.py`, `share_service.py`, `ws.py`, `runtime.py`,
  `sheets.py` + tests. Treat it as an untrusted first draft by an unreviewed
  agent: harvest it, re-derive every claim, adversarially review it. Do not
  merge it.
Both branches sit on a PRE-Pi-removal base (`2408e40f` / `4d09585a`). Do NOT
fast-forward or merge them into `mega-campaign` — extract the diff and apply it
with `git apply --3way`, or re-implement against current files.

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
  evidence into the campaign log: for S-W5, a real container hitting the cap and
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
   `sec(S-W5): isolate sandbox egress and bound resources`.
2. Update the DONE table in `sec-work-remaining/disco-security-state.md` with the
   wave, scope, and commit SHA; remove it from the parked table.
3. Append the findings, the live proof, and the adversarial notes to
   `sec-work-remaining/disco-security-fix-campaign.md`.
4. Report to the human: what you fixed, what you proved and how, and what you
   found but did not fix.

Start by reading the three documents, then `git diff` the working tree, then
begin S-W5 from the current implementation and acceptance tests.
