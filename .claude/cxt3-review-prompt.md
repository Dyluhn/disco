# Codex PLAN Review — PR CXT-3 (context resolve/snip)

This is a DESIGN/PLAN review. The code is intentionally NOT written yet — judge the DESIGN, not file
presence. CXT-1 and CXT-2 are merged. Inspect packages/core/src/disco/core/events.py and view.py to
ground your judgment. Return exactly: APPROVE | REVISE | BLOCKED_CODEX_UNAVAILABLE.

The central design decision to judge:
disco already has CondensationEvent (reversible seq-range tombstone + inline summary, applied by View.of,
reversed by recover_span) and microcompact (system NO-OP tombstoning). The campaign wants an AGENT-driven
DEFERRED snip layer (mark now, execute later, only once durable state exists). The plan ADDS 3 events for
the intent layer (ContextResolvedEvent=deferred mark, ContextSummaryEvent=durable-summary record,
ContextCompactionEvent=execution bookkeeping) but REUSES CondensationEvent as the actual tombstone so
View.of filtering + recover_span are inherited (no second forgetting path).

Judge specifically:
(a) Is reusing CondensationEvent for the actual forgetting (rather than a new filtering path) correct and
    safe, or does it conflate agent-snip with system-condensation in a harmful way?
(b) Are the compaction guards sufficient to honor the campaign invariants "never compact until durable
    state exists elsewhere" (require a ContextSummaryEvent) and "unresolved failures never compacted"
    (caller-supplied protected_seqs)? Any hole?
(c) Are the 3 new events genuinely additive, or is one redundant with CondensationEvent?
(d) Is keeping the functions PURE (return events to append; loop-wiring deferred to CXT-7) acceptable?
(e) Union/adapter integrity risk from adding 3 event types?

## PR Plan
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

### Codex review
- verdict: (pending)


## Required response format
VERDICT: APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE
REASONS:
- ...
REQUIRED_REVISIONS:
- ...
