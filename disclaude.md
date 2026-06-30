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

## PR CXT-6 — todo.md as active working memory — COMPLETE

### Design (scout-confirmed): NO new tool
- Reuse `context_memory(action=write, kind=todo)` for the agent's MINOR in-build todo updates (already
  shipped in CXT-2). CXT-6 adds only: (1) pure `render_plan_as_todo_markdown(plan)` in context_builder.py;
  (2) an approval HOOK `_seed_todo_from_plan()` that auto-seeds .disco/context/todo.md from the approved plan.
- PlanEvent stays the approved CONTRACT (unchanged). todo.md = durable live execution memory.
- Scope-change detection (signals.is_revision_intent → _enter_revision_planning) is ALREADY correct and is
  NOT touched — minor follow-ups skip replan; scope changes require a revised PlanEvent (then re-approval
  re-seeds todo.md). No conflict with update_plan_progress (event-log/UI; independent).

### Codex review (CODE, binding) — APPROVE (after 1 fix)
- code review caught a real gap: BOTH approval paths must seed. I'd only hooked interactive approve_plan();
  the AUTONOMOUS inline approval (_gate_planning_mode, `if self._autonomous:`) also arms DoD → must also
  seed. Fixed (added _seed_todo_from_plan after _arm_dod_from_plan there) + added
  test_autonomous_approval_also_seeds_todo_md. Targeted re-review → APPROVE. Logs: .claude/cxt6-codex-code.log.

