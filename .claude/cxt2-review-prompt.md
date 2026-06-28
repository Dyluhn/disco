# Codex Plan Review — PR CXT-2 ROUND 2

You previously returned REVISE with 5 required revisions (matrix clarification + 4 test-coverage items).
Below is the plan + REVISION 1. Confirm each required revision is resolved. Inspect the repo as needed.
Return exactly one of: APPROVE | REVISE | BLOCKED_CODEX_UNAVAILABLE.

Your round-1 REQUIRED_REVISIONS:
1. Codify exact durable kind matrix + count (9 vs 10); ensure_initialized creates exactly the intended set.
2. Test that context_memory is actually in active tool scope (not dead registration).
3. Tool tests: invalid action, structured-kind write rejection, list determinism.
4. Core tests: reconstruct/round-trip over ALL structured kinds (incl source_priority) + missing-file safe default.
5. Track CXT-7 auto-write hook obligation as a binding follow-up dependency.

## Plan (original + REVISION 1)
## PR CXT-2 — Durable .disco/context files

### Status
PLANNING → CODEX_REVIEW

### Dependencies
- CXT-1 (ContextLedger/ContextPack/refs/ArtifactMemoryKind) — COMPLETE.

### Plan
Add file-backed memory so context survives transcript truncation / conversation replay.

(A) CORE STORE — `packages/core/src/disco/core/context/store.py` (new):
- `WorkspaceFS` Protocol (async `read_file(path)->bytes`, `write_file(path, bytes)->None`) — keeps core
  dependency-free; the tool passes `ctx.sandbox` (which already satisfies it). No import of Sandbox/tools.
- `ContextRecoveryError(Exception)` — structured: `.kind: ArtifactMemoryKind`, `.rel_path`, `.detail`.
- `ReconstructResult` (frozen pydantic): `ledger: ContextLedger`, `recovery_errors: tuple[...]` (each a
  small frozen `RecoveryNote(kind, rel_path, detail)`).
- `ArtifactMemoryStore(fs, *, base=".disco/context")`:
  - file layout: narrative kinds → `<value>.md` (current_goal, todo, decisions, assumptions, summary);
    structured kinds → `<value>.json` (resource_manifest, direct_edits, unresolved_comments,
    source_priority, AND verifier_failures).
  - DEVIATION (documented, single-source-of-truth): doc lists `verifier_failures.md`, but the ledger holds
    structured `VerifierFailureRef`s and the done-when requires FAITHFUL reconstruction of failures →
    store as `verifier_failures.json` (round-trips via TypeAdapter). Avoids a two-sources-of-truth
    md+json pair (a documented anti-pattern). JSON is still human-readable; CXT-4 renders it to prompt.
  - methods: `write_goal/read_goal`, `seed_todo/read_todo`, `write_markdown(kind,text)/read_markdown(kind)`,
    `record_verifier_failures(...)`, `record_resources(...)`, `record_direct_edits(...)`,
    `record_comments(list[str])`, `write_source_priority(sp)`, `read_json(kind, adapter)`,
    `ensure_initialized()` (creates all files w/ safe defaults if absent),
    `reconstruct(conversation_id, workspace_root) -> ReconstructResult`.
  - MISSING vs CORRUPT split: a read that the FS refuses (file absent) → safe default (None / `()`);
    a file that is PRESENT but unparseable JSON → raises `ContextRecoveryError` (direct read path) /
    is captured as a RecoveryNote + safe default (resilient `reconstruct`). This is exactly the doc's
    "missing files recreated with safe defaults" + "invalid JSON yields structured recovery error".
  - JSON writes: `json.dumps(payload, indent=2).encode()` (house style); models via
    `[m.model_dump(mode="json") for m in ...]`. ids/timestamps preserved → faithful round-trip.

(B) TOOL — `packages/tools/src/disco/tools/builtin/context_memory.py` (new) + register in builtin/__init__:
- `ContextMemoryArgs(action: Literal["read","write","list"], kind: str, content: str|None=None)`.
- `ContextMemoryTool` (ToolDef: name="context_memory", needs={Capability.FILESYSTEM}, runs_in="sandbox",
  base_risk=LOW, read_only=False). `run()` validates `kind` against ArtifactMemoryKind (bad → ToolOutcome
  success=False, error="invalid_kind"); read missing → success=True empty content; write narrative kinds
  only via this tool (structured kinds are agent-opaque JSON managed by the store/runtime — the tool
  exposes read for them but write only for md kinds, to avoid the model hand-writing structured JSON;
  attempting to write a structured kind → error="structured_kind_readonly_via_tool"). Wraps the store.
- NO false affordance: list returns the real kinds; read of an absent file says "(empty)" not a fake value.

