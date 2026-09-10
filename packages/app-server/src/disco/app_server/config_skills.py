"""Skills repository split out of ConfigState (PY-0365): CRUD over the real,
persistent `.md` skill files (`PMX_SKILLS_DIR`). `ConfigState` exposes an
instance of this class as the plain `skills` attribute.
"""

from __future__ import annotations

from disco.core import SkillStore

from .config.dtos import SkillCreate, SkillDTO, SkillPatch


class ConfigSkills:
    """CRUD over the persistent .md skill files."""

    def __init__(self, skills_store: SkillStore) -> None:
        self._skills_store = skills_store

    @staticmethod
    def _skill_dto(s) -> SkillDTO:
        return SkillDTO(
            id=s.id,
            name=s.name,
            description=s.description,
            enabled=s.enabled,
            body=s.body,
            surfaces=s.surfaces,
        )

    def skills(self) -> list[SkillDTO]:
        return [self._skill_dto(s) for s in self._skills_store.list()]

    def create_skill(self, create: SkillCreate) -> SkillDTO:
        s = self._skills_store.create(
            name=create.name,
            description=create.description,
            body=create.body,
            enabled=create.enabled,
            surfaces=create.surfaces,
        )
        return self._skill_dto(s)

    def update_skill(self, skill_id: str, patch: SkillPatch) -> SkillDTO | None:
        existing = self._skills_store.get(skill_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                k: v
                for k, v in {
                    "name": patch.name,
                    "description": patch.description,
                    "enabled": patch.enabled,
                    "body": patch.body,
                    "surfaces": patch.surfaces,
                }.items()
                if v is not None
            }
        )
        self._skills_store.save(updated)
        return self._skill_dto(updated)

    def delete_skill(self, skill_id: str) -> bool:
        return self._skills_store.delete(skill_id)