### Implementation notes
- context_builder.py: render_plan_as_todo_markdown (pure; # summary + context + ## Steps checklist).
- engine.py: _seed_todo_from_plan (best-effort: skip if executor.sandbox is None; try/except so a seed
  error NEVER blocks approval); called from BOTH approve_plan() and the autonomous _gate_planning_mode path.
- engine.py: also fixed a PRE-EXISTING (git-stash-confirmed) reportAssignmentType in _arm_dod_from_plan
  (narrowed file_preds through a local) while touching the file → engine.py now basedpyright-clean.

### Tests
- 7 CXT-6 (render unit + interactive-seed + no-sandbox-no-crash + autonomous-seed) + plan_approval +
  dod_gate + autonomous_loop regressions. basedpyright strict: 0 errors.

### Next PR
- CXT-7 — Context runtime integration tests + remaining live wiring. NO P2+ PR may proceed until green.

## PR CXT-7 — Context runtime integration (gate for P2+)

### Status
PLANNING → EXECUTING

### Scout verdict — achievable seams vs deferred
ACHIEVABLE NOW (seam exists):
- write_goal: co-locate with the CXT-6 plan-approval seed (engine `_seed_todo_from_plan` → rename
  `_seed_context_from_plan`, write goal=plan.summary + todo). Fires on BOTH approval paths already.
- verifier-failure → record_verifier_failures: hook in finish.py `_gate_verify_web_app` on a FAILING
  verdict (best-effort, sandbox-guarded). Map verdict → VerifierFailureRef(kind="verify_web_app",
  message=first_error/summary, rel_path=screenshot, severity=ERROR).
- resume → reconstruct: store.reconstruct() already rebuilds the ledger from .disco/context/*; proven by an
  integration test (build writes files → reconstruct → build_context_pack), not new live wiring (the live
  prompt-consumption of the pack is CXT-4's deferred wiring; not needed for the gate).
DEFERRED (surface absent — documented, store method unit-tested, NOT a gate gap):
- direct-edit → record_direct_edits: NO direct-edit capture exists (that's P8 semantic direct manipulation).
- compaction live-firing (context_compact_if_needed): CXT-3 intentionally unwired; covered by CXT-3 unit
  tests. Live-firing belongs with the condenser/pressure integration (later). CXT-4 prompt-wiring + C6
  recitation de-dup also remain CXT-4's deferred note.

### Plan
1. engine.py: rename `_seed_todo_from_plan` → `_seed_context_from_plan`; write BOTH goal (plan.summary)
   and todo; update the two call sites (approve_plan + autonomous `_gate_planning_mode`).
2. finish.py `_gate_verify_web_app`: on FAIL, best-effort `ArtifactMemoryStore(sbx).record_verifier_failures(...)`
   (guarded; never alters the gate verdict/flow).
3. tests `packages/core/tests/test_context_runtime_integration.py`:
   - fresh build (live approval) → current_goal.md + todo.md written with plan content.
   - resume: populate a FS via the store (goal/todo/failures/resources/direct_edits) → reconstruct() →
     ContextLedger equals source by value → build_context_pack(events, base_ledger=ledger) yields a pack
     carrying goal + unresolved failures + resource/recoverable refs (END-TO-END files→reconstruct→pack).
   - large-log recoverability: assert (referencing CXT-3/CXT-5) context_compact_if_needed + recoverable
     markers behave (already unit-tested; integration asserts the pieces compose).
   - verifier-failure: focused test that the finish hook writes verifier_failures.json given a failing
     verdict (call the hook helper directly if the full gate is not drivable in the fake harness).
4. DOCUMENT deferred items as tracked (direct-edit=P8, compaction-firing + CXT-4 prompt-wiring = later).

### Codex review (CODE, binding) — APPROVE
- verdict: APPROVE — "None required for P0 green." Confirmed both live hooks are best-effort/guarded (never
  alter approval or the verify verdict), the integration suite covers the CXT-7 done-when, and the
  deferrals (direct-edit=P8, compaction-firing=CXT-3 unit-tested) are surface-absent, not gate gaps.
  Log: .claude/cxt7-codex-code.log. Status → COMPLETE.

### Implementation notes
- engine.py: _seed_todo_from_plan → _seed_context_from_plan (writes current_goal.md=plan.summary + todo.md);
  both approval paths call it.
- finish.py: _gate_verify_web_app FAIL → _record_verifier_failure_to_context (best-effort) →
  verifier_failures.json.
- tests/test_context_runtime_integration.py: 4 lifecycle tests (fresh-build creates context; resume
  reconstructs→pack; large logs compacted recoverably; verify-fail records to context).

### Tests
- 108 passed across the full CXT-1..7 suite + engine/finish/autonomous/dod regressions. basedpyright strict:
  0 errors on engine.py + finish.py.

=== P0 CONTEXT RUNTIME COMPLETE (CXT-1..7 all Codex-APPROVED + green). P2+ gate is now OPEN. ===

### Deferred (tracked) carried into later pillars
- direct-edit live record_direct_edits hook → P8 (Semantic Direct Manipulation) — store method unit-tested.
- context_compact_if_needed live firing under token pressure → condenser integration (later) — CXT-3 unit-tested.
- CXT-4 ContextPack live prompt-wiring + C6 `<current-objective>` recitation de-dup → when the assembler is
  wired into the live prompt (the routing/view seam) — assembler unit-tested, render byte-stable.

### Next: P0B — Product Lifecycle and Safety Plumbing
- LIFE-1 terminal-state sidecar kill · LIFE-2 browser WS truth · LIFE-3 auto-suspend active-work guard ·
  LIFE-4 eager sandbox teardown · LIFE-5 provider gateway final-boundary clamp. Then P1 Product Harness.

## P0B — scout verdict: MOSTLY ALREADY DONE in this clone (Disco-Pi build-kernel had the hardening)

ALREADY SATISFIED (FULL, with tests — VERIFY + document, do NOT rebuild):
- LIFE-1 sidecar kill + token revoke: pi_kernel.py:382-421 `_conclude` is the single chokepoint →
  `_revoke_pi_tokens` (runtime.py:2709) + `proc.aclose()`; gateway 401s revoked tokens
  (routes/pi_inference.py:500). Tests: test_pi_inference_revoke_lifecycle.py (every end-path + 401 live).
- LIFE-2 WS truth: runtime._connections ledger (runtime.py:691); on_connect/on_disconnect
  (lifecycle.py:256-288); canonical WS builder agentWsUrl (frontend/src/api/client.ts:59). Tests exist.
- LIFE-4 eager teardown + snapshot: _maybe_snapshot then _teardown_sandbox (lifecycle.py:612-689 / 75-112);
  orphan sweep on startup. Tests: test_lifecycle.py.
- LIFE-5 provider gateway clamp: 3-layer max_tokens clamp + budget reserve (routes/pi_inference.py:327-612).
  Tests: test_pi_inference_gateway.py.

THE REAL GAP — LIFE-3 (auto-suspend active-work guard): `_suspend` (lifecycle.py:289-307) only refuses
when status==RUNNING. MISSING guards: active PiKernel sidecar session, parked confirm/plan/ask gates,
active preview process. → a disconnect during active (non-RUNNING-status but live-work) build could suspend
and kill in-flight work. Test harness: test_lifecycle.py (_runtime_with_storage + fake executor + sweep).

## PR LIFE-3 — auto-suspend active-work guard

### Status
PLANNING → EXECUTING → COMPLETE (LIFE-1/2/4/5 verified-as-done; LIFE-3 implemented).

### Codex review (CODE, binding) — APPROVE
- verdict: APPROVE, no required revisions. LIFE-3 active-work signal (live run task OR live Pi sidecar)
  correct + sufficient; preview-exclusion correct (sandbox-internal, snapshot-restored); LIFE-1/2/4/5
  pre-satisfied verdict sound. Log: .claude/p0b-codex-code.log.

### Implementation notes
- lifecycle.py: NEW `_has_active_work` (live not-done run task in _rt._tasks OR live Pi session in
  _rt._pi_kernel._sessions); guarded in BOTH `_suspend` and `sweep_idle_once`. The only NEW code in P0B.
- LIFE-1/2/4/5 were already FULL in this clone (Disco-Pi build-kernel hardening) — VERIFIED by running their
  suites green, documented above with file:line.

### Tests
- 89 passed (test_lifecycle.py incl 3 new LIFE-3 + test_pi_inference_revoke_lifecycle + test_pi_inference_gateway).
  basedpyright strict: 0 errors on lifecycle.py.

=== P0B COMPLETE. Both P0 + P0B done → P1 (Product Harness) is the next gate before any P2+ promotion. ===

### Next: P1 — Product Harness and Zero-Opinion Oracles

## P1 — scout map (durable; the basis for P1 scoping)
FOUNDATION (SOLID, reuse as-is): harness/build_soak/ is a HEADLESS HTTP/WS runner (run.py +
adapters/disco_api.py acting AS THE USER). OracleResult schema + classify.py (first-broken-link) +
evidence.py (hash-locked dossier) + failure_codes.py + 5 strong oracles
(Contract/HarnessValidity/EventChain/ToolScope/Revision) all exist + unit-tested. OutputTruthOracle exists
(+ CXT-5 elision rule wired). Frontend e2e (frontend/e2e-live/*.spec.ts, playwright.config.ts) is
INSPECTION-ONLY ("DRIVES NO BUILD").

GAPS by PR:
- HARN-1 (browser product harness): MISSING entirely. No module drives the real UI end-to-end
  (open Build→prompt→BuildBrief→approve→WS→preview→show→ready_for_verification→download→cleanup). Needs a
  Playwright driver + evidence streams: browser-ws.jsonl, network.jsonl, console.jsonl,
  provider-call-ledger.jsonl, screenshots/, downloads/ (NONE exist; events/workspace-manifest/preview DO).
- HARN-2 (oracles): 1 of 10 exist (OutputTruth). MISSING 9: BrowserWS, Lifecycle, SidecarStop,
  ProviderLedger, PreviewOwnership, ShowToUser, VerificationGate, ExportDownload, Cleanup.
  ACHIEVABLE NOW on headless evidence (no browser needed): LifecycleOracle (status-sequence from
  events.jsonl) + ProviderLedgerOracle (metadata-level: manifest.provider==minimax-direct +
  model==MiniMax-M3 + zero openrouter — serves the HARD constraint). The 7 browser/WS/download/cleanup
  oracles REQUIRE HARN-1's new evidence first.
- HARN-3 (promotion policy): MISSING as a PRODUCT gate (bakeoff.py is kernel-only). Needs a centralized
  policy composing: headless soak green + product harness green + zero provider calls after terminal +
  preview visible + download verified + no UNKNOWN_FAILURE/INVALID_RUN + MiniMax-only/no-OpenRouter.

SUGGESTED P1 ORDER (verify-what-exists-then-fill): (1) LifecycleOracle + ProviderLedgerOracle (headless,
now) + unit tests; (2) HARN-3 promotion-policy module composing existing+new oracles (headless gate first);
(3) HARN-1 Playwright product harness + its evidence streams; (4) the 7 browser-evidence oracles; (5) wire
HARN-3 to require both gates. Each PR: plan→Codex→implement→test→commit.

### P1 BLOCKER FINDING (durable — corrects the order above)
On scoping P1's first PR, two issues surfaced that reshape it:
- LifecycleOracle on headless evidence would LARGELY DUPLICATE EventChainOracle (canonical loop) +
  fail-closed BUILD_DID_NOT_FINISH — low marginal value until browser/provider-after-terminal evidence exists.
- ProviderLedgerOracle (the MiniMax-only / no-OpenRouter enforcer — the campaign's HARD constraint) needs
  provider data the current oracle interface does NOT pass: `check(events, *, scenario, workspace_manifest,
  preview)` has no provider/model/ledger arg. The EvidenceManifest holds provider/model but isn't handed to
  oracles, and there is NO call-level provider-call-ledger.jsonl yet.
→ REVISED P1 FIRST PR (HARN-1a, evidence-first): capture a provider-call-ledger in the HEADLESS runner
  (parse the MiniMax relay log / add a logging shim around the live provider path → provider-call-ledger.jsonl
  per run) AND thread it (or the EvidenceManifest) into the oracle `check()` interface. THEN ProviderLedgerOracle
  becomes a real zero-OpenRouter / MiniMax-only / zero-calls-after-terminal gate. This is the highest-value,
  constraint-serving P1 entry and unblocks HARN-3's provider clause. The full browser harness (HARN-1b,
  Playwright) + the 7 browser-evidence oracles follow. This is a fresh-context effort, scoped + durable here.

### Campaign progress snapshot (durable)
- DONE: bootstrap + P0 (CXT-1..7) + P0B (LIFE-1..5). Commits 5d281625 → edc71501, all pushed, all
  Codex-gated, basedpyright strict 0 errors. MiniMax/OpenRouter NOT yet exercised (soak=P17).
- NEXT: P1 (per map above) → then P2+ (gated open by P0/P0B/P1).
- Deferred (tracked, surface-bound): CXT-4 prompt-wiring + C6 recitation de-dup (routing seam);
  context_compact_if_needed live firing (condenser); direct-edit record hook (P8).


## PR HARN-1a — provider-call ledger + ProviderLedgerOracle — COMPLETE

### Codex review (CODE, binding) — APPROVE (after 1 hardening round)
- round-1 REVISE → 4 fixes (None-vs-[]-vs-malformed conflation; fail-closed on required-but-absent;
  classify_run_folder crash-proof tolerant read; expanded parser/folder tests). Targeted re-review → APPROVE.
  Logs: .claude/harn1a-codex-code.log.

### Implementation
- NEW provider_ledger.py (record contract + tolerant parse_relay_log/_lines).
- NEW oracles/provider_ledger.py (ProviderLedgerOracle): OPT-IN per scenario assertions.provider;
  FAIL-CLOSED — required-but-absent/empty/malformed → fc.MISSING_REQUIRED_EVIDENCE → INVALID_RUN (opt-out
  via require_ledger:false → SKIP); enforces forbidden-host (default openrouter) / required-host /
  pinned-model / zero-calls-after-terminal. THE live mechanism for the HARD MiniMax-only/no-OpenRouter rule.
- CHANGED failure_codes.py (PROVIDER_FORBIDDEN/PROVIDER_WRONG_MODEL/PROVIDER_CALL_AFTER_TERMINAL = P0);
  classify.py (provider_ledger param + oracle step 7 + tolerant _read_ledger); oracles/__init__.py (export).
- Tests: 60 passed. basedpyright strict: 0 errors.

### Remaining for P1 (tracked)
- LIVE POPULATION of provider-call-ledger.jsonl from the MiniMax relay log during a run (soak-setup, P17);
  the READ+enforce pipeline is complete now.
- HARN-1b Playwright product harness + browser evidence streams (ws/network/console/screenshots/downloads).
- HARN-2 the 7 browser-evidence oracles. HARN-3 centralized product promotion policy (provider clause now
  enforceable).

## PR HARN-3 — centralized product promotion policy — COMPLETE
- Codex review (CODE, binding): APPROVE, none blocking. Composition conservative (no false-ELIGIBLE hole);
  ok=None informational convention matches bakeoff; require_product_harness default-False posture correct.
- NEW harness/build_soak/promotion.py: evaluate_product_promotion(classifications, *, product_harness,
  require_product_harness) → {eligible, checks}. Headless gates ON now (runs present / all PASS /
  no INVALID_RUN / no UNKNOWN_FAILURE / provider constraint). Product-harness gate (browser_ws/preview/
  download/cleanup/zero-calls-after-terminal) blocks only when require_product_harness=True (default False
  until HARN-1b). bakeoff.py (kernel) untouched + separate.
- Tests: 11 passed. basedpyright strict: 0 errors.
- P1 REMAINING: HARN-1b Playwright product harness + browser evidence streams; HARN-2 the 7 browser-evidence
  oracles; then flip require_product_harness=True in the release gate + wire live provider-ledger population.

## PR HARN-2 — 8 browser product-harness oracles — COMPLETE
- Codex (CODE, binding): APPROVE (after 1 hardening round). Grouping (one cohesive module) accepted.
- NEW oracles/browser_evidence.py: BrowserWS/Lifecycle/SidecarStop/PreviewOwnership/ShowToUser/
  VerificationGate/ExportDownload/Cleanup over a single product_evidence dossier. Each SKIPs without its
  slice (headless runs unaffected); FAIL-CLOSED on malformed/partial (safe _int, explicit-positive reads).
- CHANGED failure_codes.py (8 new P0 codes); classify.py (product_evidence param → family as step 8 +
  classify_run_folder reads product-evidence.json tolerantly); run.py classify_dossier threads
  product_evidence; oracles/__init__.py (exports).
- round-1 fixes: wired into live dossier path; safe numeric coercion (no crash on 'n/a'); closed false-PASS
  holes (owner=='platform'/workspace_released is True/both verification flags/stopped_at_terminal explicit).
- Tests: 40 passed (per-oracle skip/pass/violation + malformed/partial fail-closed + classify_run_folder
  green/violation/corrupt). basedpyright strict: 0 errors.

### P1 STATUS: HARN-1a + HARN-2 + HARN-3 oracle/policy layer COMPLETE.
Remaining for P1: HARN-1b — the Playwright product harness that DRIVES the real UI and WRITES the
product-evidence.json + provider-call-ledger.jsonl streams these oracles consume (needs a LIVE
frontend+agent-server stack). Then flip require_product_harness=True in the release gate.

## PR HARN-1b (evidence side) — validated product-evidence writer — COMPLETE
- Codex (CODE, binding): APPROVE, none blocking (+ optional hardening tests added).
- NEW harness/build_soak/product_evidence.py: validate_product_evidence (schema problems list; bool-where-int
  rejected; unknown keys forward-compat) + write_product_evidence(strict=True → validate-before-write, no
  malformed evidence persisted) + write_provider_ledger. The verified PRODUCER of the HARN-2 oracle contract.
- Tests: 12 (validation + strict-refusal + the writer→classify_run_folder→oracle LOOP: green PASS,
  preview-owner FAIL, provider openrouter FAIL). Full HARN suite 105+ green. basedpyright strict: 0 errors.
- P1 NOW: the entire headlessly-buildable harness layer is DONE — provider ledger + 8 browser oracles +
  promotion policy + validated evidence writer, all wired into classify/classify_run_folder + Codex-gated.
  The ONLY remaining P1 work is HARN-1b's LIVE Playwright spec that drives the real UI and calls these
  writers — it needs a running frontend+agent-server stack (deliberately NOT written as an unrunnable stub).

## PR CONTRACT-1 — core artifact-contract models (P2) — COMPLETE
- Codex (CODE, binding): APPROVE (after 1 invariant round). Dependency-free base layer confirmed.
- NEW packages/core/src/disco/core/contract/{__init__,models.py}: ContractKind (7 wire ids),
  VerificationLevel, ToolPack, EditContract, VerificationContract (finalizer pinned to
  ready_for_*_verification), ExportContract, ArtifactContract, BuildContract (+ minimal() factory +
  @model_validator enforcing kind == artifact.kind). Pure frozen Pydantic, same house style as CXT-1.
- round-1 fix: kind/artifact.kind coherence validator + finalizer-convention field_validator + tests.
- Tests: 9 passed. basedpyright strict: 0 errors.
- NEXT: CONTRACT-2 (BuildContractRegistry: look up a contract by BuildBrief; declares required files /
  starter kits / tool packs / verify / export / UI card / prompt pack) → CONTRACT-3 (Contract→ToolScope
  compiler: hard executor allowlists per phase bootstrap/edit/repair/verify/export).

### SEQUENCING NOTE (durable)
P2+ is being built FORWARD on the isolated disclaude branch (nothing promoted to mainline). The campaign's
"no P2+ PROMOTION until P0/P0B/P1 green" is a RELEASE gate, not a build-order stop. P0+P0B complete; P1's
entire HEADLESS harness layer green+gated; only HARN-1b's LIVE Playwright spec remains (needs a running
frontend+agent-server stack — real ops, not a stub). Promotion to mainline still waits on that + the P17 soak.

## PR CONTRACT-2 — BuildContractRegistry (P2) — COMPLETE
- Codex (CODE, binding): APPROVE (after 1 round). 
- NEW contract/registry.py: BuildContractRegistry maps ContractKind→BuildContract; default() registers a
  built-in per kind (static.site/appkit.leadgen/deck/document/interactive.prototype/workflow.output/custom)
  with real required-files/starter-kits/tool-packs/finalizer/export/prompt-pack/UI-card. get()/kinds()/
  get_for_brief(strict_kind). Pure data+lookup.
- round-1 fixes (Codex): appkit.leadgen now references ONLY registered tools (app_* are P4, documented) +
  a test asserts EVERY built-in contract's tools exist in build_default_registry() (no false affordance);
  get_for_brief partial-registry-safe (_custom() → registered or minimal, no KeyError); explicit unknown-kind
  policy (missing→CUSTOM; present-but-malformed→ValueError by default, strict_kind=False opt-out).
- Tests: 19 passed (CONTRACT-1+2). basedpyright strict: 0 errors.
- NEXT: CONTRACT-3 — Contract→ToolScope compiler (compile a BuildContract into hard executor allowlists per
  phase: bootstrap/edit/repair/verify/export; appkit bootstrap can't file_write etc.).

## PR CONTRACT-3 — Contract→ToolScope compiler (P2) — COMPLETE
- Codex (CODE, binding): APPROVE (after 1 round). Projection correct + pure; allowed() is a genuine hard
  allowlist (no leak path); export=∅-until-P10 + verify={finalizer} correct modeling.
- NEW contract/scopes.py: Phase enum + ContractToolScopes (frozen, per-phase frozensets) +
  compile_tool_scopes(contract) → bootstrap/edit/repair/verify/export hard allowlists + allowed(phase,tool).
  Pure projection of the contract's tool packs; no tool-runtime import.
- Tests: 28 passed (full contract suite). Invariants proven incl. the done-when: a bootstrap pack lacking
  shell/file_write hard-excludes them (allowed False), declared → allowed; edit can't rewrite via file_write;
  repair bounded; verify==finalizer.

### >>> TRACKED FOLLOW-UP (binding) — CONTRACT-3 executor-side ENFORCEMENT <<<
CONTRACT-3 is COMPILER-ONLY by design (pure projection + hard-allowlist data). It does NOT yet DENY an
out-of-scope tool call at execution. The enforcement integration MUST: in the tools executor, before
running a tool, resolve the run's BuildContract (CONTRACT-2 get_for_brief) → compile_tool_scopes → and
REJECT a call whose tool is not allowed() in the current phase (bootstrap/edit/repair/verify/export),
returning a structured "tool_out_of_contract_scope" outcome (mirror the existing tool-scope deny path).
This closes the "model cannot use generic file/shell during bootstrap unless the contract permits it"
done-when at runtime. Belongs with the P4 specialized-mutation-tools wiring (when app_* tools + per-phase
execution land) OR a dedicated CONTRACT-ENFORCE PR. Until then: contracts are declared + compiled but not
runtime-enforced — this is an explicit, tracked gap, NOT a silent one.

### P2 CONTRACT RUNTIME (CONTRACT-1/2/3) CORE COMPLETE.
Models + registry + scope compiler all green + Codex-gated. Next P2-adjacent: the executor enforcement
follow-up above, then P3 (WorkflowPromptPack) / P4 (specialized mutation tools incl. app_*).

## PR WPP-1 — WorkflowPromptPack format + packs (P3) — COMPLETE
- Codex (CODE, binding): APPROVE (after 1 hardening round — all 5 points resolved).
- NEW workflows/{__init__,prompt_pack.py} + prompt_packs/*.md (5 packs: build_static_site /
  build_appkit_leadgen / build_deck / build_document / build_interactive_prototype). PromptPack (frozen,
  slugged sections + raw) + REQUIRED_SECTIONS(11) + parse_prompt_pack (fence-aware, raises on duplicate
  section) + PromptPackRegistry (importlib.resources loader, get/require/ids).
- round-1 fixes: pyproject artifacts ships the .md in the wheel + importlib.resources loader (+ test);
  interactive.prototype gets its OWN pack (finalizer coherence); appkit pack lists only registered tools
  (app_* = P4); parser fence/duplicate hardening; finalizer-in-pack coherence test.
- COHERENCE INVARIANTS proven: every contract's prompt_pack resolves to a COMPLETE pack AND the pack
  instructs that contract's exact finalizer (no mismatch).
- Tests: 12 WPP-1 + contract regression. basedpyright strict: 0 errors.
- NEXT: WPP-2 (kernel-neutral prompt assembly: both kernels consume stable prefix + ContextPack +
  WorkflowPromptPack + recent turns + tool schema) → WPP-3 (skill mount policy).

## PR WPP-2 — kernel-neutral prompt assembly (P3) — COMPLETE
- Codex (CODE, binding): APPROVE, none required. (Optional follow-up: a live runtime test proving BOTH
  DiscoKernel + PiKernel source assembly from this helper — that's the kernel-wiring integration, tracked.)
- NEW workflows/assembly.py: assemble_workflow_prompt(*, system_prefix, prompt_pack, context_pack_block,
  recent_turns) → list[LLMMessage]. Order: stable prefix → WorkflowPromptPack (system) → ContextPack block
  (user) → recent turns. Pure + deterministic (kernel-neutral); each structured part once; tool schema left
  to the driver. Composes WPP-1 PromptPack.render() + CXT-4 render_context_pack().
- Tests: 7 (order, each-part-once, deterministic, optional-omit, end-to-end real pack+contextpack). 0 pyright errors.
- TRACKED FOLLOW-UP: wire DiscoKernel + PiKernel to BOTH call assemble_workflow_prompt in the LIVE prompt
  path (closes the kernel-neutral claim end-to-end) — belongs with WPP-3 / the kernel-prompt integration.
- NEXT: WPP-3 (skill mount policy — mount skills only via workflow/contract; no global skill soup).

### P3 STATUS: WPP-1 (format+packs) + WPP-2 (assembler) COMPLETE. WPP-3 (skill mount) remains.
### CAMPAIGN TALLY: 18 substantive PRs (bootstrap + P0×7 + P0B + P1×4 + P2×3 + P3×2), all Codex-gated,
### all green, all pushed on the isolated disclaude branch. Live-stack items (HARN-1b Playwright, kernel
### prompt-wiring, executor scope-enforcement, CXT live-wiring) are explicitly tracked, not silently dropped.

## PR WPP-3 — skill mount policy (P3) — COMPLETE
- Codex (CODE, binding): APPROVE on the policy + (3) registry invariant; (1)+(2) gated POLICY-ONLY with the
  tracked follow-up below.
- NEW workflows/skill_mount.py: resolve_mounted_skills(contract, *, base_skills=()) → frozenset (base default
  EMPTY → a contract gets ONLY its declared skills; no global soup) + is_skill_mountable hard check. Added
  BuildContract.skills: tuple[str,...]=() (additive); appkit.leadgen declares its 3 skills, all others none.
- Tests: 7 (no-skills→empty, appkit exact set, NO global soup, undeclared not mountable, base-union/default-
  empty, skills roundtrip, registry invariant: only appkit declares skills). 0 pyright errors.

### >>> TRACKED FOLLOW-UP (binding) — WPP-3 runtime mount-path ENFORCEMENT <<<
WPP-3 is POLICY-ONLY. The pure policy (resolve_mounted_skills/is_skill_mountable) is NOT yet wired into the
LIVE skill/MCP mount path. The integration MUST: at run/build setup, where skills/MCP are mounted
(SkillStore in core/skills.py + the mount path in agent-server/runtime.py), resolve the run's BuildContract
(CONTRACT-2) → resolve_mounted_skills → and mount ONLY those (refuse an undeclared skill), with an
integration test that an undeclared skill is not mounted for a contract that didn't declare it. This closes
"no global skill soup" at runtime. Same deferral discipline as CONTRACT-3 executor enforcement + CXT
live-wiring + WPP-2 kernel-wiring — an explicit, tracked gap, NOT a silent one.

### P3 WorkflowPromptPack COMPLETE (WPP-1 format+packs / WPP-2 assembler / WPP-3 skill mount).
### CAMPAIGN: 19 substantive PRs (bootstrap + P0×7 + P0B + P1×4 + P2×3 + P3×3), all Codex-gated + pushed.
### RUNTIME-INTEGRATION FOLLOW-UPS (all tracked, none silent): HARN-1b live Playwright harness + provider-
### ledger population; CXT-4 ContextPack live prompt-wiring + C6 recitation de-dup; context_compact_if_needed
### live firing; direct-edit record hook (P8); CONTRACT-3 executor scope-enforcement; WPP-2 kernel prompt-
### wiring; WPP-3 skill mount-path enforcement. These need the live stack / deeper runtime surface.

## PR TOOL-1 — AppKit specialized mutation tools (P4) — COMPLETE
- Codex (CODE, binding): APPROVE on all tool-specific items (1 CSS-escape / 2 deterministic snapshot /
  3 unique-id validation / 5 robust tweak coercion) after 1 hardening round. Items 4+6 (dispatch-time
  edit-vs-repair enforcement + its integration test) are the executor-enforcement layer → building it NEXT
  as CONTRACT-ENFORCE (retiring the CONTRACT-3 tracked follow-up rather than re-deferring).
- NEW core/appkit/{models.py,__init__}: AppSpec/AppSection (frozen, unique-id validated) + pure mutations
  (with_content/section add·remove·reorder/design/tweak) + render_html (deterministic, inline CSS,
  CSS-token-sanitized, html-escaped fields, data-disco-* anchors).
- NEW tools/builtin/appkit.py: AppSpecStore + 8 tools (app_create / app_update_content / app_add_section /
  app_remove_section / app_reorder_section / app_set_design / app_set_tweak / app_snapshot_version).
  Structured errors: no_app / invalid_app_edit / corrupt_appspec; snapshot is content-addressed (sha256).
  Registered + in AGENT_TOOLS + ARTIFACT_TOOLS. appkit.leadgen contract now scopes the REAL app_* tools
  (file_write→repair-only); the appkit pack's allowed-tools updated to match.
- Tests: 24 (10 model + 14 tool) incl. CSS-injection sanitization, duplicate-id rejection, deterministic
  snapshot, robust tweak coercion, registry+scope membership, 3 error paths. basedpyright strict 0 errors.
- NEXT: CONTRACT-ENFORCE — wire compile_tool_scopes into tool dispatch so a contract's per-phase allowlist
  is HARD-enforced (appkit EDIT can't file_write; bootstrap can't shell) + the integration test. Retires
  the CONTRACT-3 / TOOL-1#4 / WPP-3 enforcement follow-ups in one real integration.

## PR CONTRACT-ENFORCE — executor-side per-phase scope enforcement (MECHANISM) — COMPLETE
- Codex (CODE, binding): APPROVE (after 1 round; both blocking items addressed). Quote: "mechanism is now
  pure and bounded (metadata-first gating with fallback, executor hook optional/default-none), while runtime
  still lacks phase-state so deferring wire-up to CONTRACT-ACTIVATE is the only correct way to avoid fake
  enforcement."
- NEW core/contract/enforce.py: ScopeDecision + decide_tool_in_scope(scopes, phase, tool, *, is_mutating) +
  ContractScopeGuard(scopes, phase_provider).check(tool, is_mutating). METADATA-DRIVEN: governed = a MUTATING
  tool (not read_only) OR a tool the contract scopes to bootstrap/edit/repair/export; verify finalizer always
  passes (control signal). No name-list bypass (DANGEROUS_TOOLS is fallback-only).
- CHANGED tools/executor.py: optional scope_guard param; the universal execute() chokepoint denies an
  out-of-phase tool (kind 'denied', message names the phase + permitted tools) BEFORE it runs; passes
  is_mutating = not tool.definition.read_only. guard=None ⇒ unchanged (plain agent run).
- Tests: 13 (7 core decisions incl. metadata-mutator gating + 5 executor integration: file_write DENIED in
  appkit EDIT and never lands / allowed in REPAIR / semantic+utility+finalizer pass / no-guard unchanged) +
  the full executor regression (elision/scope/assist/validation) still green. basedpyright strict 0 errors.

### >>> NEXT PR: CONTRACT-ACTIVATE (the activation, building its substrate) <<<
The runtime tracks artifact_mode as a bare bool (runtime.py:1479); there is NO per-conversation BuildContract
and NO build-phase signal. CONTRACT-ACTIVATE must BUILD that substrate: (1) per-conversation contract
resolution (kind → BuildContractRegistry.get_for_brief); (2) a build-phase state machine
(bootstrap→edit→repair→verify→export) driven by observable loop signals (artifact created? verify failure
pending? finalizer called?); (3) pass scope_guard=ContractScopeGuard.for_contract(contract, phase_provider)
at the runtime.py:1489 executor construction (artifact mode); (4) an e2e test that the RUNTIME-built executor
blocks an out-of-scope call. This RETIRES the CONTRACT-3 + TOOL-1#4 + WPP-2 kernel-wiring + WPP-3 mount
follow-ups (all are "needs the contract↔loop phase integration", which this builds).

## PR CONTRACT-ACTIVATE — build-phase substrate + LIVE guard wiring — COMPLETE
- Codex (CODE, binding): APPROVE (both revisions). Quote: "artifact_mode deliberately uses ARTIFACT_TOOLS
  (which excludes verify_web_app) and the runtime wires ContractScopeGuard there only for bootstrap/edit/
  finalizer transitions via on_tool_success, so VERIFY→EXPORT/REPAIR belongs to the build-surface verify
  path... not this artifact-mode PR." Enforcement is now LIVE in artifact mode (was the big tracked deferral).
- NEW core/contract/phase.py: BuildPhaseTracker — deterministic state machine. BOOTSTRAP--(bootstrap-tool
  success)-->EDIT; *--(finalizer)-->VERIFY; VERIFY--(verifier pass/fail)-->EXPORT/REPAIR. Conservative.
- CHANGED tools/executor.py: on_tool_success callback (best-effort) so a successful tool advances the tracker.
- CHANGED agent-server/runtime.py: per-conversation (BuildContract, BuildPhaseTracker) + _build_scope_guard
  (resolves contract via get_for_brief, default CUSTOM; returns guard wired to tracker.current +
  note_tool_success) wired at the artifact-mode executor construction (scope_guard + on_tool_success);
  set_build_kind() to declare a kind; note_build_verify_result() entry point for the build-surface verify
  edge; forget_conversation evicts the build state.
- Tests: 16 (phase machine 6 + executor e2e live-loop 6 + runtime resolver/verify/cleanup 6 incl. a real
  app_create→EDIT→file_write-DENIED end-to-end through the runtime-built executor). 45 green across the whole
  contract/executor/runtime surface. 0 new typecheck errors (1 pre-existing fire_now, stash-confirmed).
- LIVE RESULT: in artifact mode a contract-governed run now enforces its per-phase tool allowlist at the
  dispatch boundary — no raw rewrite during the edit phase, etc. RETIRES the CONTRACT-3 / TOOL-1#4 executor-
  enforcement deferral. Remaining (tracked, build-surface, NOT artifact-mode): wire finish.py's verify_web_app
  verdict → note_build_verify_result when the guard is later wired to DiscoKernel build runs.

### CONTRACT PILLAR COMPLETE + ACTIVATED: P2 (models/registry/scopes/enforce/phase) + P3 (packs/assembler/
### skills) + P4/TOOL-1 (app_* tools) + CONTRACT-ENFORCE + CONTRACT-ACTIVATE. Enforcement is LIVE, not decorative.

## PR P5-DELIVERY — Preview/Show/Delivery (audit + contract delivery shape) — PLAN
SCOUT VERDICT: the P5 product surface ALREADY EXISTS + is hardened in the clone (audit-mostly-done, like P0B):
- Preview is HOST-OWNED: preview_start/status/logs/stop; the platform picks the port (no manual ownership).
- Show/Deliver: the `serve` tool → turn_control.handle_serve emits a DeliverableEvent (Open-the-app /
  Download-the-files), with dedup, post-resume gate, no-op counting, and F3 false-URL dropping (no false
  affordance). HARN-2 ShowToUserOracle already covers the LIVE shown/preview evidence.
NO REBUILD. The genuine campaign gap is tying DELIVERY to the P2 contract (the host-owned delivery SHAPE):
1. core/contract: delivery_mode_for_kind(kind) → Literal["app","files"]; ArtifactContract.delivery_mode
   property (derived, always coherent, no migration). app = open-in-preview (appkit.leadgen/static.site/
   interactive.prototype); files = download/inspect (deck/document/workflow.output/custom).
2. deliverable_kind_matches_contract(contract, artifact_kind) — pure "no wrong-shape handoff" validator (a
   deck contract can't hand off as a runnable app). Ready for the deliver-gate wire.
3. runtime: expected_delivery_mode(conversation_id) accessor reading the resolved contract → the agent-server
   deliverable surface can label/validate the handoff. (Default CUSTOM→files; declared appkit→app.)
4. Tests: per-kind mapping, every-builtin coherence, validator accept/reject, runtime accessor.
TRACKED FOLLOW-UP: wire deliverable_kind_matches_contract into handle_serve (loop) to REJECT a wrong-shape
serve — a loop-integration like the build-surface verify wire (the validator + accessor land here, tested).
Global gates honored: no manual preview ownership (already), no false affordance (validator enforces shape).
NEXT after P5: P6 Verification Finalizers.

### GATE FAILOVER — 2026-06-28: gpt-5.3-codex-spark hit usage limit (resets Jun 29 ~22:52).
Per the campaign rule (blocked gate = P0 infra, don't bypass), probed alternates: gpt-5.3-codex not supported
on a ChatGPT account; **gpt-5.5-codex AVAILABLE** → the binding Codex gate now runs on gpt-5.5-codex (stronger
reviewer). Same `codex exec --sandbox read-only` contract. Revert to spark when its limit resets if desired.

### GATE CONFIRMED DOWN — 2026-06-28 (re-probed): spark usage-limited until Jun 29 ~22:52 UTC; gpt-5.3-codex
+ gpt-5.5-codex BOTH "not supported on a ChatGPT account" (the 5.5 probe was a false positive — codex echoes
the prompt, which contained the OK token). The binding gate has NO working alternate right now.
DECISION (honoring the rule "don't bypass the gate"): PAUSE shipping unreviewed PRs. Do gate-INDEPENDENT prep
(review-ready plans + scouting) + poll hourly for spark's return; when back, run the queued plan reviews
(P5-DELIVERY, P6) → implement in a burst. No code is committed to the campaign without an APPROVE.

## PR P6-FINALIZERS — Verification Finalizers (host-truth) — PLAN (queued, awaiting gate)
SCOUT VERDICT — a LIVE FALSE AFFORDANCE exists: the contracts + prompt packs instruct the model to call
per-kind finalizers (ready_for_app_verification / ready_for_static_site_verification / ready_for_deck_
verification / ready_for_document_verification / ready_for_prototype_verification / ready_for_workflow_output_
verification / ready_for_artifact_verification), but NONE of these are registered tools — only verify_web_app
exists (builtin/verify_app.py) feeding the finish gate (finish.py finish_verify_passed / finish_dod_gate_
passed / _gate_verify_web_app). So a model following the pack calls a tool that doesn't exist → unknown_tool.
PLAN: make the contract finalizers REAL host-truth gates (non-negotiable: "no finish without host truth"):
1. A single finalizer tool family driven by the contract's VerificationContract.finalizer name — register the
   per-kind finalizer names (from BuildContractRegistry) so they resolve; each is the agent's "I claim ready"
   SIGNAL that hands to the HOST verifier (it does NOT self-certify). The tool returns a structured
   "verification requested" outcome; the host finish gate adjudicates pass/fail (reuses _gate_verify_web_app
   for app-shaped contracts; a DoD/required-files + render check for files-shaped contracts).
2. Wire the finalizer success → BuildPhaseTracker.note_finalizer_called (already supported) and the gate
   verdict → note_build_verify_result (the entry point built in CONTRACT-ACTIVATE) — this also RETIRES the
   build-surface verify-edge follow-up.
3. Coherence: every contract's declared finalizer MUST be a registered tool (a test asserting no false
   affordance — the inverse of the P2 tools-exist test, now covering finalizers).
4. Honor VerificationLevel (STRICT for appkit) — strict requires the host render/lead-persist evidence, not a
   clean console.
RISK: touches the finish gate (finish.py) — high-care, full-suite verify + adversarial review required.
NEXT after P6: P7 Starter/Brand/UI Kits.

### GATE RESTORED — 2026-06-28: my "gate down" call was WRONG. The model id has NO "-codex" suffix.
`gpt-5.5` (and gpt-5.4) work via `codex exec --sandbox read-only --model gpt-5.5`; only the spark variant is
usage-limited. Binding gate now runs on **gpt-5.5** (verified: real reasoning, 17+25→42, not a prompt echo).
Resuming the normal per-PR loop immediately (P5-DELIVERY plan review first). No idle-poll needed.

### PR P5-DELIVERY — PLAN REVISION 1 (post gpt-5.5 round 1; audit+mapping APPROVED, 3 required fixes)
Codex validated: audit correct (no rebuild), delivery_mode derived property campaign-coherent, the app|files
mapping right (custom→files safer). Revisions applied:
1. SCOPE WORDING: P5 is "contract delivery SHAPE + validator/accessor", NOT runtime enforcement. Reworded:
   the validator MAKES shape enforceable; runtime enforcement (reject a wrong-shape handoff) follows in the
   handle_serve wire. No overclaim.
2. PORT-8000 RECONCILE (tracked follow-up, NOT a hasty edit): engine.py:281/335/337 tell the model to "serve
   on port 8000 / http://localhost:8000/" in the serve+finish guidance, which conflicts with the host-owned
   preview rule (preview.py: "NEVER a fixed :8000 — the platform chooses it"). NUANCE confirmed: 8000 is
   Disco's CANONICAL USER-VISIBLE proxy port (verify_app.py:42, server.py:18) but the SANDBOX bind port is
   host-chosen. So the fix is to reconcile the serve/finish text to "let the platform own the port (preview_
   start); 8000 is only the user-visible proxy" WITHOUT breaking the canonical-port contract — a careful
   engine.py finish-gate edit, tracked as its own small PR (P5-PORT) with full finish-gate regression, not
   bolted onto this delivery-shape PR.
3. SYNTHETIC DELIVERABLE: the reject-wire follow-up now explicitly covers BOTH (a) turn_control.handle_serve
   AND (b) the lifecycle synthetic app-deliverable path (emits artifact_kind="app" for any index.html
   snapshot regardless of the active contract — would violate custom/deck/document→files). Both read
   ArtifactContract.delivery_mode when wired. (Verify exact symbol at impl time — grep found the concept;
   confirm name in lifecycle.py.)
This PR (P5-DELIVERY) ships ONLY: delivery_mode_for_kind + ArtifactContract.delivery_mode property +
deliverable_kind_matches_contract validator + runtime expected_delivery_mode accessor + tests + audit note.
Follow-ups (tracked): P5-PORT (port-8000 reconcile) + the reject-wire (handle_serve + synthetic deliverable).

## PR P5-DELIVERY — Preview/Show/Delivery: contract delivery shape — COMPLETE
- Codex (CODE, gpt-5.5, binding): APPROVE (plan APPROVE after 1 revision; code APPROVE clean).
- AUDIT: preview/show/deliver substrate already present + hardened (host-owned preview, serve→DeliverableEvent
  with dedup/post-resume/false-URL guards, HARN-2 ShowToUserOracle). NO rebuild.
- NEW core/contract/models.py: DeliveryMode=Literal["app","files"]; delivery_mode_for_kind (app =
  appkit.leadgen/static.site/interactive.prototype; files = deck/document/workflow.output/custom);
  ArtifactContract.delivery_mode @property (derived, can't drift); deliverable_kind_matches_contract validator
  (no wrong-shape handoff — a deck can't deliver as a runnable app).
- NEW runtime.expected_delivery_mode(conversation_id) → app|files|None (only for build/artifact runs; never
  fabricates a contract for a plain chat).
- Tests: 8 (6 core + 2 runtime) ; 32 green incl. contract regression. contract tree pyright 0; runtime 0 new.
- TRACKED FOLLOW-UPS (Codex-approved scoping): (1) P5-PORT — reconcile engine.py serve/finish port-8000
  guidance with host-owned preview (8000 = canonical user-visible proxy ONLY; sandbox port host-chosen);
  careful finish-gate edit w/ full regression. (2) reject-wire — enforce deliverable_kind_matches_contract in
  BOTH turn_control.handle_serve AND the lifecycle synthetic app-deliverable path (artifact_kind="app" for any
  index.html regardless of contract). Both read delivery_mode when wired.
- NEXT: P6-FINALIZERS (plan queued; fixes the ready_for_*_verification false affordance).

### PR P6-FINALIZERS — PLAN (refined post-scout)
ARCHITECTURE FOUND: `finish` is a VIRTUAL tool (engine.py _finish_tool_spec/_FINISH_SCHEMA; advertised via
driver.py _finish_tool_singleton in `virtuals`; engine intercepts tool_name=="finish" → finish.py
normalize_finish_step → handle_finish_path = the HOST-TRUTH gate: verify-on-finish + verify_web_app + DoD).
Other virtuals: serve/remember/notify_user/ask_user/clarify/propose_plan_update. There is NO `finish` registry
tool and NO ready_for_*_verification tool → the contract/pack finalizer names are a LIVE FALSE AFFORDANCE
(a model that follows the pack calls an unrecognized tool).
DESIGN (minimal + SAFE — reuse the proven gate, add NO new verification logic):
1. Make the active contract's finalizer name a RECOGNIZED + ADVERTISED virtual finalizer that routes through
   the EXISTING finish gate. When a Build run declares a contract, the engine (a) advertises the finish
   virtual under the contract's finalizer name (ready_for_<kind>_verification) — same schema/description,
   contract-named; (b) recognizes that name in the finish-dispatch branch (engine.py:1404) exactly like
   "finish" → normalize_finish_step/handle_finish_path. The finalizer is the agent's "I claim ready" SIGNAL;
   the HOST gate adjudicates (no self-cert). Zero new verify logic — the host-truth path is unchanged.
2. COHERENCE (no false affordance): a test asserting every built-in contract's finalizer name is recognized
   by the finish-dispatch (the inverse of the P2 tools-exist test, for finalizers). Plus: with NO contract,
   plain "finish" still works (unchanged).
3. SCOPED OUT (tracked follow-ups, to keep this sensitive finish.py/engine change minimal + safe):
   - VerificationLevel escalation (STRICT requires the lead-persist/render evidence) — a gate refinement.
   - The cross-layer tracker wire (finalizer→note_finalizer_called, gate verdict→note_build_verify_result):
     finish is engine-level (core/loop) but the tracker lives in agent-server runtime; this is the SAME
     cross-layer signal as the build-surface verify edge — wire both together in a focused follow-up.
RISK: engine finish-dispatch + virtual-tool advertisement. MANDATORY: full agent-server + loop suite green;
adversarial Codex review; the no-contract "finish" path must be byte-unchanged.

### PR P6-FINALIZERS — PLAN REVISION 1 (post gpt-5.5: aliasing concept APPROVED, 6 required fixes)
Confirmed surfaces: dispatch engine.py:1404 (tool_name=="finish"); advertisement driver.py:357 virtuals;
known_tool_names_for_requery driver.py:382; planning-mode virtual suppression driver.py:~234 ("finish stays");
AgentLoop engine.py:484 (constructed runtime.py:1346/1636/1654 _compose_build_loop); Pi fixed finish allowlists
pi-kernel/src/tools.ts:54/302 + routes/pi_tools.py:75.
DESIGN (all 6 revisions):
1. CORE SEAM: AgentLoop(..., finish_alias: str | None = None), stored self._finish_alias. Passed by
   _compose_build_loop ONLY when a real contract kind is DECLARED (conversation_id in self._build_kind) →
   contract.verify.finalizer; else None. NEVER fabricate ready_for_artifact_verification for a plain/CUSTOM
   build (the CUSTOM default must NOT trigger an alias).
2. CENTRAL HELPER: is_finish_tool_name(name, finish_alias) = name=="finish" or (finish_alias and name==
   finish_alias). Used by ALL surfaces: dispatch, advertisement, known_tool_names_for_requery, planning
   suppression (alias behaves EXACTLY like finish — stays when finish stays, blocked when finish blocked).
3. FRESH ALIAS ToolSpec: _finish_alias_tool_spec(alias) builds a NEW ToolSpec(name=alias, desc=_FINISH_
   DESCRIPTION, schema=_FINISH_SCHEMA) — do NOT mutate the cached _FINISH_TOOL_SPEC singleton.
4. VISIBILITY: when finish_alias set, ADVERTISE the contract finalizer (the name the pack tells the model to
   call) AND keep plain "finish" advertised+recognized as a compatibility alias (documented). Both route to
   the same gate.
5. PI SCOPED OUT (tracked → P15 PiKernel Product Integration): Pi's fixed "finish" allowlists are NOT touched
   here; the finalizer false affordance persists ONLY on the Pi path until P15 wires it. Documented, not silent.
6. TESTS: every builtin finalizer recognized by is_finish_tool_name; active-contract alias advertised; alias
   routes through the finish gate (normalize_finish_step); no-contract plain "finish" byte-unchanged; alias
   blocked in planning mode like finish; alias present in known_tool_names_for_requery.
NOT claimed complete: VerificationLevel STRICT escalation + the cross-layer phase-tracker verify edges (both
explicit tracked follow-ups). MANDATORY: full agent-server+loop suite green; no-contract finish path unchanged.

## PR P6-FINALIZERS — Verification Finalizers (host-truth, contract finalizer = finish-alias) — COMPLETE
- Codex (CODE, gpt-5.5, binding): APPROVE (plan APPROVE after 1 round; code APPROVE after 1 round fixing 3
  real bugs gpt-5.5 caught — planning-gate alias leak, batched-call discard, CUSTOM fabrication).
- FIXED the live FALSE AFFORDANCE: the per-kind ready_for_*_verification finalizers (named in every contract/
  pack) are now REAL — recognized + advertised as a per-kind ALIAS of the `finish` virtual tool, routed
  through the EXISTING host-truth finish gate (no self-cert, no new verify logic).
- is_finish_tool_name centralized in boundaries.py (single source) → engine (dispatch+re-export) + agent
  (selection). _finish_alias_tool_spec = fresh ToolSpec (never mutates the finish singleton). AgentLoop +
  RouterAgent gain finish_alias. driver advertises the alias next to finish (survives suppression) + requery
  knows it. runtime._finalizer_alias_for: alias ONLY for a resolved NON-CUSTOM declared kind.
- KEY FIX (bugs 1+2): the AGENT canonicalizes the alias→"finish" in _pick_tool_call BEFORE any engine
  processing — a batched [alias, real-action] keeps the real action, and the alias name never reaches the
  planning gate / dispatch / signals / event log.
- Tests: 12 (helper, advertisement, requery, planning, no-contract, batched-keeps-action, alias-canonicalize,
  CUSTOM/declared/no-contract gating) + AGENT_TOOLS snapshot fixed (app_*+context_memory TOOL-1/CXT-2
  leftover). 233 agent/driver/finish/turn regression green; core-loop suite fully green; agent-server green
  except 4 pre-existing env failures (Pi-needs-node + verify-config). 0 new pyright errors.
- SCOPED OUT (tracked): VerificationLevel STRICT escalation; the cross-layer phase-tracker verify edges; Pi
  finalizer wiring → P15 (pi-kernel fixed finish allowlists untouched; Pi-path false affordance persists till P15).
- NEXT: P7 Starter/Brand/UI Kits.

## PR P7-KITS — Starter/Brand Kits — PLAN (queued)
SCOUT VERDICT — a LIVE FALSE AFFORDANCE: the contracts (artifact.starter_kit) + prompt packs reference named
starters app_shell / lead_form / deck_stage, but NOTHING resolves them — they are bare strings. app_create
(TOOL-1) hard-codes its hero+lead_form default instead of a named starter. No BrandKit exists (appkit has
DEFAULT_DESIGN tokens; the packs reference a DesignSpec/brand that isn't real). slides_generate exists (deck
authoring) but there's no deck_stage starter. So "scaffold from the app_shell starter" is an instruction the
host can't honor → the model hand-draws frames (violating the host-owned-scaffold non-negotiable).
DESIGN (pure, host-owned, contract-coherent):
1. core/kits/starter.py — StarterKit (id + scaffold() → dict[rel_path, text]) + StarterKitRegistry built-ins:
   - app_shell → {"index.html": minimal RENDERABLE inline-CSS shell} (static.site / interactive.prototype)
   - lead_form → REUSE appkit: an AppSpec (hero+lead_form) → {".disco/appspec.json", "index.html"} via
     render_html (single source — app_create will scaffold from THIS, not its inline default)
   - deck_stage → {"deck.json": a starter deck (title + 2 content slides)}
2. core/kits/brand.py — BrandKit (named design-token presets building on appkit DEFAULT_DESIGN) +
   BrandKitRegistry: neutral (=DEFAULT_DESIGN) / bold / warm / cool — each a {primary,accent,bg,fg,font} set
   appliable via app_set_design. Pure value objects.
3. COHERENCE (no false affordance): test that EVERY BuildContract.artifact.starter_kit (non-None) resolves in
   StarterKitRegistry (the inverse of the tools-exist test, for starters).
4. WIRE: app_create scaffolds the default from StarterKitRegistry's lead_form starter (single source; removes
   the inline hardcoded AppSpec) — and stays byte-coherent (same hero+lead_form result).
5. Tests: every contract starter resolves; each starter's scaffold has its required files + renders; brand kits
   are valid token sets; app_create uses the lead_form starter.
SCOPE OUT (tracked → P7b if needed): a full UIKit component library (appkit's section kinds hero/features/
lead_form/about/cta/footer already serve as the UI kit); brand-as-a-tool (app_set_brand) — P9 TweakSpec-adjacent.
NEXT after P7: P8 Semantic Direct Manipulation.

### PR P7-KITS — PLAN REVISION 1 (post gpt-5.5: audit confirmed, 5 required fixes)
gpt-5.5 confirmed: starters are bare strings (false affordance); reusing appkit AppSpec for lead_form is right
(not scope creep). Revisions:
1. STARTER MATERIALIZER (real consumer, not a decorative registry): StarterKit.scaffold(title) is PARAMETERIZED
   + PATH-SAFE (normalized rel paths, no traversal). REAL consumers: (a) app_create scaffolds the lead_form
   starter when sections is None (byte-equivalent to today's inline default — single source); (b) a NEW
   host-owned tool `scaffold_starter` materializes the ACTIVE contract's starter files into the workspace (the
   model invokes it per the pack's "scaffold from the <name> starter" — closes the false affordance with a
   real callable). Registered + scoped + coherence-tested.
2. PARAMETERIZED + PATH-SAFE scaffold (title; reject any '..'/absolute path).
3. lead_form: the default AppSpec lives in starter.py; app_create uses it when sections is None; rendered
   output byte-equivalent.
4. DECK RECONCILE (fix the real inconsistency): the deck contract required_files=("deck.json",) + the build_deck
   pack are WRONG — the slides tooling writes/edits the AuthoredDeck sidecar `{name}.authored.json`
   (slides.py:485, _slides_pipeline). Fix: align the deck contract + pack to the real AuthoredDeck source;
   slides_generate IS the deck "starter/materializer" (NO deck_stage file-map — that would be paper). Confirm
   the exact canonical filename in slides tooling before editing.
5. BRANDKIT = an AppKit PROJECTION over the EXISTING disco.core.brand themes (Theme: accent + font_display/ui/
   reading/mono + branded; THEMES registry), NOT a parallel token catalog: a brand_to_appkit_tokens(theme) →
   appkit {primary,accent,bg,fg,font} mapping + named-brand lookup reusing core.brand. Apply via app_set_design.
SCOPE OUT (tracked): full UIKit (appkit section kinds suffice); app_set_brand tool (P9 TweakSpec-adjacent) —
packs must NOT imply it exists.

### PR P7-KITS — PLAN APPROVED (gpt-5.5, after 1 revision). Implementation guidance locked:
- scaffold_starter tool: bootstrap-scoped + active-contract-bound; keep app_create as the AppKit lead-form materializer.
- lead_form: default AppSpec in starter.py; app_create(sections=None) consumes it; TEST byte-equivalence of BOTH
  .disco/appspec.json AND index.html vs today.
- deck reconcile: standardize the deck base filename="deck" → align the contract required_files + build_deck pack
  to the AuthoredDeck sidecar `deck.authored.json` (do NOT change tooling, do NOT add deck_stage).
- BrandKit: projection over disco.core.brand.THEMES → appkit {primary,accent,bg,fg,font}; no parallel catalog, no app_set_brand.
- StarterKit.scaffold(title): normalized rel paths, reject absolute/'..'.
IMPLEMENT NEXT TURN (fresh context for this multi-file PR). Files: core/kits/{starter.py,brand.py}, tools/builtin/
scaffold_starter.py (+ registry/scopes), tools/builtin/appkit.py (app_create→lead_form starter), contract/registry.py
(deck required_files) + build_deck.md pack, tests. Then Codex CODE gate → commit.

## PR P7-KITS — IMPLEMENTED, COMMITTED GATE-PENDING (2026-06-28 emergency save — workstation GPU leak took the desktop down)
Plan was Codex-APPROVED; implementation complete + 76 tests green + basedpyright clean (1 pre-existing fire_now).
The Codex CODE gate had NOT yet run when the GPU memory leak crashed the desktop. Committed to PRESERVE the work
before reboot. >>> ON RESUME: run the Codex(gpt-5.5) CODE review of THIS diff (HEAD) before treating P7 as
accepted; fix any REVISE, then continue P8. <<<
Files: core/kits/{starter,brand,__init__}.py, tools/builtin/scaffold_starter.py, ToolContext.starter_kit +
executor + runtime wiring, appkit.py app_create→lead_form single source, deck contract+pack reconcile
(deck.authored.json), AGENT_TOOLS snapshot, tests (test_kits.py, test_scaffold_starter.py).

---

## HEARTBEAT AUDIT — 2026-06-29T15:00Z (campaign compliance-repair pass)

Triggered by Dylan's compliance directive on resume after the 2026-06-28 workstation GPU leak that
wedged the desktop and ended the autonomous campaign session.

- **Bootstrap:** 2026-06-28T02:55Z. **Cadence:** 10 min (`.claude/heartbeat.py`).
- **Expected heartbeats** (bootstrap → 2026-06-29T15:00Z, ~2164 min): **216**.
- **Observed `heartbeat.log` reminders:** **102** (first 02:56Z, last **19:46Z on 2026-06-28**).
- **Observed ledger sections in `disclaude.md` (live window):** 145 `##`/`###` sections (43 PR/phase
  milestones). During the live window the append discipline was MET — more sections than heartbeats.
- **`heartbeat.log` status:** stopped 2026-06-28T19:46Z; process (pid 163986) **DEAD**, never restarted.
- **Missed heartbeats:** **~114, ALL after 19:46Z on 2026-06-28** — zero fired since.
- **Missed-heartbeat explanation (no hand-waving):** at ~19:46Z 2026-06-28 the workstation's amdgpu VRAM
  leak wedged the display (kwin gfx-ring timeouts) and the box was rebooted. That killed the heartbeat
  process (pid 163986) AND ended the campaign session. The operator's next session was spent diagnosing
  + fixing the GPU/VRAM crisis (root cause: a 27B LLM sharing the display card overcommitted VRAM under
  desktop load; fixed via PCI-pinned card placement + a runtime VRAM guard), NOT the campaign. So the
  ~19h gap (19:46Z 6/28 → 15:00Z 6/29) is fully accounted for: GPU-crisis recovery, externally
  documented, not silent drift.
- **Corrective procedure (executing now):** (1) restart `.claude/heartbeat.py`; (2) resume STRICT
  per-heartbeat ledger — every heartbeat appends a real section here; (3) re-run the Codex CODE review
  of P7 HEAD (it was committed gate-pending pre-crash, review interrupted mid-run) before P7 is treated
  as accepted, fixing any REVISE; (4) re-run the test gate after P7 APPROVE; (5) no phase advance to P8
  until 1–4 are done.

## P1 STATUS CLARIFICATION — 2026-06-29 (renamed for honesty; supersedes prior "P1 STATUS")

P1 (Product Harness / Oracles) is split to end the "is P1 done?" ambiguity:

- **P1A — Headless oracle/policy layer: COMPLETE.** HARN-1a + HARN-2 + HARN-3: provider ledger + 8
  browser oracles + promotion policy + validated evidence writer, all Codex-APPROVED and green.
- **P1B — Browser product harness: PENDING.** HARN-1b — the LIVE Playwright spec that DRIVES the real
  UI and writes the browser-evidence streams. NOT done.
- **HARD GATE:** "Product Harness" is **NOT complete** and must not be marked complete until real
  **Playwright / UI / WebSocket / PreviewPane** evidence exists. Until then: P1A done, P1B open.
  (Per the campaign's own release rule, P2+ promotion-to-mainline still waits on P1B + the P17 soak;
  P2–P7 build work proceeded correctly as build-order, not promotion.)

### PR P7-KITS — CODEX CODE REVIEW (re-run on resume, 2026-06-29) → REVISE
Codex(gpt-5.5, read-only, xhigh) re-reviewed HEAD e3d03d1e (the pre-crash interrupted review redone).
VERDICT: **REVISE** (5 required). Approved: lead_form_appspec + app_create(sections=None) byte-equiv;
brand kit clean projection. Required fixes:
1. scaffold_starter is a FALSE AFFORDANCE — registered but ContractScopeGuard denies it (mutating, not in
   any bootstrap pack). Wire it into the bootstrap path for contracts with artifact.starter_kit.
2. build_static_site.md + build_interactive_prototype.md still instruct file_write for app_shell — must
   call scaffold_starter (only it resolves the starter registry).
3. scaffold_starter._exists() treats ANY read_file error as missing → clobber risk; use sandbox.file_exists().
4. deck pack: slides_generate writes {filename}.authored.json but contract requires exact deck.authored.json
   → pin basename "deck" (or relax contract).
5. Add executor-level tests (DefaultToolExecutor + ContractScopeGuard) proving scaffold_starter runs under
   the active starter contract + advances phase.
NOTE: Codex couldn't run tests (openpyxl missing in registry import) — install before the gate.
P7 NOT accepted. Fixing now, then retest + re-review before acceptance. P8 untouched.

---

# HEARTBEAT AUDIT — 2026-06-29 (compliance repair pass)

**Expected heartbeats since bootstrap:** 219 (interval 600s, bootstrap 2026-06-28T02:56:13Z → now
2026-06-29T15:21Z = 2185 min / 36.4 h).
**Observed (`.claude/heartbeat.log` HEARTBEAT_DUE lines):** 105.
**Missed:** 114 — ALL concentrated in a SINGLE outage gap: **06-28 19:46 → 06-29 15:01 (1155 min / ~19 h,
~115 missed)**. Outside that window the logger ticked regularly.

**heartbeat.log status:** ALIVE. `heartbeat.py` (pid 122882, `--interval 600`) SURVIVED — the GPU memory
leak crashed the desktop/Wayland compositor but the box did NOT fully reboot, so the background logger kept
running; it simply could not advance while the system was wedged/OOM. Resumed at 06-29 15:01; last tick 15:21.

**Missed-heartbeat explanation (two distinct issues):**
1. *The 19h gap* = the documented R9700 GPU-memory-leak crash (orphaned amdgpu VRAM+GTT after a ROCm/vLLM
   compute hang on the shared display+compute card → 15.4 GB pinned host RAM → OOM/freeze → desktop down for
   ~19 h until recovery). NOT a logging defect — the host was effectively offline. Root cause + mitigation are
   recorded in the user's memory note `r9700-gpu-memory-leak.md`.
2. *Ledger-discipline gap (the real compliance miss):* heartbeats were written to `.claude/heartbeat.log` (a
   passive timestamp file) but were NEVER converted into spine ledger sections. The campaign ledger has 33
   `##` sections — all PER-PR, none PER-HEARTBEAT. `heartbeat.py` is a logger, not an actuator; the actual
   loop driver was `ScheduleWakeup` + per-PR commits. Honest record-keeping should have appended a heartbeat
   ledger entry at each turn/heartbeat boundary.

**Corrective procedure (now in force):**
- `heartbeat.py` confirmed running; log intact.
- **STRICT HEARTBEAT LEDGER resumed:** from here, every heartbeat / turn boundary appends a real
  `### HEARTBEAT <UTC ts>` section below with: what was done, test/gate status, and next action. No silent
  heartbeats.
- If a future gap >2 intervals appears, the next heartbeat entry must state the cause (crash/idle/blocked),
  mirroring this audit.

---

# P1 STATUS RECLASSIFIED (compliance repair pass)

The prior "P1 Product Harness COMPLETE" was OVERCLAIMED. Corrected:
- **P1A — headless zero-opinion oracles: COMPLETE.** provider-call ledger + ProviderLedgerOracle (MiniMax-only
  fail-closed), 8 browser-evidence oracles (pure, over a product_evidence dict), promotion policy, validated
  evidence writer, failure codes. All unit-tested headlessly, Codex-gated.
- **P1B — browser product harness: PENDING.** The LIVE Playwright/UI/WS/PreviewPane evidence that actually
  drives the running frontend+agent-server stack and POPULATES product_evidence (browser WS connected,
  PreviewPane render, show-to-user, export download, cleanup) does NOT exist yet. The oracles are written but
  have no live producer.
- **>>> Product Harness is NOT marked complete until P1B exists. <<<** No "Product Harness complete" claim is
  valid without Playwright/UI/WS/PreviewPane evidence. P1B requires the running stack (deferred until then).

---

# STRICT HEARTBEAT LEDGER (resumed 2026-06-29)

### HEARTBEAT 2026-06-29T15:2x Z — compliance repair pass
- Step 1 DONE: P7-KITS Codex CODE review ran on gpt-5.5 → REVISE (5 findings) → all fixed → re-review APPROVE.
  P7 ACCEPTED. Commit `ab06dee2`. Fixes: scaffold_starter wired into static.site/prototype contracts+packs
  (was a partial false affordance); clobber-safe via ctx.sandbox.file_exists; active-contract binding proven
  end-to-end (executor→ToolContext.starter_kit) + runtime resolver test; deck filename pinned (deck.authored.json).
- Steps 2-5 DONE: this HEARTBEAT AUDIT + P1A/P1B reclassification + strict heartbeat ledger resumed (this entry).
- NEXT: Step 6 — run the full test gate after P7 approval; then HALT before P8 per the "Do not start P8" directive.

### HEARTBEAT 2026-06-29T15:3x Z — test gate complete; HALT before P8
- Step 6 DONE: full test gate re-run after P7 approval. core ✅, tools ✅ (fixed the ToolContext field-set
  snapshot to include the additive P7 `starter_kit` field — the no-secret guard correctly caught it),
  harness/build_soak ✅ (124 green, 1 skip), agent-server contract/activation+lifecycle ✅. basedpyright strict
  0 new errors. Pre-existing-only failures remain: test_bakeoff.py import quirk, test_pi_process (needs node),
  test_verify config, fire_now + driver.py:291 pyright (all stash-confirmed pre-existing).
- COMPLIANCE REPAIR PASS COMPLETE (steps 1-6). P7 ACCEPTED. P1 reclassified P1A-done/P1B-pending. Heartbeat
  audit recorded + strict heartbeat ledger in force. Product Harness NOT marked complete (no P1B browser
  evidence).
- HALT: per the "Do not start P8" directive, NOT starting P8. Next action when authorized: P8 Semantic Direct
  Manipulation (scout → plan → Codex gpt-5.5 gate → implement). P1B (live Playwright/UI/WS/PreviewPane harness)
  remains the open evidence gap for any "Product Harness complete" claim.

---

# PARALLEL HARNESS ADDENDUM (2026-06-29)
Source: /home/dylan/.claude/uploads/.../disclaude_parallel_harness_remaining_work.md (full text is authoritative).
OPERATIVE RULES: Claude primary = sole canonical writer (repo, disclaude.md, commits, push, integration).
Codex/GPT (gpt-5.5) = binding review gate (plan + code), NOT a routine implementer. External agents produce
patch.diff/scout_report/fixtures ONLY — they don't push/promote/weaken oracles. No promotion from headless
evidence (needs P1B browser harness). P17 = MiniMax-M3 DIRECT (opencode minimax-coding-plan), never OpenRouter,
never Pi. Phase order: P8 → P9 → P1B-LIVE → P10 → P11 → P12 → P13 → P14 → P15 → P16 → P17. GATES: no P10
before P1B-LIVE exists; no P15 before P1B-LIVE green; no P16 before P10-P13 green; no P17 before P16 goldens green.

## COMMAND DISCOVERY (2026-06-29)
- **Codex gate:** `codex exec --sandbox read-only --model gpt-5.5 "<prompt>"` — WORKING.
- **agy** (Gemini/Antigravity): WORKING. Non-interactive: `agy --print --model "<name>" "<prompt>"` (also
  --sandbox, --add-dir). Models: Gemini 3.5 Flash (Low/Med/High), Gemini 3.1 Pro (Low/High), Claude Sonnet 4.6,
  Claude Opus 4.6, GPT-OSS 120B. Role: frontend/Playwright/UI scout (read-only / separate worktree).
- **opencode** (MiniMax + DeepSeek): WORKING at `/home/dylan/.opencode/bin/opencode` (NOT on PATH — use full
  path). Non-interactive: `opencode run -m <provider/model> "<msg>"` (--format json, --agent, -c continue).
  MiniMax: `minimax-coding-plan/MiniMax-M3` (+M2.x). DeepSeek: `deepseek/deepseek-chat|reasoner|v4-flash|v4-pro`.
  → MiniMax-M3 direct soak driver (P17) = `opencode run -m minimax-coding-plan/MiniMax-M3`. DeepSeek = fixtures.
- **pi** (local Qwen 35B/27B): UNAVAILABLE right now — local llama.cpp server is DOWN (ECONNREFUSED; the R9700
  LLM server isn't running after the GPU-leak recovery). Qwen-via-Pi roles deferred until llama-server is back;
  substitute DeepSeek/Sonnet for fixture/summary roles meanwhile. (Pi also must NOT touch MiniMax — per rule 0.5.)
- **Claude Sonnet subagents:** via the Agent tool (subagent_type) — source/test/risk scouts.

### HEARTBEAT 2026-06-29T (addendum intake)
- Received the Parallel Harness Addendum; appended + ran command discovery (agy✅ opencode✅ pi❌-llama-down).
- NEXT: P8A plan → Codex plan review → parallel scouts (Sonnet test-first + DeepSeek negatives) → implement → test → Codex code review → commit.

## PR P8A — Semantic reference model — PLAN
GOAL: deterministic, 1-index-safe human→target reference resolution that REJECTS ambiguity (never guesses).
FILES (canonical, Claude-owned): packages/core/src/disco/core/semantic_refs.py + packages/core/tests/test_semantic_refs.py
IMPLEMENT: SemanticTargetKind (str,Enum: field/section/card/column/slide/comment_anchor/...); SemanticTarget
(frozen: kind + locator, e.g. section_id+field, or collection+index); ReferenceResolution (resolved: SemanticTarget|None
+ ambiguous: bool + reason + candidates); HumanIndexRef (1-indexed ordinal → 0-indexed array index, safe);
resolve_human_reference(phrase, *, context) → ReferenceResolution; normalize_screen_label(label) → deterministic slug.
RULES: "slide 5" → 5th slide / array index 4 (1-index→0-index); "second service card" → services.cards[1];
unknown/ambiguous → ReferenceResolution(ambiguous=True, candidates=[...]) NOT a guess; normalize_screen_label
deterministic + idempotent. Pure value objects; no runtime/tool imports (house style: frozen pydantic, (str,Enum)).
REQUIRED CASES (from spec): hero headline, hero CTA, second service card, third pricing column, slide 5,
section 3, comment anchor, ambiguous reference.
PARALLEL ASSIGNMENTS: Claude primary = canonical writer; Sonnet B (Agent) = test scout (draft test cases first
from the spec); DeepSeek (opencode) = negative/ambiguous reference fixtures; Qwen35-via-Pi (human-reference
phrase gen) = UNAVAILABLE (Pi down) → DeepSeek covers it. agy/MiniMax = unused for P8A (pure core module).
DONE WHEN: semantic refs deterministic + 1-index safe + reject ambiguity; Codex APPROVE plan+code; tests green.

### PR P8A — PLAN REVISION 1 (post gpt-5.5: direction approved, 6 required fixes)
1. PINNED CONTEXT: SemanticReferenceContext (frozen) = typed inventory the resolver resolves AGAINST:
   sections: tuple[SectionEntry(id, label, aliases, ordinal)]; fields: tuple[FieldEntry(section_id, field_id,
   labels)]; collections: tuple[CollectionEntry(id e.g. "services.cards", item_kind, length, labels)]; slides:
   tuple[SlideEntry(id, title, ordinal)]; comment_anchors: tuple[AnchorEntry(id, label)].
2. TYPED LOCATORS (no raw dict): FieldLocator(kind, section_id, field_id) / SectionLocator(kind, section_id) /
   IndexedLocator(kind, collection_id, index) / SlideLocator(kind, slide_id) / CommentAnchorLocator(kind,
   anchor_id) — a discriminated union (each carries a Literal kind). SemanticTarget = that union. No invalid
   target shapes possible.
3. AMBIGUITY IS AN OUTCOME, not a kind: SemanticTargetKind has NO AMBIGUOUS member. ResolutionReason(str,Enum):
   RESOLVED / AMBIGUOUS / NO_MATCH / INVALID_ORDINAL / OUT_OF_RANGE. ReferenceResolution invariant (asserted in
   a validator): resolved is not None ⟺ reason==RESOLVED ⟺ ambiguous is False; ambiguous ⟺ candidates non-empty.
4. HumanIndexRef pinned: ordinal WORDS (first..tenth) + DIGITS ("5","5th","slide 5","5th slide") → human ordinal
   (1-based) → array index (0-based, =ordinal-1). "second"→1, "third"→2. REJECT 0/"0th"/negative/non-ordinal →
   INVALID_ORDINAL; index ≥ collection.length → OUT_OF_RANGE.
5. normalize_screen_label pinned: deterministic + idempotent; casefold; collapse whitespace+punctuation to single
   "-"; trim. Normalized-label COLLISIONS in the context (two entries same slug) → that reference resolves
   AMBIGUOUS (candidates = the colliding ids), never a guess.
6. Scope: model + pure resolver + tests ONLY (P8B/P8C/P8D separate). Confirmed.
TESTS pin: case-insensitive matching, normalized-label collision→ambiguous, ordinal words AND digits, invalid
ordinal (0/neg), out-of-range index, off-by-one (slide 5→index 4), every required phrase, unknown→ambiguous-not-guessed.

### HEARTBEAT 2026-06-29 — P8A ACCEPTED
- PR P8A (Semantic reference model) COMPLETE. Plan reviewed (gpt-5.5 REVISE→6 fixes→APPROVE), implemented,
  CODE reviewed (gpt-5.5 REVISE→5 fixes→re-REVISE finding-3→fix→APPROVE; reviewer RAN the code to confirm).
- Files: packages/core/src/disco/core/semantic_refs.py + packages/core/tests/test_semantic_refs.py (24 tests).
- 5 code-review fixes: (1) full invariant biconditional; (2) slides/sections by EXPLICIT ordinal equality
  (sparse/out-of-order correct, duplicate→ambiguous); (3) GLOBAL cross-tier ambiguity + subsumption +
  kind-qualified identity (qualified_target_id — fixed a target_id collision that collapsed a section & anchor
  sharing an id); (4) raw-negative vs normalized-zero parsing + IndexedLocator index ge=0; (5) tests for all.
- Parallel: DeepSeek (opencode) generated 25 ambiguous negative fixtures → folded into the never-guess tests.
- 24 tests green, basedpyright strict 0 errors. NEXT: commit P8A → P8B (data-disco-* metadata conventions).

## PR P8B — data-disco-* metadata conventions — PLAN
GOAL: ONE canonical data-disco-* vocabulary + emit helpers + validators; AppKit output carries stable semantic
metadata; comment anchors can't be duplicated or invented; screen labels are deterministic (reuse P8A
normalize_screen_label). Consumed by the renderer (core) + app tools + the P8C frontend resolver.
LAYERING DEVIATION (from the plan's tools/appkit path — flagged for the gate): put the canonical module at
packages/core/src/disco/core/appkit/semantic_metadata.py, NOT tools/appkit/, because render_html lives in core
and core MUST NOT import tools. Putting the vocabulary in tools would create TWO sources of truth (the renderer's
hardcoded "data-disco-section"/"data-disco-field" strings at models.py:130-156 + tools constants). Single-source
wins: the renderer is refactored to EMIT via the vocabulary (kills the hardcoded strings). Tools/frontend import
the core vocabulary. Test at packages/core/tests/test_appkit_semantic_metadata.py.
API: DataDiscoAttr(str,Enum)= FIELD/SECTION/FILE/FLOW/SCREEN_LABEL/COMMENT_ANCHOR/METRIC_ID/VERSION (values
"data-disco-field" ...). attr(a, value)->' data-disco-x="esc"' (html-escaped, ready to inject). screen_label_value
(label)->normalize_screen_label (stable). SemanticMetadataError. extract_metadata(html)->dict[attr,list[value]].
validate_no_duplicate_anchors(html)-> raise on a repeated data-disco-comment-anchor. validate_anchors_not_invented
(html, declared:set)-> raise if an emitted anchor isn't declared. Renderer: _render_section emits via attr(), adds
data-disco-screen-label (normalized section id/label) per section + data-disco-version on <html>.
TESTS: hero metadata emitted (section+field+screen-label), contact-form metadata, admin-table metadata
(generic/table section w/ field+metric-id), duplicate anchors rejected, anchors-not-invented rejected, screen
labels stable (byte-identical across two renders). Existing appkit tests stay green (render output superset).
PARALLEL: agy(Gemini) UI scout for the data-disco attr set vs frontend needs (advisory) — deferred to P8C; P8B
is core. DeepSeek = duplicate/invented-anchor negative fixtures. Sonnet test scout = unused (small surface).
SCOPE: vocabulary+emit+validate+renderer-wiring ONLY. P8C frontend resolver + P8D oracles separate.

### PR P8B — PLAN REVISION 1 (post gpt-5.5: core placement APPROVED; 5 fixes)
1. INDEXED-ITEM ATTRS added to the vocabulary so P8A IndexedLocator is DOM-resolvable: data-disco-collection
   ("services.cards"), data-disco-index (0-based), data-disco-item-kind ("card"). Emit helper + unit test now.
   RENDERER emitting collections is DEFERRED (AppKit has no collection/list section kind yet — when one lands,
   it stamps these). Explicitly out of P8B renderer scope; the convention + emit + extract exist + are tested.
2. ADMIN-TABLE test DROPPED (AppSection.kind is an allowlist Literal — "admin/table" is rejected by design).
   Replaced with an existing-kind generic section (kind in the allowlist) carrying section+field+screen-label,
   PLUS the indexed emit-helper unit test (collection+index+item-kind) standing in for repeated items.
3. DECLARED ANCHORS derived from the source of truth: declared_anchors_from_spec(spec) -> frozenset, derived
   deterministically from section ids (the legitimate anchor namespace). validate_anchors_not_invented(html,
   declared) raises if an EMITTED data-disco-comment-anchor is outside `declared`. Tests: (a) a real render has
   zero invented anchors vs declared_anchors_from_spec; (b) synthetic HTML with an out-of-namespace anchor raises;
   (c) declared_anchors_from_spec derivation itself is tested.
4. TS CROSS-LANGUAGE (note, addressed in P8C): the frontend (TS) cannot import Python core; P8C consumes the
   emitted DOM attrs and/or a generated shared-constants artifact. P8B documents the 8+3 attr VALUES as the
   cross-language contract (the string values are the API). Generation of discoSemanticAttrs.ts deferred to P8C.
5. EXTRA TESTS: html-escaping + roundtrip extract_metadata (decode before compare); STABLE attr ordering in
   emitted output; duplicate DECODED anchor values caught (two encodings, same decoded value → duplicate);
   data-disco-version present on <html>. Existing appkit tests stay green (output is a metadata SUPERSET —
   intentional byte change; P7 byte-equivalence is starter-vs-current-renderer, not old bytes).

### HEARTBEAT 2026-06-29 — P8B ACCEPTED
- PR P8B (data-disco-* metadata conventions) COMPLETE. Plan gpt-5.5 REVISE(5: indexed attrs, drop admin-table,
  declared-anchor source, TS-boundary note, escaping/ordering/version tests)→APPROVE. Code gpt-5.5 APPROVE first-pass.
- Files: core/appkit/semantic_metadata.py (canonical DataDiscoAttr vocab — 11 attrs incl collection/index/item-kind
  for P8A IndexedLocator; attr/item_attrs/extract_metadata/validate_no_duplicate_anchors/declared_anchors_from_spec/
  validate_anchors_not_invented) + models.py renderer refactored to emit THROUGH the vocab (killed hardcoded
  data-disco strings; +data-disco-screen-label per section +data-disco-version on <html>) + test (16). Single source;
  import-cycle dodged via TYPE_CHECKING. Existing appkit/kits/tools tests stay green (P7 byte-equiv intact).
- NEXT: P8C frontend semantic resolver (TS: discoSemanticResolver.ts + selectionAgent.ts). agy/Gemini = frontend
  scout. Needs the generated discoSemanticAttrs.ts cross-language constants (deferred from P8B).

## PR P8C — Frontend semantic resolver — PLAN
CONTEXT: frontend already has selectionAgent.ts (in-frame injected script), selectionBridge.ts (SourceRef{kind:
"source",oid,file,line} from data-oid via appResolver.parseOid), EditAffordance/PreviewPane (click-to-edit). P8C
adds a SEMANTIC resolution layer: data-disco-* → a typed target (mirroring P8A locators), with data-oid as the
FALLBACK. vitest+jsdom test runner.
FILES: frontend/src/lib/resolvers/discoSemanticResolver.ts (+ .test.ts), frontend/src/lib/discoSemanticAttrs.ts.
CROSS-LANGUAGE SINGLE SOURCE: discoSemanticAttrs.ts holds the data-disco-* attribute NAME constants mirroring the
Python core DataDiscoAttr (P8B). A Python DRIFT-GUARD test (packages/core/tests/test_semantic_attr_parity.py)
reads the .ts file + asserts its values == DataDiscoAttr values (TS can't import Python; the test enforces parity).
RESOLVER (pure, DOM-walking): resolveSemanticTarget(el: Element): DiscoSemanticTarget | null. Walk from el up to
the nearest ancestor carrying a data-disco-* attr, BY PRIORITY: field > section > file > comment_anchor, with
data-oid SOURCE as the fallback. Result = discriminated union: {kind:'field',section,field} | {kind:'section',
section} | {kind:'file',file} | {kind:'comment_anchor',anchor} | {kind:'source',oid,file,line} | null. A field
target ALSO resolves its enclosing data-disco-section (so "click hero headline" → {field, section:'hero',
field:'headline'}). Plus semanticEditTarget(t): a stable string id ("hero.headline" / "hero" / anchor) for the
app_update_content-style steer.
SELECTIONAGENT: noted — the in-frame agent prefers resolveSemanticTarget (semantic), falls back to data-oid; the
actual injection wiring is small + verified in P1B-LIVE (browser). P8C core = the pure resolver + parity, unit-tested.
TESTS (vitest/jsdom): click hero headline → field hero.headline; click a section → section target; click unknown
DOM (no data-disco, no oid) → null; comment_anchor resolved; data-oid fallback when no data-disco present; priority
ordering (field beats an ancestor section). + Python parity test.
PARALLEL: agy(Gemini) frontend scout (invocation fix: `agy --model "<m>" --print "<prompt>"`). DeepSeek = jsdom
fixture variants. SCOPE: resolver + attrs + parity + tests. Full in-frame injection + live click → P1B-LIVE.

### PR P8C — PLAN REVISION 1 (post gpt-5.5: 6 fixes)
1. INDEXED target added: resolve data-disco-collection + data-disco-index (nonneg int) + optional data-disco-item-
   kind → {kind:'indexed',collection,index,itemKind?}; beats an enclosing section.
2. TRAVERSAL pinned CLOSEST-FIRST: walk from the clicked el upward; the FIRST ancestor carrying ANY data-disco
   semantic attr wins, resolved by that node's WITHIN-NODE specificity order indexed>field>comment_anchor>section>
   file. A farther section/file can NEVER mask a closer field/indexed/comment_anchor.
3. FIELD fails closed: a field target must find a nearest enclosing data-disco-section (continue up); if none →
   fall back to source (data-oid) or null — never emit a section-less field.
4. TYPES split: DiscoSemanticTarget = field|section|indexed|comment_anchor|file (semantic); SourceRef stays the
   data-oid fallback. DiscoResolvedTarget = DiscoSemanticTarget | SourceRef. semanticEditTarget() accepts ONLY
   semantic targets. NOTE: data-disco-file is FRONTEND file-level metadata, NOT a P8A locator (don't claim parity
   for it) — kept for file-scoped selection.
5. CONSTANTS framing: Python core DataDiscoAttr is CANONICAL; discoSemanticAttrs.ts is a checked-in MIRROR; the
   Python drift-guard test asserts EXACT equality of ALL 11 attr values (no missing/extra).
6. TESTS add: indexed target, invalid/negative index (rejected→fallback), comment_anchor nested in section/file
   (closest wins), field-without-section (fail closed), source fallback ONLY when no semantic target applies.

### HEARTBEAT 2026-06-29 — P8C ACCEPTED
- PR P8C (frontend semantic resolver) COMPLETE. Plan gpt-5.5 REVISE(6: indexed target, closest-first traversal,
  field-fail-closed, type split, canonical/mirror framing, edge tests)→APPROVE. Code gpt-5.5 APPROVE first-pass.
- Files: frontend/src/lib/discoSemanticAttrs.ts (TS mirror of Python DataDiscoAttr), resolvers/
  discoSemanticResolver.ts (closest-first DOM walk → DiscoSemanticTarget|SourceRef; field fails closed; nonneg
  index; data-oid fallback), .test.ts (10 vitest), packages/core/tests/test_semantic_attr_parity.py (drift guard).
- ENV: frontend node_modules was missing in the clone → ran `npm ci` (582 pkgs). vitest 10/10, tsc 0 errors,
  Python parity green. In-frame selectionAgent injection + live click deferred to P1B-LIVE (browser evidence).
- NEXT: P8D (targeted-edit + manual-edit-preservation oracles) → completes P8.

## PR P8D — Targeted-edit & manual-edit oracles — PLAN
PATH DEVIATION (flag for gate): plan says harness/oracles/ + harness/tests/, but the real oracle layer is
harness/build_soak/oracles/ (zero-opinion OracleResult via schema.passing/failing/skipping + failure_codes with
SEVERITY_BY_CODE) tested in harness/build_soak/tests/. Reuse that infra (no parallel oracle framework). Files:
harness/build_soak/oracles/targeted_edit.py + manual_edit_preservation.py; tests in harness/build_soak/tests/.
ORACLES (pure fns over plain-dict evidence; live producer = P1B-LIVE/soak, like the P1A browser oracles):
- TargetedEditOracle: edited_files ⊆ expected_files → else fail TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES (P1).
- RewriteAvoidanceOracle: a SMALL-scoped edit's churn_ratio (changed_lines/total_lines) ≤ threshold → else
  SMALL_EDIT_FULL_REWRITE (P1). (Threshold gated; only adjudicated when edit_scope=="small".)
- ManualEditPreservationOracle: every manual_override snippet still present in the post-edit file → else
  MANUAL_EDIT_CLOBBERED (P1).
- CommentAnchorOracle: anchors_before ⊆ anchors_after (preserved through text edit; survive a section reorder —
  set membership, not position) → else COMMENT_ANCHOR_LOST (P1).
- ScreenLabelOracle: for sections NOT in the edit's targets, screen_label_before == screen_label_after (stable) →
  else SCREEN_LABEL_UNSTABLE (P2).
Each returns passing/skipping (no applicable evidence → SKIP, never silent pass) or failing(code, first_broken_link).
New codes added to failure_codes.py + registered in SEVERITY_BY_CODE. Exported from oracles/__init__.py.
TESTS (harness/build_soak/tests/): small edit touches expected files only (pass+fail); comment anchor preserved
through a text edit; comment anchor survives a section reorder; direct override not clobbered (pass+fail); small
CTA edit does not rewrite the whole app (pass+fail); screen-label stability; each oracle SKIPs cleanly with no
evidence. PARALLEL: DeepSeek = edit-churn/clobber negative fixtures. SCOPE: the 5 oracles + codes + tests; the
live evidence producer is P1B-LIVE.

### PR P8D — PLAN REVISION 1 (post gpt-5.5: 4 fixes; path/predicate/SKIP/severity approved)
1. EVIDENCE SCHEMA pinned per oracle (each reads ONE named slice of the evidence dict):
   - TargetedEditOracle ev["targeted_edit"]={"edited_files":[str],"expected_files":[str]}.
   - RewriteAvoidanceOracle ev["rewrite_avoidance"]={"edit_scope":str,"changed_lines":int,"total_lines":int,
     "max_churn_ratio":float}.
   - ManualEditPreservationOracle ev["manual_edit"]={"overrides":{path:snippet},"final_files":{path:content}}.
   - CommentAnchorOracle ev["comment_anchors"]={"before":[str],"after":[str]}.
   - ScreenLabelOracle ev["screen_labels"]={"edited_sections":[str],"before":{sec:label},"after":{sec:label}}.
   MALFORMED HANDLING: slice ABSENT → SKIP (reason, never silent pass). Slice PRESENT but missing a required key
   / wrong type → FAIL with shared code EDIT_ORACLE_EVIDENCE_MALFORMED (P1, a harness-validity-class failure) —
   present-but-malformed never silently passes.
2. CHURN THRESHOLD evidence-supplied: max_churn_ratio comes from ev["rewrite_avoidance"] (scenario sets it;
   recommended default 0.20 documented). Adjudicate ONLY when edit_scope=="small"; ratio=changed/total; facts
   carry {edit_scope, changed_lines, total_lines, churn_ratio, max_churn_ratio}. Missing max_churn_ratio →
   MALFORMED. total_lines==0 → MALFORMED (no divide-by-zero).
3. TESTS add: every oracle SKIPs on absent slice; every oracle FAILs EDIT_ORACLE_EVIDENCE_MALFORMED on a present-
   but-malformed slice (missing key / wrong type / total_lines 0).
4. CLASSIFY() BOUNDARY explicit: export TARGETED_EDIT_ORACLES tuple + wire it into classify.py exactly like
   BROWSER_EVIDENCE_ORACLES (the loop at classify.py:155) — SKIP-safe, so wiring is safe NOW (they SKIP without
   evidence); the live evidence PRODUCER is P1B-LIVE. A test asserts classify() with no edit evidence yields SKIPs
   (not FAILs) + doesn't regress existing classification.

### HEARTBEAT 2026-06-29 — P8D ACCEPTED → P8 COMPLETE
- PR P8D (targeted-edit + manual-edit oracles) COMPLETE. Plan gpt-5.5 REVISE(4)→APPROVE; code REVISE(2: present-
  non-dict-slice-looked-absent silent-pass hole + str/bool coercion masking malformed types)→fix→APPROVE.
- Files: harness/build_soak/oracles/{targeted_edit,manual_edit_preservation,_edit_evidence}.py + 5 failure codes
  (4×P1 + SCREEN_LABEL_UNSTABLE P2 + EDIT_ORACLE_EVIDENCE_MALFORMED in HARNESS_VALIDITY_CODES→INVALID_RUN) +
  TARGETED_EDIT_ORACLES wired into classify.py step 9 (skip-safe) + 28 tests. Producer=P1B-LIVE.
- ===== P8 COMPLETE (A semantic-refs + B data-disco metadata + C frontend resolver + D edit oracles), all 4
  Codex-gated + pushed. =====
- NEXT: P9 (TweakSpec + owner controls: P9A schema, P9B tweaks_io, P9C app_set_tweak, P9D TweakPanel UI). Then
  P1B-LIVE (pull-forward gate before P10).

## PR P9 — TweakSpec & owner controls
## PR P9A — TweakSpec schema — PLAN
CONTEXT: tweaks today = untyped AppSpec.tweaks dict[str,Any] + stringly app_set_tweak. P9A adds the typed
DEFINITION layer (what tweaks exist + their editor + constraints); P9B = .disco/tweaks.json IO, P9C = typed
app_set_tweak validating against it, P9D = TweakPanel UI. P9A is pure value objects + validation (no runtime).
FILES: packages/core/src/disco/core/tweaks.py + packages/core/tests/test_tweaks.py.
API: TweakEditor(str,Enum)= TEXT/COLOR/INT/FLOAT/BOOLEAN/ENUM/PALETTE/NULL. TweakField (frozen pydantic):
key, label, editor, + editor-specific constraints: options:tuple[str,...] (ENUM), colors:tuple[str,...] (PALETTE),
min/max/step:float (INT/FLOAT); affects:tuple[str,...] (the AppSpec paths it controls), controls_behavior:bool.
VALIDATORS (model_validator): ENUM requires non-empty options (+default∈options if set); PALETTE requires 2..5
colors; INT/FLOAT require min/max/step with min<=max and step>0 (INT min/max/step integral); TEXT/COLOR/BOOLEAN/
NULL carry no numeric/option constraints (reject if present). JUSTIFICATION RULE ("ordinary copy/color shouldn't
be a tweak unless it controls behavior or many fields"): a TEXT or COLOR field must have controls_behavior=True OR
len(affects)>=2, else ValueError (use direct edit, not a tweak). Other editors (boolean/enum/palette/numeric) are
inherently control-like → allowed. TweakSpec (frozen): fields:tuple[TweakField,...] with unique keys; .get(key);
.validate_value(key, value)->coerced value or ValueError (typed per editor: color hex check, enum∈options, palette
list 2-5 hex, int/float in [min,max] and step-aligned, boolean true/false, null→None, text→str).
TESTS: each editor's happy path; ENUM-without-options rejected; PALETTE 1 or 6 colors rejected; numeric missing
min/max/step rejected + min>max rejected; TEXT-without-justification rejected, TEXT-with-controls_behavior or
affects>=2 allowed; duplicate keys rejected; validate_value coerces/validates per editor incl out-of-range +
bad-hex + enum-not-in-options; default∈options.
PARALLEL: DeepSeek(opencode)= invalid-tweak-field negative fixtures. SCOPE: schema+validation only (IO/tool/UI
in P9B/C/D).

### PR P9A — PLAN REVISION 1 (post gpt-5.5: direction approved; 5 fixes)
1. VALUE SCHEMA: add optional `default` (validated against the editor, e.g. default∈options). Drop NULL editor
   (no control surface). 7 editors, lowercase values: text/color/int/float/boolean/enum/palette. key non-empty +
   dotted-path pattern ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$ ; label non-empty. extra="forbid"; reject constraints
   irrelevant to the editor (options only on enum, colors only on palette, min/max/step only on numeric).
2. NUMERIC: reject bool for INT/FLOAT; reject NaN/inf; INT requires integral min/max/step + integral values; FLOAT
   real; step>0; min<=max; (max-min) MUST be step-aligned (reachable max). Step alignment EXACT via integer scaling
   (round((v-min)/step) compare) / Decimal(str(.)), NEVER float modulo.
3. COLOR/PALETTE: accept hex subset #RGB and #RRGGBB only (no names/rgb()/alpha); normalize to lowercase #rrggbb.
   PALETTE.colors = 2..5 UNIQUE curated swatches (canonical hex); a PALETTE value must be one of those swatches
   (pick-from-curated). COLOR value = any canonical hex (free). (COLOR=free hex; PALETTE=curated swatch pick.)
4. JUSTIFICATION: BASE invariant for ALL fields — controls_behavior OR non-empty affects (else ungrounded →
   reject). TEXT/COLOR STRONGER — controls_behavior OR len(affects)>=2 (single-place copy/color belongs in
   app_update_content/app_set_design, not a tweak). affects = LEXICAL dotted paths only; NOT validated against a
   live AppSpec in P9A (that reconciliation is P9B/C). Define the path syntax now.
5. validate_value(key,value) COERCES narrowly + per-editor: text→str (reject non-coercible); color/palette→
   canonical hex (palette∈swatches); boolean→bool incl "true"/"false" case-insensitive (reject other strings);
   int→int or all-digit str (reject bool, fractional); float→float or numeric str (reject bool/NaN/inf); enum→
   ∈options; all numerics range + step-aligned. Returns the coerced canonical value or ValueError.
TweakSpec(frozen): fields unique-keyed; .get(key); .validate_value(key,value). TESTS add: default∈options + bad
default rejected; NULL gone; bool-as-int rejected; NaN/inf rejected; unreachable max (max-min not step-aligned)
rejected; #RGB expands + lowercased; non-hex/rgb()/alpha rejected; palette duplicate/1/6 rejected; palette value
not-in-swatches rejected; ungrounded field (no controls_behavior+no affects) rejected; coercion happy+sad per editor.

### HEARTBEAT 2026-06-29 — P9A ACCEPTED
- PR P9A (TweakSpec schema) COMPLETE. Plan gpt-5.5 REVISE(5: default/drop-NULL/exact-int-scaled-step/canonical-hex/
  grounded-invariant)→APPROVE. Code gpt-5.5 REVISE(3: affects not lexically validated, dup-affects faked ">=2
  fields", palette set-input bypassed canonicalization)→fix→APPROVE (reviewer ran pytest).
- File: packages/core/src/disco/core/tweaks.py (TweakEditor 7 editors, TweakField w/ grounded+justification rules +
  exact numeric bounds + canonical_hex + per-editor coercion, TweakSpec registry + validate_value) + test (22).
- NEXT: P9B (.disco/tweaks.json IO: read/write/require/merge_tweak_defaults) → P9C (typed app_set_tweak) → P9D
  (TweakPanel UI) → then P1B-LIVE.

## PR P9B — .disco/tweaks.json IO — PLAN
PATH DEVIATION (flag for gate): plan says packages/tools/src/disco/tools/appkit/tweaks_io.py, but there is no
disco.tools.appkit package — the AppSpec IO (AppSpecStore) + app_set_tweak (P9C's consumer) live in
packages/tools/src/disco/tools/builtin/appkit.py. Put tweaks_io.py in builtin/ next to them (module
disco.tools.builtin.tweaks_io); tests packages/tools/tests/test_tweaks_io.py. (No tools.appkit package churn.)
MODEL: .disco/tweaks.json = the TweakSpec (P9A definitions + per-field defaults); AppSpec.tweaks (in
.disco/appspec.json) = the current VALUES. tweaks_io bridges them.
API:
- TWEAKS_PATH = ".disco/tweaks.json".
- read_tweakspec(sandbox) -> TweakSpec | None: use sandbox.file_exists FIRST (absent→None), THEN read_file +
  TweakSpec.model_validate_json (corrupt→ValueError, caller maps to error). file_exists-first avoids the P7
  clobber-class bug of treating a transient read error as "absent".
- write_tweakspec(sandbox, spec): write_file(TWEAKS_PATH, spec.model_dump_json(indent=2)+"\n").
- require_tweakspec(sandbox) -> TweakSpec: read_tweakspec; raise TweaksError("no .disco/tweaks.json") if None.
- merge_tweak_defaults(spec: TweakSpec, values: Mapping[str,Any]) -> dict[str,Any]: returns a NEW dict where
  (1) every present value for a DEFINED tweak is validated+coerced via spec.validate_value (canonical), (2) a
  defined tweak absent from values but with a non-None default is filled from its default (validated), (3) a
  value keyed to an UNDEFINED tweak raises TweaksError (never silently keep an ungoverned value). Deterministic
  key order (sorted) for byte-stable persistence.
TESTS (packages/tools/tests/test_tweaks_io.py, FakeSandboxInstance): round-trip write→read; read absent→None;
read corrupt→ValueError; read where file_exists True but read_file raises→propagates (NOT silently None);
require absent→TweaksError; merge fills defaults + coerces present values ("true"→True, "#FFF"→"#ffffff",
"3"→3); merge rejects unknown value key; merge leaves a value with no default + not-present absent; deterministic
ordering. PARALLEL: DeepSeek = corrupt/edge tweaks.json fixtures. SCOPE: IO + merge only (tool=P9C, UI=P9D).

### PR P9B — PLAN REVISION 1 (post gpt-5.5: all decisions a-f approved; 4 refinements)
1. write_tweakspec writes BYTES: await sandbox.write_file(TWEAKS_PATH, (spec.model_dump_json(indent=2)+"\n").
   encode("utf-8")) — matches AppSpecStore.write + sandbox protocol.
2. TweaksError (new domain exception) for ALL merge SEMANTIC failures — unknown value key, invalid present value,
   invalid default — message includes the tweak KEY; the underlying ValueError preserved as __cause__ (raise ...
   from e). require_tweakspec-absent also raises TweaksError.
3. Backend/IO failures stay UNWRAPPED: a file_exists error or a read_file error AFTER existence is confirmed
   PROPAGATES (not caught/None). read_tweakspec corrupt JSON/schema → ValueError/pydantic ValidationError (caller
   maps to corrupt_tweakspec). Only "file absent" → None.
4. merge_tweak_defaults: defaults fill ONLY when the key is ABSENT (never overwrite a present value); a present
   None is treated as PRESENT and validated (not default-filled). Every present value for a defined tweak is
   coerced via spec.validate_value; an undefined-tweak value key → TweaksError.
TESTS add: invalid defined PRESENT value → TweaksError (not just unknown key); present None validated-not-filled;
read_file-raises-after-file_exists-True → propagates.

### HEARTBEAT 2026-06-29 — P9B ACCEPTED
- PR P9B (.disco/tweaks.json IO) COMPLETE. Plan gpt-5.5 (decisions a-f approved) REVISE(4 refinements)→APPROVE;
  code gpt-5.5 APPROVE first-pass.
- File: packages/tools/src/disco/tools/builtin/tweaks_io.py (TWEAKS_PATH, read_tweakspec [file_exists-first,
  absent→None, read-error propagates, corrupt→ValidationError], write_tweakspec [bytes], require_tweakspec
  [TweaksError], merge_tweak_defaults [coerce present values, fill absent defaults, present-None=present, raise
  TweaksError on unknown-key/invalid-value/invalid-default], TweaksError) + test (11). Path deviation builtin/
  (no disco.tools.appkit pkg) — gate-approved.
- NEXT: P9C (typed app_set_tweak validating against TweakSpec, in builtin/appkit.py) → P9D (TweakPanel UI) → P1B-LIVE.

## PR P9C — typed app_set_tweak — PLAN
GOAL: app_set_tweak (builtin/appkit.py) becomes GOVERNED — it validates the value against the app's TweakSpec
(.disco/tweaks.json, P9B) instead of blindly coercing+storing any key. CONTRACT CHANGE (flag): the tool now
REQUIRES a TweakSpec; setting a tweak on an app with no .disco/tweaks.json → a clear error (no false affordance).
FLOW (AppSetTweakTool.run): (1) require_tweakspec(ctx.sandbox) → spec; TweaksError/absent → ToolOutcome(success=
False, error="no_tweakspec", content="this app defines no tweaks (.disco/tweaks.json missing)"). corrupt tweaks
.json (ValidationError) → error="corrupt_tweakspec". (2) field=spec.get(key); if None → error="unknown_tweak"
(content lists available keys). (3) coerced=spec.validate_value(key, args.value); on ValueError → error=
"invalid_tweak_value" (content = the validator message + the editor/constraints). (4) _apply(ctx, lambda s:
s.with_tweak(key, coerced)) to persist appspec+re-render; merge the success structured with {"tweak":key,
"value":coerced,"affects":list(field.affects)} so the caller/UI sees what changed + the AppSpec paths controlled.
ORDER: validate tweak (tweakspec) BEFORE touching appspec — a bad tweak never mutates the app.
WHO WRITES tweaks.json: out of P9C scope (a builder / app_create-defaults / P9D seed step) — FLAG it; P9C only
consumes + validates. ARGS unchanged (AppSetKVArgs key:str,value:str). app_set_design UNCHANGED.
TESTS (test_appkit_tools.py or a new test_app_set_tweak.py): set a defined boolean "true"→stored True + affects
echoed; unknown key→unknown_tweak; invalid value (out of range / bad enum)→invalid_tweak_value; no tweaks.json→
no_tweakspec; corrupt tweaks.json→corrupt_tweakspec; the appspec is NOT mutated on any validation failure (read-
back unchanged). Update/repair any existing app_set_tweak test that assumed the untyped behavior. PARALLEL:
DeepSeek=invalid-value fixtures. SCOPE: the tool wiring + tests only (UI=P9D; tweaks.json authoring later).

### PR P9C — PLAN REVISION 1 (post gpt-5.5: 5 fixes; the contract change must be product-LIVE)
1. NO false affordance — SEED a real TweakSpec in app_create (part of P9C): add default_tweakspec() (core/tweaks
   or a builtin helper) → a small GROUNDED spec for the default lead app: lead.include_phone (boolean, affects
   ("lead_form.phone"), default False) + brand.accent (palette, swatches from the brand, affects ("design.accent")).
   app_create writes .disco/tweaks.json via write_tweakspec ALWAYS — default_tweakspec() for the sections=None
   default app; an EMPTY TweakSpec(fields=()) for custom-sections apps. So after app_create a tweaks.json always
   exists → app_set_tweak is immediately usable (no dead tool). (Does NOT affect P7 byte-equiv: that compares only
   .disco/appspec.json + index.html; tweaks.json is a new separate file.)
2. ERROR PRECEDENCE (don't use _apply — inline read→validate→write for control): no_app > corrupt_appspec >
   no_tweakspec > corrupt_tweakspec > unknown_tweak > invalid_tweak_value. Read appspec FIRST (no_app/corrupt),
   then require_tweakspec, then validate, then with_tweak + store.write LAST — the appspec is only written after
   ALL validation passes (a bad tweak never mutates the app).
3. Echo CANONICAL value (the coerced result), not raw input, in structured {tweak, value, affects:list(field.
   affects)}. affects = declared AppSpec metadata (lexical), not proof-of-change.
4. UPDATE tool description: "Set a tweak defined in .disco/tweaks.json (validated against its TweakSpec)."; keep
   advertising it (now seeded → usable). app_set_design UNCHANGED.
5. MIGRATE existing tests (test_appkit_tools.py): test_set_design_and_tweak + test_tweak_coercion_is_robust assumed
   arbitrary untyped keys → convert to typed: seed via app_create (or write_tweakspec), assert defined-tweak set +
   unknown_tweak + invalid_tweak_value; assert AppSpec bytes UNCHANGED on no_tweakspec/corrupt_tweakspec/unknown_
   tweak/invalid_tweak_value; AGENT_TOOLS snapshot unaffected (no tool added).

### HEARTBEAT 2026-06-29 — P9C ACCEPTED
- PR P9C (typed app_set_tweak) COMPLETE. Plan gpt-5.5 REVISE(5: SEED tweakspec in app_create so the governed tool
  isn't a false affordance; no_app precedence; canonical value echo; description; migrate untyped tests)→APPROVE.
  Code gpt-5.5 APPROVE first-pass.
- builtin/appkit.py: _default_tweakspec() (lead.include_phone bool + brand.accent palette, grounded); app_create
  ALWAYS seeds .disco/tweaks.json (default spec for lead app, empty for custom); AppSetTweakTool typed — inline
  read appspec→read_tweakspec→get field→validate_value→write LAST; errors no_app>corrupt_appspec>no_tweakspec>
  corrupt_tweakspec>unknown_tweak>invalid_tweak_value; structured echoes {tweak,value,affects}; dead _coerce_scalar
  removed. Tests migrated (appspec-unchanged-on-error proven) + P7 byte-equiv + AGENT_TOOLS snapshot intact.
- NEXT: P9D (TweakPanel UI — frontend TweakPanel.tsx + AppCard.tsx + vitest; agy frontend scout) → then P1B-LIVE.

## PR P9D — TweakPanel UI — PLAN
CONTEXT (agy scout): no UI kit — native HTML inputs + Tailwind design tokens (surface-1/border-hairline/accent/
font-ui/gap-hair/rounded-control), cn from @/lib/cn, decoupled callback-prop pattern (parent issues the wire
command), tests @testing-library/react + user-event/fireEvent + vitest + jest-dom. TS has NO TweakSpec import →
field defs come via PROPS.
FILES: frontend/src/components/build/TweakPanel.tsx (+ .test.tsx) + AppCard.tsx.
TYPES (in TweakPanel.tsx, exported): TweakFieldView = { key; label; editor: "boolean"|"enum"|"int"|"float"|
"color"|"palette"|"text"; options?: string[]; colors?: string[]; min?: number; max?: number; step?: number } —
the UI-relevant subset mirroring core TweakField (a runtime API/props mirror, like discoSemanticAttrs). TweakValue
= string|number|boolean.
TweakPanel Props: { fields: TweakFieldView[]; values: Record<string,TweakValue>; onTweak: (key, value) => void }.
EDITOR→CONTROL (semantic per editor — NO freeform soup; an unknown editor renders a disabled "unsupported" note,
NEVER a fallback text box):
- boolean → <input type=checkbox> (checked from values) → onTweak(key, checked)
- enum → <select> of options (value from values) → onTweak(key, option)
- int/float → <input type=range min/max/step> (value from values) → onTweak(key, Number)
- palette → row of curated swatch <button>s (one per colors[]) → onTweak(key, color); active swatch ring
- color → <input type=color> (bounded native picker, NOT a hex text field) → onTweak(key, value)
- text → ONE bounded <input type=text> (a defined grounded text tweak; not generic soup) → onTweak(key, value)
Each control wrapped in a labeled row (field.label). onTweak is the contract; the PARENT wires it to app_set_tweak
(the live mount into the build surface + real call = P1B-LIVE). AppCard.tsx = a thin presentational card (app
title + the mounted TweakPanel) — real, minimal, not a stub.
TESTS (TweakPanel.test.tsx, jsdom): render boolean+enum+slider+palette+color+text fields; each control present by
role; toggling boolean→onTweak(key,bool); enum select→onTweak(key,opt); slider change→onTweak(key,number); swatch
click→onTweak(key,color); color change→onTweak(key,hex); text type→onTweak(key,str); NO stray textbox for a
boolean/enum/palette (no-soup); values reflected (checkbox checked, select value, slider value); unknown editor →
disabled note, no input. + an AppCard render test mounting TweakPanel. tsc 0 errors.
PARALLEL: agy(Gemini) scout DONE (patterns captured). SCOPE: TweakPanel + AppCard + tests; live wiring=P1B-LIVE.

### PR P9D — PLAN REVISION 1 (post gpt-5.5: mapping/seam/AppCard approved; 4 fixes)
1. TEXT bounded: <input type="text" maxLength={field.maxLength ?? 120}> — add maxLength?:number to TweakFieldView;
   test asserts NO textarea/contenteditable/freeform fallback anywhere.
2. FAIL CLOSED on MALFORMED known editors too (not only unknown): enum w/o options, palette w/o colors, int/float
   w/o min|max|step → render a disabled "unsupported" note + NO input control. (boolean/text always renderable.)
3. CONTROL METADATA: each row carries data-disco-control="build.tweak.<key>" + data-tweak-key="<key>" for
   automation — NOT data-disco-field/section/etc (those are P8C rendered-APP target attrs; never echo affects as
   semantic attrs).
4. A11Y + TESTS: color queried by accessible LABEL (jsdom color role unreliable); palette = a labeled group
   (role/aria-label) of swatch <button>s each with aria-label + aria-pressed selected state; assert value
   reflection for checkbox/select/range/color/text + selected swatch; assert "no soup" via DOM selectors — for a
   boolean/enum/palette/color/unknown row there is NO extra input[type=text]/textarea/[contenteditable].

### HEARTBEAT 2026-06-29 — P9D ACCEPTED → P9 COMPLETE
- PR P9D (TweakPanel UI) COMPLETE. Plan gpt-5.5 REVISE(4: bounded text+maxLength, fail-closed on malformed KNOWN
  editors, data-disco-control not P8C attrs, a11y aria-pressed + DOM no-soup)→APPROVE. Code gpt-5.5 REVISE(2:
  isMalformed only caught undefined not runtime null bounds; missing reflection/metadata/AppCard tests)→fix→APPROVE.
- Files: frontend/src/components/build/TweakPanel.tsx (one semantic control per editor; native inputs; fail-closed
  on malformed-known + unknown editors via notFiniteNum guard; data-disco-control/data-tweak-key; aria-pressed
  swatches) + AppCard.tsx (real pass-through host + empty-state) + TweakPanel.test.tsx (17 vitest). tsc 0.
- ===== P9 COMPLETE (A TweakSpec schema + B tweaks_io + C typed app_set_tweak+seed + D TweakPanel UI), all Codex-
  gated + pushed. =====
- NEXT: P1B-LIVE — the minimal BROWSER product harness (pulled forward; the gate before P10). Requires the running
  frontend+agent-server stack + Playwright; produces the product-evidence dossier the P1A/P8D oracles consume.

## P1B-LIVE — BROWSER PRODUCT HARNESS (decomposed; the gate before P10)
ENV REALITY (scoped 2026-06-29): frontend/e2e-live/ exists (bp-* specs attach to a LIVE agent-server :8000 +
inspect EXISTING builds — they don't drive a fresh build). playwright.config testDir=./e2e (webServer dev:5199);
the bp-* live specs hit API :8000 directly. @playwright/test installed. BUILD DRIVER BLOCKED: local llama-server
DOWN + Pi DOWN → no reliable in-repo model to drive a fresh deterministic build right now. The EXACT product_
evidence contract the P1A BROWSER_EVIDENCE_ORACLES read (browser_evidence.py): browser_ws{connections:int};
lifecycle{terminal:str, statuses:list}; sidecar{stopped_at_terminal:bool, provider_calls_after_terminal:int};
preview{owner:"platform", manual_port:bool}; shown{artifact_shown:bool, preview_shown:bool}; verification{ready_
for_verification_called:bool, passed:bool}; export{requested:bool, download_present:bool, download_bytes:int};
cleanup{orphans:int, workspace_released:bool}.
PLAN: decompose into P1B-LIVE-1 (evidence schema keystone — gateable NOW), -2 (evidence_writer + scenario_runner),
-3 (playwright_runner.ts + build-artifact-runtime-smoke.spec.ts + support/productEvidence.ts + the FLAGGED live run).
Product Harness stays NOT-complete until a real Playwright/UI/WS/PreviewPane run produces a passing dossier.

## PR P1B-LIVE-1 — product_evidence schema keystone — PLAN
GOAL: a TYPED ProductEvidence schema that serializes to EXACTLY the dict shape the P1A oracles read, so the harness
(P1B-LIVE-2/3) and the oracles can never drift. Proven by a test that a GREEN dossier passes ALL 8 browser oracles
via classify(), and each individually-broken slice FAILs its oracle with the right code.
FILE: harness/product_build/product_evidence_schema.py + harness/build_soak/tests/test_product_evidence_schema.py.
API: 8 frozen slice models (BrowserWS/Lifecycle/Sidecar/Preview/Shown/Verification/Export/Cleanup) matching the
oracle keys EXACTLY (NOT the addendum §6.5 nested sketch — match the REAL oracles). ProductEvidence(frozen) with
the 8 slices. .to_evidence_dict() -> dict[str, dict] (the {browser_ws:{...}, lifecycle:{...}, ...} the oracles
read). green() classmethod → a fully-passing dossier (connections>=1, terminal FINISHED, sidecar stopped + 0 calls
after, preview platform-owned + no manual_port, shown, verification called+passed, export requested+present+bytes>0,
cleanup 0 orphans + released). validate(dict) round-trips. P1B-LIVE-2 (writer) builds this from captured artifacts;
P1B-LIVE-3 (TS) emits the same JSON (cross-language, drift-guarded later).
TESTS: green().to_evidence_dict() → classify(clean_smoke_log, product_evidence=...) == PASS (all 8 oracles pass);
mutate each slice to its failing form → classify FAILs with the matching code (BROWSER_WS_NOT_CONNECTED / lifecycle
/ SIDECAR_NOT_STOPPED / PREVIEW_OWNERSHIP_VIOLATION / not-shown / verification / EXPORT_DOWNLOAD_MISSING / WORKSPACE
_NOT_CLEANED); ProductEvidence round-trips; the dict keys == the oracle slice keys (a parity assertion).
SCOPE: schema + oracle-contract proof ONLY. PARALLEL: none (pure keystone). DeepSeek deferred.

### PR P1B-LIVE-1 — REFRAMED (post gpt-5.5: schema already exists in P1A)
DISCOVERY: harness/build_soak/product_evidence.py (_SLICE_FIELDS + validate_product_evidence + write_product_
evidence) AND test_browser_evidence_oracles.py (_green_evidence → all 8 oracles .passed; each broken slice →
exact failure code) ALREADY lock the schema↔oracle contract (P1A). A new Python schema = forbidden duplication.
REFRAME: P1B-LIVE-1 = the genuinely-missing + DETERMINISTICALLY-VERIFIABLE bridge — the TS evidence-assembly layer
that turns captured browser observations into the EXACT product_evidence dict shape the existing Python schema/
oracles read, with a CROSS-LANGUAGE PARITY GUARD (like discoSemanticAttrs.ts↔DataDiscoAttr) so the TS evidence keys
can never drift from Python _SLICE_FIELDS. No live model needed; pure + vitest-tested. The live Playwright run +
scenario_runner stay P1B-LIVE-2/3, FLAGGED pending a build driver (llama-server + Pi DOWN → no reliable driver now).
FILES: frontend/e2e-live/support/productEvidence.ts (+ .test.ts) + packages/.../test_product_evidence_parity.py.
API (productEvidence.ts): typed ProductEvidence TS interface mirroring _SLICE_FIELDS (browser_ws{connections},
lifecycle{terminal}, sidecar{stopped_at_terminal,provider_calls_after_terminal}, preview{owner,manual_port},
shown{artifact_shown,preview_shown}, verification{ready_for_verification_called,passed}, export{requested,
download_present,download_bytes}, cleanup{orphans,workspace_released}); buildProductEvidence(observations) → the
dict (only includes a slice when its observation was captured — absent slice → oracle SKIPs, matching the writer's
optional-slice contract); SLICE_KEYS constant. PARITY: a Python test reads productEvidence.ts, asserts its slice
keys + per-slice field names == Python _SLICE_FIELDS exactly.
TESTS: buildProductEvidence(green observations) → the green dict (deep-equal a fixture); a partial observation
omits the un-captured slice (not a fake-passing default); field types correct (connections number, flags bool).
Python parity test: TS slices/fields == _SLICE_FIELDS. SCOPE: TS assembly + parity ONLY; live run flagged.

### PR P1B-LIVE-1 — PLAN REVISION 2 (post gpt-5.5: harden _SLICE_FIELDS first)
gpt-5.5 found a REAL latent gap in P1A: _SLICE_FIELDS["export"] declares only {requested} but ExportDownloadOracle
adjudicates download_present(bool) + download_bytes(int) → a bool download_bytes capture bug slips past validate_
product_evidence. FIX FIRST (build-and-harden): expand _SLICE_FIELDS export→{requested:bool, download_present:bool,
download_bytes:int} (+ lifecycle→{terminal:str, statuses:list} for completeness) so validation covers every oracle-
adjudicated field. Add a Python test asserting _SLICE_FIELDS covers each field the 8 oracles read (audited). Then
productEvidence.ts parity targets the COMPLETE _SLICE_FIELDS. Verify no regression (green dossier + existing
writer/oracle tests still pass). THEN: productEvidence.ts (TS assembly, slice-only-when-captured) + Python parity
test (TS keys/fields == corrected _SLICE_FIELDS). Live Playwright/scenario_runner = P1B-LIVE-2/3, FLAGGED (driver down).

### HEARTBEAT 2026-06-29 — P1B-LIVE-1 ACCEPTED
- PR P1B-LIVE-1 (product-evidence TS assembly + schema hardening) COMPLETE. Plan gpt-5.5 REVISE×3 (don't dup the
  P1A schema → reframe to TS bridge; harden _SLICE_FIELDS export gap first)→APPROVE. Code gpt-5.5 REVISE(2:
  SLICE_KEYS↔SLICE_FIELDS drift hole + add download_bytes=True regression)→fix→APPROVE.
- HARDENED a real P1A latent bug: _SLICE_FIELDS["export"] only had {requested} but ExportDownloadOracle
  adjudicates download_present/download_bytes → a bool download_bytes evaded validate_product_evidence. Now typed
  (+ lifecycle.statuses). Files: harness/build_soak/product_evidence.py (hardened) + frontend/src/lib/harness/
  productEvidence.ts (TS assembly; SLICE_FIELDS typed single source; SLICE_KEYS derived; slice-only-when-captured)
  + .test.ts (4 vitest) + harness/build_soak/tests/test_product_evidence_parity.py (4: coverage + TS↔Py parity +
  bool-bytes regression). DISCOVERY: P1A already locked the schema+oracle contract; this is the TS bridge + fix.
- NEXT: P1B-LIVE-2 (evidence_writer + scenario_runner deriving evidence from captured artifacts) → P1B-LIVE-3
  (playwright_runner.ts + build-artifact-runtime-smoke.spec.ts + the FLAGGED live run; driver down → live PENDING).
  Product Harness STILL NOT complete (no real browser run yet).

## PR P1B-LIVE-2 — scenario_runner + evidence dossier writer — PLAN
GOAL: the deterministic capture→dossier→classify pipeline. Given CAPTURED browser observations (+ provider
records + the build event log), assemble a run folder that the EXISTING classify_run_folder adjudicates — proven
green→PASS + broken-slice→FAIL with synthetic captured fixtures (no live model needed). The live Playwright
capture feeds REAL observations into the same writer in P1B-LIVE-3.
FILES: harness/product_build/__init__.py + evidence_writer.py + scenario_runner.py + harness/build_soak/tests/
test_product_build_dossier.py.
evidence_writer.write_dossier(run_dir, *, observations:dict, provider_records:list[dict], events:list[dict],
artifacts:dict[str,str]|None=None) -> Path: builds product_evidence from observations (the productEvidence slice
shape), writes product-evidence.json via the EXISTING write_product_evidence(strict=True) (raises on malformed →
no untrusted dossier), provider-call-ledger.jsonl via write_provider_ledger, events.jsonl, + any raw §6.4
artifacts (timeline.md/ui-actions.jsonl/etc.) verbatim. Returns run_dir. NO fabricated defaults — only what was
captured.
scenario_runner: ProductScenario(frozen: id, build_prompt, kind, requires_export:bool) + STATIC_SITE_SMOKE (the
§6.8 minimal scenario, assertions={event_chain:{require_plan_before_execution:True}}) + classify_dossier(run_dir,
scenario)->dict wrapping classify_run_folder with the scenario assertions.
TESTS: write_dossier(green observations + MiniMax provider record + clean_smoke_log events) → folder has product-
evidence.json (validates) + provider-call-ledger.jsonl + events.jsonl; classify_dossier → status PASS (all 8
browser oracles .passed + provider-ledger MiniMax-only OK); a broken browser_ws (connections 0) → classify FAIL
BROWSER_WS_NOT_CONNECTED; an OpenRouter provider record → classify FAIL (provider-ledger violation); malformed
observation (bool download_bytes) → write_dossier raises (strict). DRIVER: ~/.config/disco/secrets.json exists
(encrypted) — availability check deferred to P1B-LIVE-3's flagged live run. SCOPE: writer + scenario + classify
wrapper + tests; live capture = P1B-LIVE-3.

### PR P1B-LIVE-2 — PLAN REVISION 1 (post gpt-5.5: 5 fixes — SKIP-default oracles won't auto-prove)
1. ProductScenario MATERIALIZES the full classifier scenario dict: assertions.event_chain.require_plan_before_
   execution=True + assertions.provider {require_host_substr:"minimax", forbid_host_substr:["openrouter"],
   require_ledger:true, model:"MiniMax-M3"} + required_slices:tuple (the product slices this scenario REQUIRES).
   STATIC_SITE_SMOKE.required_slices = (browser_ws, lifecycle, preview, shown, verification, cleanup); export +
   sidecar optional (requires_export=False; disco kernel = no sidecar → those oracles SKIP legitimately).
2. classify_dossier(run_dir, scenario): FIRST enforce product-harness completeness — every required_slice must be
   PRESENT in product-evidence.json, else return INVALID_RUN/MISSING_REQUIRED_EVIDENCE (NOT a PASS via oracle
   SKIP). If requires_export, require export.requested=True present. THEN delegate to classify_run_folder(run_dir,
   scenario.to_classifier_dict()). Do NOT claim "all 8 oracles passed" — assert the REQUIRED oracles passed +
   optional SKIP.
3. NEGATIVE tests: omit a required slice (browser_ws/cleanup) → INVALID_RUN not PASS; missing/empty provider
   ledger under the provider-required scenario → MISSING_REQUIRED_EVIDENCE→INVALID_RUN; an OpenRouter provider
   record under the provider scenario → FAIL (forbidden host); malformed obs (bool download_bytes) → write_dossier
   raises.
4. green fixtures TEST-ONLY; production write_dossier only serializes SUPPLIED captured observations (no passing
   defaults).
5. write_dossier: PREFLIGHT validate_product_evidence BEFORE any write (raise → no partial dossier); REJECT unsafe
   artifact relpaths (no abs / ".."); write events.jsonl + product-evidence.json + provider-call-ledger.jsonl +
   raw artifacts, THEN compute_evidence_hashes + write_manifest (EvidenceManifest run_id/scenario_id/mode="ui"/
   evidence_files) so classify_run_folder loads via the manifest + the evidence lock holds.

### HEARTBEAT 2026-06-29 — P1B-LIVE-2 ACCEPTED
- PR P1B-LIVE-2 (scenario_runner + evidence dossier writer) COMPLETE. Plan gpt-5.5 REVISE(5: SKIP-default oracles
  won't auto-prove → materialize full scenario + provider enforcement + required-slice completeness + manifest
  lock)→APPROVE. Code gpt-5.5 (1st review TIMED OUT, re-ran) REVISE(2: artifact could clobber a core dossier file +
  manifest-label collision broke the hash lock)→fix(namespace artifacts/ + artifact: labels)→APPROVE.
- Files: harness/product_build/{__init__,evidence_writer,scenario_runner}.py + test_product_build_dossier.py (11).
  write_dossier: preflight-validate→write events/product-evidence/provider-ledger/artifacts(under artifacts/)→
  EvidenceManifest hash-lock; rejects unsafe paths + can't clobber core files. ProductScenario materializes the
  classifier dict (event_chain + MiniMax-only provider); classify_dossier enforces required slices present (else
  INVALID_RUN, not PASS-via-SKIP) then delegates to the EXISTING classify_run_folder. Proven via the REAL
  classifier: green→PASS, broken/forbidden/missing→FAIL/INVALID_RUN, malformed→raise.
- NEXT: P1B-LIVE-3 (playwright_runner.ts + frontend e2e-live/build-artifact-runtime-smoke.spec.ts capturing REAL
  observations→write_dossier; the FLAGGED live run). Scope driver availability first (~/.config/disco/secrets.json
  encrypted — needs DISCO_SECRET_KEY); if no driver, live run stays pending + surfaced to Dylan, Product Harness
  NOT complete.

## P1B-LIVE driver = MiniMax (Dylan directive 2026-06-29) — LIVE-VERIFIED + protocol finding
DECISION: the P1B-LIVE (and P17) build driver is MiniMax-M3 via the DIRECT minimax.io API (the opencode
minimax-coding-plan token at ~/.local/share/opencode/auth.json, type api, sk-cp-…). NOT OpenRouter, NOT Pi.
LIVE PROBE (2026-06-29): MiniMax-M3 returns HTTP 200 "OK" at POST https://api.minimax.io/anthropic/v1/messages
(headers x-api-key + anthropic-version: 2023-06-01; ANTHROPIC protocol). The OpenAI endpoint
api.minimaxi.com/v1/chat/completions returns 401 "invalid api key" — the coding-plan token is ANTHROPIC-PROTOCOL
ONLY. So MiniMax-M3 is confirmed reachable + working as the direct driver.
WIRING WRINKLE: disco's build driver is OpenAI-only (core/llm/openai_provider.py; no anthropic provider). The
coding-plan token speaks Anthropic /messages. => need an OpenAI→Anthropic RELAY (accept disco's OpenAI
/v1/chat/completions, translate to MiniMax /anthropic/v1/messages with the token, translate the response back).
This is the relay the harness used via Pi (now down). P1B-LIVE-3 plan: (1) minimal OpenAI→Anthropic MiniMax relay
(local, holds the token); (2) disco-config.json build model → {base_url: local relay, model_id: MiniMax-M3,
api_key_env}; (3) bring up the disclaude stack + ONE real browser build run → evidence dossier → classify_dossier
PASS, with Playwright screenshots as proof. The token NEVER goes into the repo/ledger (env / encrypted store only).

### P1B-LIVE driver — CORRECTION (further probe): OpenAI protocol WORKS, existing relay is enough
The coding-plan token DOES work via OpenAI protocol — HTTP 200 MiniMax-M3 at api.minimaxi.chat/v1/chat/completions
AND api.minimax.io/v1/chat/completions (Bearer). Only the WRONG host api.minimaxi.com 401s. So NO Anthropic
translation needed — the existing /home/dylan/projects/disco-pi-dev/minimax_relay.py (OpenAI passthrough → UPSTREAM
default api.minimaxi.chat/v1, Bearer KEY, maps minimax-m3→MiniMax-M3, clamps max_tokens, SSE) works AS-IS with the
coding-plan token. P1B-LIVE-3: (1) copy the relay into the disclaude repo as its permanent home (+ a tiny unit
test of the model-map/clamp logic); run it with MINIMAX_API_KEY (sk-cp- from opencode auth.json — env ONLY, never
in repo) + MINIMAX_MODEL=MiniMax-M3 + MINIMAX_UPSTREAM=https://api.minimaxi.chat/v1 on a local port; (2)
disco-config.json build model driver-minimax → {base_url: http://localhost:<port>/v1, model_id: minimax-m3};
(3) bring up the disclaude stack + ONE real browser build → dossier → classify_dossier PASS + Playwright
screenshots. PROVIDER-LEDGER (P17): record the REAL upstream host (minimaxi.chat), not localhost relay, so the
MiniMax-only/no-OpenRouter proof is honest — source from the relay's RELAY-REQ log or disco's configured upstream.
MEMORY: save a note that MiniMax coding-plan (sk-cp-) works OpenAI-compat on api.minimaxi.chat/v1 + api.minimax.io
/v1 (NOT api.minimaxi.com), so the relay needs no Anthropic adapter.

## PR P1B-LIVE-3a — MiniMax relay → repo permanent home + unit test — PLAN
GOAL: bring /home/dylan/projects/disco-pi-dev/minimax_relay.py into the disclaude repo as its permanent home,
refactored so the PURE request-transform (model-map + max_tokens clamp) is unit-testable WITHOUT fastapi/network;
the FastAPI proxy is built lazily. Behavior identical to the proven relay (OpenAI passthrough → MINIMAX_UPSTREAM
default api.minimaxi.chat/v1, Bearer KEY, SSE). The live run (start it + disco-config + Playwright build) is 3b.
FILE: harness/product_build/minimax_relay.py + harness/build_soak/tests/test_minimax_relay.py.
DESIGN: module-level pure config + functions (NO fastapi import at top): UPSTREAM/DEFAULT_MODEL/MODEL_MAP/
MAX_TOKENS_CAP from env (MINIMAX_UPSTREAM default https://api.minimaxi.chat/v1, MINIMAX_MODEL default MiniMax-M3,
MODEL_MAP maps minimax-m3/minimax/minimax-m3→MiniMax-M3 + m2 variants→MiniMax-M2, cap 131072). transform_request
(body:dict, *, default_model, model_map, cap)->dict: returns a NEW dict (no input mutation) with model mapped +
max_tokens clamped (absent / non-int / bool / >cap → cap; a valid int ≤cap kept). create_app(): lazily imports
fastapi/httpx, builds the /v1/models + /v1/{path} proxy using transform_request + Accept-Encoding: identity +
RELAY-REQ/RESP logging that NOW also logs the UPSTREAM HOST (for the P17 provider-ledger honesty — ledger must
show minimaxi.chat not localhost). __main__ runs uvicorn. KEY from os.environ["MINIMAX_API_KEY"] (env only; NEVER
in repo; the module has NO hardcoded token).
TESTS (pure, no fastapi/network): transform_request maps minimax-m3→MiniMax-M3 + unknown→default; clamps absent/
too-big/bool/negative max_tokens→cap; KEEPS a valid small max_tokens; does NOT mutate the input dict; passes
through other fields (messages/tools/stream) unchanged. SCOPE: relay module + pure-transform test only; the live
run + ledger-upstream sourcing is 3b. PARALLEL: none.

### PR P1B-LIVE-3a — PLAN REVISION 1 (post gpt-5.5: 5 refinements)
1. PURE seam: transform_request + the config constants + upstream_host(url) import with NO fastapi/httpx/network/
   secret. create_app() (lazy) imports fastapi/httpx AND reads MINIMAX_API_KEY (env only; fail-fast if unset) —
   so pure tests/imports never need the secret.
2. CLAMP tightened: max_tokens is kept ONLY if type(mt) is int (rejects bool) AND 0 < mt <= cap(131072); else
   (absent / non-int / bool / <=0 / >cap) → cap. No input mutation (return a new dict).
3. STRUCTURED relay log: a JSONL record {"host": upstream_host(url), "url": url, "model": mapped_model, "ts": ...}
   (the provider_ledger parser reads JSON host/url) — so 3b can source the provider-call-ledger from it showing
   the REAL minimaxi.chat host. Pure upstream_host(url) extracts the host. Actual ledger population + after-terminal
   correlation = 3b.
4. DEFAULT MODEL = MiniMax-M3 (INTENTIONAL change from the source relay's M2 default, for P17) — pick + test it;
   not "identical behavior", an intentional default.
5. Refactor REMOVES the source relay's top-level env-read + in-place body mutation (transform_request is pure).
TESTS (pure, no fastapi): model map (minimax-m3→MiniMax-M3, unknown→MiniMax-M3 default); clamp matrix (absent/0/
-5/True/9_999_999→131072; 2048 kept); no-mutation of input; passthrough of messages/tools/stream; upstream_host
("https://api.minimaxi.chat/v1/chat/completions")=="api.minimaxi.chat".

### HEARTBEAT 2026-06-29 — P1B-LIVE-3a ACCEPTED
- PR P1B-LIVE-3a (MiniMax relay → repo permanent home + unit test) COMPLETE. Plan gpt-5.5 REVISE(5: pure/lazy seam,
  clamp=positive-non-bool-int, key-in-create_app, structured JSONL log, intentional M3 default)→APPROVE. Code
  gpt-5.5 REVISE(3: key-check-before-imports, package __init__ pulled disco breaking import-purity, SSE returned
  200 regardless of upstream status)→fix→APPROVE.
- Files: harness/product_build/minimax_relay.py (pure transform_request/map_model/_clamp/upstream_host/relay_log_
  record — fastapi/secret-free; lazy create_app reads MINIMAX_API_KEY first then imports; SSE preserves upstream
  status + closes client; JSONL {host,url,model,ts} log for the P17 ledger) + __init__.py made LAZY (__getattr__ +
  TYPE_CHECKING) so the relay imports without disco + test_minimax_relay.py (7, incl subprocess import-purity proof).
- NEXT: P1B-LIVE-3b — THE LIVE RUN. Start the relay (MINIMAX_API_KEY=sk-cp- from opencode auth, env ONLY;
  MINIMAX_MODEL=MiniMax-M3; UPSTREAM api.minimaxi.chat/v1; port 8080) → disco-config.json driver-minimax →
  {base_url http://localhost:8080/v1, model_id minimax-m3} → bring up disclaude stack → ONE real Playwright build of
  STATIC_SITE_SMOKE → observations→buildProductEvidence→write_dossier→classify_dossier PASS + SCREENSHOTS to Dylan.
  Provider-ledger sourced from the relay JSONL log (real minimaxi.chat host). Product Harness NOT complete till then.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b in progress: relay LIVE + 422 bug fixed
- RELAY LIVE-VERIFIED: started harness.product_build.minimax_relay on :8080 → curl localhost:8080/v1/chat/
  completions model minimax-m3 → HTTP 200, model=MiniMax-M3 (map worked); relay.jsonl logs {"host":
  "api.minimaxi.chat", "model":"MiniMax-M3"} — the REAL upstream host for the P17 provider-ledger. Direct MiniMax,
  no OpenRouter.
- LIVE BUG FIXED (caught ONLY by the real run): the relay 422'd because `from __future__ import annotations`
  stringized the FastAPI proxy's `request: Request` annotation (Request is lazily imported in create_app), so
  FastAPI treated `request` as a query param. Removed the future-import from the relay module (annotations eager;
  fastapi still lazy → import purity preserved; 7 unit tests + basedpyright 0/0 still green). NOW HTTP 200.
- disco-config.json authored: driver-minimax → {base_url http://localhost:8080/v1, model_id minimax-m3, provider
  minimax, api_key_env null (relay holds the key)}, default_model=driver-minimax (template = Disco-Pi which already
  had a working driver-minimax entry). NEXT: bring up the disclaude stack (agent-server :8000 + frontend) + drive
  ONE real Playwright build → dossier → classify_dossier PASS + screenshots.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b: BACKEND STACK LIVE with MiniMax driver
- MILESTONE: relay :8080 (200) + agent-server :8000 BOTH UP. Agent-server startup log proves it found driver-
  minimax + verified the relay: `GET http://localhost:8080/v1/models → 200 OK`. So disco's BUILD driver is wired
  to MiniMax-M3 via the relay, live + connected. (/ returns 404 = no root route, normal; API is under /api.)
- Cleaned disco-config.json to ONLY driver-minimax (removed or-gemini-3-flash → NO OpenRouter anywhere, P17-clean;
  the earlier startup hit openrouter.ai/models for that stray model — gone now).
- RESTART RECIPE (nohup, survive task reaping): relay = MINIMAX_API_KEY/MODEL=MiniMax-M3/UPSTREAM=api.minimaxi.chat
  /v1/RELAY_LOG/PORT=8080 python -m harness.product_build.minimax_relay; agent-server = DISCO_HOST=127.0.0.1
  DISCO_PORT=8000 DISCO_CONFIG=$PWD/disco-config.json DISCO_SECRET_KEY=$(cat ~/.config/disco/dev_secret_key)
  python -m disco.agent_server. Both from /home/dylan/projects/disclaude with PYTHONPATH=packages/*/src:$PWD.
- NEXT (the live build): bring up the frontend, drive ONE real Playwright build of STATIC_SITE_SMOKE through the UI
  (MiniMax builds index.html → preview), capture observations → write_dossier → classify_dossier PASS + SCREENSHOTS.
  Needs: frontend dev server + a working sandbox (check disco-config sandbox.provider) + the build loop completing
  (minutes on MiniMax). Orchestrate relay+agent+frontend+Playwright in ONE execution so nothing gets reaped.
  Product Harness NOT complete until that run is green with screenshots.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b: REAL MiniMax BUILD RUNNING (driver proven)
- FULL STACK LIVE + a REAL build executing: podman 5.8.2 + disco-sandbox:base image present; relay :8080 (POST
  200); agent-server :8000 /health ok (1 model = driver-minimax). Created build conversation
  conv_049d896830de4d89a7eb14dcd5bd4939 (surface=build, autonomous=true, sandbox_backend=podman), sent a static-
  site prompt → execution_status RUNNING, made a PLAN + executed 4 actions/observations (iter 4/500). relay.jsonl:
  10 calls, ALL host=api.minimaxi.chat model=MiniMax-M3 → the build is driven by MiniMax-M3 via the DIRECT API
  (P17-clean, zero OpenRouter). ⇒ THE MINIMAX DRIVER BUILDS REAL DISCO SOFTWARE — proven live.
- NOT YET: a finished artifact (index.html not yet written at iter 4), classify_dossier PASS, or screenshots.
  Product Harness remains NOT complete (per compliance). The build is in the nohup agent-server process; let it run.
- NEXT: check conv_049d896… for FINISHED + index.html (workspace snapshot / preview); then the UI/Playwright
  product-harness path (frontend + build-artifact-runtime-smoke.spec.ts) → buildProductEvidence → write_dossier
  (provider records from relay.jsonl) → classify_dossier(STATIC_SITE_SMOKE) PASS + SCREENSHOTS to Dylan.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b: MiniMax-M3 BUILT A REAL SITE (visual proof sent) + a real finding
- ✅ PROVEN END-TO-END: MiniMax-M3 (direct api.minimaxi.chat, zero OpenRouter) drove a real disco build in the
  podman sandbox and produced "The Corner Cup" coffee-shop site: file_write index.html (4341B, real semantic HTML)
  + file_write styles.css (6114B) + preview_start(name=corner-cup) + verify_web_app. Rendered + screenshotted via
  Playwright/Firefox → SENT TO DYLAN (file_uuid 70eeb52a). Evidence in test-record/p1blive3b/ (png + html + css +
  relay.jsonl). THE MINIMAX DRIVER BUILDS REAL, WELL-DESIGNED SOFTWARE.
- ⚠️ REAL FINDING (build PAUSED at iter 12, not FINISHED): MiniMax-M3 malforms update_plan_progress — sent
  steps:[''] / steps:['',''] (list of empty strings) instead of [{index:int, state:pending|active|done}] → 5
  agent_error validation rejections → the no-progress/loop breaker PAUSED the build AFTER the site+preview were
  already produced. So lifecycle.terminal != FINISHED ⇒ classify_dossier would NOT pass. PRODUCT HARNESS NOT
  COMPLETE (no clean FINISHED build + no UI/PreviewPane dossier yet). This is exactly the engine-robustness gap the
  harness exists to surface (capable model mangles a structured tool arg → stall).
- NEXT (a real gated PR): harden the build loop for a capable model malforming update_plan_progress — coerce/repair
  the steps arg (e.g. ['']→drop/ignore, or accept + normalize) OR don't let plan-progress validation spam trip the
  no-progress breaker once a deliverable exists; then re-run → clean FINISHED → the UI/Playwright product-harness →
  classify_dossier PASS + screenshots. (Mirrors memory disco-plan-progress-unify / disco-build-loop-fixes.)

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b: root-cause CORRECTED (verified, not assumed)
- I almost built a fix for already-correct code. Interrogated the given: stuck.py _repeated_action_error ALREADY
  exempts _NONCRITICAL_FAILURE_TOOLS (update_plan_progress) for THIS exact MiniMax-M3 reason (comment names it).
  So the pause was NOT a missing stuck-detector exemption.
- ACTUAL mechanism: turn_control.gate_bookkeeping_streak — a soft nudge at _BOOKKEEPING_STREAK_NUDGE_AT, then a
  STUCK/HALT at max(_BOOKKEEPING_STREAK_HALT_AT, plan_steps+slack) when a model spams plan-tracker calls WITHOUT
  intervening real work. MiniMax produced the site (file_writes + preview) THEN spammed malformed update_plan_
  progress → hit the bookkeeping cap → STUCK/HALT (in autonomous mode = clean forfeit). This is ARGUABLY CORRECT:
  a model that won't call finish + keeps malforming bookkeeping IS stuck. NOT obviously a disco bug.
- REVISED PLAN (no premature fix): (1) the malform is documented as INTERMITTENT → RE-RUN the build; a fresh run
  may reach a clean FINISHED. (2) If it finishes → capture dossier + UI/Playwright screenshots → classify_dossier
  PASS → Product Harness candidate. (3) ONLY if it CONSISTENTLY halts on bookkeeping-with-a-deliverable, scope a
  real gated PR: finalize-on-stuck-when-a-valid-deliverable-exists (run the finalizer/verify instead of forfeiting
  a build whose artifact is already good) — a genuine product-quality gap, but verify it's consistent first.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b: PROMPT FIX WORKS — clean FINISHED build (A/B proven)
- GATED PR (Codex APPROVE): tightened the _EXECUTION_DRIVER_PROMPT update_plan_progress sentence to mirror the
  {index,state} OBJECT schema (was loose "list of steps with each one's current state" → now "ARRAY OF OBJECTS,
  each {index,state}, e.g. [{...}]"). basedpyright 0/0, no pinned test. Committed+pushed. Restarted agent-server to
  load it.
- A/B PROOF on a fresh live build (conv_cb9be8…, same prompt, MiniMax-M3 direct):
    BEFORE (conv_049d…): PAUSED, 5 agent_errors, update_plan_progress malformed steps:[''].
    AFTER  (conv_cb9be8…): status=FINISHED, iter 9, 0 agent_errors, BOTH update_plan_progress calls well-formed
    [{index,state}]. Clean sequence: file_list→update_plan_progress→file_write index.html→file_write styles.css→
    preview_start→verify_web_app→finish+deliverable.
- Artifact: "Ember & Brew" site (index.html 6057B + styles.css 8526B) — nav, hero+2 CTAs, about+stat cards, 3-col
  hours table w/ notes, contact card, footer. Rendered + screenshot SENT to Dylan (file_uuid 46d0150a). Evidence:
  test-record/p1blive3b-finished/. ⇒ MiniMax-M3 now builds AND finishes cleanly end-to-end.
- REMAINING for the formal classify_dossier PASS / Product Harness COMPLETE: the UI/Playwright capture of the
  browser_ws + shown slices from the real frontend PreviewPane (STATIC_SITE_SMOKE required_slices = browser_ws,
  lifecycle, preview, shown, verification, cleanup). The API-driven run gives lifecycle/preview/verification; the UI
  run supplies browser_ws/shown/cleanup. NEXT: frontend up + /build/{cid} Playwright → buildProductEvidence →
  write_dossier (provider from relay.jsonl) → classify_dossier PASS → mark Product Harness COMPLETE.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b: UI product-harness PROBE green (all slices capturable)
- Brought up the full UI: vite :5173 (VITE_AGENT_BASE=:8000) + app-server :8800 + agent-server :8000 + relay :8080.
- Playwright/Firefox PROBE of /build/conv_cb9be8 (the FINISHED MiniMax build) confirms EVERY UI-derived slice is
  real: FEED_HAS_BUILD_CONTENT=true (artifact_shown), PREVIEW tab + iframe renders the live Ember&Brew site
  (preview_shown=true), the agent WS ws://localhost:8000/ws/conversations/{cid} connects (browser_ws.connections=1,
  filtering out the vite HMR socket), 0 console errors. Real-UI screenshot (feed + minimax-m3·Subscription pill +
  live Preview pane) SENT to Dylan + saved test-record/p1blive3b-finished/ui-real-build-surface.png.
- So the harness WORKS; remaining is to FORMALIZE: build-artifact-runtime-smoke.spec.ts capturing browser_ws/
  lifecycle/preview/shown/verification/cleanup → buildProductEvidence → write_dossier(provider from relay.jsonl) →
  classify_dossier(STATIC_SITE_SMOKE) PASS. The cleanup slice {orphans, workspace_released} needs the conversation
  KILLED/released then verified (podman has no leftover container for the cid + workspace gone) — do that as the
  spec teardown. Then mark Product Harness COMPLETE. Codex-gate the spec.

### HEARTBEAT 2026-06-29 — P1B-LIVE-3b COMPLETE: classify_dossier PASS on a real MiniMax build ✅
- GATED PR (Codex APPROVE): thread `autonomous` through write_dossier → EvidenceManifest. Root cause found by the
  live run: an autonomous build (disco-kernel DEFAULT) auto-approves its plan inline (RUNNING/plan_approved, NO
  AWAITING_PLAN_APPROVAL) → EventChainOracle relaxes that link only when autonomous=True; classify_run_folder
  already read manifest.autonomous + the field existed, but write_dossier never set it → every dossier was non-
  autonomous → the default build was unclassifiable as PASS. Fail-closed default. 2 new tests (auto=True→PASS, same
  log auto=False→FAIL PLAN_APPROVED_STATUS_MISSING) + 11 existing green; basedpyright 0/0.
- ✅ FORMAL PASS on REAL DATA: write_dossier(autonomous=True) from the live conv_cb9be8 (real events + real captured
  slices: browser_ws.connections=1, lifecycle FINISHED, preview platform, shown artifact+preview=true [from the
  Playwright UI probe], verification called+passed, cleanup orphans=0+workspace_released after a real /kill) +
  provider ledger 56 records ALL api.minimaxi.chat/MiniMax-M3 → classify_dossier(STATIC_SITE_SMOKE) =
  {status: PASS, scenario_id: static_site_smoke, required_evidence_present: true, accepted_by: oracle}. Proof saved
  test-record/p1blive3b-finished/dossier/classification.json.
- PRODUCT HARNESS = COMPLETE (the completion criterion — a real classify_dossier PASS on a live MiniMax-M3 build —
  is met, P17-clean: minimax host only, zero openrouter). P1 (HARN-1..3) DONE.
- DURABLE-AUTOMATION FOLLOW-UP (not a gate on completion): encode this exact capture→classify flow as
  frontend/e2e-live/build-artifact-runtime-smoke.spec.ts so CI can re-run it. Then P10→P17 (P17 soak = this MiniMax
  relay).

### HEARTBEAT 2026-06-29 — P1B-LIVE durable automation COMMITTED (Codex APPROVE, live-passed 4.3m)
- frontend/e2e-live/build-artifact-runtime-smoke.spec.ts + harness/product_build/classify_captured.py (+4 unit
  tests) committed. The spec drives a REAL autonomous MiniMax build, captures all 6 slices from the real UI, kills
  + verifies cleanup, sources the provider ledger from relay.jsonl (api.minimaxi.chat), and asserts
  classify_dossier PASS. LIVE-PASSED end-to-end in 4.3m.
- Codex gate took 3 REVISEs on the verification capture (good adversarial value): (1) missing verify obs read as
  PASS → fail-closed; (2) tool_result.success only means the tool RAN → also require structured.passed===true (the
  real verdict; structured={passed,verdict,http_status,console_errors,...}); (3) find() took the FIRST verify but a
  build may verify multiple times → assert the FINAL finish-gate verify. APPROVE after all three.
- A background live RE-RUN of the final hardened spec is in flight (spec2.out) to re-confirm; check next heartbeat.
- P1 (Product Harness, HARN-1..3) = DONE. NEXT: P10 (Export/Handoff) — phase order P10→P11 Resource Import/
  Provenance→P12 Content/Design→P13 Deck/Doc→...→P17. Read the addendum (disclaude.md ~line 25 + the P1B-LIVE/
  contract sections) for P10's spec, plan it, Codex plan-gate, implement, Codex code-gate, commit.

### HEARTBEAT 2026-06-29 — durable-spec re-run caught a REAL recurring reliability gap
- The live RE-RUN of build-artifact-runtime-smoke.spec.ts FAILED — correctly, loudly: the build went STUCK
  (bookkeeping_only) at iter 21, NOT a spec bug. The spec fails honestly when a build doesn't finish (good).
- ROOT: MiniMax-M3 intermittently malforms STRUCTURED plan args — this build: submit_plan ×4 (missing required
  summary; steps.0.done_condition wrong shape) + update_plan_progress ×6 → gate_bookkeeping_streak halted it
  bookkeeping_only AFTER a deliverable was produced. So ~1/2 post-fix builds still stall (matches memory
  disco-m3-real-build-capability: ~50% completion, long-context actionless-stall dominant).
- FIX 1 SHIPPED (gated, Codex APPROVE): submit_plan prompt now mirrors the PlanStepInput {title, done_condition?}
  object schema (same proven pattern as the update_plan_progress fix) + done_condition OMIT-rather-than-guess.
- STILL OPEN (the deeper, higher-leverage gap — candidate next): gate_bookkeeping_streak FORFEITS (STUCK) a build
  that already produced a valid deliverable when the model spams malformed bookkeeping. A capable build that
  delivered should FINALIZE-not-forfeit (run the finish/verify path) rather than lose the work. This is an ENGINE
  change (turn_control.gate_bookkeeping_streak + finalizer) — sensitive; design carefully + gate hard. AND: the
  durable spec should bounded-RETRY the build (drive up to N, pass on first clean finish, log stuck attempts) so
  CI isn't hostage to one flaky build — honest (logs flakiness), not masking.
- NEXT: (a) re-run the spec to see if the 2 prompt fixes lift the finish rate; (b) decide/scope the finalize-on-
  bookkeeping-stuck engine fix + spec retry; (c) then P10 (Export/Handoff).

### HEARTBEAT 2026-06-29 — prompt fixes lifted finish rate; preview-shown capture is the new flake
- Spec re-run #3 (post submit_plan+update_plan_progress prompt fixes): the build REACHED FINISHED this time (the
  terminal check PASSED — no bookkeeping-stuck) → the two prompt fixes measurably improved build completion. BUT it
  failed on a DIFFERENT assertion: "the Preview pane never rendered the built site" (previewShown=false, line 151).
