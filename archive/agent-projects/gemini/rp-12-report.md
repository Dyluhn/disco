# RP-12 Report — B-series residue + Weak-model FC Kit + DEFECT-6

## Deviations
- **Rung 4 (Grammar)**: Implemented schema groundwork (sanitization, coercion, JSON mode support). Explicit GBNF grammar generation for llama.cpp was deferred in favor of Rung 5 (Repair) and Rung 6 (Requery), as these provide robust coverage across providers without the complexity of grammar generation.
- **Kernel Discipline**: Output path uses `/workspace/.outputs/` as requested. Tracebacks and stderr are explicitly excluded from truncation.

## Implementation Summary

### B-Series Residue
- **B2 Kernel Output Discipline**: Modified `ProcessKernel` and `GatewayKernel` in `kernel.py`. Outputs > 2000 chars are saved to `.outputs/` and returned as a head (~500 chars) + pointer stub.
- **B3 Tail Variation**: Implemented in `view.py`. `ActionEvent` (thought) and `ObservationEvent` (tool output) now rotate through 3 surface variants based on `seq % 3`. This preserves KV-cache stability (B5) while preventing model over-fitting.
- **B9 Action-Space Masking (Prefill)**: Added `assistant_prefill` to `CompletionRequest`. Implemented in `OpenAIProvider` (appends assistant message to prompt) and `RouterAgent` (injects "I've analyzed..." prefix in `PLANNING` mode when `PMX_PLAN_PREFILL=1`).

### Weak-model FC Kit
- **Rung 5 (Layered Repair)**: Implemented `_repair_json` (strips markdown fences, fixes trailing commas, escapes control chars) and `_coerce_args` (casts strings to expected types based on tool schema) in `OpenAIProvider`.
- **Rung 6 (Bounded Requery)**: Modified `AgentLoop.run` in `engine.py`. Invalid steps (unknown tools) or provider rejections (4xx) now trigger up to 2 "requery" attempts with transient corrective hints before persisting to the log.
- **Rung 7 (Invalid-tool Reroute)**: Integrated with the requery logic. Valid JSON with unknown tool names is caught, hinted, and retried. If retries exhaust, it falls through to a failed `ToolResult` as a final refusal.

### DEFECT-6 Amendment
- **4xx Handling**: Provider rejections (HTTP 4xx) are now caught in the `AgentLoop` and redirected to the requery path instead of causing a terminal conversation error.
- **Name Sanitization**: Implemented `_sanitize_tool_name` in `openai_provider.py`. Tool names are sanitized both ways: outgoing names have dots/prefixes stripped to satisfy OpenAI requirements, and incoming echoed names are stripped and mapped back to the canonical tool spec.

## Test Results
- **Units (Tools)**: `packages/tools/tests/test_kernel_session.py` — 8 passed (including new B2 discipline tests).
- **Units (Core)**:
    - `packages/core/tests/test_tail_variation.py` — 2 passed (B3 rotation/determinism).
    - `packages/core/tests/test_prefill_masking.py` — 1 passed (B9 prefill injection).
    - `packages/core/tests/test_toolcall_defense.py` — 4 passed (Rung 5 repair/coercion, DEFECT-6 sanitization).

Total: 15 passed. Logs recorded in `test-record/rp-12/`.
