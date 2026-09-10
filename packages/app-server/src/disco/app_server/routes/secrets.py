"""Generic provider-secret routes — any API key, encrypted at rest.

Generalizes the OpenRouter key trio to ARBITRARY provider keys (paid models,
web-search, extraction, TTS), each keyed by its `api_key_env` var NAME. The
agent-server overlays the decrypted value into that env var at build time, so
the plaintext never lands on disk. The OpenRouter key keeps its dedicated route
(`/api/openrouter/key`); this surface refuses the reserved "openrouter" name.

Values are write-only over the wire: GET never returns a key, only whether one
is stored and the store's lock/can-store state.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from ..config.dtos import ProbeResult, SecretBody, SecretsListDTO, SecretStatus
from ..config_state import ConfigState

# Provider keys are referenced by their env-var name; constrain the path param to
# a valid env-var identifier (also blocks path-traversal / odd characters).
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _validate(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="secret name must be an env-var identifier ([A-Za-z_][A-Za-z0-9_]*)",
        )
    return name


def make_secrets_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/secrets")
    async def list_secrets() -> SecretsListDTO:
        return state.secrets_admin.list_secrets()

    @router.get("/api/secrets/{name}")
    async def get_secret_status(name: str) -> SecretStatus:
        return state.secrets_admin.secret_status(_validate(name))

    @router.put("/api/secrets/{name}")
    async def put_secret(name: str, body: SecretBody) -> SecretStatus:
        try:
            return state.secrets_admin.set_secret(_validate(name), body.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/secrets/{name}")
    async def delete_secret(name: str) -> SecretStatus:
        try:
            return state.secrets_admin.clear_secret(_validate(name))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/secrets/{name}/test")
    async def test_secret(name: str) -> ProbeResult:
        """Probe T4.1 — a REAL authenticated call to the provider that uses this
        key (an OpenAI-compatible models list). Expected failures (no endpoint,
        bad key, unreachable) return ok=False at 200; only a bad NAME is a 400."""
        return await state.secrets_admin.test_secret(_validate(name))

    return router
