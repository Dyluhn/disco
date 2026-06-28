# Codex CODE Review — CONTRACT-ACTIVATE (build-phase substrate + live guard wiring) — IMPLEMENTED

This is the activation Codex required after CONTRACT-ENFORCE: enforcement is now WIRED + LIVE in the runtime.

Inspect:
NEW core/contract/phase.py: BuildPhaseTracker — deterministic state machine advanced by observable signals.
  BOOTSTRAP --(a bootstrap-scoped tool succeeds)--> EDIT; * --(finalizer)--> VERIFY; VERIFY --(verifier
  passed/failed)--> EXPORT/REPAIR. current() is the live phase. Conservative (never wrongly broadens).
CHANGED tools/executor.py: new optional on_tool_success callback — after a SUCCESSFUL tool call the executor
  notifies it (best-effort, never fatal) so the tracker advances. (scope_guard from CONTRACT-ENFORCE reads
  tracker.current.)
CHANGED agent-server/runtime.py: per-conversation (BuildContract, BuildPhaseTracker) store +
  _build_scope_guard(conversation_id) → (ContractScopeGuard.for_contract(contract, tracker.current),
  tracker.note_tool_success); resolves the contract from an optional declared kind (set_build_kind), default
  CUSTOM. At the artifact-mode executor construction (runtime.py:~1489) it now passes scope_guard +
  on_tool_success; non-artifact runs pass (None, None) → unchanged.

Tests (16): phase machine transitions (6); executor END-TO-END (7) incl. the live loop — a real app_create
succeeds → tracker BOOTSTRAP→EDIT via on_tool_success → a subsequent raw file_write is DENIED and never
lands; runtime resolver (4) — default CUSTOM, declared appkit.leadgen governs (file_write denied at
bootstrap), the runtime's guard advances with phase via on_success, set_build_kind(None) resets. Plus: 63
existing agent-server runtime/artifact tests still green; executor regression green. basedpyright: my files 0
errors (a pre-existing ScheduleService.fire_now error is unrelated — confirmed present with my changes
stashed).

Judge: (a) is the phase machine correct + conservative (never grants an escape hatch early; the worst case
keeps raw rewrite denied)? (b) is the runtime wiring sound — per-conversation isolation, default CUSTOM
real-but-permissive, declared-kind path, on_tool_success advancing the SAME tracker the guard reads? (c)
is the CUSTOM default's enforcement meaningful + not breaking normal artifact runs (file_write allowed at
bootstrap, denied at edit)? (d) any way a build run bypasses the guard, or a non-artifact run is affected?
(e) test sufficiency for "live, runtime-wired enforcement". Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE
+ REASONS + REQUIRED_REVISIONS.
