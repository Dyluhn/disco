# Security-wave handover prompt

Paste the block below to a fresh agent (no prior context) to execute the remaining
parked security wave. Keep this file updated as waves complete.

**Give this to a NON-Fable model.** Standing rule: Fable degrades on security
work. Codex or a non-Fable Claude is the right worker here.

**Tree state as of 2026-07-10:** S-W5 is complete at `10433330`; S-W4 is
`212f6e89`, and the signed-origin regression audit is `d6651e7e`. The only
remaining campaign wave is S-W6. S-W5's proof and adversarial notes are recorded
in the campaign log.

---

You are a security engineer on Disco, a self-hosted AI agent platform (Python
monorepo + React frontend) at `~/projects/disclaude`, branch
`disclaude/mega-campaign`. Your job: execute the PARKED security wave S-W6
(output sinks + share + the low-severity cluster). S-W5 is committed and verified;
do not rebuild or regress it.

S-W4 and S-W5 are completed and adversarially reviewed in the campaign log.
S-W6 has an unreviewed draft on an archive branch. Assume that draft is wrong
until you have proven otherwise yourself.

READ FIRST, in this order, before touching anything:
1. `sec-work-remaining/disco-security-state.md` — the single source of truth:
   what is DONE (S-W1/W2/W3/W4/W5 + Pi removal, with commits), what S-W6 covers
   (output sinks + share + low-severity cluster), and the fitness gates that
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

COMPLETED S-W5 BASE — do not regress it:
`10433330` gives Build/artifact finite filtered egress and Agent/browser public-web
through a private/host-denying policy sidecar on an internal per-instance network.
It binds published ports to loopback, inventories all local/remote host addresses,
pins validated DNS answers, applies real tmpfs workspace quotas plus CPU/memory/PID/
FD caps, bounds shell/file/kernel/event/WS/preview transfers, keeps unmet DoD
fail-closed, and argv-quotes preview paths. Its concentrated exploit harnesses are
`packages/tools/tests/test_sw5_isolation_bounds.py` and
`packages/core/tests/test_sw5_event_bounds.py`. Preserve the invariant that no
model-shaped sandbox spec reaches a raw bridge.

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
- Run the real S-W6 path once against the running stack and paste the evidence into
  the campaign log: a real exfil attempt against a real sink, refused.
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
   `sec(S-W6): snapshot shares and close output sinks`.
2. Update the DONE table in `sec-work-remaining/disco-security-state.md` with the
   wave, scope, and commit SHA; remove it from the parked table.
3. Append the findings, the live proof, and the adversarial notes to
   `sec-work-remaining/disco-security-fix-campaign.md`.
4. Report to the human: what you fixed, what you proved and how, and what you
   found but did not fix.

Start by reading the three documents, then `git diff` the working tree, then
begin S-W6 by reviewing (not merging) archive commit `d627d287` against the
current implementation and acceptance tests.
