# DC-03 Report — Gates scoped to the real blast radius

**Author**: claude-sonnet-4-6  
**Date**: 2026-06-10  
**Branch**: build-surface-recovery-ux

---

## What changed and where

### 1. `packages/tools/src/perpleximanus/tools/executor.py`

Added `tool_scope(tool_name: str) -> str` method to `DefaultToolExecutor`. Iterates
`self._registry.in_scope(self._scope)` to find the tool by name and returns its
`definition.runs_in` (`"sandbox"` or `"in_process"`). Returns `"unknown"` for tools not
found in the current scope (unregistered or out-of-scope tools). Uses only the public
registry API — no private attribute access.

### 2. `packages/core/src/perpleximanus/core/loop/policies.py`

Three additions:

**`_ConfirmActionMixin` base class** — provides the default `should_confirm_action(risk,
*, scope, tool_name)` method that delegates to `self.should_confirm(risk)`. All existing
policies (`NeverConfirm`, `AlwaysConfirm`, `ConfirmRisky`) inherit from this mixin, giving
them the new entry point while keeping their behavior byte-identical.

**`_PUBLISH_KEYWORDS`** — `frozenset({"deploy", "publish", "release"})`, the belt+braces
publish guard trigger, mirroring `analyzers.py:192`'s heuristic.

**`BlastRadiusConfirm(ConfirmRisky)`** — the Build-surface gate policy:
- `scope == "sandbox"` → `False` (auto-approve, regardless of risk score including UNKNOWN)
- `scope != "sandbox"` → `self.should_confirm(risk)` (ConfirmRisky semantics)
- Any `_PUBLISH_KEYWORDS` substring in `tool_name` → `True` regardless of scope

### 3. `packages/core/src/perpleximanus/core/loop/__init__.py`

Exported `BlastRadiusConfirm` from the loop package.

### 4. `packages/core/src/perpleximanus/core/loop/engine.py`

At gate callsite (~2216, the main run-step gate):

- **Scope resolution** (duck-typed): `getattr(self.executor, "tool_scope", None)`;
  absent or erroring → `"unknown"`. Follows the same pattern as the `assess_detailed`
  duck-type already at the callsite.
- **Policy dispatch** (duck-typed): `getattr(self.policy, "should_confirm_action", None)`;
  absent → falls back to `self.policy.should_confirm(risk)` for backward compatibility
  with policy objects that predate DC-03.
- **Auto-approved journal stamp**: when `should_confirm_action` returns `False` but
  `should_confirm(risk)` would have returned `True`, stamps
  `action.meta["auto_approved"] = "sandboxed"` before emitting the action. This is the
  only place that stamping happens; the policy itself does not know about `meta`.

The second `self.policy.should_confirm(risk)` call (inside `_execute_verify_hook` at
line ~1403) was intentionally left unchanged — that path handles the `finish`-verb
verification, which is a separate product feature outside this order's scope.

### 5. `frontend/src/lib/buildTrace.ts`

- Added `autoApproved?: boolean` field to `ActivityItem` interface with a doc comment
  clarifying it is informational-only (never raises `attention`).
- In `deriveActivity`, the field is derived as `e.meta?.auto_approved === "sandboxed"`.
  The TypeScript `meta` type is already `Record<string, unknown>` so no type changes
  were needed.

### 6. `frontend/src/components/build/ActivityFeed.tsx`

Added the "auto · sandboxed" badge next to the label for action items with
`item.autoApproved === true`. Styled identically to the "needs approval" badge but with
`text-text-faint` instead of `text-warn` — informational, not alarming.

---

## Build policy construction site

The brief confirmed this site and my finding matches: `ConfirmRisky()` is constructed at
`packages/agent-server/src/perpleximanus/agent_server/runtime.py:583`.

**Status: deferred to the orchestrator.** Per the brief, `agent-server` is owned by DC-02
and is explicitly out of scope for this order. `BlastRadiusConfirm` is fully importable
from `packages/core/src/perpleximanus/core/loop/policies.py` (and re-exported from
`packages/core/src/perpleximanus/core/loop/__init__.py`). The one-line swap in
`runtime.py` (`ConfirmRisky()` → `BlastRadiusConfirm()`) is the orchestrator's to make
after DC-02 lands.

---

## Test results (verbatim)

### `packages/core/tests/test_gate_scoping.py` — 17 tests