### Scope boundaries (for the gate)
- This PR delivers the STORE + TOOL + reconstruction. The event-driven AUTO-writes ("on plan approval seed
  todo", "on verifier failure update", "on build start write goal") touch the loop/runtime and are wired in
  the integration PR (CXT-7) — same deferral pattern Codex APPROVED for CXT-1. The store EXPOSES those
  methods now so the wiring is a thin call later; the tool gives a manual path immediately. Flagging for
  APPROVE/REVISE.
- Done-when ("a resumed run CAN reconstruct goal/todo/decisions/failures/resources/direct-edits without
  replaying chat") is satisfied at the mechanism level by `store.reconstruct()` + round-trip tests.

### Tests
- core `packages/core/tests/test_artifact_memory.py` (uses an in-test in-memory WorkspaceFS fake):
  - durable files created on write (paths + extensions correct)
  - ensure_initialized creates all 9 files with safe defaults
  - missing file → read returns None/() (safe default), no raise
  - PRESENT corrupt JSON → read_json raises ContextRecoveryError (kind/rel_path/detail populated)
  - full round-trip: write goal+todo+decisions+failures+resources+direct_edits → reconstruct() yields a
    ContextLedger equal (by value) to the source; recovery_errors empty
  - resilient reconstruct: one corrupt json file → ledger has safe default for that kind + exactly one
    RecoveryNote naming the file; other kinds intact
- tools `packages/tools/tests/test_context_memory.py` (FakeSandboxInstance):
  - write+read a narrative kind round-trips through the tool
  - invalid kind → error="invalid_kind"
  - read of absent kind → success, "(empty)"
  - write of a structured kind via tool → error="structured_kind_readonly_via_tool"

### Codex review (round 1)
- verdict: REVISE — (a)-(e) ALL APPROVED (json verifier_failures justified; WorkspaceFS protocol correct;
  missing/corrupt split correct; CXT-7 deferral OK if tracked; tool md-write/json-read split sound).
  5 required revisions, all test-coverage + one matrix clarification. Log: .claude/cxt2-codex-r1.log.

### PR CXT-2 — plan REVISION 1 (post-Codex round 1)

KIND MATRIX (codified — resolves the 9-vs-10 question):
ArtifactMemoryKind has 10 values, split into:
- 9 DURABLE SINGLETONS (exactly one canonical file each; `ensure_initialized()` creates ALL 9):
  · markdown (4): current_goal.md, todo.md, decisions.md, assumptions.md
  · json (5): resource_manifest.json, direct_edits.json, unresolved_comments.json,
    source_priority.json, verifier_failures.json
- 1 MULTI-INSTANCE kind: SUMMARY — NOT a singleton, NOT initialized. It is the per-resolved-range
  summary artifact created on demand in CXT-3 (many files, e.g. `summary/<range_id>.md`), referenced
  via `ArtifactMemoryRef.rel_path`. Excluded from ensure_initialized by design.
`_SINGLETON_KINDS`, `_MD_KINDS`, `_JSON_KINDS` are module constants; a unit test asserts
`_MD_KINDS | _JSON_KINDS == _SINGLETON_KINDS` and `len == 9` so the matrix can't silently drift.

SOURCE_PRIORITY note: ContextLedger has no source_priority field (it's assembler policy, not run-state).
The store owns source_priority.json via `write_source_priority(sp)/read_source_priority()->SourcePriority`
(default `SourcePriority.default()`); it is NOT part of `reconstruct()`'s ledger, it's a sidecar the
CXT-4 assembler reads. Round-trip tested independently.

TOOL SCOPE (revision #2 — not a dead registration): add `"context_memory"` to BOTH `AGENT_TOOLS` and
`ARTIFACT_TOOLS` in registry.py (safe FS tool; advertised by default since advertised_tools=None ⇒
advertise-all-allowed). A test asserts `build_default_registry().in_scope(agent_scope(...))` includes a
tool named context_memory AND that it's in `artifact_scope()`.

TESTS (expanded per Codex):
- core test_artifact_memory.py adds: round-trip over ALL 5 structured kinds INCLUDING source_priority;
  resilient reconstruct when ANY ONE singleton file is missing (safe default for that kind, others
  intact, no spurious RecoveryNote for a merely-absent file); the `_MD_KINDS|_JSON_KINDS==_SINGLETON_KINDS,
  len==9` matrix-guard test.
- tool test_context_memory.py adds: invalid `action` (e.g. "delete") → error="invalid_action";
  structured-kind write rejection → error="structured_kind_readonly_via_tool"; `list` output is the
  deterministic SORTED set of durable kinds; AND the in-scope/advertised assertions above.

CXT-7 BINDING OBLIGATION (revision #5 — deferral tracked, not dropped): CXT-2 ships store methods + tool
ONLY; it does NOT wire runtime auto-writes. CXT-7 MUST add focused hook tests proving the runtime calls:
  build-start → store.write_goal(...);  plan-approval → store.seed_todo(...);
  verifier-failure → store.record_verifier_failures(...);  resource-import → store.record_resources(...);
  user-direct-edit → store.record_direct_edits(...).
Recorded as a tracked dependency in the CXT-7 section + the campaign open-items list below.

### Codex review (round 2)
- verdict: (pending)


## Required response format
VERDICT: APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE
REASONS:
- ...
REQUIRED_REVISIONS:
- ...
