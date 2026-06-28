# Codex CODE Review — P2 / CONTRACT-2 (BuildContractRegistry) — IMPLEMENTED

Inspect packages/core/src/disco/core/contract/registry.py + packages/core/tests/test_contract_registry.py
(+ __init__ export). Built on CONTRACT-1 (merged).

BuildContractRegistry maps ContractKind → a concrete BuildContract. default() registers a built-in contract
per kind (static.site, appkit.leadgen, deck, document, interactive.prototype, workflow.output, custom) with
real required files / starter kits / bootstrap+edit ToolPacks / verify finalizer / export pipeline / prompt
pack / UI card. get(kind), kinds(), get_for_brief(brief) → reads brief['kind'], unknown/missing → CUSTOM
fallback (never None, so a run always has a contract). Pure data + lookup; no runtime imports. The
CONTRACT-1 model validators (kind==artifact.kind, finalizer convention) guarantee every built-in is coherent.

Tests: 16 passed (default has every kind; every built-in coherent + round-trips; brief lookup; unknown→custom;
appkit shape; custom rewrite_allowed; register override). basedpyright strict 0 errors.

Judge: (a) are the per-kind contracts coherent + sensible (tool packs/finalizers/required-files match each
kind's real disco surface)? (b) is the get_for_brief CUSTOM-fallback (never None) the right posture, or
should an unknown kind be an error? (c) is the registry API sufficient for CONTRACT-3 (the Contract→ToolScope
compiler will consume bootstrap/edit/repair tool packs)? (d) test sufficiency. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
