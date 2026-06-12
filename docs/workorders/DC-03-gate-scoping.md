# DC-03 — Gates scoped to the real blast radius

**Read `README.md` first. De-complexity Wave 0 (docs/decomplexity-wave-plan.md DC-03).
Scope = packages/core (loop policies + engine gate callsite), packages/tools
(executor protocol method ONLY), frontend (feed badge). Do NOT touch
packages/agent-server AT ALL — runtime.py/app.py are owned by a parallel live
order (dc-02). Do NOT touch the analyzers, the hard-deny list, or the egress
proxy.**

## Why

Confirmation prompts fire for operations whose worst case is confined to the
sandbox — `rm -rf` in /workspace, package installs, process kills. The sandbox
IS the blast radius; that's why it exists. Today the gate
(`engine.py` ~2216: `self.policy.should_confirm(risk)`) sees ONLY the risk
enum — no tool identity, no runs_in — so a HIGH-scored sandboxed shell command
and a HIGH-scored host-affecting op gate identically. Meanwhile the real
containment mechanisms are independent of the gate and stay untouched: the
egress allowlist proxy (sandbox/base.py `egress_allowed` + the sidecar), the
pre-gate hard-deny (engine.py ~2181-2197), the 4-failure circuit breaker
(engine.py ~726/1569+), and the router spend cap.

## The decided design (locked)

### 1. The engine learns WHERE an action runs — via the executor, not the runtime

`packages/tools/src/disco/tools/executor.py`: add a small read-only
method to `DefaultToolExecutor`:

```python
def tool_scope(self, tool_name: str) -> str:
    """'sandbox' | 'in_process' | 'unknown' — where this tool executes.
    Policy input for the blast-radius gate (DC-03)."""
```

resolved from the registered ToolDefs' `runs_in`; unknown tool → `"unknown"`.

`packages/core/.../loop/engine.py`, at the gate callsite (~2199-2224): obtain
`scope = executor.tool_scope(action.tool)` using the SAME duck-typed
"supports it?" pattern the callsite already uses for `assess_detailed`
(getattr + fallback); absent method or any error → `"unknown"`. Then:

```python
if self.policy.should_confirm_action(risk, scope=scope, tool_name=action.tool):
```

### 2. `packages/core/.../loop/policies.py` — the policy grows one default method

On the `ConfirmationPolicy` protocol/base: `should_confirm_action(self, risk,
*, scope: str, tool_name: str) -> bool` with a DEFAULT implementation that
returns `self.should_confirm(risk)` — `NeverConfirm`, `AlwaysConfirm`,
`ConfirmRisky` all keep their exact behavior with zero edits.

New policy `BlastRadiusConfirm(ConfirmRisky)` — the Build-surface default:

- `scope == "sandbox"` → **False** (auto-approve). The sandbox is the blast
  radius: egress is proxy-enforced, catastrophes are hard-denied pre-gate,
  loops are breaker-guarded. This includes UNKNOWN risk — confinement, not
  scoring, is the argument.
- `scope != "sandbox"` (in_process, unknown) → exactly `ConfirmRisky`
  semantics (UNKNOWN gates; >= threshold gates).
- Belt+braces publish guard: if `tool_name` contains `deploy`/`publish`/
  `release` → gate REGARDLESS of scope (mirrors analyzers.py:192's heuristic;
  publishing leaves the blast radius by definition).

Where the Build loop picks its policy: VERIFIED — `ConfirmRisky()` is
constructed at `packages/agent-server/.../runtime.py:583`, which is OWNED BY A
PARALLEL ORDER. Do NOT edit it. Make `BlastRadiusConfirm` importable from
policies.py and stop there; the orchestrator wires the one-line swap after
dc-02 lands. Your unit tests construct `BlastRadiusConfirm` directly.

### 3. Journal, not silence

