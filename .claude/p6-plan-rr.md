Re-review the 6 required revisions to PR P6-FINALIZERS (see "PLAN REVISION 1" in
/home/dylan/projects/disclaude/disclaude.md). You approved the aliasing concept; the fixes:
1. AgentLoop finish_alias core seam, passed by _compose_build_loop ONLY for a DECLARED contract kind
(conversation_id in self._build_kind) → contract.verify.finalizer; None for plain/CUSTOM (no fabricated
ready_for_artifact_verification).
2. One is_finish_tool_name(name, finish_alias) helper used by dispatch + advertisement +
known_tool_names_for_requery + planning suppression (alias behaves exactly like finish).
3. Fresh _finish_alias_tool_spec(alias) ToolSpec (same schema/desc); does NOT mutate the cached finish singleton.
4. Advertise the contract finalizer AND keep plain finish advertised+recognized (compat, documented).
5. Pi explicitly scoped out → P15 (fixed finish allowlists in pi-kernel/tools.ts + pi_tools.py untouched;
false affordance persists on the Pi path only, documented).
6. Tests: every builtin finalizer recognized; alias advertised under active contract; alias routes the finish
gate; no-contract finish unchanged; alias blocked in planning mode like finish; alias in requery known-names.
VerificationLevel escalation + tracker verify-edge explicitly NOT claimed (tracked).
Confirm these resolve your 6 revisions and the scoping. Return APPROVE or REVISE + one line each.
