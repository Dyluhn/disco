# Codex CODE Review — P2 / CONTRACT-1 (core artifact-contract models) — IMPLEMENTED

NOTE on sequencing: this is built on the ISOLATED disclaude experimental branch (nothing is promoted to
mainline). The campaign's "no P2+ PROMOTION until P0/P0B/P1 green" is a release-gate; P0+P0B are complete
and P1's entire headless harness layer (provider ledger + 8 browser oracles + promotion policy + validated
evidence writer) is green + Codex-gated — only HARN-1b's LIVE Playwright spec (needs a running stack)
remains. Building P2's pure models forward on the branch is in-scope.

Inspect packages/core/src/disco/core/contract/{__init__,models.py} + packages/core/tests/test_contract_models.py.

CONTRACT-1 = pure frozen Pydantic v2 value objects (house style identical to disco.core.context / CXT-1):
ContractKind (appkit.leadgen/static.site/interactive.prototype/deck/document/workflow.output/custom),
VerificationLevel, ToolPack, EditContract, VerificationContract, ExportContract, ArtifactContract,
BuildContract (+ minimal() factory). No runtime/tool/frontend deps. The registry (CONTRACT-2) and
Contract→ToolScope compiler (CONTRACT-3) consume these next.

Tests: 7 passed (roundtrip all models, wire-value enum, extra-field reject, bad-enum reject, frozen
immutability, minimal() well-formed for every kind, defaults). basedpyright strict 0 errors.

Judge: (a) is the model set stable + faithful to the campaign's BuildContract/ArtifactContract/EditContract/
VerificationContract/ExportContract/ToolPack spec? (b) is keeping these dependency-free correct for a base
layer? (c) is minimal() a sound factory (no false affordance)? (d) test sufficiency. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