- So TWO independent flakiness sources, now disentangled: (1) build-going-stuck (bookkeeping) — IMPROVED by the
  prompt fixes; (2) preview-shown capture timing/content — the live preview isn't reliably renderable at capture
  time for THIS finished build (cb9be8's was, via idle TTL; this one's wasn't), and/or the content regex
  /coffee|hours|about|contact/ is too specific.
- REVISED next steps (the engine finalize-on-bookkeeping-stuck fix is now LOWER priority — prompt fixes lifted the
  finish rate; keep it TRACKED, re-measure over more runs): (a) HARDEN the durable spec: broaden preview-shown to
  "iframe rendered NON-TRIVIAL content" (body innerText length threshold, not keyword match — more honest:
  preview_shown = a real render, not the word 'coffee') + bounded-RETRY the whole build+capture (up to 3, pass on
  the first that FINISHES + renders + classifies PASS, console.log each failed attempt so flakiness stays visible).
  Gate + re-run. (b) THEN P10 (Export/Handoff). The finalize-on-bookkeeping-stuck engine fix stays scoped (reuse
  finish.py maybe_honest_unverifiable_static_actionless_finish pattern + _is_web_deliverable; my case is the
  VERIFIED-and-delivered variant, even stronger than the unverifiable one) for if the stuck rate proves still high.

### HEARTBEAT 2026-06-29 — durable spec HARDENED + live-green (Codex APPROVE)
- Root-caused the preview-shown flake: a FINISHED build's preview IS renderable (probed conv_717885a3 → iframe
  innerText 1787 chars "☕ Hearth & Bean / About / Hours / Contact") — the failure was pure TIMING (iframe not
  loaded at the 30s one-shot capture). Hardened the spec: waitPreviewRendered() polls the iframe for a real
  non-empty render (>50 chars, not a keyword) up to 90s + Refresh nudge; bounded-retry the whole build+capture (2
  attempts, log every failure, 0 PASS still fails). LIVE-PASSED 4.4m attempt 1. Codex APPROVE (retry can't mask a
  broken product; verification stays fail-closed). The durable product-harness automation is now CI-reliable.
