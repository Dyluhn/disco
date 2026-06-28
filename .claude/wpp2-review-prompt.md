# Codex CODE Review — P3 / WPP-2 (kernel-neutral prompt assembly) — IMPLEMENTED

Inspect packages/core/src/disco/core/workflows/assembly.py + packages/core/tests/test_workflow_assembly.py
(+ __init__ export). Built on WPP-1 (PromptPack) + CXT-4 (render_context_pack).

assemble_workflow_prompt(*, system_prefix, prompt_pack=None, context_pack_block=None, recent_turns=()) ->
list[LLMMessage]: the SHARED assembler both kernels (DiscoKernel/PiKernel) call so product behavior can't
diverge. Order: stable system prefix → WorkflowPromptPack (system) → ContextPack block (user) → recent
turns (verbatim). The allowed tool schema is NOT a message (the driver gets it as `tools`). Pure +
deterministic; each structured part appears exactly once; optional parts (no pack / empty context block)
omitted cleanly (no empty messages).

Tests: 7 (order; each-part-once; deterministic kernel-neutral [same inputs → identical msgs]; optional
parts omitted; end-to-end composing the real static.site WPP-1 pack + a CXT-4 context-pack render → blob
contains the pack's finalizer + the context goal + the live turn). basedpyright strict 0 errors.

Judge: (a) is the assembly order + the system/user role assignment sound (pack as system, context as
user)? (b) is "each structured part once + stable prefix first" enough to guarantee the kernel-neutral +
cache-stable-prefix properties? (c) is leaving the tool schema to the driver (not a message) correct? (d)
test sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
