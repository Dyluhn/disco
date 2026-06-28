# Codex CODE Review — P3 / WPP-1 (WorkflowPromptPack format + packs) — IMPLEMENTED

Inspect packages/core/src/disco/core/workflows/{__init__,prompt_pack.py} + prompt_packs/*.md (4 packs) +
packages/core/tests/test_prompt_packs.py.

WPP-1 = the pack FORMAT + loader. A pack is a markdown file whose `## Section` headers are slugged into a
PromptPack.sections dict; REQUIRED_SECTIONS (11) pins the mode-specific operating manual (role/
artifact_contract/workflow_steps/allowed_tools/forbidden_tools/targeted_edit_law/preview_rule/verify_rule/
export_rule/context_policy/done_criteria). parse_prompt_pack + PromptPack (frozen) + PromptPackRegistry
(loads bundled prompt_packs/<id>.md, get/require/ids). 4 real packs authored: build_static_site /
build_appkit_leadgen / build_deck / build_document — matching the prompt_pack ids the CONTRACT-2 registry
references. Pure; no runtime imports.

Key coherence test: test_every_contract_prompt_pack_resolves_to_a_complete_pack — every BuildContract's
prompt_pack id resolves to a pack with ALL required sections (workflow.output/custom legitimately have none).

Tests: 7 passed (parse, required-sections constant, loads-every-pack, all-required-sections-present, render,
missing-pack get/require, contract↔pack coherence). basedpyright strict 0 errors.

Judge: (a) is the markdown→sections parse robust (header slugging, last-section capture, no leak)? (b) is
the required-sections completeness gate the right contract for a pack? (c) the .md packs are read via
Path(__file__).parent/prompt_packs — fine for dev (PYTHONPATH→src); production wheel packaging (include
*.md as package-data) is a pyproject follow-up — acceptable to defer? (d) are the authored packs faithful to
their contracts (tools/finalizers/required-files match)? (e) test sufficiency. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