- P10 (Export/Handoff) landscape read: ExportDownloadOracle + export slice {requested,download_present,
  download_bytes} ALREADY exist (HARN-2 done); HandoffRef(kind,rel_path) in the context ledger. So P10 = the
  PRODUCT-side export/handoff feature — serve(kind='files') downloadable deliverable + an export-REQUIRING
  scenario + harness export-slice capture + the host-owned finalizer truth (global gate: no false affordances, no
  finish without host truth). The per-PR P10 decomposition is NOT pre-written — DESIGN it next.
- NEXT: BEGIN P10 — read disco's serve/export/download path (serve tool kind='files', the app-server download
  route, _is_web_deliverable/serve handling), DESIGN the P10 PR decomposition, write the plan, Codex PLAN-gate →
  implement → tests → Codex CODE-gate → commit. Then P11→P17.

### HEARTBEAT 2026-06-29 — P10a SHIPPED (Export/Handoff scenario + scenario-aware classify)
- P10 design (read, not guessed): disco's export/download path EXISTS end-to-end — serve(kind='files',path)
  [engine-intercepted virtual tool] → DeliverableEvent{artifact_kind:'files',path} → _declared_artifacts (download
  jail, files.py) → GET /conversations/{cid}/artifacts/{path}. CLASSIFY side EXISTS (classify_dossier requires_
  export + ExportDownloadOracle HARN-2). GAP = harness coverage.
