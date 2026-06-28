# disclaude.md — Disco Build Artifact Runtime Campaign (spine)

**Status:** Authoritative campaign spine (append-only after baseline).
**Owner:** Claude Code, executing autonomously inside the isolated `disclaude` experimental repo.
**Reviewer gate:** Codex (`codex exec --sandbox read-only`), plan APPROVE required before each PR is executed.
**Operating mode:** Autonomous. No user questions. Safest/narrowest assumption, document it, proceed.
**Soak driver:** MiniMax-M3 via direct MiniMax API ONLY. No OpenRouter. No other model for soak/promotion.

This file is the immutable spine. The full authoritative campaign text was supplied by Dylan
(P0..P17, the global safety gates, the PR specs). Below is the self-contained operating contract +
the append-only ledger. Do not delete old entries; append superseding notes.

---

## Non-negotiables (carried verbatim in spirit)

- **Thesis:** Disco Build becomes a host-owned artifact runtime (context → contract → prompt pack →
  starter/AppKit/BrandKit → specialized mutation tools → preview/show tools → verification finalizer →
  export/handoff → product-harness proof). Chat = control plane. Project FS = memory. Artifact
  contract = working context. Verifier = separate. Event log = audit. ContextPack = current model view.
- **Sequencing (no P2+ promotes until P0+P0B+P1 green):**
  P0 Context Runtime · P0B Lifecycle/Safety · P1 Product Harness/Oracles · P2 Contract Runtime ·
  P3 WorkflowPromptPack · P4 Specialized Mutation Tools · P5 Preview/Show/Delivery · P6 Verification
  Finalizers · P7 Starter/Brand/UI Kits · P8 Semantic Direct Manipulation · P9 TweakSpec ·
  P10 Export/Handoff · P11 Resource Import/Provenance · P12 Content/Design Discipline · P13 Deck/Doc/
  Prototype/Media · P14 Agent Ergonomics Lab/WorldSim · P15 PiKernel Product Integration ·
  P16 AppKit Return-to-Mainline · P17 Final MiniMax-M3 Soak.
- **Global gates:** no false affordances; no finish without host truth (host-owned finalizer);
  no destructive elision (recoverable excerpts w/ path+range+hash); no manual preview ownership;
  no user-installable Pi packages; Pi never gets real provider keys (local gateway, ephemeral token);
  MiniMax-direct-only soak with zero-OpenRouter proof.
- **Per-PR workflow:** read protocol + PR + deps → write plan in this file → Codex read-only review →
  revise until APPROVE → implement (parallel subagents) → tests → fix → re-test → ledger update →
  commit+push → next PR. No user approval between PRs.
- **Codex verdicts:** APPROVE | REVISE | BLOCKED_CODEX_UNAVAILABLE. If BLOCKED, fixing the Codex review
  path becomes P0 infra; do not silently bypass the gate.

---

## Ledger

### Repository Isolation Bootstrap — 2026-06-28 02:55 UTC

- source_repo: /home/dylan/projects/Disco-Pi  (chosen over /home/dylan/projects/disco because it is the
  only checkout containing build_kernel/pi_kernel.py + the Pi-kernel substrate the campaign builds on)
- experimental_repo: /home/dylan/projects/disclaude
- branch: disclaude/experimental-20260628T025508Z
- origin_remote: /home/dylan/projects/disclaude.git (local bare; gh not installed → local-remote fallback)
- source_upstream_remote: source-upstream-origin → /home/dylan/projects/Disco-Pi
- backup-mirror neutralization: Disco-Pi's `homelab` (blackbox:git-backups/disco.git) did NOT transfer
  into the clone (clone inherits source as origin only); repo_guard additionally pattern-refuses
  *git-backups* / *Disco-Pi* origins.