Auto-approved-by-scope actions must be visible. NO new event type (replay
compatibility): at the gate callsite, when `BlastRadiusConfirm` auto-approves a
risk that WOULD have gated under `should_confirm(risk)`, stamp
`action.meta["auto_approved"] = "sandboxed"` (same meta-stamping idiom as
`risk_assessment`).

Frontend: `frontend/src/lib/buildTrace.ts` — derive
`autoApproved: boolean` on action ActivityItems from
`e.meta?.auto_approved === "sandboxed"`; render in the activity feed item as a
small muted "auto · sandboxed" badge next to the risk chip (find the component
that renders ActivityItem risk — follow its exact styling idiom). The badge is
informational, never `attention: true`.

### 4. What does NOT change

Hard-deny (still pre-empts EVERYTHING, including sandboxed catastrophic
commands); the circuit breaker; the analyzers and risk scoring; plan approval
(`AWAITING_PLAN_APPROVAL` — product feature, untouched); egress allowlist
proxy; the spend cap; Research's `NeverConfirm`.

## Acceptance ladder

1. **Unit — `packages/core/tests/test_gate_scoping.py`** (NEW; follow
   test_security_policy.py's stub patterns):
   - sandboxed HIGH-risk action under BlastRadiusConfirm → no gate, executes,
     `meta["auto_approved"] == "sandboxed"`;
   - sandboxed UNKNOWN → no gate (confinement argument);
   - in_process HIGH → gates; in_process UNKNOWN → gates;
   - `scope == "unknown"` (executor without tool_scope) → gates like
     ConfirmRisky — the conservative fallback;
   - tool named `deploy_site` (in sandbox!) → gates (publish guard);
   - hard-deny still refuses a sandboxed `rm -rf /` BEFORE any policy runs;
   - default `should_confirm_action` delegates: NeverConfirm/AlwaysConfirm/
     ConfirmRisky behavior byte-identical via the new entry point.
2. **Tools unit** — `tool_scope()` resolution: sandbox tool → "sandbox",
   in_process tool → "in_process", unregistered → "unknown" (add to an
   existing executor test file or a small new one in packages/tools/tests).
3. **Existing gate tests stay green UNTOUCHED** (test_loop_confirmation.py,
   test_security_policy.py, test_security_integration.py use the old policies,
   whose behavior is unchanged). If any fails, that's a design violation —
   stop and report, don't adapt the test.
4. **Frontend unit** — buildTrace derivation test for `autoApproved` (extend
   the existing buildTrace test file) + the badge render in the feed
   component's test if one exists. Run the frontend unit suite
   (`npx vitest run --root frontend` or the repo's documented invocation).
5. Run `uv run pytest packages/core -q` AND the touched packages/tools test
   files (NEVER mix core/tools in one pytest invocation; never agent-server).
   Logs → `test-record/dc-03/units-core.log`, `units-tools.log`,
   `units-frontend.log` (create the dir).
6. **Report** — `agent-projects/sonnet/dc-03-report.md`: what changed, where
   the Build policy construction site turned out to be (and whether the swap
   was in-scope or deferred to the orchestrator), verbatim test output, honest
   deviations. No commits; no git writes.

The bp-16-replay rung (zero gates across a full build with answer_gates
instrumentation) is RUN BY THE ORCHESTRATOR after the policy swap lands.

## Manifest (orders.yaml `dc-03` — touch nothing outside it)

- packages/core/src/disco/core/loop/policies.py
- packages/core/src/disco/core/loop/engine.py
- packages/tools/src/disco/tools/executor.py
- packages/core/tests/test_gate_scoping.py
- packages/tools/tests/test_executor_scope.py
- frontend/src/lib/buildTrace.ts
- frontend/src/lib/buildTrace.test.ts
- frontend/src/components/build/ActivityFeed.tsx
- frontend/src/components/build/ActivityFeed.screenshot.test.tsx
- test-record/dc-03/units-core.log
- test-record/dc-03/units-tools.log
- test-record/dc-03/units-frontend.log
- agent-projects/sonnet/dc-03-report.md