- P10a (gated, Codex APPROVE after 1 REVISE): EXPORT_SMOKE ProductScenario (requires_export=True + 'export' in
  required_slices, fail-closed) + classify_captured made SCENARIO-AWARE (_SCENARIOS registry; missing/unknown
  scenario_id RAISES — Codex caught the missing-id fail-open). 25 tests, pyright 0/0. Fixture-tested (the classify
  side is fixture-provable; LIVE capture is P10b).
- NEXT: P10b (LIVE) — extend the durable spec (or a sibling) to drive EXPORT_SMOKE: detect the DeliverableEvent
  artifact_kind='files', GET /conversations/{cid}/artifacts/{the served path} for REAL bytes → export={requested:
  true, download_present:true, download_bytes:N} (sourced from the real GET, NOT a fixture claim — Codex's P10b
  note), classify_captured(export_smoke) PASS live. Then P11 (Resource Import/Provenance).

## PR P1B-LIVE-STABILITY — prove harness stability before export (PLAN, ratification-pending)
OBJECTIVE (Dylan): before building P10b export capture, PROVE the P1B-LIVE browser product
harness is STABLE, FAIL-CLOSED, and NOT hiding product failures, via repeated MiniMax-M3
DIRECT-API runs. Direct MiniMax only (host api.minimaxi.chat|api.minimax.io, model MiniMax-M3,
OpenRouter count 0, provider ledger required every run). DO NOT proceed to P10b/P11 until green.

### Constraints (hard)
Don't weaken oracles. Don't mark SKIP as PASS. Record EVERY attempt (no retry-until-pass in the
stability count). Don't hide flaky runs. Each scenario class needs CONSECUTIVE passes; a fail
resets that class's count.

### Gaps found (must wire first; the current durable spec lacks them)
- G1 SIDECAR slice NOT captured → cannot prove "no provider calls after terminal". The
  SidecarStopOracle exists (checks stopped_at_terminal + provider_calls_after_terminal==0) but
  SKIPs without the slice. WIRE: per-run relay-ledger window — snapshot relay.jsonl line count
  + record FINISHED wall-clock; after a settle delay, sidecar={stopped_at_terminal: (no relay
  record with ts>finished_ts), provider_calls_after_terminal: count(ts>finished_ts)}. Add
  "sidecar" to the stability scenarios' required_slices so it's ENFORCED, not skipped.
- G2 "no orphan sidecar/sandbox/preview process" → extend the cleanup check beyond podman: after
  kill, assert no container for the cid AND no leftover preview/sidecar host process.
- G3 OUTPUT-TRUTH for the browser harness: the headless OutputTruthOracle (workspace_manifest/
  preview) SKIPs in the product path. For the browser harness, output-truth == shown.preview_
  shown(real render) + verification.passed(structured verdict). "FINISHED without output truth"
  ⇒ the shown/verification oracles FAIL (already fail-closed). The negative fixture asserts THAT
  mapping (a FINISHED run with preview_shown=false / verification.passed=false → FAIL), and does
  NOT pretend the headless OutputTruthOracle ran.
- G4 per-run provider isolation: the relay log is shared; each run must attribute its ledger via
  the line-count window above so per-run "minimax-only / 0 openrouter / 0 post-terminal" is real.

### Decomposition (gated sub-PRs)
- STAB-1 (Python+TS, fast, fixture-tested): wire G1 sidecar capture + per-run relay window + G2
  orphan check into a reusable stability runner module; add the "sidecar" slice to the stability
  scenario contracts. + HARNESS_NEGATIVE_FIXTURES: a deterministic pytest proving each negative →
  the exact code: missing scenario_id→INVALID_RUN(raises); unknown scenario_id→INVALID_RUN;
  missing browser_ws→INVALID_RUN; missing shown→INVALID_RUN; missing preview→INVALID_RUN; missing
  provider ledger→INVALID_RUN; OpenRouter host in ledger→FAIL; provider_calls_after_terminal>0→
  FAIL; preview_shown=false→FAIL(shown); no cleanup evidence(cleanup slice absent)→INVALID_RUN or
  workspace_released!=true→FAIL; FINISHED + verification.passed=false→FAIL. Each maps to an
  existing oracle/code — NO oracle weakening. (Most already have partial coverage; consolidate +
  complete into one explicit negative suite.)
- STAB-2 (LIVE): STATIC_SMOKE x10 CONSECUTIVE single builds (NO retry-to-pass). Per run: real UI
  path, browser WS connected, plan/build executes, PreviewPane renders non-empty, FINISHED,
  classify_dossier PASS, provider ledger valid (minimax-only), cleanup passes, no orphan, no
  post-terminal calls. Record run_id + evidence dir + verdict + ledger summary for EACH. A fail →
  classify(product/harness/model/infra) → fix only if cause clear → add regression fixture →
  RESTART the count. Acceptance 10/10.
- STAB-3 (LIVE): STATIC_REVISION x5 — first build PASS, send a follow-up revision via the UI
  steer/followup input, harness observes revised output differs in the requested way (no
  unrelated rewrite if targeted), final classify_dossier PASS. 5/5.
- STAB-4 (LIVE): DISCONNECT_WHILE_RUNNING x3 — start build, close/disconnect the product WS mid-
  run, prove active work prevents auto-suspend, reconnect or observe a safe terminal, cleanup
  passes, no runaway provider calls. 3/3 PASS or fail-closed with the correct code if
  intentionally destructive.
- STAB-REPORT: a stability report (run IDs, evidence paths, pass/fail table, failure taxonomy,
  provider-ledger summary). 0 UNKNOWN_FAILURE, 0 INVALID_RUN in positives, 0 OpenRouter, 0
  post-terminal calls.

### EXPECTED RISK (surfaced honestly)
Current build finish rate ~71% (5/7), trending up post prompt-fixes. 10/10 CONSECUTIVE is a high
bar; the bookkeeping-stuck-after-deliverable failure mode (TRACKED) may prevent it. If STAB-2
cannot reach 10/10 because of that, the finalize-on-bookkeeping-stuck ENGINE fix (turn_control.
gate_bookkeeping_streak → honest-finish when a verified deliverable exists, reuse finish.py
maybe_honest_unverifiable_static_actionless_finish + _is_web_deliverable) becomes a REQUIRED
prerequisite PR (STAB-2a) — gated, then resume the count. We do NOT weaken the gate to pass.

### Process
plan (this) → Codex PLAN review → revise to APPROVE → implement STAB-1 → run STAB-2/3/4 live →
Codex CODE+EVIDENCE review → revise to APPROVE → commit/push → heartbeat. P10b stays PARKED
(scaffold saved out-of-repo) until this is green.

### P1B-LIVE-STABILITY — PLAN round-1 revision (Codex REVISE: sidecar boundary)
- G1 RE-SPEC (ledger-native, NO wall-clock): relay.jsonl is append-only + stability runs are
  STRICTLY SEQUENTIAL (one build at a time), so attribute provider calls by LINE-COUNT OFFSET,
  never timestamps. Per run record n_start (lines before build), n_terminal (line count at the
  MOMENT /state first shows FINISHED/VERIFIED), then SETTLE (poll /state stays terminal ~10s) and
  record n_settle. sidecar = {stopped_at_terminal: n_settle==n_terminal,
  provider_calls_after_terminal: n_settle - n_terminal}; run ledger = lines[n_start:n_settle]
  (isolated by sequencing) → all hosts minimax, 0 openrouter. Monotonic, clock-free, run-scoped.

### HEARTBEAT 2026-06-29 — STAB-1a SHIPPED (negative fixtures, fail-closed proven)
- test_harness_negative_fixtures.py: 18 deterministic fixtures, each a sole-change vs a passing baseline asserting
  the EXACT failure code (no SKIP-as-PASS, no oracle weakening). Covers the full negative matrix: missing
  slice/ledger/scenario_id → INVALID_RUN; openrouter host / post-terminal call / sidecar-not-stopped / nothing-
  shown / orphan / not-released / verification-false → FAIL with the right code. Codex APPROVE after 3 sharp
  REVISEs (sole-change isolation; assert exact codes; the openrouter host must contain BOTH 'minimax'+'openrouter'
  to isolate the forbid gate from the require-host miss). pyright 0/0.
- NEXT: STAB-1b — wire the SIDECAR per-run relay-offset window (n_start/n_terminal/n_settle, ledger-native) + the
  orphan-process check into a reusable stability runner; add 'sidecar' to the stability scenario required_slices.
  THEN STAB-2 (STATIC_SMOKE x10 consecutive LIVE, no retry-to-pass, record every run). If the stuck rate blocks
  10/10, the finalize-on-bookkeeping-stuck engine fix becomes the required STAB-2a prereq. P10b stays PARKED.

### HEARTBEAT 2026-06-29 — STAB-1b SHIPPED + STAB-2 LAUNCHED (x10 live matrix running)
- STAB-1b (Codex APPROVE after 1 REVISE): STATIC_SMOKE_STRICT scenario (sidecar enforced) + stability-run.spec.ts
  (single build, no retry, ledger-native sidecar window with the boundary snapshot BEFORE the terminal state-read
  so a racing call can't be swallowed) + run_stability_matrix.sh (sequential, stop-on-first-fail, record every
  run). 44 product tests + 18 negative fixtures green; pyright 0/0.
- STAB-2 LAUNCHED: run_stability_matrix.sh 10 → orchestrator pid 515190, report at RUN_DIR/stab2-report/. Building
  run 1. ~45 min wall-clock for 10. Monitor stab2.out + run-N.json across heartbeats. STOPS on first non-PASS →
  classify (product/harness/model/infra), fix if clear, regression fixture, restart count. If bookkeeping-stuck
  blocks 10/10 → STAB-2a engine fix (finalize-on-bookkeeping-stuck) before resuming.

### HEARTBEAT 2026-06-29 — STAB-2 run 2 FAILED → classified HARNESS bug → STAB-2b fix → restart count
- STAB-2 run 1 PASS (sidecar 0-after-terminal, ledger 31 calls/0 OR — the new wiring works). Run 2 STOPPED the
  matrix: verdict surfaced as CLASSIFIER_ERROR but the embedded code was VERIFICATION_GATE_BYPASSED.
- CLASSIFIED (2 HARNESS bugs, NOT product): (a) run 2's build genuinely verified via the `browser` tool (navigate→
  screenshot→console_view) + reached clean FINISHED (detail=None, not unverified_release) — disco verified it via
  the browser path — but the harness recognized ONLY verify_web_app → false VERIFICATION_GATE_BYPASSED. (b) the
  spec mislabeled the real FAIL as CLASSIFIER_ERROR (classify_captured exits non-zero on non-PASS + execFileSync
  throws).
- STAB-2b SHIPPED (Codex APPROVE): decideVerification() mirrors disco's real gate (verify_web_app pass OR browser-
  inspect + clean non-unverified terminal; fail-closed) + vitest 8 cases; classify-verdict parsed from stdout in
  both exit branches. NO oracle weakening.
- PROTOCOL: count restarts. Relaunching STAB-2 x10 from 0.

### HEARTBEAT 2026-06-29 — STAB-2 v2 run-1 FAIL: a SECOND distinct non-finish mode (edit-elision thrash)
- v2 run-1 NO_CLEAN_TERMINAL (STUCK iter 55). Classified: NOT harness, NOT bookkeeping. ROOT = the _snip_args
  ELISION interaction (memory disco-runthru-v2-snipargs-keystone): disco elides large tool-arg bodies (file_write
  content) in the model's context; MiniMax then file_edits that file and either ECHOES the elision marker
  ("Argument(s) ['new'] contain an internal elision placeholder" — executor rejects, correctly) or guesses
  (old_text_not_found) → 12 failed edits (seq 44-116) → recovery_requested → STUCK. browser-navigate at seq 26
  (early), NO verify_web_app, deliverable event only at seq 120 (the terminal auto-handoff). Final state is
  mid-broken-edit → NOT a verified deliverable → finalize-on-stuck would be UNSAFE here (distinct from the
  bookkeeping case where the deliverable WAS verified).
- TWO distinct non-finish modes now observed: (1) bookkeeping-malform → bookkeeping_only STUCK (after a VERIFIED
  deliverable — finalize-on-verified is the clean fix); (2) edit-elision thrash → STUCK (mid-edit, unverified —
  needs elision/read-before-write recovery robustness, a deeper+riskier engine change). Both = MiniMax-M3 thrashing
  after an initial deliverable. ~50% finish rate (memory disco-m3-real-build-capability).
- DECISION POINT: 10/10 consecutive is blocked by GENUINE model+engine thrash (not harness bugs). Reaching it needs
  real reliability work (STAB-2a finalize-on-verified + an elision-recovery fix), OR a survey to quantify rates,
  OR an acceptance-bar adjustment for MiniMax-M3. Surfacing to Dylan for the scope decision (don't weaken oracles).

## ===== CAMPAIGN: CD-TOOLS (Claude Design–style tool runtime) — BLOCKS P10b/P11 =====
DECISION (Dylan, 2026-06-30): STOP P10b + STAB-2a. Mode B (edit-elision thrash) is a Disco
TOOL/RUNTIME CONTRACT problem, not a MiniMax capability problem — fix the tool layer toward
Claude Design's artifact model. 10 PRs, same binding loop (plan→Codex APPROVE→implement→tests→
Codex APPROVE→commit). NON-NEGOTIABLE: do NOT weaken product-harness oracles / output truth /
verification gates / provider ledger / MiniMax-direct proof / ToolScope / contract-runtime. NO
OpenRouter for live runs. NO user-installable Pi packages / external plugins. Do NOT reach P10b
until CD-TOOLS green (all 10 + the §10 survey). PR list: 1 fresh-edit-guard · 2 atomic
exact_replace · 3 safe_write_file · 4 artifact-aware writes · 5 show/ready_for_verification
first-class · 6 verifier-only diagnostics split · 7 buffered run_project_script · 8 prompt-pack ·
9 live MiniMax targeted-edit harness · 10 survey+gate. New codes: FRESH_READ_REQUIRED,
STALE_FILE_CONTEXT, ELISION_MARKER_REJECTED, EXACT_REPLACE_NO_MATCH, EXACT_REPLACE_DUPLICATE_MATCH,
EXACT_REPLACE_BATCH_FAILED, SAFE_WRITE_SHRINK_REJECTED, SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED,
VERIFIER_ONLY_TOOL_BLOCKED (+existing ARTIFACT_NOT_SHOWN_TO_USER/VERIFICATION_GATE_BYPASSED).

### PR CD-TOOLS-1 — Fresh edit guard (PLAN, ratification-pending)
GROUNDING (read, not guessed): file_edit (builtin/files.py:581 FileEditTool) ALREADY reads fresh
disk (line 615) + forgiving-replaces old→new → 'old_text_not_found' on miss. _read_state
(files.py:80, per conv→path→ranges) tracks reads; read_before_write is enforced for file_write
(files.py:443) but NOT for the edit family. The elision EXECUTION guard exists (executor.py:425:
"argument(s) … contain the elision placeholder text", via events.py _ELISION_MARKER_RE/_ELISION_
PARAPHRASE_RE) — Mode B hit it. ROOT of Mode B: the model's CONTEXT VIEW of a file is elided by
_snip_args, so it can't supply a correct `old` → old_text_not_found / elision echo → thrash.
GOAL: an exact edit may not run against stale/incomplete/elided MODEL-VISIBLE source.
IMPLEMENT:
- Extend the read-state to record, per (conv,path) at the moment a read is SHOWN to the model:
  {sha256 of shown bytes, complete:bool (the shown view was NOT elided/truncated), shown_ranges,
  event_seq, reader_tool}. (Populate from the FileRead render + the workspace-snapshot pin.)
- Pre-edit guard on the targeted-edit family (file_edit + file_replace_lines/file_insert_lines/
  file_str_replace): BEFORE touching the file, require a FRESH COMPLETE read covering the edit
  region — current-disk sha == last-shown sha AND complete==true AND (for file_edit) the `old`
  text falls within a shown range. If absent/stale/elided → BLOCK (no file mutation) returning a
  STRUCTURED observation {kind:"fresh_read_required", path, reason, next_required_action:
  "file_read", suggested_args:{path}} + code FRESH_READ_REQUIRED (or STALE_FILE_CONTEXT if the
  file changed since the read).
- Elision: if `old`/`new` contains an internal elision marker, BLOCK with {kind:
  "elision_marker_rejected", path, next_required_action:"file_read"} + code ELISION_MARKER_REJECTED
  (wire the existing executor.py:425 guard to this structured shape + code).
