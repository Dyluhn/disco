"""Skill mount policy (WPP-3).

Skills (and MCP servers) mount ONLY via the active contract — there is NO global skill
soup in the Build/Agent base context. The mounted set for a run is the contract's
declared ``skills`` (optionally unioned with an explicit, small base set the host
configures). A skill the contract did not declare is NOT mountable.

Pure policy over the CONTRACT-1 BuildContract; no runtime/MCP imports.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..contract import BuildContract


def resolve_mounted_skills(
    contract: BuildContract, *, base_skills: Iterable[str] = ()
) -> frozenset[str]:
    """The skills mounted for a run under ``contract``.

    Default ``base_skills`` is EMPTY — a contract gets ONLY the skills it declares (no
    global soup). A host may pass a small explicit base set (e.g. an always-on safety
    skill); it is unioned in, never inferred."""
    return frozenset(base_skills) | frozenset(contract.skills)


def is_skill_mountable(
    skill: str, contract: BuildContract, *, base_skills: Iterable[str] = ()
) -> bool:
    """True iff ``skill`` is permitted for a run under ``contract`` — a hard check
    against the resolved mount set (the model can't pull in an undeclared skill)."""
    return skill in resolve_mounted_skills(contract, base_skills=base_skills)