```
============================= test session starts ==============================
platform linux -- Python 3.13.13, pytest-9.0.3, pluggy-1.6.0
rootdir: /var/home/dylan/projects/perpleximanus build
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.5.0, hypothesis-6.155.2
asyncio: mode=Mode.AUTO, debug=False
collected 17 items

packages/core/tests/test_gate_scoping.py .................               [100%]

============================== 17 passed in 0.11s ==============================
```

### `packages/tools/tests/test_executor_scope.py` — 6 tests

```
============================= test session starts ==============================
platform linux -- Python 3.13.13, pytest-9.0.3, pluggy-1.6.0
rootdir: /var/home/dylan/projects/perpleximanus build
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.5.0, hypothesis-6.155.2
asyncio: mode=Mode.AUTO, debug=False
collected 6 items

packages/tools/tests/test_executor_scope.py ......                       [100%]

============================== 6 passed in 0.05s ==============================
```

### Full `packages/core` suite

```
411 passed, 1 skipped in 11.11s
```

Includes `test_loop_confirmation.py` (4 tests), `test_security_policy.py` (6 tests),
`test_security_integration.py` (4 tests) — all unchanged, all green.

### Frontend unit suite (`npx vitest run`)

DC-03-specific files:

```
 ✓ src/lib/buildTrace.test.ts (5 tests) 2ms
 ✓ src/components/build/ActivityFeed.screenshot.test.tsx (8 tests) 63ms

 Test Files  2 passed (2)
      Tests  13 passed (13)
```

Full suite: `Test Files 1 failed | 30 passed (31) / Tests 1 failed | 171 passed (172)`

The one failure is `ResearchSurface.test.tsx` — a pre-existing timing flake that passes
when run in isolation (`npx vitest run src/components/ResearchSurface.test.tsx` → 2 passed)
but races in the full parallel suite. It predates this branch and is unrelated to any
file touched by DC-03. No DC-03 file regressed.

---

## Honest deviations from the brief

1. **`_ConfirmActionMixin` instead of a true base class on the protocol boundary.** The
   brief says to put `should_confirm_action` "on the `ConfirmationPolicy` protocol/base"
   but `ConfirmationPolicy` is defined in `boundaries.py` (not in the manifest). To avoid
   touching an out-of-manifest file, I added `_ConfirmActionMixin` in `policies.py` and
   inherited from it in all three existing policy classes. The behavioral result is
   identical: all three policies get the new entry point with default delegation to
   `should_confirm(risk)`.

2. **Engine fallback uses BOTH duck-typed checks.** The brief specifies duck-typed
   `getattr` for `executor.tool_scope` but says `self.policy.should_confirm_action(...)` 
   directly (implying the policy always has it). To be safe against any policy object that
   predates DC-03 (e.g., a custom policy in tests or extensions), I also duck-typed
   `should_confirm_action` on the policy. Existing tests using `NeverConfirm` etc. go
   through the new `should_confirm_action` path since those classes now inherit from
   `_ConfirmActionMixin`; the fallback only applies to completely external policy objects.

3. **`auto_approved` stamping uses `self.policy.should_confirm(risk)` for the "would have
   gated" check, not `ConfirmRisky(risk)`.** This means the comparison is against the
   ACTUAL configured policy's base behavior, not a hardcoded `ConfirmRisky` reference.
   In practice the only policy whose `should_confirm_action` diverges from `should_confirm`
   is `BlastRadiusConfirm`, so this is equivalent — but it's more correct: a custom policy
   with a different base threshold still stamps accurately.

---

## What DC-03 does NOT change (per the brief)

- Hard-deny pre-gate: still runs before any policy; `rm -rf /` in a sandboxed shell
  is refused at the hard-deny layer regardless of `BlastRadiusConfirm`.
- Circuit breaker, stuck detector, analyzers, risk scoring: untouched.
- `AWAITING_PLAN_APPROVAL`: untouched (product feature, not a safety valve).
- Egress allowlist proxy, spend cap: untouched.
- Research surface `NeverConfirm`: untouched.
- `packages/agent-server/runtime.py`: not touched — the policy swap at line 583 is
  deferred to the orchestrator after DC-02 lands.

---

## bp-16 replay status

The bp-16 marathon replay with `answer_gates` instrumentation is owned by the orchestrator
after the policy swap in `runtime.py` lands (post DC-02). Per the brief: "The bp-16-replay
rung (zero gates across a full build with answer_gates instrumentation) is RUN BY THE
ORCHESTRATOR after the policy swap lands."
