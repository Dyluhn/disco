# Self-Reviews

**Status: APPEND-ONLY.** One entry per hourly review. Never edit or delete a
past entry.

An hourly review is **not a checkpoint and not permission to stop.** After
writing one, continue immediately with the next campaign action.

## Required shape

Every entry must answer every field. A timestamp is not a review — the
PostToolUse hook validates these fields structurally and will not advance the
schedule until each is answered.

```text
## Review <N> — <local time> / <UTC time>

Source fingerprint: sha256:<scripts/source_fingerprint.py --which source --quiet>
Work completed since prior review:
Evidence that it actually worked:
What went well and why:
What went rough / consumed time or tokens:
Immediate process or technical correction:
Recent fixes reviewed together:
Repeated pattern detected? (yes/no):
  # if yes:
  - shared earliest broken invariant:
  - structural product/harness remedy:
  - signal that would recognize it earlier next time:
  - existing/new regression that protects it:
  - why the remedy remains target-neutral and flexible:
Overhardening check:
  - observed failure or authoritative contract requiring each open item:
  - any theoretical tail to drop:
Next action:
```

A pattern is promoted to [`RELIABILITY-PATTERNS.md`](./RELIABILITY-PATTERNS.md)
only with evidence across at least two fixes/failures, or one demonstrated
cross-cutting mechanism.

---

## Review 1 — 2026-07-25 21:58 CDT / 2026-07-26T02:58:00Z

Source fingerprint: sha256:bd1b1bba209e6b9362df1e69e360b0dd6d8b56087b147bfa72eeae81a63c8b98

Work completed since prior review: First review of the campaign; covers Epic 0
from session start. Verified the entire handoff state against the brief rather
than assuming it (branch, HEAD `271af2e6`, `f55efb03` ancestry, the three active
product paths, empty Serena memories, absent AGENTS.md, external archive
manifest). Reconciled §5 architecture vocabulary against real code. Authored the
nine-file governance surface, `scripts/source_fingerprint.py`,
`scripts/check_governance_seal.py`, five `.claude/hooks/*` scripts, and
`.claude/settings.json`. Created and fail-closed verified the Ollama-only GLM 5.2
OpenCode launcher.

Evidence that it actually worked: `sha256sum -c MANIFEST.sha256` → 33/33 OK,
exit 0. Seal gate exercised across its full state machine — exit 3 (no manifest)
→ 1 (rebaseline refused without env) → 0 (rebaselined) → 0 (verify) → 1 (drift,
named exactly) → 0 (restored). Guard mutation matrix: 11/11 mutation spellings
denied (Edit, Write, `>>`, `>`, `sed -i`, `tee`, `rm`, `mv`, `cp`,
`git checkout`, `truncate`, `python -c`, basename-only), 5/5 legitimate
operations allowed with no false positives. Review mechanism proven on real
wall-clock: interval set to 60 s at 21:56:48, not due at 21:56:28, due at
21:57:09 with injection fired, source mutation denied while overdue, ledger
write still allowed, bare-timestamp review rejected naming all 10 missing
fields with the schedule not advancing. `opencode debug config` assertion
passed: only `ollama-cloud` enabled, `opencode-go` disabled,
`baseURL=https://ollama.com/v1`, key present and never printed.

What went well and why: Verifying the hook schema against the installed binary
(2.1.220) instead of from memory paid for itself immediately — it confirmed
`permissionDecision`, `additionalContext`, the `Stop` `decision/reason` shape,
`stop_hook_active`, and match-all matcher semantics, so the hooks worked on
first run with no schema guesswork. Reconciling §5 against code before sealing
caught that `DeliveryShape`/`PreviewModality` do not exist as types; sealing
those names would have made a frozen boundary false on day one.

What went rough / consumed time or tokens: Two self-inflicted defects, both
found only because I ran adversarial controls rather than trusting the happy
path. (1) The `sed -i` guard pattern matched only `"sed -i"`, so the target path
fell outside `match.group(0)` and the check silently failed open. (2) The
completion sentinel matched its own documentation inside a fenced code block, so
the ledger reported the contract SATISFIED at Epic 0 — the Stop gate would have
let me stop immediately.

