# RP-12 Fix 1 Report

## F1 — ROOT CAUSE FIX (DEFECT-6)
- **Engine Fix**: Modified `packages/core/src/disco/core/loop/engine.py:1500` and `:1507` (approx) within `_execute_and_observe` to explicitly pass `tool_call_id=action.tool_call.call_id` when emitting `AgentErrorEvent`.
- **Provider Defense**: Updated `OpenAIProvider._message` in `packages/core/src/disco/core/llm/openai_provider.py` to downgrade `role: "tool"` messages to `role: "user"` if `tool_call_id` is missing/falsy, prefixing the content with `"Tool error: "`. This prevents the upstream 400 "missing field 'tool_call_id'" error.
- **Verification**: Added `test_agent_error_serialization_defense` to `packages/core/tests/test_toolcall_defense.py`.

## F2 — BLOCKER: Echoed history names unsanitized
- **Provider Fix**: Updated `OpenAIProvider._message` to apply `_sanitize_tool_name()` to the names of assistant `tool_calls` when serializing from history. This ensures that invalid names (e.g. with dots) stored in the event log don't poison subsequent request payloads.

## F3 — Stub test implementation
- **Test Fix**: Implemented `test_tool_calls_sanitization_both_ways` in `packages/core/tests/test_toolcall_defense.py`. It now verifies that:
  1. Outgoing assistant tool calls from history are sanitized.
  2. Incoming sanitized tool names from the model correctly map back to the original `ToolSpec` names.

## F4 — Turntaking failure investigation
- **Verdict**: The failure in `test_notify_user_emits_message_and_continues` was caused by the combination of withholding `notify_user` on Turn 1 (fresh session) and the new "Unknown tool reroute" logic.
- **Evidence**: 
  - On Turn 1, `_actions_since_last_resume` is 0, setting `fresh_session = True`.
  - `_tools_for_step` withheld `notify_user` when `suppress_meta_tools` (fresh_session) was True.
  - The reroute logic at `engine.py:1913` saw `notify_user` as an unknown tool and triggered a requery.
  - The `ScriptedAgent` used in the test returned its next step (`finish`) on the requery, effectively skipping the `notify_user` move.
- **Fix**: Modified `engine.py:_tools_for_step` to move `notify_user` and `ask_user` out of the withheld set. These tools are non-destructive and already protected by the `_actionless_valve` circuit breaker, so allowing them on Turn 1 is safe and preserves the intended test behavior.
- **Verification**: `test_notify_user_emits_message_and_continues` now passes.

## Evidence
- `test-record/rp-12/units-tools.log` — `uv run pytest packages/tools/tests/test_kernel_session.py -v` (PASSED)
- `test-record/rp-12/units-core.log` — `uv run pytest packages/core/tests/test_tail_variation.py packages/core/tests/test_prefill_masking.py packages/core/tests/test_toolcall_defense.py packages/core/tests/test_cluster2_turntaking.py -v` (PASSED)
