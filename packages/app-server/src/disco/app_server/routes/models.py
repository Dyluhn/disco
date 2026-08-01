"""Models catalogue + assignments routes — the deterministic, manual model matrix.

The literal `/api/models/assignments` routes are declared BEFORE the
`/api/models/{model_id}` routes so the literal path wins over the path param.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config.dtos import (
    AssignmentsDTO,
    AssignmentsPatch,
    ModelDTO,
    ModelUpsert,
)
from ..config_state import ConfigState


def make_models_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models")
    async def get_models() -> list[ModelDTO]:
        return state.models.models()

    @router.post("/api/models", status_code=201)
    async def add_model(upsert: ModelUpsert) -> list[ModelDTO]:
        try:
            return state.models.add_model(upsert)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/models/assignments")
    async def get_assignments() -> AssignmentsDTO:
        return state.assignments.assignments()

    @router.put("/api/models/assignments")
    async def put_assignments(patch: AssignmentsPatch) -> AssignmentsDTO:
        # Absolute: the system uses exactly what is set; capabilities are advisory
        # + fail-loud at runtime, not blocked here. The one structural guard is that
        # the model KEY must exist in the catalogue (else routing can't resolve it).
        try:
            return state.assignments.update_assignments(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Declared AFTER /assignments so that literal path wins over {model_id}.
    @router.put("/api/models/{model_id}")
    async def put_model(model_id: str, upsert: ModelUpsert) -> list[ModelDTO]:
        try:
            return state.models.update_model(model_id, upsert)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/models/{model_id}")
    async def delete_model(model_id: str) -> list[ModelDTO]:
        try:
            return state.models.remove_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