Immediate process or technical correction: Every mutator regex now spans to the
end of its shell segment so the path is always inside the tested match. The
completion check now strips fenced blocks, ignores indented/quoted lines, and
requires an exact standalone line; both a negative control (documentation
mention) and a positive control (real assertion) are proven.

Recent fixes reviewed together: the `sed -i` fail-open and the sentinel false
affordance.

Repeated pattern detected? (yes/no): yes
  - shared earliest broken invariant: a check whose *scope* is narrower than the
    claim it is trusted to make. The regex tested 2 characters and concluded
    "no protected path"; the substring test read documentation and concluded
    "contract satisfied". In both cases the mechanism reported a stronger
    negative/positive than its evidence supported.
  - structural product/harness remedy: a predicate must be evaluated over the
    full span of the thing it judges — the whole shell segment, the whole line
    in assertion position — and citation contexts (fenced, indented, quoted)
    must be excluded before a claim is read as an assertion.
  - signal that would recognize it earlier next time: any guard or gate that has
    never been run against a deliberately hostile input. Both defects appeared
    on the first adversarial control and neither was visible in normal use.
  - existing/new regression that protects it: the guard mutation matrix (11 deny
    / 5 allow) and the sentinel negative+positive control pair, both re-runnable.
  - why the remedy remains target-neutral and flexible: it constrains only how a
    predicate is *scoped*, adding no framework-, model-, or scenario-specific
    knowledge; the guard stays heuristic by design and the hash gate remains the
    authority.

Overhardening check:
  - observed failure or authoritative contract requiring each open item: the
    guard matrix entries each correspond to a real shell spelling that would
    have mutated a sealed file; the sentinel controls correspond to an observed
    false SATISFIED reading; the seal state machine corresponds to the brief's
    explicit mutation-proof requirement.
  - any theoretical tail to drop: yes — dropped. I did not pursue exotic shell
    evasions (base64-decoded writes, `exec` redirection, editor subprocesses).
    The brief explicitly forbids turning this into an adversarial shell-parser
    project, and `check_governance_seal.py` catches drift regardless of spelling.

Next action: set the review interval to 3,600 s, reconcile root `CLAUDE.md` into
a bootstrap pointing at the governance authority order, then commit only the
context/governance paths — never the three active product paths.

## Review 2 — 2026-07-25 23:00 CDT / 2026-07-26T04:00:00Z

Source fingerprint: sha256:b94c9002bbc19bd0bdead6cdeeca7ef32d1ed2398778857ce4188e58c78da4b0

Work completed since prior review: Closed Epic 0 and committed it as `32a09957`
(85 paths, three active product paths excluded by explicit pathspec). Proved hook
wiring end-to-end with two real sessions. Found and fixed a defect I had
introduced in the Stop gate. Opened Epic 1 and closed three of its named gaps:
browser-evidence horizon binding, the mutable-head fallback, and the
authority-race fence (adding resume and agent-view supersession to the existing
user-turn and run-intent checks, unified into one choke point). Then found and
fixed a defect in the pre-existing dirty Ruling-2 code that would have made the
freeze useless in production.

Evidence that it actually worked: full `harness/build_soak/tests/` suite exit 0
(~890 passed, 1 skipped) after every change. `test_freeze_before_kill.py` 11
passed. The committed `test_progressing_hardcap_freeze_order.py` reproduction
still 3 passed. Two real `claude -p` sessions attempted to mutate sealed files
via Edit/Write and via Bash and were blocked by the PreToolUse hook in the
model's own words, with the seal exit 0 and both files untouched afterwards. The
new real-path positive test asserts the freeze lands, names the exact immutable
version (`version_seq`, `tree_digest`), preserves `index.html` byte-exact and the
PNG byte-exact across the kill, retains
`RUN_TIMEOUT_WHILE_PROGRESSING`, and issues exactly one kill.

