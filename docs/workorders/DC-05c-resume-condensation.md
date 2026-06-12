# DC-05c — resume-time condensation of degenerate trailing segments

**Read `README.md` first. De-complexity Wave 0 follow-up to DC-05a/b.
Scope = packages/agent-server ONLY. Dispatch AFTER the Phase B regression
re-run (we want clean before/after data on DC-05a/b alone first).**

## Why (research-grounded, 2026-06-10)

DC-05b repairs the *structure* of a resumed transcript (dangling actions,
reality block, plan recitation). The research record says that is necessary
but NOT sufficient when the trailing transcript is already degenerate:

- arXiv 2505.06120: a corrective recap appended to a bad transcript recovers
  only ~77% vs ~93% from a clean context — models self-condition on their own
  degenerate history and "do not recover" in place.
- arXiv 2206.02369: repetition is self-reinforcing — every replayed repeat
  raises the probability of the next one. The attempt-3 register (one fact
  ×179) is a maximal few-shot prompt for emitting #180 (Manus: "Don't Get
  Few-Shotted").
- Every production remedy REPLACES rather than repairs: Claude Code /compact,
  OpenHands condenser, Anthropic long-running-harness guidance.

DC-05a's breakers cap future degeneration at ~3 turns, so this matters most
for (a) histories that degenerated before DC-05a, (b) anything that slips the
breakers. The journal is both state AND behavioral prompt: synthetic events
repair structure; summarize-and-replace repairs behavior.

## Existing machinery (verified file:line — reuse, do not reinvent)

- `CondensationEvent` (events.py:341-356): tombstone with
  forgotten_start_seq/forgotten_end_seq/summary/summary_role/reason; View.of
  (view.py:228-355) skips the span and renders the summary in-place.
- `reason` field accepts "request" | "tokens" | "events" | "hard_reset".
- Pinning (view.py:137-150): latest PlanEvent + KnowledgeEvent +
  DatasourceEvent seqs must never be forgotten — the detector must respect
  `_pinned_seqs` semantics (do NOT condense across a pinned seq; KnowledgeEvent
  spam is exempted from pinning protection ONLY when the events are exact
  duplicates — see Decision 3).
- `microcompact` (view.py:50-116): the deterministic-tombstone precedent
  (no model call) — follow its shape.
- Resume seam: `resume_conversation` (runtime.py:~1895) loads `events` at
  ~1917 then calls `_reconstruct_resume_context` at ~1929.

## The decided design (locked)

New private method on ConversationRuntime:
`_condense_trailing_degeneracy(self, events) -> CondensationEvent | None`
— pure on the event list, unit-testable on archived logs, NO model call
(deterministic, like microcompact).

1. **Detection** — scan the tail backwards, stopping at the first real
   ActionEvent (non-bookkeeping tool) or USER MessageEvent. Within that tail
   segment count: (a) AGENT MessageEvents, (b) KnowledgeEvents grouped by
   (scope, sha256(snippet.strip())), (c) plan-revision events. Degenerate iff
   the segment contains ≥6 events AND zero real actions AND (≥3 agent prose
   messages OR any duplicate-knowledge group with ≥3 members).
   (Threshold 6 is deliberately ABOVE DC-05a's breaker cap of 3 so a normal
   breaker-paused tail is not condensed — only pre-DC-05a histories or
   breaker escapes.)
2. **Tombstone** — forgotten_start_seq = first event of the degenerate
   segment, forgotten_end_seq = last; summary (factual, no spin):
   "[Condensed N degenerate turns: the agent repeated itself without calling
   any tools (M duplicate knowledge entries, K plan revisions). No work was
   performed in this span. Do not imitate this pattern — proceed by calling
   tools.]"; summary_role="user"; reason="hard_reset".
3. **Duplicate-knowledge pinning exemption** — `_pinned_seqs` protects
   KnowledgeEvents, which would block the span. Resolution (locked): the
   detector limits forgotten span boundaries so the FIRST instance of each
   duplicated knowledge fact stays OUTSIDE the span (it remains pinned and
   visible); only the redundant repeats fall inside. If that is impossible
   (interleaved), shrink to the largest clean suffix. Never special-case
   view.py — work within its existing contract.
4. **Wiring** — in `resume_conversation`, after events load and BEFORE
   `_reconstruct_resume_context`:
   ```python
   tombstone = self._condense_trailing_degeneracy(events)
   if tombstone is not None:
       await self._store.append(conversation_id, tombstone)
       events = await self._store.get_events(conversation_id)
   ```
   so the reconstructor and the resumed View both see the condensed log.
5. **Anti-scope** — no engine.py/view.py/frontend changes; no config knobs;
   no model-call summarization (deterministic text only).

## Acceptance ladder

1. Unit — `packages/agent-server/tests/test_resume_condensation.py` (NEW):
   - healthy tail (actions present) → None;
   - breaker-paused tail (3 prose messages then PAUSED) → None (below
     threshold — DC-05a owns that case);
   - degenerate tail (8 agent messages, 0 actions) → tombstone spanning them,
     summary counts correct;
   - duplicate-knowledge tail (1 fact ×10) → tombstone, FIRST instance's seq
     outside the forgotten span;
   - **DEFECT-4 attempt-3 replay**: load
     test-record/marathon/events-conv_c1b4675689484c63b1f45d92d45b95be-attempt3-loop-snapshot.json
     FULL register (not the first-PAUSED slice), run the detector → tombstone
     condensing the spam; then View.of(events + [tombstone]) renders WITHOUT
     the ×179 repeats and total rendered messages drop by >100;
   - integration: resume_conversation on a degenerate log → tombstone in
     store before the reconstruction events, both before RUNNING flip.
2. Run ONLY test_resume_condensation.py + test_resume_reconstruction.py +
   test_resume.py + test_lifecycle.py. Log → test-record/dc-05/units-c.log.
3. Report → agent-projects/gemini/dc-05c-report.md. No commits.

## Manifest (orders.yaml `dc-05c`)

- packages/agent-server/src/disco/agent_server/runtime.py
- packages/agent-server/tests/test_resume_condensation.py
- test-record/dc-05/units-c.log
- agent-projects/gemini/dc-05c-report.md
