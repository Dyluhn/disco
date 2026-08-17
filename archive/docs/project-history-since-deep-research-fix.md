# disco — Work Log Since the Deep-Research Sub-Question Fix

**Anchor:** `d2696ae` *tune(deep-research): synthesis quality — prompts + bound* (2026-06-06).
That was the fix for the battery-economics report that read like a primer and bounded the
actual question (commercialization/economics) out of the plan. It retuned `decompose.py`
(sub-questions must ANSWER the specific question, ordered by importance, background last) and
`synthesis.py` (cross-source synthesis, attribute vendor claims, cut hype, surface the
announcement-vs-reality gap).

**Span covered:** `d2696ae` → `e48c5bc` (HEAD), 2026-06-06 → 2026-06-09. ~75 commits, 4 days.
Organized by workstream, not commit order. Every item maps to a real commit (hash in brackets).
Where something was later superseded, it says so.

---

## 1. Deep Research — finished the surface
- **The decomposition/synthesis tune itself** [d2696ae] — the anchor fix above.
- **Deep Research UI** [09b7ece] — plan gate, live progress, assembling-report view.
- **Depth tiers wired end-to-end** [bffa904] — `depth_tier` (quick/standard/exhaustive) was being
  ignored at conversation-create; now applied via `set_depth`. (Found + fixed during this
  session's live search testing.)
- **Checkpointed resume** [e6b31a8] — a resumed Deep Research carries completed sections forward
  instead of restarting from zero.
- **ddgs rate-limit resilience** [af0fce5] — retry-with-backoff after live runs returned 0 sources.
- **Live verification this session:** all four research modes (quick / standard / standard-deep /
  exhaustive) run live and verified, with screenshots [382b73a, ddcb3ed, 3693b0c].

## 2. Build surface — matured from "proof of life" to a real product
- **Plan-first lifecycle** [7159637, ac8285d, da8e3a5] — PLANNING → submit_plan → approval gate →
  execution; planner gets read tools + markdown context; a hard execution gate refuses FINISH
  without a productive action.
- **Build UI** [2cdea75, f9d4dc5, 7074f34, 54380a7] — visible thoughts, expandable actions,
  resizable inspector, sticky plan tracker (plan + progress pinned on scroll), follow-the-stream
  auto-scroll, auto-switch to Preview on finish.
- **Previews** [43375fc, ab04beb, 53a0d7a, e68a294] — backend-aware live preview, reachable over
  Tailscale, proxied through the agent-server origin (no random ports), workspace auto-served.
- **Deliverable export** [9f750ee] — deployment_url + project manifest export.
- **Persistent Build workspaces** [42249bb, ee2ac21] — list / reopen / download; surface recovered
  from manifest after a server restart.

## 3. Long-horizon coherence — the "context survival" cluster (GAP A/B/C/D/G/H)
This was the diagnosed root cause behind most agent-loop failures (per the Manus gap analysis).
- **Coherence pass** [fbbb26b] — GAP A/B/C/D/G + structured logging.
- **Prompt-cache markers** [eb302cc] — GAP H: `prompt_cache_key` + Anthropic `cache_control`.
- **Microcompact no-op turns** [5d4e15b] — GAP A S3: reversible, no-model compaction of dead turns.
- **Escape-then-halt on stuck** [c97c1b3] — one high-temperature retry before declaring STUCK.
- **Circuit breaker** [0528e63] — after repeated distinct failures, synthesize an AlternativesEvent
  and hand off to the user instead of grinding.
- **verify_app** [391390e] — server-aware self-verification when a build finishes.
- **auto-continue recovery** [200b62e] — never freeze on an incomplete plan.

## 4. Session lifecycle — the "Cluster 1" workstream
- **Orphan reconciliation** [8bcdef9] — reconcile conversations left RUNNING after a crash on startup.
- **Resume button** [e0bd746] — explicit resume for a paused build.
- **WS auto-reconnect** [b3694a0] — backoff reconnect; a network blip no longer fatals the UI.
- **Auto-suspend idle sandboxes** [a98d224] — free an idle build's sandbox on tab-close (snapshot
  first; skip if RUNNING). Cluster 1 marked complete [adf84cf].

## 5. Two-way Ask-gate + Stop/Kill — the "Cluster 4" workstream
- **Two-way Ask-gate** [b1e0772, 6e0e8f1, 31f81d9] — AskPanel for free-form `ask_user`; offline demo
  + full-tree integration test + a real-Firefox E2E spec with screenshot evidence.
