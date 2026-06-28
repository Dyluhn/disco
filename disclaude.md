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
