# Codex Plan Review (ROUND 2)

You are the read-only gate for Disco Build campaign PR CXT-1 (Core context models).
You previously returned REVISE with 4 required revisions. Below is the ORIGINAL plan followed by
REVISION 1 which addresses your points. Confirm whether REVISION 1 fully resolves your required
revisions. Return exactly one of: APPROVE | REVISE | BLOCKED_CODEX_UNAVAILABLE.

Your round-1 REQUIRED_REVISIONS were:
1. Define exact enum value sets + timestamp/id field types for all models.
2. ContextPack.from_ledger must accept an explicit CompactionPolicy (not a hardcoded path).
3. Add tests for SourcePriority.default() order semantics + compaction-by-kind incl never_compact.
4. Decide enum base intentionally to match repo standard.

Verify each is resolved. Only APPROVE if all four are satisfied and the scope is still correct/minimal.

## Plan (original + REVISION 1)
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
- verdict: (pending)


## Required response format
VERDICT: APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE
REASONS:
- ...
REQUIRED_REVISIONS:
- ...
