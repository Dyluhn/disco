"""Skills routes — real, persistent .md instruction modules (CRUD over ConfigState)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config.dtos import SkillCreate, SkillDTO, SkillPatch
from ..config_state import ConfigState


def make_skills_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/skills")
    async def get_skills() -> list[SkillDTO]:
        return state.skills()

    @router.post("/api/skills", status_code=201)
    async def create_skill(create: SkillCreate) -> SkillDTO:
        return state.create_skill(create)

    @router.put("/api/skills/{skill_id}")
    async def put_skill(skill_id: str, patch: SkillPatch) -> SkillDTO:
        updated = state.update_skill(skill_id, patch)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown skill {skill_id!r}")
        return updated

    @router.delete("/api/skills/{skill_id}", status_code=204)
    async def delete_skill(skill_id: str) -> None:
        if not state.delete_skill(skill_id):
            raise HTTPException(status_code=404, detail=f"unknown skill {skill_id!r}")

    return router