What went well and why: Two methods paid for themselves repeatedly. First,
running adversarial controls instead of trusting the happy path — every single
defect this session was found by a deliberate negative control, never by normal
use. Second, the revert-check: after fixing the raw-row bug I re-broke it on
purpose and confirmed the new test fails with exactly the production symptom
(`FREEZE_TIMEOUT != frozen`) while all 11 fake-based tests still pass. That
turned "I wrote a test" into "I proved this test protects this fix".

What went rough / consumed time or tokens: Three things. (1) My e2e probe
sessions ran with `--dangerously-skip-permissions` and made uninstructed writes
in the campaign worktree — rewriting CAMPAIGN-STATUS.md, stripping
`.serena/project.yml` comments, and creating a 212-line `scripts/glm_delegate.py`
built against a *local* Ollama endpoint, contradicting the mandated
`ollama-cloud` route. All reverted. (2) The `authority-race` GLM delegation cost
1.13M tokens and produced a report I had largely superseded by reading the code
directly. (3) I burned 27,793 tokens on an invalid negative control that could
not have failed, because `glm-run.sh` re-exports the very variable I was trying
to poison.

Immediate process or technical correction: Probe sessions now run read-only or in
a disposable copy — never with write permission in the campaign worktree.
Delegations get a narrower scope and a stated token expectation; anything I can
answer by reading one file, I read. A negative control must be checked for
whether it can actually fail before I trust its result.

Recent fixes reviewed together: the `sed -i` fail-open, the completion-sentinel
false affordance, the unbounded Stop gate, and the raw-row freeze bug.

Repeated pattern detected? (yes/no): yes
  - shared earliest broken invariant: a consumer read a *shape* the producer
    never emits. `freeze_progressing_workspace` read `status`, `trigger`,
    `version_seq`, `run_intent_id` and `agent_view_id` straight off raw SQLite
    rows, where only `seq`/`kind`/`source` are real columns and everything else
    lives inside an unparsed JSON `payload` string. Every one of those reads
    returned None, so the freeze would have reported FREEZE_TIMEOUT on 100% of
    real runs. `normalize_event`'s own docstring records a prior P0 false-negative
    with the identical shape — "a row with top-level `kind` had its payload
    dropped" — so this is the second occurrence, not an anecdote.
  - structural product/harness remedy: exactly one canonical normalizer, applied
    at every boundary where persisted rows enter logic. `normalize_events` was
    already used at four other sites in this adapter; the freeze was the sole
    path that skipped it.
  - signal that would recognize it earlier next time: a test whose fixture is
    hand-built flat dicts while the production source is a database row. The
    fake modelled a shape the real producer never emits, so it could not fail.
  - existing/new regression that protects it: the real-path positive test, which
    drives run_once against real SQLite rows and a real ProjectStore version.
    Proven by revert-check to fail on the bug while the fakes stay green.
  - why the remedy remains target-neutral and flexible: it adds no new contract
    and no scenario-specific knowledge — it routes one existing consumer through
    the normalizer the codebase already treats as canonical.

Overhardening check:
  - observed failure or authoritative contract requiring each open item: the
    horizon clip and the workspace-source registration each trace to a specific
    named gap and an observed loss (counted seeds 460004/460005); the resume and
    agent-view fences are explicit acceptance items; the subordinate-verdict test
    protects an acceptance item about not laundering the primary verdict.
  - any theoretical tail to drop: yes — dropped one. I removed a test asserting
    that an event with no usable `seq` is excluded by the horizon clip: the
    schema enforces `NOT NULL` on `events.seq`, so the durable log cannot produce
    one. The three-line defensive branch stays because it is fail-closed and free;
    asserting an impossible state does not.

Next action: finish Epic 1's remaining acceptance — the symlink and mutable-head
fail-closed identity checks around collection, and explicit confirmation that the
product pause/kill APIs and kill semantics are unchanged — then commit Epic 1 as
one coherent package and move to Epic 2.
