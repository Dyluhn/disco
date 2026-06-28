# Codex CODE Review — P3 / WPP-3 (skill mount policy) — IMPLEMENTED

Inspect packages/core/src/disco/core/workflows/skill_mount.py + contract/models.py (new skills field) +
contract/registry.py (appkit declares skills) + tests/test_skill_mount.py.

WPP-3 enforces "skills mount ONLY via the active contract — no global skill soup in the Build/Agent base
context." Added BuildContract.skills: tuple[str,...] = () (additive, default empty). resolve_mounted_skills
(contract, *, base_skills=()) → frozenset: the mounted set is base_skills ∪ contract.skills, base default
EMPTY (a contract gets ONLY what it declares). is_skill_mountable(skill, contract) hard-checks against it.
appkit.leadgen declares ("appkit.leadgen","cloudflare_export","design_recipe"); others declare none.

Tests: 6 (no-skills→empty mount; appkit mounts exactly its declared set; NO global soup [cloudflare_export
mountable under appkit, NOT under static.site]; undeclared skill not mountable; base_skills unioned but
default empty; skills field round-trips on the frozen contract). Full contract/workflows suite green (42).
basedpyright strict 0 errors.

Judge: (a) does this genuinely enforce "no global soup" (a skill not declared by the contract is never
mountable)? (b) is base_skills default-empty + union the right host-escape posture (no silent broadening)?
(c) is the additive skills field on BuildContract sound (no invariant break)? (d) test sufficiency. Return
APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
