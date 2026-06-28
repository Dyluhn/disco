# Codex PLAN Review — P6-FINALIZERS (make per-kind verification finalizers real host-truth gates)

Read "### PR P6-FINALIZERS — PLAN (refined post-scout)" in /home/dylan/projects/disclaude/disclaude.md.

Verifiable context: `finish` is a VIRTUAL tool — engine.py _finish_tool_spec/_FINISH_SCHEMA, advertised via
driver.py _finish_tool_singleton in the `virtuals` list, engine intercepts tool_name=="finish" (engine.py
~1404) → finish.py normalize_finish_step → handle_finish_path (verify-on-finish + verify_web_app + DoD = the
host-truth gate). The contracts (contract/registry.py) declare per-kind finalizers ready_for_*_verification
and the prompt packs tell the model to CALL them, but NONE are registered/virtual tools → unknown_tool (a
live false affordance). No `finish` registry tool exists.

PLAN: make the active contract's finalizer name a recognized + advertised VIRTUAL alias that routes through
the EXISTING finish gate (no new verification logic; the finalizer is the "I claim ready" signal, host
adjudicates). Coherence test that every builtin finalizer is recognized. Plain "finish" stays unchanged when
no contract governs. SCOPE OUT (tracked): VerificationLevel escalation + the cross-layer tracker/verify-edge
wire (finish is core/loop; tracker is agent-server runtime).

Judge: (a) is aliasing the per-kind finalizer to the EXISTING finish virtual+gate the right, safe design
(reuse proven host-truth path, fix the false affordance) — or does P6 require a DISTINCT finalizer mechanism
(finalizer ≠ finish)? (b) is advertising the finish virtual under the contract finalizer name + engine
recognizing it sound, and how do we avoid breaking the no-contract plain-"finish" path? (c) is scoping out
VerificationLevel-escalation + the cross-layer tracker wire acceptable for THIS PR (consistent with prior
cross-layer deferrals), or must they be in P6? (d) any risk in the engine finish-dispatch / virtual-tool
advertisement change I'm underestimating? Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS +
REQUIRED_REVISIONS.
