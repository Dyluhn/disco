# Reliability Patterns

**Status: APPEND-ONLY.** Evidence-backed recurring bug patterns and their
structural remedies.

## Admission rule

A pattern is admitted only with **evidence across at least two fixes/failures,
or one demonstrated cross-cutting mechanism.**

Do not turn a single anecdote into a framework. Do not record theoretical
hardening ideas here — this is a ledger of what actually broke, more than once.

For each pattern prefer **one structural choke point** plus **one regression
that detects the whole family**. Do not add a keystone configuration table for
every framework or model.

---

## Standing patterns (carried in as already evidenced)

These were evidenced during earlier phases of this program. They are recorded
here as the working set an hourly review checks new fixes against.

### P1 — Lifecycle teardown ordered before durable evidence capture

**Shape.** A run is torn down (killed, stopped, cleaned) before the evidence
that proves what it did is durably captured. The evidence is then gone or
describes a state that no longer exists.

**Earliest broken invariant.** Evidence must be bound to exact event-bound
immutable state *before* the authority that produced it is dismantled.

**Structural remedy.** Freeze first, then tear down: establish the durable
marker and the immutable version, bind evidence at or before that horizon, and
only then run the ordinary teardown path.

**Signal to recognise it earlier.** A teardown path that runs before any
`*Version`/`PAUSED`-style durable marker is appended.

---

### P2 — Mutable head substituted for exact event-bound immutable state

**Shape.** Collection reads "latest", the ProjectStore head, or a symlink
instead of the exact immutable version an event named. The result looks
plausible and is silently wrong.

**Earliest broken invariant.** Evidence must reference the exact immutable
version bound to the accepted event horizon
([BOUNDARIES §7](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** Identity/digest checked around collection; symlink and
mutable-head fallback **fail closed** rather than substituting.

**Signal to recognise it earlier.** Any read path that can resolve to a head
pointer when an exact version is available.

---

### P3 — Model-facing context loses a stable host capability fact

**Shape.** A host refusal or capability constraint is stated once, then dropped
by condensation. The model repeats the forbidden operation because, from its
view, it never happened.

**Earliest broken invariant.** Host authority must survive context handling
([BOUNDARIES §9](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** Host-authored **typed** runtime constraints with stable
key, scope, lifetime/expiry, bounded guidance, and a usable alternative —
carried independently of prose summarization.

**Signal to recognise it earlier.** The same normalized error recurring after a
condensation boundary.

---

### P4 — Provider protocol / tool markup accepted as ordinary prose

**Shape.** Raw DSML or tool-call protocol markup from the provider is stored as
though it were a summary, then replayed to the model as context.

**Earliest broken invariant.** Protocol output is not prose
([BOUNDARIES §9](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** Structurally validate summarizer output; reject protocol
markup; allow one bounded repair; fall back to a truthful host-generated summary
that says what it omitted.

**Signal to recognise it earlier.** Persisted summary text containing
tool-call/protocol delimiters.

**Caution.** Ordinary HTML, JSX, and shell/code snippets are legitimate content.
A rejection rule that eats them is overhardening.

---

### P5 — Returned view and event list describe different horizons

**Shape.** A builder appends condensation and rebuilds while the materialization
path can still return pre-build events. The same span is then condensed twice.

**Earliest broken invariant.** One response, one horizon
([BOUNDARIES §8](./ARCHITECTURE-BOUNDARIES.md)).

**Structural remedy.** The returned view and its events share a single
post-condensation horizon by construction.

**Signal to recognise it earlier.** Condensation count rising faster than
distinct spans.

---

### P6 — Harness cleanup/config not using the same environment as startup

**Shape.** Startup uses one environment; cleanup or config resolution uses
another. Resources look released but are not, or config diffs appear that were
never intended.

**Earliest broken invariant.** Truth measurement must use the same environment
as the thing it measures.

**Structural remedy.** One environment contract shared by startup, cleanup, and
config binding; a config diff proves the sole intended difference.

**Signal to recognise it earlier.** A leak or config discrepancy that only
appears in the harness, never in product use.

---

### P7 — A host fallback or false classification masks the original verdict

**Shape.** A secondary failure (timeout, cleanup error, subordinate gate)
overwrites or launders the primary verdict, so the real cause disappears from
the record.

**Earliest broken invariant.** The primary verdict is authoritative; subordinate
facts are recorded *alongside* it, never in place of it.

**Structural remedy.** Subordinate outcomes are recorded as subordinate, with
the primary verdict retained verbatim.

**Signal to recognise it earlier.** A verdict field whose value can be
overwritten by a later, lesser failure.

---

### P8 — Stale campaign/test authority overriding current product contracts

**Shape.** An old plan, matrix, work order, or handoff is treated as current
status, and work is done against a target that no longer exists.

**Earliest broken invariant.** Authority order
([README](./README.md)): current code + tests + evidence beats every document.

**Structural remedy.** One finite authority surface; everything else explicitly
marked history; stale counting authorities re-authored against the actual final
bytes before anything counts.

**Signal to recognise it earlier.** A document being cited for *status* rather
than for design rationale.

---

## Campaign-observed patterns

*(Appended as they are evidenced during this campaign.)*