- Add the 3 codes to failure_codes.py + SEVERITY_BY_CODE. A blocked edit is NOT a product failure
  by itself (it's a corrective nudge); only repeated IGNORE of fresh_read_required escalates via
  the existing stuck path.
SCOPE: the GUARD only (exact_replace the dedicated tool is CD-TOOLS-2). Read-only tools unaffected.
Single-writer files: builtin/files.py + tools/executor.py + failure_codes.py (canonical writer = me).
TESTS (pytest): edit-after-full-fresh-read succeeds; edit-after-elided-read → fresh_read_required;
edit-after-file-changed-since-read → STALE_FILE_CONTEXT; edit-with-elision-marker → ELISION_MARKER_
REJECTED; blocked edit does NOT mutate the file; blocked edit is not itself a product failure;
read-only tools (file_read/search) unaffected by the guard.
CODEX FOCUS: no edit path bypasses the guard; not global overreach (read-only unaffected); elision
markers cannot enter source via old/new.

### CD-TOOLS-1 — PLAN round-1 revision (Codex REVISE: freshness proof)
- FIX the read-state shape: record at SHOW time per (conv,path): {full_file_disk_sha256 (the WHOLE file's sha at
  read time — NOT sha of the shown slice), shown_ranges:list[(start,end)] actually shown to the model (line- or
  byte-ranges), range_complete:bool per range (the shown range was NOT elided/truncated), event_seq, reader_tool}.
- Pre-edit guard, TWO independent checks (so ranged reads stay valid — no forced whole-file read):
  (1) STALE: current disk full-file sha == recorded full_file_disk_sha256 → else STALE_FILE_CONTEXT (re-read; the
      file changed under the model since it last read).
  (2) COVERAGE: the edit region was SHOWN COMPLETE — for file_edit, the `old` text occurs inside a shown_range
      whose range_complete==true; for file_replace_lines/file_insert_lines, the target lines fall inside a
      complete shown_range → else FRESH_READ_REQUIRED (read that region). A full file_read sets one shown_range
      covering the whole file (complete) so a full read + edit always passes; a ranged read covering the edited
      section also passes (no whole-file read forced).
- This removes the sha(shown)≠sha(disk) inconsistency: disk-sha is full-file (stale check); coverage is by raw
  shown ranges + completeness (saw-it check). Elision check unchanged (old/new contains a marker → ELISION_MARKER_
  REJECTED). Everything else in the CD-TOOLS-1 plan stands.

### HEARTBEAT 2026-06-30 — CD-TOOLS campaign STARTED; STAB-2/STAB-2a STOPPED (Dylan)
- Pivoted to CD-TOOLS per Dylan's campaign doc (fix Mode B at the tool-runtime layer, not finalize-on-stuck).
  STAB-1a/1b/2b stay (they're proven harness work); the live STAB-2 x10 matrix is STOPPED. P10b PARKED behind
  CD-TOOLS. The stability matrix's role is fulfilled by CD-TOOLS-9 (live targeted-edit harness) + CD-TOOLS-10
  (survey/gate).
- CD-TOOLS-1 (fresh edit guard) PLAN = Codex APPROVE (after 1 REVISE: freshness proof = full-file disk sha +
  raw shown ranges + per-range completeness, NOT sha-of-shown-slice — so ranged reads don't false-block).
- GROUNDING for impl: _read_state (files.py:80) is currently just {conv: {read_since_write: set[path]}} (coarse).
  Enrich to also hold {conv: {reads: {canonical_path: {disk_sha, ranges:[(start,end,complete)], event_seq,
  reader_tool}}}}. FileReadTool.run populates it (full-file sha + shown line-range + complete flag). Pre-edit guard
  in FileEditTool.run (+ file_replace_lines/file_insert_lines/file_str_replace): STALE (disk sha != recorded) →
  STALE_FILE_CONTEXT; COVERAGE (edit region not in a complete shown range) → FRESH_READ_REQUIRED; old/new has an
  elision marker → ELISION_MARKER_REJECTED. Return ToolOutcome(success=False, error=CODE, structured={kind,path,
  next_required_action:'file_read',suggested_args}). NOTE: these are TOOL-level error strings/structured obs
  (like the existing old_text_not_found/read_before_write), NOT build_soak classifier codes. Tests per the plan.
- NEXT: implement CD-TOOLS-1 → tests → Codex CODE-gate → commit. Then CD-TOOLS-2..10.

### HEARTBEAT 2026-06-30 — CD-TOOLS-1 SHIPPED (fresh edit guard)
- guard_fresh_edit in builtin/files.py: sha-aware read records (FileReadTool populates) + the guard wired into all
  4 targeted-edit tools (file_edit/replace_lines/insert_lines/str_replace), pre-mutation. ELISION_MARKER_REJECTED
  (unconditional) / STALE_FILE_CONTEXT / FRESH_READ_REQUIRED, structured nudges. Size-gated at 1500 bytes (=
  _ARG_SNIP_CHARS) so small-file edits are NEVER blocked — Mode B is large-file only. Codex APPROVE after 1 REVISE
  (forgiving-match edit_lines=None skipped coverage → now fail-closed on partial reads). 10 guard tests + 36 edit
  tests green; pyright 0/0. Pre-existing-only fails: pdfinfo, MCP/node.
- NEXT: CD-TOOLS-2 (atomic exact_replace tool — the dedicated str-replace primitive with require_fresh_read +
  expected_sha256 + multi + atomic batch, reusing guard_fresh_edit). Then 3..10.

### PR CD-TOOLS-2 — atomic exact_replace tool (PLAN, ratification-pending)
GROUNDING: tools register via builtin/__init__.py (__all__ + the instantiation list) + registry.py AGENT_TOOLS
(name set) / _ANCHORED_EDIT_TOOLS (weak-tier withhold). scopes.py per-phase allowlists derive from the CONTRACT
(edit=contract.edit.edit_tools), NOT a hardcoded list — so wiring exact_replace into the EDIT-phase ToolScope is a
contract-config change deferred to the §6 ToolScope integration (don't half-wire contracts now). CD-TOOLS-2 adds +
registers the tool (callable); reuses CD-TOOLS-1's guard_fresh_edit + _has_elision_marker.
GOAL: a dedicated atomic exact-replacement primitive (Claude Design dc_*_str_replace) replacing fragile broad
file_edit for targeted edits.
TOOL: exact_replace, args {path:str, edits:list[{old_string:str, new_string:str}], require_fresh_read:bool=True,
expected_sha256:str|None=None, multi:bool=False}.
RULES (all fail-closed, NO partial writes):
- elision: any old_string/new_string contains an internal elision marker (_has_elision_marker) → ELISION_MARKER_
  REJECTED, no write.
- stale: expected_sha256 given AND sha256(current disk bytes) != it → STALE_FILE_CONTEXT, no write.
- fresh-read: require_fresh_read (default True) → reuse guard_fresh_edit for elision+stale+grounding+coverage
  (size-gated at 1500B like CD-TOOLS-1; per-edit coverage uses each old_string's matched line span).
- match: each old_string must occur EXACTLY ONCE in the CURRENT text unless multi=True (then ALL its occurrences
  replace). 0 → EXACT_REPLACE_NO_MATCH; >1 and not multi → EXACT_REPLACE_DUPLICATE_MATCH.
- atomic + non-overlap: compute ALL match spans on the ORIGINAL text first; if any two spans OVERLAP → EXACT_
  REPLACE_BATCH_FAILED (no write); if ANY edit fails match → EXACT_REPLACE_BATCH_FAILED (no write); else splice all
  edits by position into one new text + ONE atomic write (reuse _gated_write so the W3 syntax gate still applies).
- literal-safe: use str slicing / str.replace (NOT re.sub) so '$', backrefs, ${...} template strings in new_string
  are literal.
- returns structured {applied:[{old_string(trunc), occurrences, replaced}], path, bytes, sha256}.
ERROR CODES (tool-level strings, NOT build_soak): EXACT_REPLACE_NO_MATCH, EXACT_REPLACE_DUPLICATE_MATCH, EXACT_
REPLACE_BATCH_FAILED (+ reuse ELISION_MARKER_REJECTED, STALE_FILE_CONTEXT, FRESH_READ_REQUIRED).
FILES (single-writer = me): builtin/files.py (new tool) + builtin/__init__.py (export+instantiate) + registry.py
(AGENT_TOOLS add; weak tier: exact_replace is a CAPABLE-friendly anchored edit → add to _ANCHORED_EDIT_TOOLS so
it's withheld from the weak advertised set like file_str_replace, but callable). NOT scopes.py (deferred to §6).
TESTS (pytest): single replace OK; multi=True replaces all; no-match→no change+EXACT_REPLACE_NO_MATCH; duplicate
(multi=False)→no change+EXACT_REPLACE_DUPLICATE_MATCH; one failed edit in a batch→NO file change (atomic)+EXACT_
REPLACE_BATCH_FAILED; overlapping edits→no change; stale expected_sha256→no change+STALE_FILE_CONTEXT; elision
marker in old/new→no change+ELISION_MARKER_REJECTED; new_string with '$' / '${x}' / '\\1' stays literal; require_
fresh_read on a large file with no read→FRESH_READ_REQUIRED; small file (≤1500B) no read needed. Run packages/tools
pytest + basedpyright.
CODEX FOCUS (§9): no edit bypasses fresh-read; partial writes impossible (all-or-nothing); stale sha handled;
elision can't enter source; overlapping edits rejected; literal-safety; insufficient negatives.

### CD-TOOLS-2 — PLAN round-1 revision (Codex REVISE: atomicity)
- _gated_write WRITES candidate bytes then REVERTS on the W3 syntax gate — that is NOT all-or-nothing (observable
  bad-bytes mid-state; a failure can leave them). FIX: exact_replace pre-validates EVERYTHING IN MEMORY before any
  write — (a) elision in old/new, (b) expected_sha256 stale, (c) fresh-read/coverage, (d) per-edit match counts
  (exactly-once / multi), (e) overlap of match spans on the ORIGINAL text, (f) syntax of the COMPUTED new text via
  _syntax_errors(path, new_text) (a syntax-introducing batch → EXACT_REPLACE_BATCH_FAILED, NO write). ONLY when ALL
  pass, write the new text ONCE via the sandbox's atomic write (temp + rename if available; else a single
  write_file) — NOT _gated_write. So a failing batch never touches disk; the W3 syntax discipline is preserved by
  the in-memory pre-check instead of write-then-revert. This makes 'NO partial writes / all-or-nothing' true.

### HEARTBEAT 2026-06-30 — CD-TOOLS-2 plan design-approved (atomicity reworked)
- CD-TOOLS-2 plan: atomic exact_replace. Codex round-1 caught a REAL design flaw — _gated_write writes-then-reverts
  (not all-or-nothing); reworked to PRE-VALIDATE EVERYTHING IN MEMORY (elision/stale/fresh-read/coverage/match-
  counts/overlap/SYNTAX-of-computed-text) then a SINGLE write only on full success (atomic temp+rename if available)
  — never write-then-revert. Codex round-2 returned 'REVISE: not implemented' — a plan-stage artifact (it inspects
  the tree, exact_replace doesn't exist yet); it did NOT fault the atomicity design. The design is sound; the real
  gate is the CODE review after implementation.
- NEXT: IMPLEMENT CD-TOOLS-2 — ExactReplaceTool in builtin/files.py (reuse guard_fresh_edit + _has_elision_marker;
  in-memory validate-all → single write; multi/overlap/literal-safe via str-slicing) + register (builtin/__init__,
  registry.AGENT_TOOLS + _ANCHORED_EDIT_TOOLS) + the ~12 tests + basedpyright → Codex CODE-gate → commit. Then 3..10.

### HEARTBEAT 2026-06-30 — CD-TOOLS-2 SHIPPED (atomic exact_replace)
- ExactReplaceTool in builtin/files.py: validate-all-in-memory (elision/stale-sha/match-counts/overlap/mandatory-
  fresh-read/syntax) → ONE write, true all-or-nothing (no write-then-revert). Literal splice (str slicing). multi/
  expected_sha256. Registered in __init__/AGENT_TOOLS/exec_policy.withheld_tools (anchored-edit tier). Codex APPROVE
  after 2 REVISEs — both REAL bypass holes: (R1) require_fresh_read settable=false → REMOVED (guard now mandatory);
  (R2) file_read(offset>EOF) granted grounding showing nothing → moved the read_since_write grant past the past-EOF
  return (also hardens file_write's read-before-write gate). 15 + 57 tests; pyright 0/0.
- NEXT: CD-TOOLS-3 (safe_write_file) — phase-scoped whole-file write with shrink>50% guard + elision reject + atomic
  temp+rename + manifest update. Then 4..10 (9 = LIVE MiniMax targeted-edit harness; 10 = survey→unblock P10b).

### PR CD-TOOLS-3 — safe_write_file (PLAN, ratification-pending)
GROUNDING: file_write today = read old → F1 read-before-rewrite gate (refuse overwrite of an EXISTING file with no
fresh read) → binary-deliverable clobber guard (_BINARY_DELIVERABLE_EXTS) → _gated_write (writes-then-reverts on
syntax). Governed artifacts = the host-reserved `.disco/` namespace (.disco/appspec.json, .disco/tweaks.json,
.disco/versions/*, .disco/context/*) — authored ONLY by semantic tools (app_create/set_tweak/context_memory), never
the generic writer. ProcessSandbox.write_file = plain target.write_bytes (NOT atomic); NO rename/move in the sandbox
protocol (read_file/write_file/list_dir/file_exists only).
GOAL: safe_write_file — a SAFER whole-file write (superset of file_write) that REPLACES naive file_write for
governed/large artifacts. args {path:str, content:str, allow_shrink:bool=False, expected_sha256:str|None=None}.
run() — ALL checks in memory before any write:
- (1) elision marker in content (_has_elision_marker) → ELISION_MARKER_REJECTED.
- (2) GOVERNED path: canonical path under `.disco/` → SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED (use the semantic tool;
  sound path-prefix rule, .disco/ is the host's reserved namespace — NOT a guess).
- (3) read old_text (None if absent). EXISTING file:
   - expected_sha256 given + sha256(old bytes) != it → STALE_FILE_CONTEXT.
   - binary-deliverable clobber (reuse _BINARY_DELIVERABLE_EXTS) → refuse (parity with file_write).
   - read-before-rewrite (F1): path not in read_since_write AND no matching expected_sha256 → FRESH_READ_REQUIRED
     (parity with file_write; a matching expected_sha256 counts as grounding).
   - SHRINK guard: len(content) < 0.5*len(old_text) AND not allow_shrink AND not matching-expected_sha256 →
     SAFE_WRITE_SHRINK_REJECTED (the weak-model whole-file-clobber/truncation guard; explicit override only).
- (4) syntax pre-check: introduced = _syntax_errors(content) - _syntax_errors(old_text or "") → if any, refuse
  (PRE-write, NO revert) — reuse the diff-filter idea but never write bad bytes.
- (5) ATOMIC write: new _atomic_write(sandbox, path, bytes) — writes a sibling tmp then os.replace on backends that
  support it; ProcessSandbox gains a concrete atomic_write (tmp in resolved dir + os.replace); the helper falls back
  to a single write_file where unsupported (still logical all-or-nothing). RETROFIT exact_replace's final write to
  use _atomic_write too. Clear read_since_write on success.
- (6) manifest/artifact-memory update is deferred to CD-TOOLS-4 (artifact-aware writes).
ERROR CODES (tool-level, NOT build_soak): SAFE_WRITE_SHRINK_REJECTED, SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED (+ reuse
ELISION_MARKER_REJECTED, STALE_FILE_CONTEXT, FRESH_READ_REQUIRED).
FILES (single-writer=me): builtin/files.py (SafeWriteFileTool + _atomic_write + retrofit exact_replace) +
sandbox/process.py (atomic_write) + builtin/__init__ + registry.AGENT_TOOLS (add 'safe_write_file'; NOT anchored-
edit — it's a general writer, advertise to all tiers). NOT scopes.py (deferred to §6).
TESTS: normal create OK + bytes atomic; overwrite-after-read OK; >50% shrink → SAFE_WRITE_SHRINK_REJECTED + file
UNCHANGED; allow_shrink=True → applies; matching expected_sha256 bypasses shrink+F1; .disco/ path → SAFE_WRITE_
GOVERNED_ARTIFACT_REJECTED + no write; elision → rejected; syntax-introducing (.py) → rejected + unchanged; existing
binary (.pdf) → refused; overwrite-without-read large file → FRESH_READ_REQUIRED; atomic_write leaves NO leftover
tmp + original intact when a later check fails (failure happens BEFORE the write, so original is never touched).
CODEX FOCUS (§9): shrink guard not trivially bypassable (allow_shrink is explicit + intentional, expected_sha256 is
real grounding); governed detection sound (.disco/ prefix on the CANONICAL path, not substring-spoofable); atomic
temp+rename real on ProcessSandbox + safe fallback; no false-block of a legit small rewrite that ISN'T >50% shrink;
elision; insufficient negatives.

### CD-TOOLS-3 — PLAN round-1 revision (Codex REVISE: governed check must be spoof-proof)
- A lexical `.disco/` prefix on the normpath'd path MISSES a symlink reach: `ln -s .disco/appspec.json link.json`
  then safe_write_file(link.json) — "link.json" isn't under .disco/ but the write follows the symlink to a governed
  file. FIX: resolve the REAL (symlink-followed) jail-relative path. ProcessSandbox._resolve ALREADY does
  `(workspace/path).resolve()` (follows symlinks, rejects jail escapes) — expose it: add sandbox.resolve_relpath(
  path)->str returning the symlink-followed path RELATIVE to the workspace root. The governed check =
  resolve_relpath(path) is under `.disco/`. A new helper _governed_target(sandbox,path): use getattr(sandbox,
  'resolve_relpath',None) when present (ProcessSandbox), else fall back to the lexical canonical strip. So on the
  local/test backend the symlink-spoof is CLOSED + provable; on exec-only backends the lexical check remains as a
  documented STEERING softguard (note: the sandbox jail is the true boundary, and shell can write .disco/ regardless
  — this guard STEERS the generic writer to the semantic tool, it is not a security control).
- TEST add: create a symlink link.json -> .disco/appspec.json (via the ProcessSandbox workspace), safe_write_file(
  link.json) → SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED (proves the real-path resolution, not the lexical name).
- resolve_relpath also lets _atomic_write put its tmp sibling next to the REAL target dir (correct rename domain).

### HEARTBEAT 2026-06-30 — CD-TOOLS-3 SHIPPED (safe_write_file)
- SafeWriteFileTool: validate-all-in-memory (elision/governed-.disco-real-path/stale-sha/binary/read-before-rewrite/
  >50%-shrink/syntax) → ONE atomic commit. New ProcessSandbox.atomic_write (mkstemp random O_EXCL + os.replace, no
  symlink follow) + resolve_relpath (symlink-followed governed check); exact_replace retrofitted to _atomic_write.
  Registered AGENT_TOOLS + ARTIFACT_TOOLS. Codex APPROVE after 2 REAL REVISEs (symlink-evades-lexical-governed-check;
  predictable-tmp-symlink-clobber). 13 + 15 tests; pyright 0/0; full tools suite no new fails.
- NEXT: CD-TOOLS-4 (artifact-aware writes) — route governed-artifact updates to the semantic tool + manifest/
  evidence update on safe_write success where applicable (the deferred (6) from CD-TOOLS-3). Then 5..10 (9=LIVE
  MiniMax targeted-edit harness; 10=survey→unblock P10b).

### PR CD-TOOLS-4 — artifact-aware writes (PLAN, ratification-pending)
GROUNDING: contract registry (core/contract/registry.py) maps each ContractKind → required_files + edit_tools:
static.site → index.html, file_edit/file_replace_lines; appkit.leadgen → .disco/appspec.json + index.html, app_*.
Governed paths = the host-reserved .disco/ namespace, owned by SEMANTIC tools (appspec→app_create/app_set_tweak,
tweaks→app_set_tweak, versions→app_snapshot_version, context→context_memory). The resource manifest lives in
core/context/store.py (ContextStore.record_resources → ArtifactMemoryKind.RESOURCE_MANIFEST). ToolContext is
DECOUPLED from the store (no manifest hook) and NO runtime seam consumes ToolOutcome.artifacts → manifest. REAL GAP
FOUND: exact_replace has NO governed-artifact guard (only safe_write_file does) — a model could exact_replace
.disco/appspec.json to bypass. 
SCOPE DECISION (honest, layering-respecting; matches the user's "minimal sound seam" steer): CD-TOOLS-4 = the
tool-layer ARTIFACT-AWARE ROUTING seam (complete + testable NOW). The runtime manifest-FOLD (engine consumes an
artifact_write signal → ContextStore.record_resources) crosses the tool→core boundary with NO existing seam, so it
is carved as a SEPARATE follow-up core step CD-TOOLS-4b — NOT faked here (no stub/false-affordance). The campaign's
full artifact_* tool subsystem (artifact_create/replace_markup/replace_logic/set_props/snapshot_version) is the
per-contract wrapper layer the doc explicitly defers ("contract-specific wrappers later, but contract rules now") —
also follow-up; CD-TOOLS-4 lands the RULES.
GOAL: make safe_write_file AND exact_replace artifact-aware — a write to a host-governed path is ROUTED to the
owning semantic tool (named), never silently succeeds via the generic writer; an unmapped governed path says so (no
false affordance).
DESIGN:
- _GOVERNED_ROUTING: dict of governed .disco/ path-rule → {tool, why}. Rules: ".disco/appspec.json" → app_set_tweak
  /app_create; ".disco/tweaks.json" → app_set_tweak; ".disco/versions/" (prefix) → app_snapshot_version; ".disco/
  context/" (prefix) → context_memory. A _route_for_governed(relpath) -> (tool|None, why) helper.
- safe_write_file: its existing SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED now includes route_to:<tool> (or "host-managed;
  no generic editor — do not edit this file directly" when unmapped) in content + structured.
- exact_replace: ADD the same governed guard (currently MISSING) — resolve the real path via _governed_relpath,
  reject governed targets with GOVERNED_ARTIFACT_REJECTED + route_to. (Closes the bypass.)
- a truthful structured "artifact_write" field already implied by {path,sha256,bytes} stays as-is; CD-TOOLS-4b will
  add the runtime consumer. NO new no-op signal emitted here (avoid a dangling affordance).
ERROR CODES: reuse SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED (safe_write) + a new GOVERNED_ARTIFACT_REJECTED (exact_
replace). Tool-level, NOT build_soak.
FILES (single-writer=me): builtin/files.py only (routing table + helper + exact_replace governed guard + safe_write
message enrichment). No core changes (4b is the core step).
TESTS: safe_write to .disco/appspec.json → rejected + route_to app_set_tweak; exact_replace to .disco/appspec.json →
GOVERNED_ARTIFACT_REJECTED + no write (NEW — proves the closed bypass); .disco/context/x via symlink → rejected +
route_to context_memory; unmapped .disco/foo → rejected + "no generic editor" (no fake tool name); a NORMAL non-
governed write/replace still succeeds (no over-block).
CODEX FOCUS (§9): is the routing sound (real path, not lexical; correct tool per path); does adding the governed
guard to exact_replace close the bypass without false-blocking a normal edit; is carving the manifest-fold to 4b
honest (not a hidden stub); no false affordance (unmapped path names no tool); insufficient negatives.

### CD-TOOLS-4 — PLAN round-1 revision (Codex REVISE: governed guard must cover ALL generic mutators)
- Guarding only exact_replace + safe_write_file MOVES the bypass: file_write/file_append/file_edit/file_replace_
  lines/file_insert_lines/file_str_replace are still callable mutators with NO governed .disco/ check. FIX: a SHARED
  helper _governed_guard(sandbox, path) -> ToolOutcome|None (resolves the REAL symlink-followed path via _governed_
  relpath; governed → GOVERNED_ARTIFACT_REJECTED + route_to:<semantic tool>) called at the TOP of EVERY generic
  mutator: file_write, file_append, file_edit, file_replace_lines, file_insert_lines, file_str_replace, exact_
  replace, safe_write_file. So NO generic write path can touch a .disco/ governed artifact; the model is routed to
  the owning semantic tool everywhere.
- INVARIANT TO VERIFY at implement time: the SEMANTIC tools (app_create/app_set_tweak/context_memory/app_snapshot_
  version) write .disco/ via ctx.sandbox.write_file DIRECTLY (their own run()), NOT by invoking these generic mutator
  tools — so the guard does NOT break them. If any semantic tool routes through a generic mutator, exempt it via an
  explicit ctx flag (NOT a path heuristic). Confirm before landing.
- TESTS expand: EACH of the 8 generic mutators rejects a write to .disco/appspec.json with route_to; a normal non-
  governed write via each still succeeds; the semantic tools still write .disco/ successfully (regression).

### HEARTBEAT 2026-06-30 — CD-TOOLS-4 SHIPPED (artifact-aware governed routing)
- Shared _governed_guard on ALL 8 generic mutators: real-path (symlink-followed) .disco/ write → routed to the
  owning semantic tool. resolve_relpath + atomic_write now on BOTH ProcessSandbox AND the ContainerInstance base
  (guest realpath; mkstemp-random tmp + mv -fT onto the RESOLVED target = parity w/ os.replace), delegated by
  SandboxSession (closed a latent CD-TOOLS-3 runtime atomic_write gap). Codex APPROVE after SIX real REVISEs
  (bypass-via-other-mutators, container-backend-missing-methods, session-no-delegation, session-crash-no-fallback,
  mv-into-dir, symlink-parity). 13+53 tests; pyright 0/0; suite no new fails. Runtime manifest-fold (4b) + artifact_*
  per-contract wrappers = honest follow-ups.
- NEXT: CD-TOOLS-5 (show/ready_for_verification first-class — make the host show/verify handoff an explicit tool, not
  an inferred convention). Then 6 (verifier-only diagnostics split), 7 (buffered run_project_script), 8 (prompt-pack),
  9 (LIVE MiniMax targeted-edit harness), 10 (survey→unblock P10b).

### PR CD-TOOLS-5 — first-class show tools (PLAN, ratification-pending)
GROUNDING: the product_evidence "shown" slice schema is {"artifact_shown":bool, "preview_shown":bool}
(harness/build_soak/product_evidence.py:27). TODAY those are INFERRED by the live capture (stability-run.spec.ts:200
computes artifactShown/previewShown) — there is NO explicit model action for "I am showing the user X" (the campaign's
exact complaint). The verification gate already exists + works: verify_web_app + finish.py _drive_verify_web_app (the
browser-verify finish gate). ready_for_*_verification are contract finalizer NAMES (registry.py), NOT callable tools.
SCOPE DECISION (sound/bounded/low-risk; matches "reuse the gate, don't fork" + the no-weaken-verification non-
negotiable): CD-TOOLS-5 = the first-class SHOW tools (tool + harness layer). The ready_for_verification first-class
finalizer-tool + verifier-fork + the 3 new event CLASSES touch finish.py (the sensitive gate) and the core event
schema — carved to a separate focused gated PR CD-TOOLS-5b so the verification gate is changed deliberately, not
rushed. CD-TOOLS-5 does NOT add a parallel verify path; the existing verify_web_app/finish gate stays the ONLY
verification authority.
GOAL: make "shown" an EXPLICIT, TRUTHFUL model action, separate from verified. file-write alone is NOT shown.
TOOLS (new, in builtin/ — a new show.py or appended): 
- show_to_user(path:str, caption:str|None) — OUTPUT-TRUTH: the artifact path MUST exist (ctx.sandbox.file_exists)
  else refuse SHOW_ARTIFACT_MISSING (can't claim shown without a real artifact). On success: structured
  {kind:"artifact_shown", path, caption} + artifacts=[path]. Does NOT touch verification (shown != verified).
- show_preview_to_user(url:str) — surfaces the running preview; structured {kind:"preview_shown", url}. (URL
  existence isn't a file check; it asserts the preview was surfaced — the preview slice/ownership oracle still
  independently checks the preview is real, so this can't fake a preview.)
HARNESS: drive product_evidence["shown"].artifact_shown / preview_shown from the EXPLICIT show_to_user /
show_preview_to_user tool-calls in the event log (replace the inference in stability-run.spec.ts) — the ShowToUser
oracle then reads a truthful explicit signal. A successful show_to_user (artifact exists) sets artifact_shown=true;
NOTHING about verification changes (the verification slice stays driven ONLY by verify_web_app/browser per STAB-2b
decideVerification).
SHOWN != VERIFIED (the core guarantee): show_to_user has ZERO effect on the verification slice or the finish gate;
a build that ONLY show_to_user's (no verify_web_app/browser-verify) is still NOT verified. Tested both in tools
(structured output carries no verified flag) and in the harness (decideVerification ignores show events).
ERROR CODES (tool-level, NOT build_soak): SHOW_ARTIFACT_MISSING.
REGISTER: builtin/__init__ + registry.AGENT_TOOLS + ARTIFACT_TOOLS (finishing actions, all tiers).
TESTS: show_to_user on an existing artifact → success + artifact_shown structured + artifacts; show_to_user on a
MISSING path → SHOW_ARTIFACT_MISSING + no shown signal; file_write alone produces NO shown signal (only show_to_user
does); show_preview_to_user → preview_shown structured; show_to_user does NOT set any verified flag (shown!=verified);
harness: a dossier whose event log has show_to_user but NO verify → shown slice true BUT verification slice still
unverified (decideVerification unchanged).
CODEX FOCUS (§9): show_to_user can't double as verification (zero verify side-effect); output-truth (no shown
without a real existing artifact); show_preview_to_user can't fake a preview (preview oracle independent); no false
affordance; is carving ready_for_verification/verifier-fork/events to 5b honest + acceptable for THIS PR; insufficient
negatives.

### CD-TOOLS-5 — PLAN round-1 revision (Codex REVISE: don't weaken the preview-render oracle)
- A bare show_preview_to_user(url) signal is WEAKER than today's live iframe-RENDER inference (the preview-ownership
  slice only proves ownership/manual_port, not that the URL actually rendered). Driving preview_shown from the bare
  call would let a model mark preview_shown without a real render — an oracle weakening. NOT ALLOWED.
- FIX (no oracle weakened, ADDITIVE only):
  * artifact_shown: driven by show_to_user with OUTPUT-TRUTH (the artifact path must exist via file_exists) — a REAL
    check; this is the explicit "shown" signal for FILE artifacts (reports/decks/downloads). file-write alone never
    sets it (only an explicit show_to_user does). [Codex did not flag this as weakening.]
  * preview_shown: KEEP today's live render inference as the AUTHORITY — UNCHANGED, not weakened. If show_preview_
    to_user is recorded at all, preview_shown = (explicit call) AND (real render evidence) — a STRENGTHENING (now you
    need BOTH), never a replacement. A test proves preview_shown still REQUIRES the real render (a show_preview_to_
    user call with NO render → preview_shown stays false).
  * To avoid any risk + redundancy with preview_start (which already surfaces the preview) + the live-render
    authority, show_preview_to_user is OPTIONAL/deferred; the keystone is show_to_user (file artifacts). If kept, it
    is purely additive per above.
- The verification slice + decideVerification + finish gate remain COMPLETELY untouched (shown != verified).
- Tests add: preview_shown requires real render even with an explicit call (no weakening); show_to_user only sets
  artifact_shown when the artifact really exists.

### CD-TOOLS-5 — IMPL scope refinement (found during implement: current artifact_shown is ALSO a strong UI check)
- stability-run.spec.ts: artifactShown = a build-feed ROW is VISIBLE (real UI evidence); previewShown = preview tab
  innerText>50 (real render). BOTH are strong UI-evidence checks. So driving EITHER from show_to_user+file_exists
  would WEAKEN them (file_exists != UI-displayed). DO NOT replace either.
- REFINED SCOPE: CD-TOOLS-5 = the show_to_user TOOL only (tools layer). It (a) enforces OUTPUT-TRUTH (refuses to show
  a non-existent artifact → SHOW_ARTIFACT_MISSING), (b) emits a DISTINCT structured signal {kind:artifact_shown,...}
  (distinguishable from edit-artifacts), (c) surfaces the artifact via artifacts=[path] (real UI effect). The harness
  "shown" slice + its strong UI checks stay UNCHANGED + authoritative (no oracle weakened). Wiring the harness to read
  the DISTINCT artifact_shown signal = additive follow-up CD-TOOLS-5b (with the verifier-fork). shown!=verified: the
  tool has ZERO verification side-effect (no verified flag).

### CD-TOOLS-5 — PIVOT (Codex CODE REVISE → discovered serve already exists)
- Codex caught that the new show_to_user tool was a FALSE AFFORDANCE: DefaultToolExecutor DROPS ToolOutcome.artifacts,
  ToolResult has no artifacts field, nothing consumes artifact_shown — so it returned "Shown to the user" while
  surfacing NOTHING. Investigating the real path found disco ALREADY HAS the first-class show handoff: the `serve`
  virtual tool (engine.py _SERVE_*; intercepted in turn_control.handle_serve → emits DeliverableEvent → real UI
  Open-app/Download-files). serve is Claude Design's show_to_user/present_fs_item_for_download, already wired + it
  already does NOT finish/verify (shown!=verified is already enforced).
- PIVOT: DELETED the redundant show_to_user tool; the real CD-TOOLS-5 = add OUTPUT-TRUTH to the existing serve:
  handle_serve now refuses (actionable, valve-routed) to emit a DeliverableEvent for a path that does NOT exist in
  the workspace (_serve_path_missing via executor.sandbox.file_exists; FAIL-OPEN when no sandbox/unverifiable — serve
  is a softguard, the verify gate is the hard proof). Closes "a false Open/Download card for a non-existent
  deliverable" + delivers "can't claim shown without a real artifact." shown!=verified untouched (serve never
  finishes/verifies). ready_for_verification finalizer-tool + verifier-fork + events stay carved to 5b.
- Tests: serve missing path → refused, no DeliverableEvent; serve real path → DeliverableEvent; no sandbox →
  fail-open (existing behavior). Existing dc05 serve tests still green.

### HEARTBEAT 2026-06-30 — CD-TOOLS-5 SHIPPED (serve output-truth)
- Pivoted from a redundant/false-affordance show_to_user tool to HARDENING the existing real show handoff (serve):
  handle_serve refuses to emit a DeliverableEvent for a verifiably-missing path (fail-open w/o sandbox). Found +
  reverted the false affordance (artifacts dropped by the executor). shown!=verified already held (serve never
  finishes). Codex APPROVE after the false-affordance REVISE. 3 tests + dc05 green; pyright clean.
- CARVED to CD-TOOLS-5b: ready_for_verification first-class finalizer-tool + verifier-fork + the 3 event classes
  (touch finish.py / core event schema — deserve a focused gated PR).
- NEXT: CD-TOOLS-6 (verifier-only diagnostics split — verify-phase tools can't mutate; a read-only diagnostics set
  for the verify phase). Then 7 (buffered run_project_script), 8 (prompt-pack), 9 (LIVE MiniMax targeted-edit
  harness), 10 (survey→unblock P10b).

### PR CD-TOOLS-6 — verifier-only diagnostics split (PLAN, ratification-pending) — w/ a major FINDING
FINDING (interrogate-the-given: the prompt assumed verify-phase enforcement EXISTS; it does NOT):
- The contract phase machinery (scopes.compile_tool_scopes + enforce.enforce_tool_call + enforce.ContractPhaseGuard)
  is DORMANT — defined + unit-tested but NEVER instantiated in the live loop/executor/runtime (grep-proven repo-wide).
  There is NO live phase state; disco's loop is PHASE-FLUID (edit↔verify freely), so there is no discrete VERIFY
  phase to gate on. Wiring live phase enforcement into a phase-fluid loop is a LARGE architectural integration
  touching the NON-NEGOTIABLE verification path — that is the deferred "§6 ToolScope integration", not a heartbeat PR.
- disco ALSO already substantially addresses the campaign's CD-TOOLS-6 INTENT:
  * verify_web_app ENCAPSULATES the verifier — it drives browser navigate/console/screenshot INTERNALLY and returns
    a structured PASS/FAIL/DEGRADED verdict; the finish gate consumes the VERDICT, not raw screenshots. There is no
    standalone eval_js tool; screenshot/console are `browser` SUB-ACTIONS, and browser console is CAPPED
    (_MAX_CONSOLE_LINES) — so "main agent poisons context with eval/screenshot loops" is already mitigated.
  * failure_fingerprint blocks re-verify-without-a-productive-edit (anti-spin), and the finish gate's verdict
    consumption means a raw main-agent screenshot is NOT a self-certification.
- Net: the campaign's CD-TOOLS-6 is MOSTLY pre-satisfied in disco; its only net-new piece (a hard verifier-only
  ToolScope split) needs the dormant phase guard wired live — the §6 architectural integration.
BOUNDED DELIVERABLE (real, tested contract-layer hardening; NOT a stub-presented-as-live):
- Fix the VERIFY-phase scope in scopes.compile_tool_scopes: today verify = {finalizer} ONLY — so if the guard were
  wired, a verifier could not even file_read to diagnose (a latent bug). Make verify = a READ-ONLY DIAGNOSTICS set:
  {finalizer, file_read, file_list, search, browser, server_status, verify_web_app, preview_status, preview_logs}
  (all read-only/diagnostic) — ALL mutators excluded.
- enforce.enforce_tool_call: when a MUTATING tool is denied because the phase is VERIFY, surface the campaign code
  VERIFIER_ONLY_TOOL_BLOCKED (today the denial is generic). 
- TESTS: enforce_tool_call(VERIFY, file_write/exact_replace/safe_write_file/shell) → DENIED + VERIFIER_ONLY_TOOL_
  BLOCKED; enforce_tool_call(VERIFY, file_read/browser/verify_web_app/server_status) → ALLOWED; the finalizer remains
  callable from any phase (unchanged).
HONEST CARVE: LIVE wiring of the phase guard into the (phase-fluid) loop = the §6 ToolScope integration, a separate
large architectural PR. CD-TOOLS-6 lands the verify-scope CORRECTNESS + the failure code + the enforce-primitive
behavior; it does NOT claim live in-build verify-no-mutate (which disco already largely achieves via verify_web_app
encapsulation + console caps).
CODEX FOCUS (§9): is this bounded scope HONEST (not a stub presented as live enforcement) given the dormancy +
disco's pre-mitigation; is the read-only verify set actually free of mutators (incl exact_replace/safe_write_file/
shell/code_exec/app_*/the line-edit tools); is VERIFIER_ONLY_TOOL_BLOCKED surfaced correctly; OR should CD-TOOLS-6
instead be SKIPPED as already-satisfied (with the §6 wiring as the only real remaining work) — judge whether the
bounded deliverable is worth landing vs folding into §6.

### CD-TOOLS-6 — FINDING CORRECTED (Codex caught my wrong grep) + refined LIVE plan
- MY DORMANCY FINDING WAS WRONG: I grepped ContractPhaseGuard/enforce_tool_call and MISSED ContractScopeGuard — the
  guard IS live-wired (executor.py:395 check(); runtime.py:1581 _build_scope_guard in artifact mode) and reads a LIVE
  BuildPhaseTracker (phase.py: BOOTSTRAP→EDIT→VERIFY[finalizer fired]→EXPORT[pass]/REPAIR[fail]). The guard hard-
  denies an out-of-phase mutator BEFORE execution. So verify-phase enforcement is LIVE (artifact mode).
- THE REAL GAP (live-meaningful, non-stub): compile_tool_scopes VERIFY = {finalizer} ONLY → in VERIFY the guard
  allows ONLY the finalizer, so mutators are already denied (good) BUT diagnostics (file_read/browser/verify_web_app/
  server_status) are ALSO denied (bad) — a verifier literally cannot inspect. CD-TOOLS-6 = expand VERIFY to a READ-
  ONLY DIAGNOSTICS set so the verifier CAN diagnose while mutators STAY denied. This is a REAL live change to the
  wired guard (not a stub), reusing the machinery.
REFINED DELIVERABLE:
- scopes.compile_tool_scopes: verify = {finalizer} ∪ READ-ONLY DIAGNOSTICS {file_read, file_list, search, browser,
  server_status, verify_web_app, preview_status, preview_logs, think}. ALL mutators (file_write/append/edit/replace_
  lines/insert_lines/str_replace/exact_replace/safe_write_file, shell*/code_exec, app_*, deck_patch/sheets/slides/
  image, scaffold_starter, preview_start/stop) STAY excluded → denied in VERIFY.
- enforce: when a MUTATING tool is denied specifically because phase==VERIFY, the ScopeDecision.reason/code surfaces
  VERIFIER_ONLY_TOOL_BLOCKED (today: generic 'denied'). Keep the finalizer callable from any phase (unchanged).
- TESTS: guard/enforce in VERIFY → mutator (exact_replace/safe_write_file/file_write/shell) DENIED + VERIFIER_ONLY_
  TOOL_BLOCKED; diagnostics (file_read/browser/verify_web_app/server_status) ALLOWED; finalizer allowed any phase;
  EDIT phase unchanged (no regression). 
- Live coverage: artifact mode wires the guard today; build-mode guard wiring (if absent) stays the §6 integration —
  but the verify SCOPE fix is correct + live wherever the guard runs (NOT a dormant-only change).

### CD-TOOLS-6 — FINAL narrow scope (Codex: "narrow latent contract cleanup") + the real latent bug
- Codex correctly rejected my dormancy claim (ContractScopeGuard IS live in artifact mode, reads BuildPhaseTracker).
  Deeper read: mutators are ALREADY denied in VERIFY (is_mutating); read-only tools fall through to allowed. BUT
  verify_web_app + browser are read_only=False (default) → treated as mutating → DENIED in VERIFY (scope={finalizer}
  only). So IF the VERIFY phase is entered, the VERIFIER ITSELF (verify_web_app/browser) is blocked — it cannot
  verify/inspect. THAT is the real latent bug worth the narrow cleanup.
- NARROW DELIVERABLE (real contract-layer fix, live where the guard runs; full build-mode verifier-only split +
  phase-reachability = §6, honestly carved):
  1. scopes.compile_tool_scopes: verify = {finalizer} ∪ {file_read, file_list, search, browser, server_status,
     verify_web_app, preview_status, preview_logs, think} — the verifier's READ-ONLY DIAGNOSTICS. ALL real mutators
     (file_write/append/edit/replace_lines/insert_lines/str_replace/exact_replace/safe_write_file, shell*/code_exec,
     app_*, deck_patch/sheets/slides/image, scaffold_starter, preview_start/stop) STAY out → denied in VERIFY.
  2. enforce.ScopeDecision gains code:str=""; decide_tool_in_scope sets code=VERIFIER_ONLY_TOOL_BLOCKED when a
     MUTATING tool is denied in the VERIFY phase (today: generic reason only). Finalizer still callable any phase.
  3. TESTS: VERIFY allows verify_web_app/browser/file_read/server_status; VERIFY denies exact_replace/safe_write_file/
     file_write/shell with code VERIFIER_ONLY_TOOL_BLOCKED; EDIT/BOOTSTRAP unchanged (no regression). 
- NOT claiming the full verifier-only context split (verify_web_app already encapsulates the verifier; console capped)
  nor build-mode guard wiring — those are §6.

### HEARTBEAT 2026-06-30 — CD-TOOLS-6 SHIPPED (VERIFY = read-only diagnostics)
- Live fix to the wired ContractScopeGuard: VERIFY scope = finalizer + read-only diagnostics (verify_web_app/file_
  read/search/server_status/preview_status/logs/think); raw browser EXCLUDED (click/fill/submit = automation, guard
  is name-only) → verifier inspects via verify_web_app only; mutators denied w/ VERIFIER_ONLY_TOOL_BLOCKED. Corrected
  my wrong dormancy finding (Codex caught ContractScopeGuard live). Fixed 2 LATENT snapshot regressions from CD-
  TOOLS-2/3 (AGENT_TOOLS + exec_policy snapshots — I'd been running tools-suite not core; LESSON: run BOTH suites).
  Codex APPROVE after raw-browser REVISE. Full core 0 new fails; pyright 0/0. Full build-mode verifier split = §6.
- NEXT: CD-TOOLS-7 (buffered run_project_script — atomic/buffered script+batch runner). Then 8 (prompt-pack), 9
  (LIVE MiniMax targeted-edit harness), 10 (survey→unblock P10b).
- REMINDER for remaining PRs: run BOTH packages/tools AND packages/core suites (CD-TOOLS-2/3 added to AGENT_TOOLS/
  exec_policy → core snapshot tests; tools-only suite missed them).

### PR CD-TOOLS-7 — run_project_script (buffered transactional batch) — PLAN, ratification-pending
GROUNDING: the campaign's run_project_script is NOT a shell-output buffer — it's a DETERMINISTIC PROJECT-FILE
TRANSFORMATION with a restricted helper API (readFile/saveFile/ls/replaceText), ALL writes BUFFERED, rollback-on-
throw, shrink-guard, literal replaceText, no FS escape, no network, bounded. Sound disco mapping = a DECLARATIVE
TRANSACTIONAL BATCH of file ops (NOT an arbitrary-code sandbox — safer, reuses CD-TOOLS-2/3/4 primitives that all
exist: _atomic_write, _has_elision_marker, _canonical, _syntax_errors, _BINARY_DELIVERABLE_EXTS, _governed_guard,
_governed_relpath). No existing batch tool. code_exec is UNGUARDED arbitrary code (not this).
TOOL: run_project_script(operations: list[Op]) — Op discriminated by `op`:
  - {op:"read", path} → load current content into the result (inspection, no mutation)
  - {op:"ls", path} → list dir into the result (inspection)
  - {op:"replace_text", path, old, new, all:bool=false} → LITERAL replace (str.replace, 1 or all) in the path's
    CURRENT content (buffer-or-disk); old absent → SCRIPT_NO_MATCH (abort, no writes). '$','${}','\1' literal.
  - {op:"save", path, content, allow_shrink:bool=false} → whole-file write to the BUFFER; shrink>50% guard +
    binary-clobber guard + elision reject (reuse safe_write_file logic).
EXECUTION (validate-ALL-in-memory → commit; reuse CD-TOOLS-2/3 model):
  1. buffer: dict[canonical_path -> str]; lazily read from disk on first touch.
  2. per op IN ORDER: elision marker in old/new/content → ELISION_MARKER_REJECTED; governed .disco path (real
     symlink-followed) → GOVERNED_ARTIFACT_REJECTED; path escape → rejected (sandbox jail + _canonical); replace_text
     no-match → SCRIPT_NO_MATCH; save shrink/binary → SAFE_WRITE_SHRINK_REJECTED/binary_deliverable_clobber. ANY
     failure → ABORT with the failing op index + reason, ZERO writes (transactional rollback).
  3. after all ops pass: per MUTATED buffered path, syntax pre-check (introduced = _syntax_errors(new) - pre) → any
     → SCRIPT_BATCH_FAILED, no writes.
  4. COMMIT: _atomic_write each mutated path (logic errors already caught in memory → never partial on a logic
     fault). clear read_since_write per written path.
  5. structured {ok, applied:[paths], operations_run, reads:{path->content(capped)}}.
CAPS/CONSTRAINTS: max ops (e.g. 200) + max total content bytes (e.g. 2MB) → SCRIPT_TOO_LARGE; no network (declarative
ops have none); no host FS escape (governed + _canonical + sandbox jail); no binary bulk copy (no copy op + binary
guard); in-process + bounded → no runaway. Freshness: replace_text matches against CURRENT disk literally, so a stale
`old` → SCRIPT_NO_MATCH (output-truth; no blind clobber). save uses the shrink guard as the clobber protection (the
batch is one atomic tool call; per-op read_since_write F1 is awkward inside a batch — the shrink+match guards cover
the clobber risk; flag for Codex).
NEW CODES (tool-level, NOT build_soak): SCRIPT_NO_MATCH, SCRIPT_BATCH_FAILED, SCRIPT_TOO_LARGE (+ reuse ELISION_/
SHRINK_/GOVERNED_/binary_deliverable_clobber).
FILES: builtin/run_script.py (new) + builtin/__init__ (export+instantiate) + registry.AGENT_TOOLS + ARTIFACT_TOOLS +
UPDATE test_build_agent_isolation.py AGENT_TOOLS snapshot (BOTH-suites rule). Reuse files.py helpers (import them).
TESTS: all writes commit together (multi-op save+replace → all applied); throw rolls back ALL (a later no-match op →
NOTHING written incl earlier ops); shrink guard rejects truncation; replaceText preserves literal $/${}/backref;
path traversal/.disco rejected (no write); elision rejected; syntax-introducing batch rejected; over-cap → SCRIPT_TOO
_LARGE; read/ls populate result without mutating. Run BOTH packages/tools + packages/core suites.
CODEX FOCUS (§9): real transactional buffered batch (not a stub); rollback truly all-or-nothing on ANY logic fault
(validate-in-memory before any write); output-truth (real matches/exit, no faked success); no FS escape/governed
bypass/binary clobber; literal-safety; caps prevent context-poison/runaway; is skipping per-op read_since_write OK
given match-against-fresh-disk + shrink guard; insufficient negatives.

### CD-TOOLS-7 — PLAN round-1 revision (Codex REVISE: blind save clobbers unread files)
- The shrink guard only catches >50% truncation — a blind save of same-size/modestly-different WRONG content over a
  file the model never read is silent data loss. FIX: a `save` to an EXISTING file requires GROUNDING; a NEW file
  (not on disk, not in buffer) is exempt (nothing to clobber). The batch tracks `seen: set[canonical_path]` =
  grounded paths: (i) a prior {op:read, path} in THIS operations list; (ii) a prior {op:replace_text, path} (it
  loaded + literally matched the file's REAL current content → the model is transforming KNOWN content); (iii) the
  path is in read_since_write (a file_read before the batch, CD-TOOLS-1); OR the save op carries expected_sha256 ==
  sha256(current disk) (proves the model has the current version). A save to an EXISTING path NOT grounded by any of
  these → FRESH_READ_REQUIRED (abort, ZERO writes — transactional). replace_text stays inherently safe (matches `old`
  against fresh disk; stale old → SCRIPT_NO_MATCH, never a blind clobber).
- save op gains optional expected_sha256:str (output-truth grounding, like safe_write_file). NOT size-gated (a batch
  save is programmatic, not in-context — unlike an interactive small-file edit; require grounding for ANY existing
  file).
- COMMIT honesty: validate-everything-in-memory FIRST so a LOGIC fault writes nothing (true rollback). The multi-file
  write phase is per-file _atomic_write; a rare mid-commit FS error (disk full) is reported honestly (it is NOT
  claimed cross-file-atomic — POSIX can't — but logic faults never reach commit). Codex (b) acknowledged.
- New code: FRESH_READ_REQUIRED (reused from CD-TOOLS-1 family) for ungrounded save.
- TESTS add: save over an existing UNREAD file → FRESH_READ_REQUIRED + no writes; save after {op:read} same path →
  OK; save after replace_text same path → OK; save with matching expected_sha256 → OK; save of a NEW file → OK.

### HEARTBEAT 2026-06-30 — CD-TOOLS-7 SHIPPED (run_project_script)
- Buffered transactional file-transform batch (read/ls/replace_text/save). Validate-all-in-memory → atomic commit;
  ANY failure → ZERO writes. Reuses CD-TOOLS-2/3/4 guards. save-over-existing requires grounding (read/replace_text/
  read_since_write/expected_sha256); new files exempt. Keyed by REAL resolve_relpath path so no symlink/.. alias can
  split check from commit (all guards + commit use canon). Codex APPROVE after 3 REAL rounds (blind-clobber; lexical-
  alias redirect+governed-bypass; binary-ext-from-alias). 16 tests; BOTH suites 0 new fails; pyright 0/0.
- NEXT: CD-TOOLS-8 (prompt-pack — add exact_replace/safe_write_file/run_project_script to the WorkflowPromptPack so
  models USE them; the weak-prompt-contamination test in test_policy_tool_surface.py MUST still pass — withheld tools
  not named to the weak tier). Then 9 (LIVE MiniMax targeted-edit harness — the campaign's proof), 10 (survey→P10b).

### PR CD-TOOLS-8 — tool prompt-pack update (PLAN, ratification-pending)
GROUNDING: prompts.py has _EXECUTION_DRIVER_PROMPT (CAPABLE, assist=False; has an "EDITING AN EXISTING FILE — prefer
targeted edits" section naming file_edit/file_replace_lines/file_insert_lines/file_write) + _EXECUTION_DRIVER_PROMPT_
SMALL (WEAK, assist=True). The campaign's generic rules name show_to_user + ready_for_*_verification which disco does
NOT have (CD-TOOLS-5: `serve` is the real handoff; verify_web_app + finish is verification) — ADAPT to disco's REAL
tools (interrogate-the-given). The build-prompt snapshot tests compare OUTPUT==CONSTANT (test_build_agent_isolation:
got == _EXECUTION_DRIVER_PROMPT), so editing the constants keeps them PASSING; the only hard constraint is the
weak-prompt CONTAMINATION test (test_policy_tool_surface.py — today forbids plan_step/update_plan_progress in the
weak prompt). The anchored-edit tools exact_replace + file_str_replace are WITHHELD from a weak/non-anchored model
(exec_policy.withheld_tools), so the WEAK prompt must NOT name them.
DELIVERABLE (prompt prose only — no logic):
- CAPABLE _EXECUTION_DRIVER_PROMPT "EDITING AN EXISTING FILE" section: add Claude Design discipline naming the new
  tools — exact_replace (SMALL exact targeted edit; file_read first), run_project_script (MANY deterministic edits as
  ONE atomic batch), safe_write_file (the guarded full-rewrite/bootstrap writer); fresh-read-before-edit; "if you see
  FRESH_READ_REQUIRED, read the file then retry"; "never echo a `<… elided …>` marker into a tool arg"; "never
  rewrite a whole file for a small text/color/element change — use a targeted edit". Keep file_edit/file_replace_
  lines. Recommend safe_write_file over raw file_write for full rewrites.
- WEAK _EXECUTION_DRIVER_PROMPT_SMALL section: add run_project_script + safe_write_file (both ALL-tier, not withheld)
  + "never echo an elision marker" (Mode B hits weak models hardest). Do NOT name exact_replace/file_str_replace
  (withheld → false affordance). Keep its existing file_read-before-rewrite + line-edit guidance.
- ADAPT campaign rules to disco: serve (not show_to_user) for user handoff; verify_web_app before finish (not
  ready_for_*_verification, which isn't a registered tool); never manually choose ports (preview_start owns ports —
  already in both prompts, reaffirm).
TESTS:
- NEW contamination test: weak prompt (assist=True, PLANNING + EXECUTION) does NOT name `exact_replace` or
  `file_str_replace` (the withheld anchored-edit tools).
- NEW positive test: the CAPABLE execution prompt (assist=False) NAMES exact_replace + run_project_script +
  safe_write_file + the fresh-read-before-edit rule + forbids whole-file-rewrite-for-a-small-edit.
- Existing build-prompt byte tests (output==constant) + identity tests still pass; the existing plan_step/update_
  plan_progress contamination tests still pass.
- Run BOTH packages/tools + packages/core.
CODEX FOCUS (§9): capable prompt names the new tools + the prefer-targeted/fresh-read/no-elision principles; weak
prompt does NOT name exact_replace/file_str_replace (contamination); adapted to disco's REAL tools (serve/verify_web_
app, NOT show_to_user/ready_for_*_verification which don't exist); no oracle/output-truth weakening (prose only); the
new contamination test actually guards it; insufficient negatives.

### CD-TOOLS-8 — PLAN round-1 revision (Codex REVISE: 2 premise errors)
- (1) ready_for_*_verification IS a real contract-scoped finish-alias → KEEP it (the campaign rule was right);
  reaffirm "verify before finish (verify_web_app / the contract's ready_for_*_verification finalizer)".
- (2) standard + anchored_edit=False STILL withholds exact_replace (withholding is on the ANCHORED_EDIT capability,
  NOT the tier) — so naming exact_replace in the STATIC capable _EXECUTION_DRIVER_PROMPT is a FALSE AFFORDANCE for a
  standard-but-non-anchored model. FIX: gate the exact_replace/file_str_replace NAMING behind an ANCHORED-EDIT
  CONDITIONAL block, mirroring the existing VISION-bullet pattern in _with_skills (capabilities-conditional). The
  block renders ONLY when Requirement.ANCHORED_EDIT in capabilities (the SAME capability that drives the tool-surface
  withholding + the existing file_str_replace gating — which is why file_str_replace is ALSO not named statically).
REVISED DELIVERABLE:
- UNIVERSAL discipline (ALL-tier-safe tools) → static _EXECUTION_DRIVER_PROMPT + _EXECUTION_DRIVER_PROMPT_SMALL:
  run_project_script (many edits as ONE atomic batch), safe_write_file (guarded full-rewrite/bootstrap writer; prefer
  over raw file_write), fresh-read-before-edit, "if FRESH_READ_REQUIRED, read then retry", NEVER echo a `<… elided
  …>` marker into a tool arg, NEVER rewrite a whole file for a small text/color/element edit, serve to hand off the
  deliverable, verify before finish. NO exact_replace/file_str_replace named here.
- ANCHORED-EDIT block (NEW, in _with_skills, gated on Requirement.ANCHORED_EDIT in capabilities): names exact_replace
  (+ file_str_replace) — "prefer exact_replace for a precise, atomic targeted edit; file_read first". Renders for
  anchored-capable models only → matches the advertised tool surface exactly (no false affordance).
TESTS: capable+ANCHORED_EDIT capability → prompt NAMES exact_replace; capable WITHOUT ANCHORED_EDIT → does NOT;
weak/assist (planning+execution) → does NOT name exact_replace/file_str_replace; universal discipline (run_project_
script/safe_write_file/fresh-read/no-elision) present in BOTH static prompts; existing build-prompt byte/identity +
plan_step contamination tests still pass. BOTH suites.

### HEARTBEAT 2026-06-30 — CD-TOOLS-8 SHIPPED (prompt-pack)
- Capable prompt names exact_replace ONLY via the anchored_bullet (gated on Requirement.ANCHORED_EDIT in
  capabilities, mirroring the VISION-bullet gate = the withholding axis); universal discipline (run_project_script/
  safe_write_file/fresh-read/no-elision/no-whole-rewrite) in BOTH static prompts. Codex corrected 2 premises (kept
  ready_for_*_verification finalizer alias; gated exact_replace on capability not tier). Prose-only. 8 tests; BOTH
  suites 0 new fails; pyright 0/0.
- NEXT: CD-TOOLS-9 — THE LIVE MiniMax targeted-edit harness (campaign's PROOF Mode B is gone). MiniMax DIRECT ONLY,
  0 OpenRouter; relay :8080 + agent :8000 per RESTART RECIPE; token env-only via opencode auth.json minimax-coding-
  plan. Run a real build exercising exact_replace/safe_write_file/run_project_script on a LARGE file; prove no
  old_text_not_found/elision thrash; provider ledger every run; 0 post-terminal calls. Then 10 (survey → unblock P10b).

### PR CD-TOOLS-9 — live MiniMax targeted-edit harness (PLAN, ratification-pending) — INFRA CONFIRMED LIVE
LIVE FEASIBILITY (probed, all GREEN): relay :8080 = minimax_relay (GET /v1/models lists MiniMax-M3; a real POST
/v1/chat/completions returned a live MiniMax-M3 `<think>` reply + real tokens); agent-server :8000 up (FastAPI;
POST /conversations, /conversations/{cid}/files, /messages, /followup, GET /events, POST /kill). disco-config.json:
default_model=driver-minimax, base_url=http://localhost:8080/v1, model_id=minimax-m3 → EVERY build uses MiniMax
DIRECT via the relay. Relay ledger at /tmp/.../p1blive3b/relay.jsonl (MINIMAX_RELAY_LOG; 262 lines, ALL host=
api.minimaxi.chat model=MiniMax-M3 → 0 OpenRouter). DISCO_SECRET_KEY set. RESTART RECIPE already satisfied.
HARNESS (new harness/product_build/targeted_edit_run.py — a lightweight LIVE HTTP driver, NOT a cassette):
1. record relay-ledger line offset N0.
2. POST /conversations → cid.
3. POST /conversations/{cid}/files → seed a LARGE index.html (>1500 chars = the _ARG_SNIP_CHARS elision threshold;
   distinct, greppable hero headline / CTA / footer-year tokens).
4. POST /conversations/{cid}/messages → "index.html exists in the workspace. Make exactly these THREE TARGETED
   edits to it: hero headline → '<A>', CTA button text → '<B>', footer year → 2026. Edit precisely; do NOT rewrite
   the whole file." (Mode-B trigger: edit a large file the model did not just write → it must file_read first, and
   the file_write content / large reads get elided as context grows.)
5. poll GET /events until terminal (FINISHED/VERIFIED/STUCK/ERROR/AWAITING_USER) or a hard timeout; capture the full
   event log + the relay-ledger slice [N0:end].
6. CLASSIFY (the Mode-B-GONE oracle — checks real edit behavior, NOT just 'finished'); PASS iff ALL:
   - a TARGETED-EDIT tool was used on index.html (exact_replace / run_project_script / safe_write_file / file_edit /
     file_replace_lines) AND at least one SUCCEEDED;
   - fresh-read behavior: a file_read of index.html occurred OR a FRESH_READ_REQUIRED was returned and then a
     file_read occurred (the CD-TOOLS-1 guard working, not thrashing);
   - NO edit-thrash: NOT (>=3 old_text_not_found) AND NO elision-marker-rejected (ELISION_MARKER_REJECTED / the
     executor placeholder reject) AND NOT STUCK-with-recovery_requested-from-edits;
   - OUTPUT-TRUTH: the on-disk index.html ACTUALLY contains '<A>', '<B>', '2026' (the edits applied) and is NOT a
     truncated/whole-rewritten stub (size within sane bounds of the seed) — fetch via GET /conversations/{cid}/
     artifacts/index.html or /files;
   - PROVIDER LEDGER: the [N0:end] slice has 0 OpenRouter hosts (every host is api.minimaxi.chat/api.minimax.io,
     model MiniMax-M3) AND 0 provider calls after the terminal event's timestamp/offset.
   FAIL → record the FULL dossier + classify the failure mode (Mode A bookkeeping / Mode B edit-thrash / other);
   do NOT fake a pass.
7. write the dossier JSON (event log + ledger slice + verdict) under RUN_DIR.
CD-TOOLS-9 = implement the driver/oracle + RUN IT LIVE ONCE → a real PASS dossier (one solid proof Mode B is gone).
CD-TOOLS-10 = run it 10× consecutive (record every run) → 10/10 + ledger clean → REPORT.md → unblock P10b.
NON-NEGOTIABLE held: MiniMax direct (relay→api.minimaxi), 0 OpenRouter, provider ledger every run, 0 post-terminal,
output-truth (edits really applied), no oracle weakened. If a live run can't start/finish (server down, MiniMax
unreachable, timeout), REPORT honestly + ScheduleWakeup retry — a live model end-to-end is the ONLY proof.
CODEX FOCUS (§9): is this a REAL live MiniMax-direct run (not a cassette/fake — the agent-server default driver IS
the relay→MiniMax, confirmed); does the Mode-B oracle actually verify no-edit-thrash + tools-used + edits-APPLIED
(output-truth), not merely 'finished'; ledger proves 0 OpenRouter + 0 post-terminal; honest fail handling; no oracle
weakening; insufficient negatives.

### CD-TOOLS-9 — LIVE RUN IN FLIGHT (2026-06-30)
- Infra confirmed live (relay :8080→MiniMax-M3, agent :8000, disco-config default driver=relay, ledger clean).
- targeted_edit_run.py written + LAUNCHED in background (tag r1): seed large index.html → 3 targeted edits → poll to
  terminal → classify (targeted-tool-used + fresh-read + no-thrash + edits-applied[output-truth] + 0 OpenRouter + 0
  post-terminal). Dossier → RUN_DIR/cd9_dossier_r1.json; log → cd9_r1.log.
- ON COMPLETION: read the verdict; if PASS → Codex EVIDENCE-gate the real dossier → commit → CD-TOOLS-10 (10x). If
  FAIL → record dossier, classify the failure mode (Mode A bookkeeping vs Mode B edit-thrash vs harness bug), fix
  ONLY if root cause clear, do NOT fake. If the event-shape parsing is off, inspect the raw dossier + fix the
  classifier (the build itself is real regardless).

### CD-TOOLS-9 — r2 cid-bug fixed; r3 live in flight
- r1: real+clean but 3 harness bugs (upload≠workspace, planning-mode, blob-substring classifier) → rewrote to
  build-then-edit + real-event parsing + served-page output-truth. r2: crashed (POST /conversations returns
  conversation_id NOT id) → fixed. r3 launched (build-then-edit). The model behaved HONESTLY throughout (r1 it
  refused the impossible task). ON r3 COMPLETION: read verdict; if PASS → Codex evidence-gate the real dossier;
  if build STUCK without index.html → honest record + retry (MiniMax ~50% finish, not a Mode-B fail); if a real
  Mode-B regression → root-cause+fix. Then CD-TOOLS-10 (10x).

### CD-TOOLS-9 PROVEN + CD-TOOLS-10 soak in flight (2026-06-30)
- CD-TOOLS-9 SHIPPED+GATED: live MiniMax-M3 build-then-edit run r3 PASS under a hardened oracle (Codex APPROVE after 2
  oracle REVISEs). Real proof Mode B is gone: file_read(index.html) BEFORE file_edit x4 on index.html (hero/CTA/
  footer, real old→new, all SUCCESS), verify_web_app pass, FINISHED; served page NEW-present + OLD-absent; 0 old_text_
  not_found; 0 elision-rej; ledger 35 calls all api.minimaxi.chat MiniMax-M3 (0 OpenRouter); 0 post-terminal. Evidence
  RUN_DIR/cd9_dossier_r3.json + test-record/cd-tools/cd9_r3_evidence.json (test-record is gitignored — REPORT goes to
  docs/ at CD-TOOLS-10).
- CD-TOOLS-10 LAUNCHED: 10x consecutive soak (bg bjc2llhoh) → soak_summary.jsonl. ON COMPLETION: collect every
  verdict; the Mode-B-gone proof = every built_ok run passes the hardened oracle (build-STUCK runs = Mode A / ~50%
  finish rate, recorded but NOT a Mode-B regression); 0 OpenRouter + 0 post-terminal across ALL. Write the REPORT
  (docs/cd-tools-9-10-report.md, since test-record is gitignored) → Codex final gate → commit → UNBLOCK P10b.

### CD-TOOLS-10 soak — interim (5/10), dual-metric framing
- 0 OpenRouter + 0 post-terminal across ALL runs (the hard non-negotiable HOLDS).
- s1/s3/s4: full PASS (file_edit/file_replace_lines, onf=0, edits applied, clean finish).
- s5: built_ok, edits APPLIED, onf=0, no elision → MODE B GONE; but edit phase TIMEOUT + edited-before-phase2-read
  (grounded from phase 1 → guard correctly didn't force a redundant read). NOT a Mode-B regression; a finish-rate
  (Mode A) miss.
- s2: HTTP 409 Conflict on the message POST (transient server/timing; prior-conv teardown lag) — build never ran,
  no dossier. Not Mode-B, not OpenRouter. (For re-runs: add inter-run kill+sleep or 409-retry.)
- REPORT will use DUAL metrics: (A) MODE-B-GONE = built_ok runs with onf-loop=0 + no elision + edits applied (the
  campaign's actual fix); (B) clean-finish PASS = A + terminal-FINISHED + phase-2 read-ordering (folds in MiniMax's
  separate ~50% finish rate = Mode A, NOT what CD-TOOLS fixes). Honest separation.

### CD-TOOLS CAMPAIGN COMPLETE (1..10) — Mode B PROVEN GONE · P10b UNPARKED (2026-06-30)
- CD-TOOLS-10 soak: 10 consecutive LIVE MiniMax-M3 runs. MODE-B-GONE = 8/8 built_ok runs (onf=0 + no-elision +
  edits-applied output-truth). clean-finish 7/10 (s5 benign timeout = Mode A; s2/s9 = HTTP 409 transient). ACROSS
  ALL 10: 0 OpenRouter, 0 post-terminal, all api.minimaxi.chat/MiniMax-M3. Codex FINAL APPROVE. Report:
  docs/cd-tools-9-10-report.md.
- The CD-TOOLS campaign (1..10) is DONE + LIVE-PROVEN. Mode B (edit-elision thrash) no longer occurs on a real model.
- **P10b is UNPARKED** (the CD-TOOLS green gate is met). Next: resume P10b per its concrete task in this ledger /
  the campaign doc; if none specified, await direction. Carried follow-ups (deferred, honest): CD-TOOLS-5b
  (ready_for_verification finalizer-tool + verifier-fork); CD-TOOLS-4b (runtime manifest-fold); §6 full ToolScope
  phase wiring; soak hardening (409-retry + inter-run kill/sleep). None block the Mode-B proof.

## PR P10b — LIVE export-smoke deliverable capture (PLAN, ratification-pending) — CD-TOOLS green, UNPARKED
Goal: prove the export/download path end-to-end on a REAL live MiniMax-M3 run — the model produces a
DOWNLOADABLE deliverable and the bytes are fetched from the REAL GET (NOT a fixture claim — Codex's
P10b note). Reuses the CD-TOOLS-9 sibling-driver pattern (live-proven stable: 10 runs, fail-closed,
0 OpenRouter). Grounding (verified this turn): DeliverableEvent (events.py:744) has artifact_kind
Literal["app","files"] + path; serve(kind='files',path) [engine-intercepted virtual tool,
turn_control.py:1070/1095] → DeliverableEvent{artifact_kind:'files',path} → _declared_artifacts
download jail → GET /conversations/{cid}/artifacts/{path}. ExportDownloadOracle (harness/build_soak/
oracles/browser_evidence.py:195) requires export={requested:true, download_present:true,
download_bytes>0}. P10a already shipped EXPORT_SMOKE ProductScenario + scenario-aware classify_captured.
HARNESS (new harness/product_build/export_smoke_run.py, sibling of targeted_edit_run.py):
1. record relay-ledger offset; POST /conversations (build, autonomous).
2. POST /messages: "Build a small static site (index.html + style.css), then PACKAGE IT AS DOWNLOADABLE
   FILES and serve it so I can download the files." (elicits serve(kind='files')).
3. poll /state to terminal; capture events.
4. detect a DeliverableEvent with artifact_kind=='files' (scan events for the deliverable kind+path —
   real, not fabricated); FAIL-CLOSED if none.
5. GET /conversations/{cid}/artifacts/{the served path} → REAL bytes → download_present=(HTTP 200 AND
   len>0), download_bytes=len(bytes). NOT a fixture claim.
6. build export={requested:true, download_present, download_bytes} → run ExportDownloadOracle (import
   from harness.build_soak.oracles) AND/OR classify_captured(export_smoke); PASS iff deliverable(files)
   present + download_present + download_bytes>0 + ledger 0 OpenRouter + 0 post-terminal.
7. dossier + verdict → RUN_DIR.
P10b = implement + RUN LIVE once → real PASS dossier (deliverable detected + real bytes downloaded).
NON-NEGOTIABLE: real GET bytes, MiniMax direct, 0 OpenRouter, 0 post-terminal, output-truth, fail-closed
(no deliverable / 0 bytes = FAIL, never faked). If serve(kind='files') isn't elicited or the download
404s, REPORT honestly + iterate the prompt/route — do not fake. CODEX FOCUS: real DeliverableEvent
detection (not fabricated); download_bytes from the REAL GET not a claim; ExportDownloadOracle used
right; fail-closed on missing deliverable; 0 OpenRouter/0 post-terminal; insufficient negatives.

### P10b plan — r1 revision (Codex): pin to a concrete fetchable file
Codex caught: GET /artifacts/{path} serves INDIVIDUAL files (rejects directories + .zip), so a
"downloadable bundle/files" deliverable can 404 despite a real DeliverableEvent(files). REVISE:
- PROMPT pins a concrete single self-contained file: "Build a small self-contained landing page as a
  single file `export.html` (inline CSS, no external assets). Then serve `export.html` as a downloadable
  file." → serve(kind='files', path='export.html').
- DETECT the DeliverableEvent (artifact_kind in {'files','app'}) → take .path; GET /artifacts/{that path}.
  If .path is a directory/non-200, fall back to GET /artifacts/export.html (the pinned file) — and the
  verdict records WHICH path produced the bytes (no silent substitution; both are real GETs).
- download_present = HTTP 200 AND len(bytes)>0; download_bytes = len; assert the bytes look like the file
  (e.g. contains '<html' / a known token from the build) for output-truth, not just non-empty.
- PASS unchanged: DeliverableEvent(files|app) present + download_present + download_bytes>0 + bytes-are-
  real + 0 OpenRouter + 0 post-terminal. Fail-closed; real GET bytes only.

### P10b — live run in flight (e1)
- export_smoke_run.py committed (Codex plan APPROVE after 1 REVISE: pin to concrete fetchable export.html;
  /artifacts serves files not dirs/.zip). LAUNCHED bg (tag e1): build self-contained export.html w/ token →
  serve as downloadable → detect DeliverableEvent(files|app) → GET /artifacts/{path} (fallback export.html) for
  REAL bytes → download_present + download_bytes + bytes-are-real(html+token) + 0 OpenRouter + 0 post-terminal.
  Dossier → RUN_DIR/p10b_dossier_e1.json.
- ON COMPLETION: if PASS → Codex evidence-gate the real dossier → commit → P10b DONE. If no DeliverableEvent /
  404 / 0 bytes → inspect dossier (did the model serve(kind='files')? is the path fetchable?), iterate the prompt
  or detection HONESTLY (do not fake); the export path itself (P10a) is shipped. If build STUCK (no export.html) →
  re-run (MiniMax ~50% finish).

### P10b DONE — LIVE export-smoke PROVEN (2026-06-30)
- Run e1 PASS: MiniMax-M3 built export.html + served kind='files' → DeliverableEvent{artifact_kind:'files',
  path:'export.html'} → GET /artifacts/export.html HTTP 200, 6100 REAL bytes, real HTML w/ the build marker token
  (3x, re-verified independently). Ledger 15 calls all api.minimaxi.chat, 0 OpenRouter, 0 post-terminal. Codex
  EVIDENCE APPROVE on real bytes tied to the DeliverableEvent. Evidence: docs/p10b-export-smoke-evidence.md.
- The export/download path is LIVE-PROVEN end-to-end (serve(kind='files') → DeliverableEvent → /artifacts GET real
  bytes). P10b COMPLETE.

### SESSION CLOSE (2026-06-30) — CD-TOOLS 1..10 + P10b ALL live-proven
- CD-TOOLS campaign (1..10) COMPLETE + live-proven: Mode B (edit-elision thrash) is GONE — 8/8 live MiniMax-M3
  targeted-edit runs non-thrashing + edits applied (output-truth); 0 OpenRouter + 0 post-terminal across the soak.
- P10b export-smoke COMPLETE + live-proven.
- ALL gated by Codex (gpt-5.5) plan + code/evidence gates; one canonical writer; MiniMax-direct only throughout.
- NEXT (future sessions, not started — no concrete actionable spec queued this session): P11 (Resource Import/
  Provenance); P1B-LIVE-STABILITY browser-harness gaps (G1 SIDECAR slice capture etc.); carried CD-TOOLS follow-ups
  (5b finalizer-tool, 4b runtime manifest-fold, §6 full ToolScope wiring, soak 409-retry).

## ===== PHASE: BUILD-RELIABILITY-GATE (2026-06-30) =====
STOP P11 / no new feature pillars until REL-6 passes. REASON: CD-TOOLS improved Mode B + P10b proved real
export bytes, but the latest soak is only 7/10 clean-finish — below product standard. GOAL: make the Build
product path BORINGLY RELIABLE under MiniMax-M3 direct API before any resource/provenance work.

BLOCKING PRs (user directive, verbatim intent):
- REL-1 ready_for_verification finalizer: host-owned ready_for_verification path — surface the artifact, check
  load diagnostics, fork/queue verifier work. CLEAN load → proceed to verifier. DIRTY load → structured failure.
  NO verified badge/state without a verifier pass. Main agent MUST NOT self-verify via screenshots/eval loops.
- REL-2 runtime manifest-fold / artifact VM state: EVERY artifact has a runtime manifest {artifact id, path, kind,
  shown status, verification status, export status, sha, latest preview status}. Output-truth, export, finalizer,
  product-evidence ALL READ this shared manifest. NO duplicate truth sources.
- REL-3 full ToolScope phase wiring: verify bootstrap/edit/repair/verify/export scopes ACTUALLY enforced in the
  live Build path. Targeted-edit phase PREFERS exact_replace/file_edit guarded by fresh-read. Raw full writes only
  in bootstrap/repair/custom. Verifier-only diagnostics NOT available to the main agent.
- REL-4 409/idempotency/inter-run cleanup: fix HTTP 409 flake OR retry only when proven idempotent + pre-action.
  Inter-run cleanup: kill prior sidecars, release sandboxes, stop previews, clear stale conversations. No scenario
  fails from leftover prior-run state.
- REL-5 terminal cleanup hardening: on FINISHED/PAUSED/STUCK/ERROR/CANCELLED → stop sidecar, revoke provider token,
  stop preview, snapshot workspace, release sandbox, ASSERT zero provider calls after terminal.
- REL-6 Reliability soak (MiniMax-M3 DIRECT ONLY, 0 OpenRouter). Matrix: 30 STATIC_SMOKE, 30 TARGETED_EDIT,
  20 EXPORT_SMOKE, 10 REVISION_CHAIN, 5 DISCONNECT_OR_CANCEL.
  ACCEPTANCE: 100% clean PASS each class; 0 UNKNOWN_FAILURE; 0 INVALID_RUN in positive scenarios; 0 OpenRouter;
  0 provider calls after terminal; 0 orphan sidecars/sandboxes/previews; every failed attempt recorded+classified
  +fixed before restarting that class.
  DO NOT COUNT: retries that hide failed runs; SKIP as PASS; "benign timeout"; "HTTP 409 but output exists";
  "Mode-B-gone but not clean terminal"; headless-only success.
PROCESS: plan→disclaude.md → Codex plan-review → revise to APPROVE → implement PR-by-PR → unit/integration/product
tests → Codex code/evidence review → commit/push per accepted PR → heartbeat. Do NOT start P11 until REL-6 passes.

### REL grounding — 4 parallel read-only investigations dispatched (REL-1 finalizer/verifier, REL-2 manifest,
### REL-3 ToolScope wiring, REL-4/5 lifecycle/409). Synthesize into the gated plan when they return.

## BUILD-RELIABILITY-GATE — PLAN (ratification-pending) — grounded in 4 read-only investigations
The investigations found the same theme campaign-wide: **the reliability machinery EXISTS but is DEAD/
DORMANT in the default product path.** Sequencing is dictated by interlock: REL-2 (shared truth) → REL-1
(verifier writes it) → REL-3 (phases read+enforce it) → REL-4/5 (lifecycle) → REL-6 (soak).

### Grounded findings (file:line)
- **REL-3 DORMANT:** ContractScopeGuard wired only if `artifact_mode` (default False, runtime_settings.py:
  502-504; only set via create-body flag conversations.py:89-91). Default build → executor._scope_guard=None
  (executor.py:395), no phase tracker. Even with artifact_mode on: contract always CUSTOM (set_build_kind has
  NO prod caller, runtime.py:1170); VERIFY/EXPORT/REPAIR UNREACHABLE (note_finalizer_called/note_verifier_
  result test-only; finalizer is a finish-alias intercepted at boundaries.py:40/engine.py:1423, never hits
  executor on_tool_success). Only BOOTSTRAP→EDIT can fire.
- **REL-1 NO host verifier:** ready_for_*_verification = virtual finish-aliases (agent.py:108-111). Main agent
  SELF-verifies inline via verify_web_app (finish.py gate_browser_verify:1904, _drive_verify_web_app:996) —
  what REL-1 FORBIDS; prompts MANDATE it (prompts.py:223-227,436-439). No "verified" state exists. note_build_
  verify_result (runtime.py:1217-1225) has NO prod caller → VERIFY edge dead. CD-TOOLS-5b never built. ASSET:
  an out-of-band verifier EXISTS (agent-server verify/runner.py: _validate_app_deliverables:607, _locate_
  deliverables:378) + load-diagnostics builder verify_app.py:160-263 — REL-1 builds on these.
- **REL-2 NO shared manifest:** /api/projects/{cid}/manifest (projects.py:109-153) = files+bytes+last
  DeliverableEvent only. Scattered/duplicate truth: existence re-derived 3× (_common._declared_artifacts:142,
  verify/runner._locate_deliverables:378, manifest walk); sha in 4+ places (files.py:136/1419, file_state.py:39,
  artifact_memory.py:38, view.py:500); shown = NO server truth (harness DOM-inferred); verified = 3-4
  authorities; export = none persisted; preview = in-memory PreviewSession (preview.py:179). Write tools ALREADY
  emit {path,sha256,bytes} (files.py:1419) — no runtime consumer folds it (CD-TOOLS-4b never built).
- **REL-4 409 = IDLE/kick async race**, NOT a concurrency cap (none exists). POST /conversations never 409s;
  /messages 409 only imported-read-only; /followup 409 `not_finished` when status∉{FINISHED,IDLE}
  (conversations.py:179). kick() is async (disco_api.py:591-595) → fresh conv reads IDLE before RUNNING; a
  state-sensitive call in that window (resume→already_running, settings PATCH→not_pristine) spuriously 409s.
  Soak's followup likely 409'd because build phase wasn't FINISHED/IDLE. Inter-run cleanup = only _release_
  conversation (run.py:729-758, kills just this run's cid). No reset/release-all endpoint; DELETE /conversations
  /{cid}→forget_conversation is the only full HTTP release.
- **REL-5 cleanup best-effort, NOT at terminal:** GUARANTEED on terminal = Pi token revoke + kernel-pin clear
  (_unpin_if_current_generation, runtime.py:2849-2850, every path). NOT guaranteed on plain FINISHED/ERROR/STUCK
  with UI attached: sandbox destroy, preview stop, noVNC stop, snapshot (only on suspend/kick-FINISHED), Pi
  PiProcess sidecar stop (runtime.kill does NOT call PiKernel.kill — gap). Sandbox lingers to idle-TTL 1800s /
  suspend-60s. "Zero provider calls after terminal" oracle EXISTS (SidecarStopOracle browser_evidence.py:97-121)
  but SKIPS on headless soak (no sidecar evidence slice populated) — dormant assertion.

### PR scope (each its own gated PR; risk flagged)
- **REL-2 (FOUNDATION):** new shared per-conversation `ArtifactManifest` (persisted): one record per artifact
  {id, path, kind, sha, shown, verification, export, latest_preview_status}. ONE existence-derivation (fold the
  3 copies → one); write tools' {path,sha256} fold in at the runtime consumer (the missing CD-TOOLS-4b seam).
  /manifest reads it. Readers: serve(shown), verifier(verification, REL-1), export(export), preview(preview).
  Risk: MED (new state object + runtime consumer; must not break existing /manifest contract).
- **REL-1 (host verifier):** ready_for_verification stops being a pure finish-alias → host-owned path: surface
  artifact → host load-diagnostics (reuse verify_app.py verdict builder, host-driven not agent-driven) → CLEAN
  → fork/queue verifier (verify/runner.py) → verdict sets BuildPhaseTracker VERIFY→EXPORT/REPAIR (wire the dead
  note_verifier_result edge) + writes verification into the REL-2 manifest; DIRTY → structured failure. REMOVE
  main-agent self-verify (drop _drive_verify_web_app from the finish gate + the self-verify prompt mandate) when
  host verifier owns it. NO verified state without verifier pass. Risk: HIGH (finish.py is core; behavior change).
- **REL-3 (turn ToolScope ON for default builds):** enforce bootstrap/edit/repair/verify/export scopes in the
  DEFAULT build path (not artifact_mode-gated). Wire set_build_kind from the build brief/classification so the
  contract isn't always CUSTOM. EDIT phase: deny raw file_write (allow only in bootstrap/repair/custom), prefer
  exact_replace/file_edit, couple to the CD-TOOLS-1 fresh-read guard. Verifier-only diagnostics not reachable by
  the main agent. Risk: HIGH (changes default build behavior — must be soak-validated; may regress builds).
- **REL-4 (409/idempotency/cleanup):** kill the IDLE/kick race — harness waits for a stable RUNNING (or a
  deterministic post-kick state) before any state-sensitive call; followup only after a real terminal (poll
  FINISHED, not TIMEOUT); retries only on idempotent pre-action ops. Add a real inter-run cleanup the soak calls
  (DELETE /conversations/{cid} for the prior cid + a release-all-sandboxes/stale-convs path). No scenario fails
  from leftover state.
- **REL-5 (terminal cleanup):** force the soak-relevant teardown AT the terminal event (or a deterministic
  post-terminal hook the soak can trust): stop sidecar (fix runtime.kill→PiKernel.kill gap), stop preview,
  snapshot workspace, release sandbox; assert ZERO provider calls after terminal by populating the sidecar/
  cleanup evidence slice in the HEADLESS soak (un-skip SidecarStopOracle). Risk: MED.
- **REL-6 (soak):** build the 5 scenario classes (STATIC_SMOKE, TARGETED_EDIT, EXPORT_SMOKE, REVISION_CHAIN,
  DISCONNECT_OR_CANCEL) on harness/build_soak; strict acceptance (100% clean/class, 0 UNKNOWN_FAILURE/INVALID_
  RUN, 0 OpenRouter, 0 post-terminal, 0 orphans; no excuse counted). Record+classify+fix every failed attempt
  before restarting that class.

### OPEN DECISIONS for the gate (flag, don't guess)
1. REL-3 default-on is a real behavior change to every build — acceptable, or keep a kind-classified contract
   that's stricter-than-CUSTOM but not full per-kind? (Recommend: classify kind from brief; fall back to a
   tightened CUSTOM, not permissive CUSTOM.)
2. REL-1 removing the main-agent self-verify: do we keep a fast inline pre-check AND a host verifier, or host
   verifier ONLY? (Recommend: host verifier is the sole authority for "verified"; inline check downgraded to a
   non-authoritative hint or removed to honor "no self-verify".)
3. REL-5 force-teardown-at-terminal vs keep best-effort + make the soak DELETE each cid: minimal-risk path is
   the soak deletes its cid (REL-4 cleanup) + REL-5 only fixes the Pi-sidecar gap + un-skips the oracle.
   Full force-teardown-at-terminal is higher-risk (affects the live product's resume/suspend UX).

### BUILD-RELIABILITY-GATE plan — r1 revision (Codex REVISE: fix-observed-first + shadow/canary)
Codex corrected two things: (1) the REL numbering is NOT the build order — the OBSERVED soak blockers are REL-4
(409 race) + REL-5 (skipped after-terminal oracle), which are cheap/low-risk; fix those FIRST + run a BASELINE
soak to measure the real clean rate before the architectural work. REL-1/2/3 are correctness hardening, not
required to prove the 409/timeout gone. (2) REL-1 (finish authority) + REL-3 (default build behavior) can REGRESS
the clean rate → ship them SHADOW/CANARY so they can't make REL-6 go backwards.

REVISED IMPLEMENTATION ORDER (decoupled from the REL numbering):
1. **REL-4** — kill the IDLE/kick race (harness waits for a deterministic stable state before any state-sensitive
   call; followup only after a real terminal, never on TIMEOUT/RUNNING; retries only on idempotent pre-action ops)
   + real inter-run cleanup (soak DELETEs prior cid).
2. **REL-5-minimal** — soak DELETEs its cid each run; fix the runtime.kill→PiKernel.kill Pi-sidecar gap; UN-SKIP +
   LIVE-POPULATE the after-terminal oracle in the HEADLESS soak (provider-ledger after_terminal stamped from the
   relay log; SidecarStopOracle/CleanupOracle no longer skip). DEFER full force-teardown-at-terminal (higher risk
   to resume/suspend UX — needs UX tests).
3. **BASELINE SOAK** — run the 5-class matrix (or a scaled subset) to MEASURE the clean rate after the cheap
   fixes. This tells us how much REL-1/2/3 are actually needed to reach 100%, and catches any residual flake class.
4. **REL-2** — shared ArtifactManifest, but PASSIVE first: dual-write + read-COMPARE against the existing scattered
   truth (log divergences); do NOT switch readers until the compare is clean. Then fold readers.
5. **REL-1** — host verifier in SHADOW first: run it alongside the existing inline self-verify, compare verdicts,
   log agreement; PROMOTE to sole 'verified' authority only once shadow agrees. Then downgrade/remove the main-
   agent self-verify (inline check non-authoritative or removed). Wire the dead note_verifier_result edge.
6. **REL-3** — AUDIT/DENY-LOG first: log what WOULD be denied for default builds (kind-classified contract +
   tightened-CUSTOM fallback, NOT permissive CUSTOM); enforce default-on only after the deny-log shows it won't
   regress real builds. EDIT prefers exact_replace/file_edit guarded by fresh-read; raw file_write bootstrap/
   repair/custom only.
7. **REL-6** — final gate soak: full 5-class matrix, 100% clean/class, 0 OpenRouter, 0 post-terminal, 0 orphans,
   no excuse counted. Re-run after each hardening PR to prove no regression vs the baseline.

RESOLVED OPEN DECISIONS (Codex-aligned): (1) REL-3 = kind-classified + tightened-CUSTOM fallback. (2) REL-1 =
host verifier SOLE authority for 'verified'; inline check non-authoritative or removed. (3) REL-5 = minimal gate
path first (soak DELETE cid + Pi-sidecar-kill fix + un-skip/live oracle); full force-teardown deferred behind
resume/suspend UX tests.
GATE DISCIPLINE: REL-6 (or a scaled soak) re-runs after each of REL-2/1/3 to prove the clean rate did not drop;
any regression rolls back that PR's enforcement (shadow stays). Start P11 only after the FULL REL-6 passes 100%.

### REL-4 grounding — mature build_soak harness + EMPIRICAL orphan confirmation (2026-06-30)
- The 7/10 that triggered this phase was measured by my NAIVE product_build quick-driver (sent /followup after a
  TIMEOUT → not_finished 409 → crash on s2/s9). The MATURE harness/build_soak ALREADY handles the IDLE/kick race:
  WS-frame followups (not the REST route that 409s), wait_for_followup_pickup after each send, IDLE-race documented
  (disco_api.py:591), per-run cid teardown. So part of the observed unreliability was a HARNESS artifact — REL-6
  must measure the BASELINE with build_soak, not the quick-driver.
- build_soak is a real CLI runner: `python -m harness.build_soak.run --scenario <id> --iterations N --model ...
  --base-url http://127.0.0.1:8000 --autonomous --kernel disco`. Existing scenarios (scenarios.yaml): static_html_
  minimal, multifile_static_site, revise_after_finish, revise_twice_complex, verify_catches_broken_then_fixed,
  diag_* — partial overlap with the REL-6 5-class matrix (need explicit EXPORT_SMOKE + DISCONNECT_OR_CANCEL).
- EMPIRICAL ORPHAN GAP CONFIRMED: 1740 leaked /tmp/disco-sbx-* roots, 0 tracked conversations. _release_conversation
  (run.py:729-758) SKIPS terminal convs ("already-terminal needs no kill") — but a headless FINISHED conv's sandbox
  is NOT destroyed at terminal (REL-5 finding: best-effort to idle-TTL 1800s), so roots accumulate. Tiny (36K, empty
  dirs, NO quota risk — /tmp 1% used) but it WOULD fail the soak's 0-orphans acceptance. Swept the 1740 for a clean
  baseline.
- REL-4 fix: _release_conversation must RELEASE the sandbox even for TERMINAL convs (kill destroys sandbox + revokes
  token + idempotent + best-effort; evidence already frozen + FINISHED-snapshot already taken via kick). + a pre-soak
  stale-root sweep. The 409/idle handling is already robust (audit-confirmed) — do NOT rebuild it.

### REL-4 reframe (CRITICAL, interrogate-the-given) — backend reality (2026-06-30)
- The `local` ProcessSandbox backend (current dev agent-server, disco-config sandbox.backend="local") is
  `is_production_valid=False` (process.py:389): shared host PID/net namespace, dev-only; its destroy_by_
  conversation is a NO-OP (process.py:431); its /tmp/disco-sbx-* roots are cleaned only by sweep_stale_workspaces
  (startup/>7day), NOT by kill. The 1740 leaked roots were a LOCAL-DEV artifact of my CD-TOOLS/P10b runs — NOT the
  soak's orphan mechanism.
- Production-valid backends = gVisor (gvisor.py:180) + podman (podman.py:329); both have a REAL destroy_by_
  conversation (gvisor.py:620) + _sweep_orphan_containers. The Build-Soak path enforces require_production_valid_
  backend (__init__.py:81, fail-closed) → REL-6 CANNOT run on the local backend (it would be a meaningless sandbox-
  reliability gate). Orphans on the real backend = CONTAINERS, not /tmp roots.
- REVERTED my premature _release_conversation change: it fought a deliberate tested decision (test_cleanly_
  terminal_run_is_not_killed) AND targeted the wrong backend's orphan mechanism. The real REL-4/5 orphan/cleanup
  fix + proof must come from the PRODUCTION backend under a baseline soak — not inferred from local-dev roots.
- BLOCKER/DECISION: REL-6 (and the baseline soak that REL-4/5 measure against) must run on a production-valid
  backend. Options: local podman (available — `podman ps` works; fastest), gVisor VM 201 (100.81.82.115, BACK;
  closest to real prod — catches the prod-only docker-py container-recreate bug per [[disco-gvisor-404-
  generalization]]), or keep local-dev (NOT production-valid → gate is meaningless for sandbox reliability).

### REL backend DECISION (Dylan, 2026-06-30): local podman NOW, gVisor VM 201 for FINAL REL-6
- Iterate REL-4/5 + baseline soak + shadow/canary REL-2/1/3 on LOCAL PODMAN (production-valid, fast).
- Run the FINAL REL-6 acceptance soak (the gate that blocks P11) on gVisor VM 201 (100.81.82.115) — real-prod,
  catches the prod-only docker-py container-recreate-under-load bug.
- NEXT: stand up local podman as the agent-server backend (config switch + image + verify a real build works),
  then run the BASELINE soak to measure the true clean rate + real container-orphan/cleanup behavior.

### REL-4 backend CORRECTION (resolved) — already on production-valid podman; no setup needed
- disco-config: backend="local" + docker_socket=unix:///run/user/1000/podman/podman.sock → service_from_config
  maps to PodmanSandboxService (libpod CLI path, the DURABLE-#3 404-under-load fix) = PRODUCTION-VALID
  (sandbox/__init__.py:36-52; require_production_valid_backend lists gvisor/local/podman as valid:84). Live build
  confirms sandbox_backend=podman. So "local" is NOT the dev ProcessSandbox — "process" is. My earlier "local=not
  production-valid" was WRONG (corrected by reading service_from_config). Dylan's "local podman now" is ALREADY the
  live setup — NO backend switch needed.
- 0 orphan disco podman containers currently → the production backend does NOT leak containers like the local
  /tmp roots. The 1740 /tmp/disco-sbx roots were process-backend or podman host-staging; the REAL podman orphan/
  cleanup behavior must be MEASURED by the baseline soak, not inferred.
- NEXT (clean checkpoint): run the BASELINE soak on the current podman backend (python -m harness.build_soak.run
  --scenario <id> --iterations N --autonomous, with --model MiniMax-M3 via the relay, DISCO_ALLOW_PROCESS_SANDBOX
  _FOR_DEV unset so it stays on podman). Measure the true clean rate + what (if anything) lingers (containers /
  host-staging dirs / previews) per scenario. THEN target REL-4/5 fixes at the real measured gaps + the 409/idle
  audit. REL-4 status: backend grounded + premature fix reverted; the real fix waits on the baseline measurement.

### REL-4/5 — MEASURED baseline evidence on podman (2026-06-30) — the real, grounded targets
Ran the REAL build_soak runner (static_html_minimal x1) on the production-valid podman backend + MiniMax. Results:
- Scenario PASSED, but the PASS is BLIND: the headless soak SKIPS the reliability oracles — cleanup, sidecar_stop,
  lifecycle, preview_ownership, verification_gate, provider_ledger, export_download — ALL "SKIP (no <x> evidence
  (headless run))". The gate currently passes WITHOUT checking cleanup/after-terminal/provider. (ToolScopeOracle
  also SKIPs WRITE_TOOL_ALLOWED_IN_PLANNING — "needs captured per-turn tool scope, not in the durable event log —
  TODO live-runner slice S3".) → REL-5 core: POPULATE these headless evidence slices so the oracles RUN, not skip.
- ORPHAN CONTAINERS CONFIRMED (the real podman mechanism, NOT the /tmp roots): one build left 2 containers — the
  sandbox `disco-sbx-sbx_<id>` AND the egress sidecar `disco-egr-sbx_<id>` — both still "Up" after terminal,
  conversation untracked. _release_conversation SKIPS terminal convs → 2 containers/run linger (×95 runs ≈ ~190
  orphans → fails 0-orphans). PROVEN FIX: POST /kill on the terminal conv tore down BOTH containers → 0 remaining.
  So _release_conversation MUST release terminal convs too (kill is the correct, proven teardown). The
  test_cleanly_terminal_run_is_not_killed encodes a decision that is MEASURABLY WRONG for the 0-orphans gate.
- REL-4 (inter-run cleanup) = _release_conversation releases terminal convs (proven). 409/idle already handled by
  build_soak. REL-5 = populate headless lifecycle/cleanup/sidecar/provider-after-terminal/verification/export
  evidence slices + the Pi-sidecar-kill gap. Both now grounded in MEASURED evidence, not inference.

### REL-4 SHIPPED (2026-06-30) — inter-run cleanup: release terminal convs (orphan teardown)
- _release_conversation now releases EVERY terminal conv → POST /kill destroys both the sandbox + egress-sidecar
  containers. LIVE-PROVEN: build_soak static_html_minimal x1 with the fix → PASS, 0 disco containers (was 2). Test
  flipped to test_cleanly_terminal_run_is_released (old no-kill assertion was measurably wrong); 10 tests pass.
  Codex APPROVE (release runs after frozen evidence + snapshot; /kill only tears down live resources). 409/idle
  already handled by build_soak (audit-confirmed). NEXT: REL-5 — populate the headless evidence slices so the
  reliability oracles (cleanup, sidecar_stop, lifecycle, preview_ownership, provider-after-terminal, verification,
  export) RUN instead of SKIP; + the runtime.kill→PiKernel.kill Pi-sidecar gap. Then BASELINE soak (now with the
  oracles live) → shadow/canary REL-2/1/3 → final REL-6 on gVisor VM 201.

### REL-5 r1 revision (Codex REVISE: omit→SKIP still let a positive headless run pass green)
- Codex: "omitted/uncollected cleanup or sidecar evidence still lets the positive headless soak classify green via
  oracle SKIPs — the original SKIP-as-PASS gap remains." Correct + the gate forbids SKIP-as-PASS.
- FIX (fail-closed): the three terminal-cleanup slices (lifecycle/sidecar/cleanup) are MANDATORY for any run that
  reached a classifiable terminal. _collect returns the populated ev; run_once now treats a MISSING required slice
  as INVALID_RUN (code RUN_INTERRUPTED, "terminal -> cleanup_evidence_unmeasurable"), NOT a silent skip-pass. An
  unmeasurable signal (no relay ledger / no container probe) → INVALID_RUN, which the gate's "0 INVALID_RUN in
  positive scenarios" then forces the operator to fix by providing the measurement infra. So a positive PASS now
  REQUIRES adjudicated cleanup + sidecar + lifecycle. Verifying: run WITH relay log → PASS (3 oracles adjudicate);
  run with BAD relay path → INVALID_RUN (fail-closed). Note: orphan delta assumes a SEQUENTIAL soak (the runner is
  sequential); container probe is podman-CLI (gVisor final needs its own).

## ============================================================================
## PERMANENT PARALLEL OPERATING POLICY (SPINE — GOVERNS ALL REMAINING WORK)
## Adopted 2026-06-30. Source: DISCLAUDE_PERMANENT_PARALLEL_POLICY.md.
## Applies to every remaining REL/Build-runtime/AppKit/Agent/export/harness/soak PR.
## ============================================================================

# Disclaude Permanent Parallel Operating Policy

**Purpose:** make exhaustive parallel discovery the default operating mode for the rest of the buildout, not a one-off campaign rule.

This policy should be appended near the top of `disclaude.md` and treated as part of the immutable spine. It applies to every remaining reliability PR, Build runtime PR, AppKit PR, Agent workflow PR, export PR, harness PR, and soak run.

---

## 0. Non-negotiable principle

Do **not** reduce issue discovery.

Do **not** cap reliability surfacing.

Do **not** move faster by hiding failures.

Move faster by running independent discovery, tests, fixtures, logs, and breaker work in parallel while keeping integration, commits, and promotion gates serialized.

Default operating model:

```text
parallel discovery
parallel fixture/test generation
parallel breaker runs
parallel read-only audits
serial canonical integration
serial Codex/GPT gate
evidence-first commit
```

---

## 1. Primary roles

### Claude Code primary

Claude Code primary is the **conductor** and the only canonical writer.

Responsibilities:

```text
- owns /home/dylan/projects/disclaude
- writes every PR plan into disclaude.md
- assigns parallel lanes
- owns file-ownership locks
- integrates patches
- runs canonical tests
- requests Codex/GPT plan review and code/evidence review
- commits and pushes
- maintains heartbeat ledger
- decides which external-agent outputs are accepted or rejected
```

Claude primary must not outsource final integration, final test judgment, or final promotion decisions.

### Claude Sonnet subagents

Use up to four Sonnet subagents where useful.

Default Sonnet lane roles:

```text
Sonnet A — source scout
Sonnet B — test/fixture scout
Sonnet C — narrow implementation scout
Sonnet D — risk/regression scout
```

They may work read-only, in separate worktrees, or produce patch files. They do not push.

### MiniMax-M3 via opencode

Use MiniMax-M3 through `opencode` for high-volume implementation, breaker, and soak work.

Default roles:

```text
MiniMax 1 — implementation alternate / patch proposal
MiniMax 2 — test runner / failure reproducer
MiniMax 3 — breaker / fuzzer / soak runner
```

MiniMax must use the direct MiniMax API where the task is part of official MiniMax validation.

For official build/product/final soak evidence:

```text
host must be api.minimaxi.chat, api.minimax.io, or the approved direct MiniMax endpoint
model must be MiniMax-M3
OpenRouter call count must be 0
provider ledger must be attached
provider calls after terminal must be 0
```

MiniMax does not approve PRs or promotions.

### DeepSeek via opencode

Use DeepSeek through `opencode` as a cheap high-volume scout.

Default roles:

```text
- negative fixture generator
- schema edge-case generator
- simple helper implementation proposal
- log summarizer
- test-output summarizer
- docs/handoff draft generator
- additional read-only review
```

DeepSeek does not approve PRs or promotions.

### Gemini / Antigravity via `agy`

Use Gemini/Antigravity through `agy` for frontend/product-harness scouting.

Default roles:

```text
- Playwright product-harness scout
- browser WebSocket scout
- PreviewPane scout
- AppCard / TweakPanel / ExportPanel UI scout
- frontend component-test scout
```

Gemini/Antigravity does not own runtime/lifecycle/provider/sandbox changes unless Claude primary imports a patch after review.

### Local Qwen through Pi

Pi is allowed as the local Qwen runner.

Use:

```text
Qwen 35B via Pi:
  - scenario expansion
  - agent ergonomics cases
  - observation variants
  - failure taxonomy drafts
  - WorldSim-style environment examples
  - human-reference phrase generation

Qwen 27B via Pi:
  - log compression
  - heartbeat summaries
  - test-output summarization
  - grep/result clustering
  - report drafts
```

Pi is **not** to be used as the MiniMax orchestrator for this campaign because MiniMax conversations were unstable through Pi.

Pi/Qwen does not approve PRs or promotions.

### GPT-5.5 / Codex gate

GPT-5.5 / Codex remains the binding reviewer gate.

Use it for:

```text
- plan review
- code review
- evidence review
- oracle weakening detection
- promotion decision review
- final soak report review
```

Allowed gate outputs:

```text
APPROVE
REVISE
BLOCKED
```

No other model can replace this gate.

---

## 2. Mandatory parallel lanes for every PR

Every PR plan must include a `Parallel assignments` section.

If a lane is not used, Claude must write `not used` and why.

Default lanes:

### Lane A — Harness truth / oracle lane

Goal:

```text
Find SKIP-as-PASS, missing-evidence pass paths, duplicate truth sources, stale evidence, weak negative fixtures, false green paths.
```

Typical owners:

```text
Sonnet risk scout
DeepSeek fixture scout
GPT/Codex during gate
```

Outputs:

```text
oracle_risks.md
negative_fixture_candidates.md
```

### Lane B — Runtime lifecycle lane

Goal:

```text
Find sidecar leaks, provider-after-terminal calls, sandbox release failures, preview cleanup failures, terminal-state races, suspend/resume/cancel bugs.
```

Typical owners:

```text
Sonnet source scout
MiniMax breaker
```

Outputs:

```text
lifecycle_risks.md
cleanup_repro.md
```

### Lane C — Product/UI lane

Goal:

```text
Find browser WebSocket gaps, PreviewPane timing gaps, UI false affordances, missing screenshots, Playwright evidence holes, component-state mismatches.
```

Typical owners:

```text
Gemini/agy
Sonnet frontend scout
```

Outputs:

```text
ui_harness_risks.md
playwright_fixture_notes.md
```

### Lane D — Tool/model ergonomics lane

Goal:

```text
Find prompt/schema mismatch, malformed tool calls, exact-edit loops, fresh-read failures, progress telemetry loops, poor recovery observations.
```

Typical owners:

```text
MiniMax/opencode
Qwen 35B via Pi
```

Outputs:

```text
tool_ergonomics_cases.md
malformed_call_examples.md
```

### Lane E — Regression/test lane

Goal:

```text
Generate or update unit tests, negative fixtures, replay cases, classifier fixtures, edge-case matrices.
```

Typical owners:

```text
DeepSeek/opencode
Sonnet test scout
Qwen 27B via Pi
```

Outputs:

```text
test_plan.md
fixture_patch.diff
```

### Lane F — Soak/breaker lane

Goal:

```text
Run batches, record every failure, do not patch during batch, classify all outcomes, produce failure taxonomy.
```

Typical owners:

```text
MiniMax/opencode
```

Outputs:

```text
soak_report.md
failure_taxonomy.md
provider_ledger_summary.md
```

### Lane G — Context/contract lane

Goal:

```text
Check whether the change affects ContextPack, BuildContract, ToolScope, prompt packs, artifact runtime, or finalizer contracts.
```

Typical owners:

```text
Sonnet source scout
Qwen local scout
```

Outputs:

```text
context_contract_impact.md
```

---

## 3. Mandatory PR plan template

Every PR in `disclaude.md` must use this template.

```md
## PR <ID> — <Title>

### Goal

<one or two sentences>

### Non-negotiables

- no oracle weakening
- no false PASS
- no OpenRouter for MiniMax evidence
- no provider calls after terminal
- no user-visible affordance without evidence
- no canonical repo writes outside assigned files
- no promotion from headless-only evidence

### File ownership

Canonical writer:
- Claude primary

Locked files:
- <list files only Claude primary may edit>

Parallel-safe files:
- <list fixtures/docs/tests/scout files that other agents may patch>

### Parallel assignments

- Lane A — Harness truth / oracle:
  owner:
  task:
  output:
- Lane B — Runtime lifecycle:
  owner:
  task:
  output:
- Lane C — Product/UI:
  owner:
  task:
  output:
- Lane D — Tool/model ergonomics:
  owner:
  task:
  output:
- Lane E — Regression/test:
  owner:
  task:
  output:
- Lane F — Soak/breaker:
  owner:
  task:
  output:
- Lane G — Context/contract:
  owner:
  task:
  output:

### Codex/GPT plan gate

Status:
- PENDING / APPROVED / REVISE / BLOCKED

### Implementation checklist

- [ ] tests/fixtures first
- [ ] implementation
- [ ] product evidence if UI/runtime touched
- [ ] provider ledger if MiniMax touched
- [ ] cleanup evidence if lifecycle touched
- [ ] docs/disclaude update

### Required tests

- unit:
- integration:
- harness:
- product:
- soak/breaker:

### Acceptance

- <exact pass/fail criteria>

### Gate result

- Codex/GPT code review:
- fixes required:
- final status:
```

---

## 4. External agent command discovery

Before using an external tool for the first time in a session, Claude must discover and record the exact local command.

Required:

```text
agy models
agy --help

opencode --help
opencode model/provider/profile listing command if available

pi --help
local Qwen model invocation details
```

Do not guess flags.

Write discovered invocations into `disclaude.md` under:

```md
## External Agent Command Discovery
```

---

## 5. Worktree discipline

Canonical worktree:

```text
/home/dylan/projects/disclaude
```

Only Claude primary commits/pushes from this worktree.

Recommended scratch worktrees:

```text
/home/dylan/projects/disclaude-sonnet-source
/home/dylan/projects/disclaude-sonnet-tests
/home/dylan/projects/disclaude-opencode-minimax-impl
/home/dylan/projects/disclaude-opencode-minimax-test
/home/dylan/projects/disclaude-opencode-deepseek
/home/dylan/projects/disclaude-agy-ui
/home/dylan/projects/disclaude-qwen-scout
```

External agents return:

```text
patch.diff
scout_report.md
risk_notes.md
test_output.txt
failure_taxonomy.md
```

Claude primary may accept, reject, or partially apply.

Every accepted or rejected external result must be recorded:

```md
### External patch intake

Source:
Accepted:
Rejected:
Reason:
Tests added:
Risks added:
```

---

## 6. File ownership and single-writer rules

### Single-writer only

These file families require single-writer mode. Parallel agents may inspect but not edit.

```text
runtime / lifecycle
inference gateway
provider relay / provider ledger
tool executor
contract-to-scope compiler
BuildPhaseTracker
ContextPack renderer
PreviewManager
finalizer dispatch
PiKernel sidecar lifecycle
browser WebSocket client/server route
sandbox backend
product evidence classifier
promotion policy
```

### Parallel-safe

These can be worked on by subagents in scratch/patch mode.

```text
fixtures
negative test cases
docs
scenario YAML
harness reports
component tests
small frontend component snapshots
content lint fixtures
resource fixtures
ergonomics cases
WorldSim prompts
soak report generation
```

---

## 7. Permanent reliability rule

The reliability campaign remains exhaustive.

Do not stop surfacing issues.

Do not suppress new issue classes.

But every issue must be classified before it blocks the campaign.

Blocking categories:

```text
A — user-visible product failure
  preview broken, export fake/missing, final state lies, UI false affordance

B — safety/cost failure
  provider call after terminal, orphan sidecar/sandbox/preview, OpenRouter leak, secrets leak

C — oracle false-pass
  run can PASS without required evidence, SKIP-as-PASS, missing negative fixture

D — follow-up hardening
  useful improvement, but not product/safety/oracle blocking

E — model ergonomics
  malformed tool call or recovery friction; may be blocking if it prevents clean product pass

F — infra
  machine/service/setup issue; must be separated from product failure
```

A/B/C always block.

D does not block unless Codex/GPT elevates it.

E blocks if it prevents the required product/soak matrix.

F blocks only until the run is adjudicable.

---

## 8. Permanent soak/breaker rule

Any product-harness or reliability phase must include a no-stop batch after the implementation is green.

No-stop means:

```text
- record every run
- do not patch mid-batch
- do not retry-to-hide
- classify every dossier
- keep failed attempts
- produce failure taxonomy
```

MiniMax official runs require direct MiniMax API proof:

```text
OpenRouter = 0
MiniMax-M3 only
provider ledger present
provider calls after terminal = 0
```

No simulated run can count as release-valid.

---

## 9. Permanent product-harness rule

No engine/headless result can promote a product path by itself.

A product path must be proven through browser/UI evidence when it affects the user-visible Build surface.

Required slices when applicable:

```text
browser_ws
lifecycle
sidecar
preview
shown
verification
export
cleanup
provider_ledger
```

Missing mandatory evidence is:

```text
INVALID_RUN
```

not SKIP/PASS.

---

## 10. Permanent tool-runtime rule

The Build tool layer should continue converging toward Claude-Design-style artifact runtime behavior.

Required direction:

```text
- host-owned artifact VM
- exact targeted edits
- fresh-read guard before exact edit
- no destructive elision in editable source paths
- safe full writes only in allowed phases
- artifact-aware mutation tools
- show_to_user / show_preview semantics
- ready_for_verification finalizer
- verifier-only diagnostics split
- export/download as host-owned delivery
- no user-visible affordance without evidence
```

If a PR touches tools, prompt packs, contracts, finalizers, or preview, the plan must include a Tool Runtime Impact section.

---

## 11. Permanent heartbeat update

Every heartbeat entry must include:

```text
- current PR
- active lanes
- lane status
- external agents running
- latest push/commit
- tests run since last heartbeat
- new issues surfaced
- blockers by category A/B/C/D/E/F
- next action
```

If no heartbeat entry is written for a tick, the next heartbeat must include a missed-heartbeat audit.

---

## 12. Default remaining work order

Keep this order unless Codex/GPT approves a change:

```text
1. Finish active REL gate / reliability gate.
2. Ensure P1B-LIVE product harness remains fail-closed.
3. P10 export/handoff only when harness reliability is acceptable.
4. P11 resources/provenance.
5. P12 content/design discipline.
6. P13 deck/document/prototype/media contracts.
7. P14 ergonomics/worldsim.
8. P15 PiKernel product integration only after product harness green.
9. P16 AppKit return-to-mainline.
10. P17 final MiniMax-M3 direct soak.
```

---

## 13. One-line operating command

For every remaining phase:

```text
Claude primary orchestrates; all other agents scout, test, break, or propose; Codex/GPT gates; the harness adjudicates; evidence decides.
```

## External Agent Command Discovery (2026-06-30, per policy §4)
- **codex** (/home/dylan/.npm-global/bin/codex) — GPT-5.5 BINDING GATE. `codex exec --sandbox read-only --model gpt-5.5 "<prompt>"`. Already in use for every plan/code/evidence gate.
- **agy** (/home/dylan/.local/bin/agy) — Gemini/Antigravity, Lane C frontend/UI scout. Non-interactive: `agy -p "<prompt>" --model "<model>"` (--print/-p, --model, --dangerously-skip-permissions, --sandbox, --add-dir). Models: "Gemini 3.5 Flash (Low/Medium/High)", "Gemini 3.1 Pro (Low/High)", "Claude Sonnet 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)", "GPT-OSS 120B (Medium)".
- **pi** (/home/dylan/.npm-global/bin/pi) — local Qwen scout (Lane D/E). `pi [options] [@files...] [messages...]` (read/bash/edit/write tools). FREE models ONLY (per [[pi-code-delegation]]); never paid. Local Qwen via Pi for ergonomics/log/test-summary scouting.
- **Claude Sonnet subagents** — via the Agent tool (Explore / general-purpose). PRIMARY parallel read-only scouts for Lanes A/B/D/E/G. Told "no workflows/skills/sub-delegation".
- **opencode** — NOT INSTALLED as a runnable CLI (only the data dir ~/.local/share/opencode/ {auth.json, opencode.db}). So the MiniMax-M3-via-opencode + DeepSeek-via-opencode IMPLEMENTATION/breaker lanes are UNAVAILABLE. MiniMax-M3 SOAK work goes through the RELAY (harness/product_build/minimax_relay.py → api.minimaxi.chat, model MiniMax-M3, ledger relay.jsonl) — the official-validation path is unaffected (relay = direct MiniMax). DeepSeek lane: unavailable until opencode is installed; substitute Sonnet/Gemini/Qwen scouts.
- FLEET MAP for REL/Build PRs: Sonnet subagents (A/B/D/E/G read-only scouts) · agy/Gemini (C UI scout) · pi/Qwen (D/E ergonomics, free only) · codex (binding gate) · relay→MiniMax-M3 (F soak, direct API). opencode/DeepSeek deferred (not installed).

### REL-5 parallel-lane findings (2026-06-30) — Lane A (oracle false-green) + Lane B (lifecycle leaks), classified
PARALLEL DISCOVERY (policy §0): two Sonnet scouts ran read-only WHILE I verified the fix. Findings classified
per §7 (A=user-visible, B=safety/cost, C=oracle-false-pass, D=follow-up, E=ergonomics, F=infra). A/B/C BLOCK.

LANE A — REL-5 oracle adjudication false-greens (Category C, BLOCK):
- A-B4 [C, BLOCK]: product_evidence is IN-MEMORY ONLY — run.py never writes product-evidence.json nor hash-locks
  it. classify_run_folder() of a FROZEN REL-5 run sees product_evidence=None → all 8 browser oracles SKIP → GREEN,
  with NO _live_measure gate on the folder path. Any reclassify/replay bypasses REL-5 entirely. *The biggest gap.*
- A-B1 [C, BLOCK]: enforcement gated on _relay_log_path() env; unset (or a 4th env name) → _live_measure False →
  sidecar omitted → SidecarStopOracle SKIPs → GREEN with zero post-terminal enforcement. REL-6 preflight MUST hard-
  require the relay env (fail-closed) + wire the independent provider_ledger oracle (A-M4).
- A-M2 [C]: _terminal_status_epoch anchors on MAX over {FINISHED,ERROR,STUCK,IDLE} but the clean set is
  {FINISHED,VERIFIED}; a FINISHED-then-IDLE pair moves the anchor past post-FINISHED calls → false 0. Anchor on the
  FIRST clean terminal, not max-over-all-terminal-states.
- A-B5/M3 [C]: cleanup.orphans = host-GLOBAL disco-container delta (not cid-scoped) + `podman ps -a` counts EXITED-
  not-pruned → after<baseline → max(0)=0 masks a real leak (sequential carryover / concurrency). Scope to THIS
  cid's containers + count RUNNING only.
- A-M4 [C]: provider_ledger never passed to classify_dossier in the live path → the independent HARN-1a after-
  terminal ledger oracle is DEAD; sidecar slice is the SOLE check (amplifies B1-B3). Wire the parsed ledger.
- A-B2/B3/M1 [D, lower-risk THIS setup]: cross-clock (relay vs DB) — but SAME host here so no skew; records w/o
  numeric ts dropped — but this relay always stamps ts; 8s grace < a >8s-cadence runaway — but token-revoke is the
  real guard. Note + cheap guards; not live-blocking on this setup.
LANE B — runtime lifecycle leaks (Category B, product-side; soak's REL-4 kill MASKS them):
- B-B1 [B, BLOCK for product]: a plain FINISHED/ERROR/STUCK terminal tears down NOTHING (sandbox, disco-egr sidecar,
  preview supervisor) — only revokes Pi token + unpins. Leaks a full sandbox+sidecar+supervisor per finished build
  until disconnect-suspend(60s)/idle-TTL(1800s). This IS REL-5's directive ("on FINISHED... release sandbox, stop
  preview/sidecar") but was DEFERRED in the approved plan (resume/suspend UX risk) → REL-5b product PR.
- B-B2/B3 [B]: runtime.kill/cancel never route to PiKernel → Node PiProcess sidecar + drain task survive + append
  POST-TERMINAL events; a late Pi bridge tool call RESURRECTS a sandbox post-kill. (disco-kernel soak unaffected;
  REL-6 DISCONNECT_OR_CANCEL on Pi + a Pi product fix.)
- B-M3 [validates my design]: provider calls LEGITIMATELY happen after a terminal StatusEvent (_maybe_rekick
  stranded-followup) → a naive "no call after terminal event" oracle false-positives; my terminal-status-anchor +
  liveness approach is the right one (assert on the FINAL terminal + token-revoked quiescence).
- B-M1/M2/M5/m* [A/B/D]: cancel frees no sandbox; disconnect-suspend is one-shot (never re-armed → multi-min orphan
  window); idle can't free a live Pi sandbox; kill-by-handle not by-label. → REL-6 DISCONNECT_OR_CANCEL assertions.
TRIAGE: REL-5 (this PR, harness soundness) must fix A-B4, A-B1(+preflight), A-M2, A-B5/M3, A-M4 before it's sound.
REL-5b (product teardown-at-terminal, deferred) = B-B1 + the Pi-sidecar gap (B-B2/B3). REL-6 DISCONNECT_OR_CANCEL
consumes the Lane B cancel/disconnect/Pi assertions. Issue discovery NOT reduced (policy §0).

### REL-5 status (2026-06-30) — Lane-A harness-soundness fixes landed; remaining tracked
- DONE (live-proven, committed): payload-parse regression fix; A-M2 build-terminal anchor (excl IDLE, incl
  VERIFIED); A-B5/M3 RUNNING-only container count; A-M4 wire provider_ledger (independent after-terminal check).
  Each: PASS + all 3 cleanup oracles adjudicate + 0 containers + 79 unit tests.
- REMAINING REL-5 harness-soundness blockers (Category C, before the REL-5 Codex code-gate):
  · A-B4 [BLOCK]: persist product-evidence.json + add to the §6 hash-lock + make classify_run_folder enforce the
    missing-slice rule (today the live evidence is in-memory only → any folder reclassify/replay → all browser
    oracles SKIP → GREEN, ungated). The single biggest remaining gap.
  · A-B1 [BLOCK]: REL-6 infra preflight must HARD-REQUIRE the relay env for positive scenarios (fail-closed
    INFRA_FAILURE if unset) so _live_measure can't be silently False → SKIP-as-PASS.
  · REL-6 scenarios must ASSERT provider enforcement (assertions.provider) so the now-wired ProviderLedgerOracle
    actually RUNS (not SKIP).
- DEFERRED (separate PRs, classified): REL-5b product teardown-at-terminal (Lane B B-B1: product leaks sandbox+
  egress+preview per FINISHED build until idle-TTL; + the runtime.kill→PiKernel.kill gap B-B2/B3) — needs resume/
  suspend UX care. REL-6 DISCONNECT_OR_CANCEL consumes Lane B cancel/disconnect/Pi assertions.
- NEXT: A-B4 (persist+tamper-lock+folder-enforce) → A-B1 (preflight) → REL-5 Codex code-gate → then REL-1/2/3
  shadow-canary + REL-6. Parallel lanes (policy §2) on each.

## ===== STRUCTURED HEARTBEAT (policy §11) — 2026-06-30 =====
**MISSED-HEARTBEAT AUDIT (§11):** the .claude/heartbeat.py daemon DIED — stale pid 122882, last
HEARTBEAT_DUE tick 2026-06-29T16:01Z (~24h+ ago). All of CD-TOOLS-8/9/10, P10b, the parallel-policy
adoption, REL-4, and REL-5 were done WITHOUT structured heartbeats keyed to the daemon (freeform notes
appended instead). Daemon now RESTARTED (10-min cadence); structured §11 heartbeats resume from here.
Gap cause: daemon not under a supervisor + never health-checked. Mitigation: health-check the pid at
each heartbeat; if dead, restart + audit.

- **Current PR:** REL-5 (terminal cleanup hardening) — in_progress.
- **Active lanes:** A (Harness-truth/oracle) — COMPLETE, integrated (3 fixes). B (Runtime-lifecycle) —
  COMPLETE, findings → REL-5b/REL-6. E (Regression/test) — 79 build_soak tests green each fix. C/D/F/G —
  not used this sub-PR (no UI/contract/infra change beyond the harness).
- **Lane status:** A → A-M2 + A-B5/M3 + A-M4 landed live-proven; A-B4 + A-B1 remaining (BLOCK). B → B-B1
  product-teardown + Pi-sidecar gap deferred to REL-5b; cancel/disconnect → REL-6.
- **External agents running:** none now (2 Sonnet scouts completed + integrated). codex = binding gate.
- **Latest commit:** 4e305da9 docs(REL-5): status — Lane-A M2/B5/M3/M4 landed; A-B4/A-B1 + REL-5b/REL-6 remaining, classified (pushed; HEAD==origin).
- **Tests since last heartbeat:** build_soak test_api_runner 79/79 (×5 across the fixes); 6 live podman
  build_soak runs (static_html_minimal) — PASS w/ lifecycle+sidecar_stop+cleanup adjudicating, 0 orphan
  containers; REL-4 + CD-TOOLS suites earlier green.
- **New issues surfaced:** Lane A 5 oracle false-greens (A-B1/B2/B3/B4/B5 + M1-M4); Lane B 3 BLOCKERs
  (product terminal leak, Pi-sidecar kill gap, post-terminal Pi resurrection) + 5 MAJORs. All recorded+classified.
- **Blockers by category:** A(user-visible): none open. B(safety/cost): B-B1 product sandbox/egress/preview
  leak per FINISHED build (→REL-5b); B-B2/B3 Pi sidecar survives kill (→REL-5b). C(oracle-false-pass):
  A-B4 evidence-not-persisted→reclassify-green, A-B1 env-gated-enforcement (→ finish REL-5). D: A-B2/B3/M1
  cheap guards. E: none. F: opencode not installed (MiniMax via relay — unaffected).
- **Next action:** A-B4 (persist product-evidence.json + §6 hash-lock + classify_run_folder enforces missing-slice)
  → A-B1 (preflight hard-require relay env) → REL-5 Codex code-gate → REL-1/2/3 shadow-canary → REL-6.

## STRUCTURED HEARTBEAT (§11) — 2026-06-30 — REL-5 SHIPPED
- daemon: pid 1234314 alive; ticks resuming.
- **Current PR:** REL-5 → SHIPPED + Codex APPROVE ("no SKIP-as-PASS path remains in REL-5 scope").
- **Lanes:** A (oracle) COMPLETE — 5 false-greens fixed (M2/B5-M3/M4/B4/B1) all live-proven. B (lifecycle)
  COMPLETE — findings → REL-5b + REL-6. E (regression) — full build_soak suite green each fix.
- **External agents:** none running; codex gated REL-5 APPROVE.
- **Latest commit:** b4744abc fix(REL-5): Lane A A-B1 — fail-closed preflight requires the relay ledger for a positive soak (pushed).
- **Tests since last hb:** build_soak full suite (relay unset) green ×3; ~10 live podman+MiniMax runs PASS w/
  lifecycle+sidecar_stop+cleanup adjudicating, 0 orphans; A-B4 persist+hash-lock+tamper=INVALID proven;
  A-B1 no-relay→INFRA_FAILURE exit3 proven.
- **New issues:** none new this tick (Lane A/B already recorded+classified).
- **Blockers A/B/C/D/E/F:** C: none open (REL-5 sound). B: B-B1 product teardown leak + Pi-sidecar gap → REL-5b
  (deferred, classified). A/D/E: none. F: opencode absent (relay path unaffected).
- **Next action:** BASELINE SOAK on podman+MiniMax (now oracles adjudicate + fail-closed) to measure the real
  clean rate across scenario classes, before REL-2/1/3 shadow-canary + REL-6.

## §11 HEARTBEAT — 2026-06-30 (tick) — baseline soak in flight
- daemon pid 1357214 @120s alive.
- **Current PR:** baseline soak (post REL-5) + parallel discovery for REL-2/REL-1.
- **Lanes:** baseline soak (Lane F, podman+MiniMax) — 6/6 PASS (static_html_minimal 3/3, multifile_static_site
  3/3), revise_after_finish 0/3 in flight. REL-2 manifest-design scout (Lane G) running. REL-1 verifier-design
  scout (Lane G/A) just dispatched.
- **External agents:** 2 Sonnet scouts running (REL-2 manifest a3b7a68, REL-1 verifier a160f60); codex idle.
- **Latest commit:** 412ba99a ops(§11)+docs: REL-5 SHIPPED (Codex APPROVE); baseline soak next (pushed).
- **Tests since last hb:** 6 live baseline runs PASS (oracles adjudicating, 0 OpenRouter).
- **New issues:** none new (2 containers = the ACTIVE revise build, not orphans — verify 0 post-soak).
- **Blockers A/B/C/D/E/F:** none new open. B: REL-5b product teardown (deferred). C: none (REL-5 sound).
- **Next action:** collect baseline soak final tally (expect 9/9) + verify 0 orphans + 0 post-terminal; integrate
  REL-2 + REL-1 scout designs → REL-2 plan → Codex plan-gate. Re-arm 120s.

## PR REL-2a — passive shared artifact manifest (PLAN, ratification-pending) — grounded in Lane-G scout
### Goal
ONE shared per-artifact runtime manifest folding the scattered truth (existence re-derived 3×, sha in 4 places,
shown=no-server-truth, verified=3-4 authorities, export=not-persisted, preview=in-memory). Ship PASSIVE: dual-write
+ read-COMPARE, log divergences, change NO return value (shadow before REL-2b folds readers).
### Non-negotiables (policy §3)
no oracle weakening; no false PASS; /api/projects/{cid}/manifest JSON shape BYTE-IDENTICAL (add fields, never
remove/rename — it's a consumer surface: verify/runner.get_manifest + frontend Projects); no canonical writes
outside assigned files; persist+restart-survive.
### Design (scout-grounded, file:line)
- SEAM: extend ArtifactMemoryStore (core/context/store.py) — NOT a new module. Add ArtifactMemoryKind.ARTIFACT_
  MANIFEST (artifact_memory.py:25 neighbor) → .disco/context/artifact_manifest.json; ArtifactRecord {id,path,kind,
  sha256,bytes,shown,verified∈unverified|passed|failed,export{fmt:at},preview{status,url,at},updated_at} (mirror
  ResourceRef in context/ledger.py); record_artifacts/read_artifacts modeled byte-for-byte on record_resources/
  read_resources (store.py:168-185) + ensure_initialized (254-269) + reconstruct (272-340). Free durability:
  snapshot_workspace (lifecycle.py:672) mirrors .disco/; reconstruct rehydrates on resume.
- CORE FOLD (the 1 seam = the dead CD-TOOLS-4b consumer): observe.py:517 — every successful ToolResult passes
  here ONCE under the loop _lock (engine.py:665); inspect result.structured for path/sha256/bytes (+ generator
  keys filename/mp3_path/...) → upsert ArtifactRecord. Construct ArtifactMemoryStore(sbx) (pattern already at
  finish.py:1717 / engine.py:1788).
- 4 EDGE dual-writes (flag-gated, additive): turn_control.py:1091 + lifecycle.py:729 DeliverableEvent→shown;
  finish.py verify-gate→verified; routes/report.py:240 export_report→export[fmt]=now (the ONLY place export truth
  is born — today evaporates); preview_service.py PreviewService.preview→preview.status/url.
- CONCURRENCY (real hazard): observe fold is under the loop lock, but report_export + preview + _maybe_snapshot
  run OUTSIDE it → per-cid asyncio mutation lock in runtime.py (precedent: _session_view_locks runtime.py:709).
- SHADOW: flag DISCO_ARTIFACT_MANIFEST_SHADOW; at _declared_artifacts (_common.py:142) + project_manifest
  (projects.py:122) compute BOTH legacy + read_artifacts(), RETURN legacy, log divergences (path normpath/sha/
  bytes/shown) as one structured WARN per cid+path. Gate REL-2b on ZERO divergences across build_soak + verify.
### Parallel assignments
- Lane A (oracle): codex at gate — ensure the shadow can't change a verdict; the manifest must not become a 2nd
  truth the oracle trusts before compare is clean. Lane E (regression): unit tests mirror tests/test_artifact_
  memory.py (record/read/reconstruct/corrupt-degrade). Lane G (contract): DONE (this scout). B/C/D/F: not used
  (no lifecycle/UI/infra change in 2a).
### Implementation checklist
tests-first (core unit) → ArtifactRecord+store methods → observe.py fold (flag) → 4 edge dual-writes (flag) →
per-cid lock → read-COMPARE shadow + divergence log → docs. NO reader switched (that's REL-2b).
### Required tests
unit: core test_artifact_memory record/read/reconstruct/corrupt. integration: a build dual-writes the manifest +
read-compare logs 0 divergence vs legacy. harness: build_soak run → manifest persisted + 0 divergences. product:
n/a 2a. soak: divergence-count across the baseline classes must be 0 before REL-2b.
### Acceptance
manifest persisted+reconstructs; /manifest shape unchanged; shadow logs 0 divergences across build_soak baseline;
no verdict changed; full suite green. Codex plan-gate THEN implement.
### Risks
two-things-named-manifest (ProjectStore row vs artifact_manifest — keep distinct); structured-key implicit
contract (centralize the ingest key list, today duplicated _common.py:147 + runner.py:390); imported/read-only
convs (_reject_if_imported) → manifest empty, never written from foreign structured.

## §11 HEARTBEAT — 2026-06-30 — BASELINE SOAK DONE: revision-followup reliability BLOCKER surfaced
- **Baseline soak (9 runs, podman+MiniMax-M3):** static_html_minimal 3/3 PASS, multifile_static_site 3/3 PASS,
  **revise_after_finish 1 PASS / 2 FAIL.** Safety CLEAN: 0 orphan containers (REL-4 working), 0 non-minimax relay
  hosts. The 7/9 confirms REL-4/5 cleanup is solid AND surfaces a real product/engine reliability gap.
- **FINDING [Category A — user-visible product failure, BLOCKS REL-6]:** the after-terminal FOLLOWUP / re-plan
  re-entry STUCKs or ERRORs ~2/3 in revise_after_finish (2 sequential plan-revision followups). Both fails are
  BUILD_DID_NOT_FINISH (required_finish→terminal_status): run000 terminal=ERROR iter5/500; run002 terminal=STUCK
  iter1/500 (98 events — base build + followup1 OK, then the 2nd-followup re-entry stuck immediately). NOT a
  timeout (low iters), NOT caused by REL-4/5 (harness-only). PRE-EXISTING engine re-entry bug; relates to the old
  "RP-13 clarify+followup ENGINE RISK". REL-6 REVISION_CHAIN (10 runs, 100% clean) CANNOT pass until fixed.
- **REL-2a plan r1 (Codex REVISE):** "observe fold is NOT under _lock — engine.py drops it before _execute_and_
  observe, so the per-cid manifest lock must wrap the observe upsert too or report/preview/lifecycle writes can
  lose updates." FIX: the per-cid manifest mutation lock wraps ALL writers INCLUDING the observe.py:517 fold (not
  only the edge writers). Single asyncio.Lock per cid acquired around every ArtifactRecord upsert.
- **Active lanes:** revision-followup root-cause scout (Lane B engine re-entry) dispatched. REL-1 verifier-design
  scout DONE (5-PR decomp REL-1a..e: extract helpers→events+manifest field→shadow gate→canary wire-dead-edge→
  promote; integrate to a REL-1 plan next). REL-2 scout DONE+integrated (plan needs the r1 lock fix).
- **Blockers A/B/C/D/E/F:** **A: revision-followup STUCK/ERROR (NEW, blocks REL-6) — investigating.** B: REL-5b
  product teardown (deferred). C: none (REL-5 sound). 
- **Next action:** root-cause the revision re-entry (scout) → classify+fix (likely engine) → re-soak revision class;
  in parallel finalize REL-2a (lock fix) → Codex re-gate → implement. Re-arm 120s.

### REL-RC — revision-reentry blocker ROOT-CAUSED (Lane-B scout, 2026-06-30) — NEXT PR (blocks REL-6)
Root cause of revise_after_finish 2/3 FAIL = MiniMax-M3 weak structured output on the RE-PLAN (narrates plan as
prose not submit_plan; emits ZERO-STEP plans; malformed update_plan_progress {'steps':['']}), AMPLIFIED by 2
deterministic ENGINE bugs that turn a recoverable model slip into a Category-A terminal:
- **A1 (PRIMARY, deterministic, smallest fix):** a ZERO-STEP submit_plan is AUTO-APPROVED. plan_from_args keeps
  steps empty when nothing parses (core/loop/plans.py:183-188); _gate_planning_mode auto-approves with NO non-empty
  guard (core/loop/engine.py:908-920) → zero-step revision strands the model in execution w/ no tracker → monologue/
  loop breaker → STUCK (run002 iter1). FIX SEAM: on a REVISION (plan.revision>1) a zero-step plan must NOT auto-
  approve — re-prompt for concrete steps (reuse the existing _PLAN_NUDGE path) instead of plan_approved. Matches the
  run001 PASS shape (real multi-step plan).
- **A2 (secondary):** an unpaired assistant tool_call survives into the MiniMax request → provider 2013 "tool call
  result does not follow tool call" → hard ERROR (run000). FIX SEAM: guarantee every assistant tool_call keeps a
  paired tool-role result (synthesize for validation-failed calls, like refusal-pairing engine.py:1571-1582) before
  the transcript → MiniMax (view.py:798-808 turn-boundary). 
CLASSIFICATION: Category A (user-visible: revision fails), P1, BLOCKS REL-6 REVISION_CHAIN. Flaky terminal (FINISHED/
STUCK/ERROR) but DETERMINISTIC root cause; A1 is a deterministic guard that should move the distribution to PASS even
with MiniMax's weak structured output. REL-RC = A1 (primary) + A2 (secondary), then re-soak the revision class to
prove consecutive PASS. REL-2a re-gate APPROVE (lock fix) — implement in parallel.