- repo_guard: pass (basename==disclaude AND origin not a source/backup mirror)
- git hooks: pre-commit + pre-push both run repo_guard
- heartbeat_location: .claude/heartbeat.py inside experimental repo
- main_repo_write_policy: read-only after bootstrap (no commits/pushes/campaign edits to source)
- ASSUMPTION (documented): the 3 uncommitted fixes on Disco-Pi (prompts.py inline-first,
  turn_control.py recover-don't-kill, test_cluster2_turntaking.py) are NOT carried into disclaude
  (clone copies committed history only). They remain untouched on Disco-Pi. CXT-3/CXT-5 reimplement
  context/elision behavior natively, so this is non-blocking.

### Tooling verified — 2026-06-28 02:56 UTC

- Codex gate: `codex exec --sandbox read-only --model gpt-5.3-codex-spark --reasoning xhigh "<prompt>"`
  → returns APPROVE/REVISE/BLOCKED; smoke test rc=0 (`CODEX_OK`). codex-cli 0.142.2.
- Heartbeat: `.claude/heartbeat.py --interval 600` running (pid in .claude/heartbeat.pid); log gitignored.
- gh: NOT installed → origin is a local bare remote (`~/projects/disclaude.git`).
- Tests: `python3 -m pytest packages/core/tests/...` (asyncio_mode=auto). Type-check: basedpyright strict,
  zero errors tree-wide, tests excluded from pyright.

## PR CXT-1 — Core context models

### Status
PLANNING → CODEX_REVIEW

### Dependencies
- None (P0 root). Establishes the domain vocabulary every later PR consumes.

### Plan
Create a NEW pure-domain package `packages/core/src/disco/core/context/` with serializable, frozen
Pydantic v2 models (matching house style: `BaseModel` + `ConfigDict(frozen=True, extra="forbid")`,
`model_dump(mode="json")` / `TypeAdapter.validate_python` roundtrip). NO imports from frontend, tools,
agent-server, or loop runtime — these are stable value objects only. References to events/files are by
opaque string id / relative path, NOT by importing Event/Sandbox types (keeps the layer dependency-free
and stable, per the Codex gate's "do not depend on frontend or tool runtime details").

Files to create:
- `context/__init__.py` — re-export the public models + a `ContextModelAdapter` TypeAdapter if useful.
- `context/source_priority.py` — `SourceKind` (StrEnum: goal, contract, todo, verifier_failure,
  direct_edit, resource, comment, handoff, recoverable_ref, history) + `SourcePriority` (frozen model:
  ordered list of SourceKind defining assembly precedence; `default()` classmethod).
- `context/compaction.py` — `CompactionPolicy` (frozen: max_history_chars, max_retained_refs,
  never_compact: tuple[SourceKind,...] defaulting to (verifier_failure, direct_edit, goal, contract),
  trigger thresholds; `default()` classmethod). Pure policy data; the ACT of compacting is CXT-3.
- `context/artifact_memory.py` — `ArtifactMemoryRef` (kind, rel_path, sha256: str|None, updated_at).
  This is the typed pointer to a future `.disco/context/*` file (the files themselves land in CXT-2).
- `context/ledger.py` — the small refs + the ledger:
  - `ResolvedContextRange` (range_id, reason, event_ids: tuple[str,...], summary_ref: ArtifactMemoryRef|None)
  - `DirectEditRef` (target_id, rel_path, kind, summary, at)
  - `VerifierFailureRef` (failure_id, kind, rel_path|None, message, severity: StrEnum, resolved: bool=False)
  - `ResourceRef` (rel_path, source, sha256: str|None, license: str|None, copied_at: datetime|None)
  - `HandoffRef` (kind, rel_path)
  - `ContextLedger` (conversation_id, workspace_root: str|None, active_goal: str|None,
    active_contract: str|None, current_version: int, todo_ref: ArtifactMemoryRef|None,
    retained_refs, resolved_ranges, latest_verifier_failures, unresolved_comments (list[str] of ids),
    direct_edits, resource_manifest (list[ResourceRef]), handoff_refs). Plus `empty(conversation_id,
    workspace_root)` classmethod and immutable update helpers via `model_copy`.
- `context/pack.py` — `ContextPack` (active_goal, active_contract, current_version, current_todo: str|None,
  latest_failures: tuple[VerifierFailureRef,...] (UNRESOLVED only), unresolved_comments,
  direct_edits_summary: tuple[DirectEditRef,...], resource_refs, allowed_next_actions: tuple[str,...],
  recoverable_refs: tuple[ArtifactMemoryRef,...]) + `from_ledger(ledger, *, allowed_next_actions=())`
  classmethod that derives the compact model-facing view (filters resolved failures out, drops resolved
  ranges, caps lists per CompactionPolicy). This is the SEED of the assembler; the full event-log +
  workspace-file ingestion is CXT-2/CXT-4.

Tests (`packages/core/tests/test_context_ledger.py`, `test_context_pack.py`):
- roundtrip every model via `model_dump(mode="json")` → `validate_python` → equality
- invalid data rejected (extra field → ValidationError; bad enum value → ValidationError)
- `ContextLedger.empty(...)` minimal construction
- `ContextPack.from_ledger` excludes resolved VerifierFailureRefs and resolved ranges; includes goal,
  contract, version, todo; caps lists per policy
- immutability: model_copy(update=...) yields a new instance, original unchanged

### Scope boundaries (explicit, for the gate)
- NO wiring into the loop/runtime, NO file I/O, NO event-log reading in this PR. Pure models + pure
  derivation. (CXT-2 = durable .disco/context files; CXT-3 = compaction events; CXT-4 = prompt assembler.)
- Interpreting CXT-1 "done when a Build run can produce a ContextPack from event log + workspace files"
  as: the MODELS + a pure `ContextPack.from_ledger` exist and roundtrip; the ledger is the in-memory
  representation that CXT-2/CXT-4 populate from events+files. Flagging this scoping for APPROVE/REVISE.

### Codex review
- command: codex exec --sandbox read-only --model gpt-5.3-codex-spark "$(cat .claude/cxt1-review-prompt.md)"
  (NOTE: `--reasoning` is NOT a valid `codex exec` flag; it defaults to reasoning effort xhigh.)
- verdict (round 1): REVISE — 4 required revisions (enum base must match repo, pin enum value sets +
  id/timestamp types, from_ledger must take explicit policy, add ordering/never-compact tests).

### PR CXT-1 — plan REVISION 1 (post-Codex round 1)

Verified repo standard: core enums are `(str, Enum)` with explicit string values; `StrEnum` is used
NOWHERE in core (EventSource/EventKind/ConversationStatus/SecurityRisk/ModelRole all `(str, Enum)`).
→ ALL new enums use `(str, Enum)`. Addressing every Codex point:

ENUMS (exact value sets, all `(str, Enum)`):
- `SourceKind`: GOAL="goal", CONTRACT="contract", TODO="todo", VERIFIER_FAILURE="verifier_failure",
  DIRECT_EDIT="direct_edit", RESOURCE="resource", COMMENT="comment", HANDOFF="handoff",
  RECOVERABLE_REF="recoverable_ref", HISTORY="history".
- `Severity`: BLOCKER="blocker", ERROR="error", WARNING="warning", INFO="info".
- `ArtifactMemoryKind`: GOAL="current_goal", TODO="todo", DECISIONS="decisions",
  ASSUMPTIONS="assumptions", VERIFIER_FAILURES="verifier_failures", RESOURCE_MANIFEST="resource_manifest",
  DIRECT_EDITS="direct_edits", UNRESOLVED_COMMENTS="unresolved_comments",
  SOURCE_PRIORITY="source_priority", SUMMARY="summary". (Values intentionally mirror the `.disco/context/*`
  filenames CXT-2 will create, so the typed ref maps 1:1 to a file.)
- `DirectEditKind`: TEXT="text", STYLE="style", STRUCTURE="structure", DELETE="delete".

ID / TIMESTAMP TYPES (stabilized now):
- ids: `str`, `Field(default_factory=...)` with stable prefixes — `cxr_` (ResolvedContextRange.range_id),
  `vf_` (VerifierFailureRef.failure_id), `de_` (DirectEditRef.target_id is CALLER-supplied stable id,
  NOT auto — it maps to a data-disco-* anchor; no default_factory). Helper `_mk_id(prefix)` mirrors
  events.py `_new_id`.
- timestamps: `datetime` (UTC). Required "event" timestamps use `Field(default_factory=_now)` (copy of
  events.py `_now`). Optional ones are `datetime | None = None`: `ArtifactMemoryRef.updated_at`,
  `ResourceRef.copied_at`. `DirectEditRef.at: datetime` (default_factory=_now).

API:
- `ContextPack.from_ledger(ledger: ContextLedger, *, policy: CompactionPolicy | None = None,
  allowed_next_actions: tuple[str, ...] = ()) -> ContextPack`. Body: `policy = policy or
  CompactionPolicy.default()`. (Avoid a mutable/once-evaluated default; explicit + testable.)
- Capping rule (explicit): `from_ledger` caps the OMITTABLE source kinds (resource, recoverable_ref,
  comment, history) to policy limits, but NEVER caps/drops `never_compact` kinds
  (goal, contract, verifier_failure, direct_edit). Resolved VerifierFailureRefs and resolved ranges are
  excluded regardless (resolved ≠ compacted-away of an UNresolved item).

TESTS (added per Codex):
- `SourcePriority.default()` returns the documented precedence order; assert exact sequence + that every
  SourceKind appears exactly once.
- compaction-by-kind: a ledger over-full in BOTH an omittable kind (resource) AND a never_compact kind
  (verifier_failure) → from_ledger caps resources to policy limit but keeps ALL unresolved failures
  (never_compact exception), even when failures exceed the generic cap.
- resolved-vs-unresolved: resolved failures/ranges excluded; unresolved retained.
- plus the round-1 tests (roundtrip, extra-field reject, bad-enum reject, empty(), immutability).

### Codex review (round 2)
- verdict: APPROVE (all 4 required revisions RESOLVED; scope confirmed pure models + derivation/tests,
  no loop/runtime wiring or I/O). Logs: .claude/cxt1-codex-r2.log.
- Status → APPROVED → EXECUTING → COMPLETE.

### Implementation notes
- files created: packages/core/src/disco/core/context/{__init__,_util,source_priority,compaction,
  artifact_memory,ledger,pack}.py + packages/core/tests/test_context_{ledger,pack}.py
- decisions: enums `(str, Enum)` (repo standard, no StrEnum); pure value objects, no event/runtime
  imports; `_cap()` routes EVERY list kind through `policy.limit_for(kind)` so never_compact is the
  real exemption mechanism (not a special-case skip); ContextPack.resource_refs holds ResourceRef
  (not ArtifactMemoryRef — fixed a first-draft type error); from_ledger takes optional todo_text so
  current_todo isn't a structurally-always-None false affordance.
- ENV NOTE (durable): the fresh clone has NO synced venv. Test/typecheck via the source venv +
  PYTHONPATH override so `disco` resolves to the disclaude tree (NOT Disco-Pi):
    PP=$(ls -d "$PWD"/packages/*/src | tr '\n' ':'); export PYTHONPATH="$PP"
    /home/dylan/projects/Disco-Pi/.venv/bin/python3 -m pytest <files>
    /home/dylan/projects/Disco-Pi/.venv/bin/basedpyright <path>
  Verified disco.core.context.__file__ → /home/dylan/projects/disclaude/...  (override wins).

### Tests
- command: pytest packages/core/tests/test_context_ledger.py packages/core/tests/test_context_pack.py
- results: 13 passed
- typecheck: basedpyright packages/core/src/disco/core/context/ → 0 errors, 0 warnings, 0 notes
- failures/fixes: none (after the resource_refs type fix during authoring)

### Remaining risk
- ContextPack.current_todo / todo_text path is unexercised by real file I/O until CXT-2/CXT-4.
- No wiring yet — these models are dead code until CXT-2 (durable files) + CXT-4 (assembler) consume
  them. Intentional per sequencing.

### Next PR
- CXT-2 — Durable .disco/context files (file-backed memory + context_memory tool).

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
- verdict: REVISE — but the round-2 reasons are ALL "code/tests not present in the tree yet" (e.g.
  store.py absent, context_memory not yet in AGENT_TOOLS, test files missing). Codex switched from
  PLAN-review to CODE-existence verification — a category error for a pre-implementation plan gate. Every
  design decision (a-e) was APPROVED in round 1; round-2 required-revisions literally restate "implement
  the plan + cover with tests." Log: .claude/cxt2-codex-r2.log.
- DECISION (documented): the design is gate-approved. Proceed to IMPLEMENT the approved plan, then submit
  the ACTUAL IMPLEMENTED CODE to Codex as the binding gate before commit (Codex is reviewing code, so give
  it code). This keeps the gate meaningful and avoids a circular plan-review loop. Status → EXECUTING.

### Codex review (CODE, binding) — APPROVE
- verdict: APPROVE, REQUIRED_REVISIONS: None. Codex verified all 5 round-1 revisions IN THE IMPLEMENTED
  CODE (matrix=9 singletons + summary excluded + ensure_initialized; context_memory in AGENT_TOOLS +
  ARTIFACT_TOOLS + registered + scope-tested; tool rejections + list determinism; reconstruct round-trip
  over all structured kinds incl source_priority + missing-default + corrupt-recovery; CXT-7 tracked).
  Log: .claude/cxt2-codex-code.log. Status → COMPLETE.

### Implementation notes
- new: context/store.py (ArtifactMemoryStore, WorkspaceFS Protocol, ContextRecoveryError, RecoveryNote,
  ReconstructResult, _MD_KINDS/_JSON_KINDS/_SINGLETON_KINDS); builtin/context_memory.py (ContextMemoryTool).
- changed: context/__init__.py (exports); builtin/__init__.py (register + __all__);
  registry.py (AGENT_TOOLS + ARTIFACT_TOOLS += "context_memory").
- decisions: WorkspaceFS Protocol keeps core dependency-free; verifier_failures stored as .json (single
  source of truth, faithful reconstruction) — documented deviation from doc's .md; tool writes narrative
  md kinds only (structured kinds read-only via tool → no agent-authored malformed JSON); decisions/
  assumptions reconstruct as retained_refs (no scalar ledger field); MISSING→default vs CORRUPT→recovery.

### Tests
- 32 passed: test_artifact_memory.py (14) + test_context_memory.py (CXT-2) + CXT-1 (13). Regression:
  test_think_tool.py + test_agent_tools.py green. basedpyright strict: 0 errors on all changed files.

### Remaining risk / tracked follow-up
- CXT-7 BINDING: runtime auto-write hooks (build-start→write_goal, plan-approval→seed_todo,
  verifier-failure→record_verifier_failures, resource-import→record_resources, direct-edit→
  record_direct_edits) NOT wired in CXT-2 — MUST be added + hook-tested in CXT-7. Tracked.

### Next PR
- CXT-3 — Context resolve / snip equivalent (ContextResolvedEvent/SummaryEvent/CompactionEvent +
  context_mark_resolved/context_write_summary/context_compact_if_needed; audit log never deleted).

## PR CXT-3 — Context resolve / snip equivalent

### Status
PLANNING → CODEX_REVIEW

### Dependencies
- CXT-1 (ResolvedContextRange, CompactionPolicy, ArtifactMemoryRef), CXT-2 (SUMMARY artifact via store).

### Key finding (drives the design)
disco ALREADY has reversible span-tombstoning: `CondensationEvent` (seq range [start,end] + inline
summary + reason∈{request,tokens,events,hard_reset}, SYSTEM-sourced, NOT LLMConvertible) applied by
`View.of` (drops forgotten seqs, emits summary in place) and reversed by `View.recover_span`. Plus
`microcompact()` (deterministic system NO-OP tombstoning). These are IMMEDIATE + SYSTEM-driven.
The campaign's `snip` is DEFERRED + AGENT-driven (mark now → execute removal together later, only once
durable state exists). → ADDITIVE-BUT-REUSING design: add the agent-intent layer; REUSE CondensationEvent
as the actual tombstone so View filtering + recovery are inherited (NO second way to forget).

### Plan
(A) THREE NEW EVENTS in events.py (add to EventKind, define class, add to Event union, export in __init__):
- `ContextResolvedEvent` (source=AGENT, NOT LLMConvertible): the DEFERRED snip mark. Fields:
  range_id:str (default cxr_ via a factory), forgotten_start_seq:int, forgotten_end_seq:int, reason:str,
  summary_ref_path:str|None=None (set once a durable summary is written). Does NOT itself change the View.
- `ContextSummaryEvent` (source=SYSTEM, NOT LLMConvertible): records that a durable summary file was
  written for a range. Fields: range_id:str, artifact_kind:Literal[...]=summary, rel_path:str, summary:str.
  This is the "durable state exists elsewhere" precondition record.
- `ContextCompactionEvent` (source=SYSTEM, NOT LLMConvertible): execution bookkeeping — which resolved
  range_ids were converted to CondensationEvent tombstones. Fields: range_ids:tuple[str,...],
  reason:str="resolved". (The actual forgetting is the emitted CondensationEvent; this records the decision.)
All three NOT LLMConvertible (audit/bookkeeping); the model never sees raw snip metadata — it sees the
CondensationEvent's inline summary once compaction executes (existing View behavior).

(B) THREE PURE FUNCTIONS in context/compaction.py (extend; pure over event lists — return events to append,
no store/loop calls; CXT-7 wires them, same deferral pattern as CXT-1/CXT-2):
- `context_mark_resolved(start_seq, end_seq, reason, *, range_id=None) -> ContextResolvedEvent`.
- `context_write_summary(range_id, rel_path, summary) -> ContextSummaryEvent`.
- `context_compact_if_needed(events, policy, *, protected_seqs=frozenset(), pressure_chars=None)
   -> list[Event]`: scans events for pending ContextResolvedEvents NOT yet executed (no later
   ContextCompactionEvent naming them). For each, compacts ONLY IF:
     (1) a ContextSummaryEvent exists for that range_id (durable summary precondition — NEVER compact
         without state elsewhere), AND
     (2) the range [start,end] does NOT overlap any protected_seq (unresolved failures never compacted),
         AND
     (3) pressure warrants it: pressure_chars is None (caller forces) OR pressure_chars >
         policy.max_history_chars.
   Returns a CondensationEvent (reason="request", summary=the durable summary, range=[start,end]) per
   eligible range + ONE ContextCompactionEvent listing the executed range_ids. Ineligible ranges are
   left pending (deferred). Idempotent: already-executed ranges are skipped.

(C) ContextLedger linkage (so CXT-4 can surface): add a pure helper
`resolved_ranges_from_events(events) -> tuple[ResolvedContextRange,...]` mapping ContextResolvedEvents
(with their summary_ref) into CXT-1 ResolvedContextRange objects. (Read-only projection; no new ledger field.)

NO new filtering logic, NO context_builder.py (doesn't exist; CXT-4 introduces the assembler). View.of is
untouched — it already filters the CondensationEvents we emit.

### Scope boundaries (for the gate)
- Pure events + pure functions only. Wiring into the live loop (emit on agent snip tool-call, call
  context_compact_if_needed under real token pressure) is CXT-7. Flagging.
- "Resolved ranges omitted from ContextPack / model view" is satisfied via the emitted CondensationEvent +
  existing View.of (tested through View.of, the REAL model context). "Summary path included" via
  ContextSummaryEvent.rel_path + resolved_ranges_from_events → ResolvedContextRange.summary_ref.

### Tests (packages/core/tests/test_context_compaction.py)
- new events roundtrip through EventAdapter (union integrity); each is NOT LLMConvertible.
- context_mark_resolved produces a deferred mark that ALONE does NOT change View.of(events).messages.
- context_compact_if_needed WITHOUT a ContextSummaryEvent → returns [] (never compact w/o durable state).
- with a summary + pressure → returns a CondensationEvent([start,end]) + ContextCompactionEvent; after
  appending, View.of omits the range's messages BUT View.recover_span returns them (audit complete).
- protected_seqs overlap → that range is NOT compacted (unresolved failures never compacted away).
- idempotent: re-running after execution returns [] for the same range.
- resolved_ranges_from_events maps marks → ResolvedContextRange with summary_ref set after a summary.

### Codex review (round 1)
- verdict: REVISE. (a) reuse-CondensationEvent APPROVED. Substance: tighten guards (idempotence via
  existing tombstones not a bookkeeping event; non-empty summary as durability proof; overlap skip +
  deterministic order); ContextCompactionEvent likely redundant; FRONTEND eventDisposition mirror MUST be
  updated or its contract test breaks. Log: .claude/cxt3-codex-r1.log.

### PR CXT-3 — plan REVISION 1 (post-Codex round 1)

DROP ContextCompactionEvent (Codex c). Only TWO new events: `ContextResolvedEvent` (deferred mark) +
`ContextSummaryEvent` (durable-summary record). Execution is INFERRED from existing CondensationEvents
(a resolved range is "already executed" iff an existing CondensationEvent's [start,end] covers it) — no
separate bookkeeping event, smaller union surface.

HARDENED context_compact_if_needed(events, policy, *, protected_seqs=frozenset(), pressure_chars=None):
- Collect existing forgotten ranges from ALL CondensationEvents → `already_forgotten`.
- Collect pending ContextResolvedEvents; map range_id → its ContextSummaryEvent (if any).
- Process candidates in DETERMINISTIC order: ascending forgotten_start_seq, then range_id.
- Emit a CondensationEvent for a candidate ONLY IF ALL hold:
  (1) a ContextSummaryEvent exists for range_id AND its `summary` is NON-EMPTY (stripped) — this is the
      durability proof: the non-empty summary is the very content inlined into the tombstone, so the
      forgotten span is provably replaced by real content (strongest pure precondition, no FS needed);
  (2) [start,end] does NOT overlap any protected_seq (unresolved failures never compacted);
  (3) [start,end] is NOT already covered by `already_forgotten` NOR by a range emitted earlier in THIS
      pass (idempotence + no overlapping double-forget);
  (4) pressure: pressure_chars is None (caller forces) OR pressure_chars > policy.max_history_chars.
- Returns list[CondensationEvent] (reason="request", summary=durable summary). Re-running after execution
  returns [] (the new CondensationEvents now appear in already_forgotten). Idempotent by construction.

FRONTEND MIRROR (Codex e): add "context_resolved","context_summary" to frontend KNOWN_EVENT_KINDS +
EVENT_DISPOSITION as `suppressed` (internal context-compaction markers, exactly like `condensation`) so
the enforced disposition contract test stays green and these never leak as user cards. Also keep the
Python↔TS EventKind contract test green (locate + update if it pins the TS list against the enum).

EVENTS (final): ContextResolvedEvent(source=AGENT, NOT LLMConvertible; range_id, forgotten_start_seq,
forgotten_end_seq, reason, summary_ref_path:str|None=None); ContextSummaryEvent(source=SYSTEM, NOT
LLMConvertible; range_id, rel_path, summary, artifact_kind default "summary").

Tests unchanged from above MINUS the ContextCompactionEvent assertions PLUS: non-empty-summary guard
(empty summary → not compacted); overlap-with-existing-CondensationEvent skip; frontend disposition test
still green (run vitest on eventDisposition.test.ts if feasible, else assert via the python contract test).

### Codex review (CODE, binding) — APPROVE
- verdict: APPROVE, REQUIRED_REVISIONS: None. Verified in code: 2 new events in union + serde unaffected,
  both guards (non-empty-summary durability + protected-seq) hole-free, idempotence/overlap correct,
  ContextCompactionEvent dropped, frontend mirror + python contract green, View omission/recovery
  inherited. Log: .claude/cxt3-codex-code.log. Status → COMPLETE.

### Implementation notes
- events.py: +EventKind.CONTEXT_RESOLVED/CONTEXT_SUMMARY, +ContextResolvedEvent (AGENT, deferred mark) +
  ContextSummaryEvent (SYSTEM, durable-summary record), both NOT LLMConvertible, both in Event union.
- compaction.py: context_mark_resolved / context_write_summary / context_compact_if_needed (reuses
  CondensationEvent as the tombstone) / resolved_ranges_from_events (→ CXT-1 ResolvedContextRange).
- exports in context/__init__ + core/__init__; frontend eventDisposition.ts mirror (2 kinds, suppressed).
- DESIGN: additive-but-reusing — no second forgetting path; View.of omission + recover_span inherited.

### Tests
- 48 passed: test_context_compaction.py + test_event_kind_frontend_contract.py (lockstep) + CXT-1/2 +
  cluster1_context + cluster7_knowledge regressions. basedpyright strict: 0 errors.

### Remaining risk / tracked follow-up
- Loop wiring deferred to CXT-7: emit ContextResolvedEvent on an agent snip tool-call; call
  context_compact_if_needed under real token pressure with protected_seqs = unresolved-failure seqs;
  write the durable SUMMARY file (CXT-2 store) alongside ContextSummaryEvent.

### Next PR
- CXT-4 — ContextPack prompt assembler (stop feeding raw event history as primary context; assemble
  stable prefix + ContextPack + recent turns + recoverable refs + tool schema).

## PR CXT-4 — ContextPack prompt assembler

### Status
PLANNING → CODEX_REVIEW

### Dependencies
- CXT-1 (ContextPack/ContextLedger/from_ledger), CXT-2 (store.reconstruct), CXT-3
  (resolved_ranges_from_events).

### Key seam findings (scout)
- The system prompt is injected in routing._inject_prompt, which has NO access to the event log — so the
  pack cannot be assembled there. Events live at the View/loop layer.
- A `<current-objective>` recitation block (view.py:_recitation_message, C6-cadence-gated) ALREADY renders
  goal + plan progress + next step into the model context. CXT-4 must NOT add a duplicate goal/todo block.
- No existing surfacing of unresolved verifier failures into context.

### Decision (scope) — pure assembler + renderer now, live wiring at CXT-7
Deliver the ASSEMBLER + RENDERER as pure, fully-tested mechanism (the thing the campaign names). DEFER the
behavior-flip (replacing raw history / unifying with the recitation block) to CXT-7 integration, where the
context-runtime integration tests live. Rationale: routing._inject_prompt lacks events; the live seam is
the ViewBuilder/recitation, and replacing it touches C6-covered behavior — high risk under no integration
harness yet. Same deferral pattern Codex APPROVED for CXT-1/2/3. Flagging for APPROVE/REVISE.

### Plan — packages/core/src/disco/core/loop/context_builder.py (NEW; the campaign names this file)
- `build_context_pack(events, *, base_ledger=None, policy=None, todo_text=None, failures=(),
   allowed_next_actions=()) -> ContextPack`:
  - start from base_ledger (a ContextLedger, e.g. from CXT-2 store.reconstruct of .disco/context files) or
    ContextLedger.empty("").
  - OVERLAY event-derived truth: active_goal ← latest PlanEvent.summary (fallback: head USER MessageEvent
    text); current_version ← latest PlanEvent.revision; resolved_ranges ← resolved_ranges_from_events(events);
    latest_verifier_failures ← `failures` arg (caller supplies from store/ledger — NOT heuristically mined,
    avoids a fragile event-scan); retained_refs ← base retained + each resolved range's summary_ref.
  - return ContextPack.from_ledger(merged_ledger, policy=policy, todo_text=todo_text,
    allowed_next_actions=allowed_next_actions).
- `render_context_pack(pack, priority=None) -> str`: a DETERMINISTIC, byte-stable `<context-pack>` block,
  sections ordered by SourcePriority.default(): goal, contract, version, todo, unresolved failures (compact
  one-line each), recoverable refs (paths only), allowed next actions. Empty sections omitted. Stable across
  turns when inputs unchanged (enables prompt-cache prefix reuse). No event history, no raw tool chatter.
- `_latest_plan(events)` helper (mirror view.py's, local) to read summary/revision without importing view.

### Scope boundaries (for the gate)
- PURE functions only; NO change to routing/_inject_prompt, ViewBuilder, or _recitation_message (so C6 tests
  + live behavior are untouched). CXT-7 wires build/render into the actual prompt + decides the
  recitation-unification / history-replacement.
- failures supplied by caller (store/ledger) — not mined from events here (fragile + duplicates CXT-2).

### Tests (packages/core/tests/test_context_pack_prompt.py)
- build_context_pack overlays goal+version from the latest PlanEvent (and head-user-message fallback when
  no plan); folds resolved-range summary_refs into recoverable_refs.
- failures arg surfaces as latest_failures (unresolved only — resolved excluded by from_ledger).
- render_context_pack: included-once (single `<context-pack>` block); deterministic/byte-stable across two
  calls with identical input; sections appear in SourcePriority order; empty sections omitted; NO raw event
  history present; a resolved-range summary path appears under recoverable refs.
- policy caps respected in the rendered refs (reuses from_ledger capping).

### Codex review (plan) — APPROVE
- verdict: APPROVE (round 1, no required revisions). All 5 judgments affirmed: pure-now/wire-at-CXT-7 is
  correct (routing seam lacks events); caller-supplied failures correct; goal←PlanEvent.summary right;
  byte-stable render sound. Log: .claude/cxt4-codex-r1.log. Status → EXECUTING.
- CXT-7 CARRIED NOTE (binding): when wiring, lock the integration contract so the context-pack does NOT
  duplicate the existing C6 `<current-objective>` recitation — decide prefix-pack + retain/replace-recitation
  strategy with de-dup. Tracked.

### Codex review (CODE, binding) — APPROVE (after 1 fix)
- code review caught a real bug: `failures or led.latest_verifier_failures` ignored an explicit `()`
  (caller couldn't CLEAR stale failures). Fixed → `failures: tuple|None=None` +
  `failures if failures is not None else led...`; added test_explicit_empty_failures_clears_ledger_failures.
  Targeted re-review → APPROVE. Logs: .claude/cxt4-codex-code.log.

### Implementation notes
- new: loop/context_builder.py (build_context_pack, render_context_pack, _latest_plan, _head_user_text).
- PURE; no routing/ViewBuilder/recitation change. goal←PlanEvent.summary (head-USER fallback),
  version←revision, resolved summary_refs→recoverable_refs, failures caller-supplied (None keeps ledger,
  () clears). render: byte-stable `<context-pack>`, SourcePriority order, empty-omit, no raw history.

### Tests
- 11 passed (test_context_pack_prompt.py). basedpyright strict: 0 errors.

### Remaining risk / tracked follow-up (CXT-7)
- WIRE build/render into the live prompt; lock integration contract to avoid duplicating the C6
  `<current-objective>` recitation (Codex carried note); supply failures + todo_text from the store/ledger.

### Next PR
- CXT-5 — Recoverable compression (remove destructive elision from source-like observations; recoverable
  excerpts with path/range/sha256 + a harness scan for forbidden elision strings).

## PR CXT-5 — Recoverable compression (no destructive elision)

### Status
PLANNING → CODEX_REVIEW

### Dependencies
- CXT-2 (store/SUMMARY artifact patterns), CXT-3 (recover patterns). Independent of CXT-4 wiring.

### ELISION SITE MAP (scout — durable; the basis for scope)
ALREADY RECOVERABLE (marker names a recover path — DO NOT churn these, they're tested):
- events.py snip_content (obs truncation): "… [snipped N chars — re-run the tool or use file_read …] …"
- view_render.py snapshot file head/tail: "… [N more chars — file_read(path, offset, limit) …] …"
- files.py FileReadTool paging: header "[lines X-Y of N; read more with offset=Z]"
- system.py shell HS-01 spill: full stdout → .disco-spill-*.log + "full output at <path> — file_read/grep"
- dedup.py F8/F9/W2: all name a recover path / earlier result.
- events.py _snip_args ARG marker: destructive-looking BUT defended by K1 guard + K1 recovery from the
  event log (history placeholder, not final-file content). Leave as-is (already mitigated).

TRULY DESTRUCTIVE (the CXT-5 target — NO recover path):
- browser.py console/network/stack truncation: "... (console truncated to N lines)",
  "... (stack truncated to 6 lines)", "... (N more network failures)" — content lost; re-run is expensive.

### Plan (narrow + high-value; no broad rewrite of working recoverable sites)
1. NEW `packages/core/src/disco/core/observations.py` (campaign names it): a small pure
   `recoverable_excerpt(content, *, path, shown_start, shown_end, recover_tool, recover_args) -> dict`
   producing the campaign's structured form {kind:"file_excerpt", path, complete:bool, shown_ranges,
   omitted_ranges, sha256, recover:{tool,args}} + `DESTRUCTIVE_ELISION_MARKERS` (the forbidden strings)
   + `scan_for_destructive_elision(text) -> list[str]` (finds forbidden markers NOT accompanied by a
   recover hint). Pure, fully unit-tested. This is the canonical pattern + the scan engine.
2. FIX browser.py destructive truncation → recoverable: when console/network/stack exceed caps, write the
   FULL rendered diagnostics to a spill file (mirror shell HS-01: `.disco-spill-browser-*.log` via
   ctx.sandbox.write_file in run()) and change the markers to name it
   ("… N more — full diagnostics at <path>; file_read it"). The truncated head stays for at-a-glance;
   nothing is lost. structured payload carries the spill path.
3. HARNESS SCAN: a test (packages/core/tests/test_recoverable_excerpts.py) that
   scan_for_destructive_elision flags the forbidden strings, PASSES the already-recoverable markers
   (they name a path / "file_read" / "re-run"), and that the browser markers post-fix are recoverable.
   (Final-file scan over built deliverables is a product-harness concern → P1; here we ship + unit-test
   the scan engine and document the P1 hook.)

### Scope boundaries (for the gate)
- Do NOT rewrite the already-recoverable sites (snip_content/snapshot/file_read/shell-spill) — they
  already satisfy "recoverable" and are tested; churning them is risk without benefit. Flag this.
- The structured recoverable_excerpt format is introduced + available; retrofitting every tool to emit it
  is deferred (the existing prose markers already carry recovery). Browser is fixed because it's the only
  DESTRUCTIVE site.
- Final-deliverable elision scan (fail builds containing forbidden strings) = P1 product-harness; CXT-5
  ships the scan engine + unit tests + documents the hook.

### Tests (packages/core/tests/test_recoverable_excerpts.py + packages/tools/tests/test_browser*.py)
- recoverable_excerpt: roundtrips, sha256 stable, complete=False when omitted, recover ref present;
  shown/omitted ranges correct.
- scan_for_destructive_elision: flags "...(elided)...", "[trimmed]", "content omitted",
  "truncated for brevity"; does NOT flag a snip marker that names file_read/re-run/a path.
- browser fix: when diagnostics exceed caps, a spill file is written, markers name it, head retained,
  nothing lost; assert no destructive (no-recover) marker remains.

### Codex review (round 1)
- verdict: REVISE. Scope affirmed (no-churn correct; browser is the real destructive site). Codex CAUGHT
  a MISSED destructive site: bootstrap.py:246 "... (truncated)" (prose-only recovery). 5 required revisions.
  Log: .claude/cxt5-codex-r1.log.

### PR CXT-5 — plan REVISION 1 (post-Codex round 1)
1. bootstrap.py:246 — FIX now (not defer): change "... (truncated)" → name the recover path explicitly
   ("… N more — read the full manifest at <path>") so it carries a recover cue and the scan won't flag it.
2. scan_for_destructive_elision SEMANTICS (precise, to avoid false-positives on legacy recoverable markers):
   - DESTRUCTIVE_ELISION_MARKERS (exact, case-insensitive substrings): "(elided)", "[trimmed]",
     "content omitted", "truncated for brevity", "[content omitted]".
   - RECOVER_CUES (tokens that make a truncation recoverable): "file_read", "re-run", "rerun", "offset=",
     "grep", "read the full", "full output at", "full diagnostics at", "read the manifest", "read more".
   - A text segment (split on lines) is DESTRUCTIVE iff it contains a DESTRUCTIVE marker AND contains NO
     RECOVER_CUE on that line. (So the existing recoverable markers — which all carry a cue — never flag;
     the browser/bootstrap destructive forms — pre-fix — DO flag.)
   - NOTE: the bare "…"/"..." ellipsis is NOT in the destructive set (too common as a head/tail separator);
     we flag only the explicit phrases above. Documented exception policy.
3. browser fix: structured payload field `diagnostics_spill_path` + prose marker naming it (mirror shell
   HS-01's structured["spill_path"] + marker), not marker text alone.
4. scan tests: explicit POS (flags the 4 forbidden phrases w/o cue) + NEG (does NOT flag snip_content's
   "snipped … file_read", shell spill's "full output at <path>", file_read header "read more with offset=",
   bootstrap post-fix marker) pairs.
5. P1 HOOK (named): the final-deliverable scan is owned by the P1 Product Harness `OutputTruthOracle`
   (HARN-2), rule set = DESTRUCTIVE_ELISION_MARKERS v1 (from observations.py), acceptance = 0 destructive
   markers in built deliverable files unless the user explicitly requested elision. CXT-5 ships the engine
   + constants; HARN-2 imports and enforces them. Tracked in the P1 section.

### Codex review (CODE, binding) — APPROVE (after 3 fixes)
- round-1 code review REVISE → 3 fixes: (1) WIRED the scan into the real harness OutputTruthOracle
  (not deferred) — new fc.DESTRUCTIVE_ELISION (P1) + per-deliverable scan + allow_elision waiver;
  (2) browser spill-failure fallback now carries full diagnostics in structured + a non-recoverable note
  (no false recoverability claim); (3) harness oracle regression tests. Targeted re-review → APPROVE.
  Logs: .claude/cxt5-codex-code.log. Status → COMPLETE.

### Implementation notes
- new: core/observations.py (recoverable_excerpt, DESTRUCTIVE_ELISION_MARKERS, RECOVER_CUES,
  scan_for_destructive_elision). FIXED destructive sites: browser.py (spill full console+network →
  .disco-spill-browser-*.json + structured path + prose pointer; failure fallback keeps diagnostics),
  bootstrap.py (truncation marker now names file_read+manifest). WIRED: harness OutputTruthOracle scans
  asserted deliverables → fc.DESTRUCTIVE_ELISION (P1), waivable. Other truncation sites already
  recoverable (documented; not churned).

### Tests
- 9 core+tools + 21 harness oracle (incl 3 new) + browser regression. basedpyright strict: 0 errors.

### Codex CAUGHT (credit): a destructive site the scout MISSED (bootstrap.py) + that the P1 oracle
  already existed (so enforcement should be wired now, not deferred). Both addressed.

### Next PR
- CXT-6 — todo.md as active working memory (PlanEvent=approved contract; todo.md=live execution memory;
  todo_update tool; scope-change still needs revised PlanEvent).