- **Planner can ask before planning** [101e1b6] — `ask_user` available in PLANNING, not just execution.
- **Graceful Stop / Kill confirmation / tabbed-away notifications** [1ee4220]. Cluster 4 complete [ba2913c].

## 6. Tools, sandbox, and memory
- **Three sandbox backends** existed just before the anchor and were hardened around it: gVisor
  [55b2c85, 2b8c1dd], Podman [e31ef00], local container [8a0aaf3], env-selectable via PMX_SANDBOX
  [d85a31e].
- **Egress proxy + preview/serve tools + filesystem-as-memory** [9b8e3ba].
- **Real persistent skills** [d0b1d4b] — `.md` skill modules wired into the agent.
- **Stateful CodeAct** [edf0185] — cross-cell namespace via pickle/dill. ⚠️ **This is the design the
  current rebuild plan replaces** — it re-serializes the whole namespace every cell (O(N·K)), which
  caused the build "hang" observed this session. See `agent-architecture-rebuild-plan.md` §B8.

## 7. Universal providers / retrieval
- **Bundled keyless providers + local encoders + deep research** [f3c89ad] — 3-tier
  (bundled/self-host/paid) search + extraction + encoders; keyless defaults so it runs with no
  external accounts.
- **Rerank memory fix** [4669a76] — cross-encoder rerank bounded 16GB → 2.3GB.
- **Reasoning-model empty-answer fix** [2188d0f] — a reasoning model returned empty grounded answers;
  fixed + canary check.

## 8. Security
- **Adversarial-review remediation** [6829685] — fixed a path-traversal + 4 other real bugs surfaced
  by an adversarial review pass.

## 9. Test & verification infrastructure
- **Harness build-out** [8ef416c] — cassette / replay / eval / fault-injection / contract / fuzz /
  canary + Makefile.
- **E2E + visual regression** [3449cbb] — Playwright E2E + visual regression (fixture mode).
- **ReplaySandbox** [f56a018] — deterministic build-surface replay.
- **Captured research cassette** [475388b] — Phase 2 `--replay` e2e green.
- **UI surfaces failures** [74a8661] — instead of failing silently.

## 10. This session's AFK directive (2026-06-09)
A single large instruction: finish every deferred item, full-test the UI + every search mode live,
then build two sites with 3 live iterations each + screenshots + a full record.
- **Deferred tracks D/B/C/A all closed** [02b11e0]; temperature kwarg threaded through fakes [1431ec9].
- **Full UI + all search modes tested live**, screenshots in `test-record/` [382b73a … 3693b0c].
- **Site 1 (macOS clone): 3 iterations** [31d7570]. **Site 2 (EE Quest): v1** [b2fd750]; v2/v3 completed
  later this session (record in `test-record/RECORD.md`).
- **The large-file editing breakthrough** — the model couldn't iterate on a big file; root cause was
  framework, not model:
  - build-finish gate requires a state-changing action [de92930]
  - **inclusive editing**: line-targeted (`file_replace_lines`/`file_insert_lines`) + forgiving
    `file_edit` [882fbcb]
  - char-budgeted **paginated file_read** so large reads aren't snipped into a corrupted middle [bab33ea]
  - These three are sound and stay.

## 11. The read-streak / force-commit bandaids — and the course-correction (the most recent work)
After the editing fixes, a series of read-loop guards were added that later analysis (and your
pushback) showed were the *wrong* class of fix:
- read-streak nudge [905ae60], both-modes [eec8377], re-fires/escalating [ecffec9],
  temperature jitter [939ce6c], and **force-commit / withhold-read-tools** [e48c5bc].
- Two genuine keepers landed in the same window: **plan-step scoping to the current plan** [c430275]
  and **snapshot-on-any-terminal-state** [56526be].
- **Status:** the read-counter family is slated for **rollback** — it penalizes familiarity and is not
  an endorsed technique (see the rebuild plan). The course-correction (this conversation) produced 3
  rounds of primary-source research and a new evidence-graded architecture plan
  (`agent-architecture-rebuild-plan.md`). c430275 + 56526be are kept.

---

### One-line shape of the 4 days
Deep Research finished → Build surface matured → context-survival coherence cluster →
lifecycle + ask-gate + stop/kill clusters → universal providers/test-harness/security →
the AFK build-and-test marathon → editing breakthrough → read-loop bandaids → caught and
redirected into a researched rebuild plan.
