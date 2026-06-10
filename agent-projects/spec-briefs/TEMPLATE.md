# Spec brief template — live acceptance spec drafted by a cheap model

<!--
USAGE (the Job 3 split, bakeoff/RESULTS-spec.md): copy this template, fill the
{{...}} slots, then run scripts/spec-draft.sh <brief> <name>. MiniMax M3 (the
strongest drafter in the bake-off) writes the draft; the draft lands in
agent-projects/spec-candidates/ and is checked against the deterministic
checklist BEFORE any live run is spent. Iteration against the real stack and
final sign-off stay with Claude — all three bake-off candidates false-failed
every real passing run on a fact no brief had transmitted; the checklist below
encodes the two facts we have already paid for so they never recur.
-->

Write ONE Playwright spec file (TypeScript, ESM) for the perpleximanus frontend that
proves — against the REAL running stack, end to end, with NO mocking or fixtures —
that: {{WHAT THE SPEC MUST PROVE, ONE SENTENCE}}.

## System facts

- Frontend: React/Vite. Playwright live config already exists
  (`playwright.live.config.ts`): testDir `./e2e-live`, Firefox project, baseURL
  `http://localhost:5174` (the config auto-starts the dev server). Your file will be
  placed at `frontend/e2e-live/<name>.spec.ts` and run as-is.
- Backend: live agent-server, FastAPI on `http://127.0.0.1:8000`. If it is not up,
  the spec must SKIP (probe `GET /health` inside try/catch — a refused connection
  must not crash the test).
- The driver is a real local LLM; real builds are SLOW. Budget up to 30 minutes
  total and make sub-timeouts actually fit inside the total.
- Agent-server HTTP API (all GET):
  - `/conversations?limit=N` → `{"conversation_ids": [...]}` newest-first.
  - `/conversations/{cid}/state` → `{"execution_status": "RUNNING" | "FINISHED" | ...}`
  - `/conversations/{cid}/events?limit=100&after_seq=S` → `{"events": [...]}`,
    paginated by `seq`; `kind === "action"` events carry
    `tool_call: { tool_name, arguments }`; errors are `kind === "agent_error"`.
- **Tool names (HARD-WON FACT — bake-off shared false-fail):** the agent runs
  commands under tool_name `shell_exec` (persistent tmux session) **and under
  plain `shell` for one-off commands — its curl/wget verifications arrive as
  `shell`.** Any predicate matching HTTP probes MUST accept BOTH `"shell"` and
  `"shell_exec"`. Matching only `shell_exec` false-fails real passing runs.
  Other tools: `shell_kill_process` (kill a named session), `shell_view`,
  `server_status`.
- **Conversation-id discovery (HARD-WON FACT):** page network traffic is unreliable
  right after approval (the live feed is a websocket). Snapshot the KNOWN
  conversation ids BEFORE submitting, then poll `/conversations` for an id not in
  the snapshot. Do not just take `limit=1` newest-first.
- UI affordances (verify selectors current before sending the brief):
  {{UI SELECTORS THE SPEC NEEDS — roles, placeholders, tab/button names, iframe titles}}

## What the spec must do and verify

{{NUMBERED REQUIREMENTS — drive the real UI, wait for terminal state with the
state poll erroring on FAILED/ERROR, persist evidence (event-log JSON under
test-record/, full-page screenshot), and the exact event-log assertions}}

## Mandatory draft checklist (your draft is rejected mechanically if it violates these)

1. HTTP-probe predicates accept tool_name `shell` AND `shell_exec`, and match hosts
   `localhost`, `127.0.0.1`, and `0.0.0.0`.
2. Conversation id found by snapshot-known-ids-then-poll-for-new, never newest-first.
3. Backend probe wrapped in try/catch + `test.skip(!healthy, msg)` — never an
   unhandled rejection.
4. Sub-timeouts sum to LESS than the total `test.setTimeout`.
5. State polling throws (with the state payload) on terminal failure statuses
   instead of silently falling through.
6. Playwright assertion messages go in `expect(value, message)` — matchers like
   `toBeTruthy()` take no message argument.

Output: the complete contents of the single `.spec.ts` file, nothing else.
