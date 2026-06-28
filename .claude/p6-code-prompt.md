# Codex CODE Review — P6-FINALIZERS (contract finalizer as a finish-alias) — IMPLEMENTED

You APPROVED this plan (alias the per-kind ready_for_*_verification finalizer to the existing `finish` virtual
+ host-truth gate; 6 revisions all addressed). Inspect the implementation:

engine.py:
- is_finish_tool_name(name, finish_alias) — THE single helper ("finish" always; the contract finalizer when
  a contract governs).
- _finish_alias_tool_spec(alias) — a FRESH ToolSpec (same _FINISH_DESCRIPTION/_FINISH_SCHEMA), never mutates
  the cached _FINISH_TOOL_SPEC singleton.
- AgentLoop.__init__ gains finish_alias: str|None=None → self._finish_alias.
- dispatch (~1437): the finish interceptor now uses is_finish_tool_name(...) and, if the name is the alias,
  normalizes step.tool_call.tool_name → "finish" (model_copy) before normalize_finish_step, so the gate +
  every downstream reader is byte-identical regardless of the name. (Verified finish.py does NOT branch on
  the literal "finish"; the virtual is intercepted with `continue` so the alias never enters the event log
  that signals.py/_NON_PRODUCTIVE_TOOLS read.)
driver.py: tools_for_step advertises _finish_alias_tool_spec alongside plain finish when self._loop._finish_
  alias is set (placed before the suppress_meta_tools block, so like finish it survives suppression);
  known_tool_names_for_requery adds the alias. PLANNING offers only planning tools → neither finish nor alias
  (auto-gated identically).
runtime.py: _compose_build_loop computes _finish_alias = the declared contract's verify.finalizer ONLY when
  conversation_id in self._build_kind (a real declared kind) — a plain/CUSTOM build gets None (never
  fabricates ready_for_artifact_verification); passed to BOTH AgentLoop constructions (artifact + build).
Pi scoped out → P15 (pi-kernel fixed finish allowlists untouched; documented).

Tests (8, test_finish_alias.py): helper (plain finish always; alias only with contract; other tools not
finish; EVERY builtin finalizer recognized-with-contract + NOT-without); driver (active-contract advertises
alias + keeps finish; no-contract finish-only; alias not advertised in PLANNING like finish; alias in requery
known-names, absent without contract). Updated the AGENT_TOOLS snapshot (app_* + context_memory — a TOOL-1/
CXT-2 leftover the snapshot test caught). FULL core-loop suite green; agent-server green except 4 PRE-EXISTING
env failures (test_pi_process needs node + a verify-config test — confirmed failing with my changes stashed).
basedpyright: 0 new errors (pre-existing driver.py:291 + runtime fire_now, stash-confirmed).

Judge: (a) is the alias recognized + gated CONSISTENTLY across ALL surfaces (dispatch, advertisement, requery,
planning) with no surface missed? (b) is the no-contract plain-"finish" path truly byte-unchanged? (c) is the
declared-kind-only gating correct (no fabricated CUSTOM finalizer)? (d) does normalizing the alias→"finish" at
dispatch + intercept-without-ActionEvent fully prevent the alias name leaking to signals/stuck/view readers?
(e) test sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
