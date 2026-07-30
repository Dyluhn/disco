"""Live Settings probes using the real TTS, image, and MCP provider clients."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter
from pydantic import BaseModel


class ProbeResult(BaseModel):
    """Wire-compatible outcome of a real provider probe."""

    ok: bool
    status: str
    detail: str = ""
    provider: str | None = None
    tool_count: int | None = None
    byte_count: int | None = None
    procedural: bool | None = None


class McpTestBody(BaseModel):
    """Saved MCP server selected for a governed handshake."""

    name: str | None = None
    url: str | None = None
    transport: str | None = None


class _McpClient(Protocol):
    async def connect(self) -> Any: ...

    async def list_tools(self) -> list[object]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _McpTarget:
    name: str
    transport: str
    url: str
    command: tuple[str, ...]
    secret_refs: tuple[str, ...]
    config: dict[str, Any]


def _resolve_key(api_key_env: str) -> str | None:
    if not api_key_env:
        return None
    try:
        from disco.core.llm.secret_refs import resolve_provider_secret
        from disco.core.llm.secrets import SecretStore

        value = resolve_provider_secret(api_key_env, SecretStore())
    except Exception:  # noqa: BLE001 - unavailable/locked store means no key
        value = None
    return value or None


def _mcp_secret_refs(server: dict[str, Any] | None) -> tuple[str, ...]:
    headers = server.get("headers") if isinstance(server, dict) else None
    if not isinstance(headers, dict):
        return ("",)
    refs = tuple(sorted(str(value).strip() for value in headers.values() if str(value).strip()))
    return refs or ("",)


def _mcp_origin_approved(url: str, name: str, refs: tuple[str, ...]) -> bool:
    from disco.core.llm import ConfigStore
    from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

    store = ConfigStore()
    purpose = f"mcp:{name}"
    return all(
        store.origin_approved(url, purpose, ref) and secret_ref_allowed_for_origin(ref, url)
        for ref in refs
    )


async def _configured_tts_audio(store: Any, provider: str, tts: Any) -> object | ProbeResult:
    word = "Disco"
    voice = tts.voice_a or "af_heart"
    if provider == "bundled":
        from disco.tools.builtin.audio_overview import _synthesize_local

        return await _synthesize_local(word, voice)

    from disco.agent_server.audio_config import SPEACHES_URL
    from disco.tools.builtin.audio_overview import _synthesize_remote

    if provider == "speaches":
        base = (tts.base_url or SPEACHES_URL).rstrip("/")
        if not store.origin_approved(base, "tts:speaches", ""):
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail="TTS endpoint origin is not operator-approved.",
                provider=provider,
            )
        return await _synthesize_remote(word, voice, base)

    from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

    base = (tts.base_url or "https://api.openai.com").rstrip("/")
    if not store.origin_approved(base, "tts:openai", tts.api_key_env):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="TTS endpoint origin is not operator-approved.",
            provider=provider,
        )
    if not secret_ref_allowed_for_origin(tts.api_key_env, base):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="TTS secret_ref is not allowed for this endpoint origin.",
            provider=provider,
        )
    key = _resolve_key(tts.api_key_env)
    if not key:
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail=(
                f"No decryptable key for {tts.api_key_env or '(unset)'} — "
                "store it under Provider API keys first."
            ),
            provider=provider,
        )
    return await _synthesize_remote(
        word,
        voice,
        base,
        api_key=key,
        model=tts.model or "tts-1",
    )


def _tts_failure(exc: Exception, provider: str) -> ProbeResult:
    if isinstance(exc, ImportError):
        detail = (
            "The bundled Kokoro TTS couldn't be imported on this server "
            f"({exc}). It ships in core deps — re-run `uv sync` — or use a "
            "self-host/paid tier."
        )
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail=detail,
            provider=provider,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return ProbeResult(
            ok=False,
            status="unauthorized" if code in (401, 403) else "error",
            detail=f"TTS endpoint answered {code}.",
            provider=provider,
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return ProbeResult(
            ok=False,
            status="unreachable",
            detail=f"Couldn't reach the TTS endpoint: {type(exc).__name__}.",
            provider=provider,
        )
    return ProbeResult(
        ok=False,
        status="error",
        detail=f"TTS failed: {exc}",
        provider=provider,
    )


def _audio_size(audio: object) -> int:
    size = getattr(audio, "size", 0)
    if isinstance(size, int) and size > 0:
        return size
    try:
        return len(audio)  # type: ignore[arg-type]
    except TypeError:
        return 0


async def _test_tts_probe() -> ProbeResult:
    from disco.core.llm import ConfigStore

    store = ConfigStore()
    tts = store.load().tts
    provider = tts.provider
    if not tts.enabled:
        return ProbeResult(
            ok=False,
            status="disabled",
            detail="Audio overview is off in Settings → Audio. Enable a tier to test it.",
            provider=provider,
        )
    try:
        outcome = await _configured_tts_audio(store, provider, tts)
    except Exception as exc:  # noqa: BLE001 - classify provider failures honestly
        return _tts_failure(exc, provider)
    if isinstance(outcome, ProbeResult):
        return outcome
    sample_count = _audio_size(outcome)
    if sample_count <= 0:
        return ProbeResult(
            ok=False,
            status="error",
            detail="The TTS tier returned empty audio.",
            provider=provider,
        )
    return ProbeResult(
        ok=True,
        status="ok",
        detail=f"{provider} synthesized {sample_count} samples for “Disco”.",
        provider=provider,
        byte_count=sample_count,
    )


async def _test_image_gen_probe() -> ProbeResult:
    from disco.core.llm import ConfigStore
    from disco.tools.builtin.image_gen import (
        ImageGenNotConfigured,
        select_image_backend,
    )

    provider = ConfigStore().load().image_gen.provider
    try:
        backend = select_image_backend()
    except ImageGenNotConfigured as exc:
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail=str(exc),
            provider=provider,
            procedural=False,
        )
    try:
        data = await asyncio.to_thread(
            backend.generate,
            prompt="disco test swatch",
            width=64,
            height=64,
            seed=7,
            fmt="png",
        )
    except Exception as exc:  # noqa: BLE001 - real backend failure
        return ProbeResult(
            ok=False,
            status="error",
            detail=f"{provider} image generation failed: {exc}",
            provider=provider,
            procedural=False,
        )
    byte_count = len(data) if data else 0
    if byte_count <= 0:
        return ProbeResult(
            ok=False,
            status="error",
            detail="The image backend returned no bytes.",
            provider=provider,
            procedural=False,
        )
    return ProbeResult(
        ok=True,
        status="ok",
        detail=f"{provider} produced {byte_count} bytes of real generated image data.",
        provider=provider,
        procedural=False,
        byte_count=byte_count,
    )


def _resolve_mcp_target(
    body: McpTestBody,
    servers: dict[str, dict],
) -> _McpTarget | ProbeResult:
    server = servers.get(body.name) if body.name else None
    if server is None:
        detail = (
            "Save and operator-approve the MCP server configuration before testing it."
            if body.url
            else "No MCP server to test — provide a saved server name or a URL."
        )
        return ProbeResult(ok=False, status="misconfigured", detail=detail)
    name = body.name or "probe"
    raw_command = server.get("command")
    command = (
        tuple(str(part) for part in raw_command) if isinstance(raw_command, (list, tuple)) else ()
    )
    return _McpTarget(
        name=name,
        transport=str(server.get("transport") or "streamable_http"),
        url=str(server.get("url") or ""),
        command=command,
        secret_refs=_mcp_secret_refs(server),
        config=server,
    )


def _mcp_approval_error(store: Any | None, target: _McpTarget) -> ProbeResult | None:
    from disco.tools.mcp.approval import compute_config_hash
    from disco.tools.mcp.migrations import get_mcp_config_approval

    connection = getattr(store, "_conn", None)
    approval = get_mcp_config_approval(connection, target.name) if connection is not None else None
    current_hash = compute_config_hash({"name": target.name, **target.config})
    if approval is not None and approval["config_hash"] == current_hash:
        return None
    return ProbeResult(
        ok=False,
        status="misconfigured",
        detail="Approve the current MCP server configuration before testing it.",
    )


def _make_mcp_client(target: _McpTarget) -> _McpClient | ProbeResult:
    if target.transport == "stdio":
        from disco.tools.mcp.stdio import McpStdioClient

        command = list(target.command) or ([target.url] if target.url else [])
        if not command:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail="Stdio MCP server has no command to launch.",
            )
        return McpStdioClient(command=command, args=[], env=None)

    if not target.url:
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="Streamable-HTTP MCP server has no URL.",
        )
    if not _mcp_origin_approved(target.url, target.name, target.secret_refs):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="MCP HTTP origin is not operator-approved.",
        )
    from disco.core.events import SecurityRisk
    from disco.tools.mcp.config import McpServerConfig
    from disco.tools.mcp.http import McpHttpClient

    server = McpServerConfig(
        name="probe",
        transport="streamable_http",
        url=target.url,
        risk_tier=SecurityRisk.MEDIUM,
    )
    return McpHttpClient(server)


def _mcp_failure(exc: BaseException) -> ProbeResult:
    text = str(exc) or type(exc).__name__
    unreachable = any(token in text for token in ("Connect", "connection", "refused", "timeout"))
    return ProbeResult(
        ok=False,
        status="unreachable" if unreachable else "error",
        detail=f"MCP handshake failed: {text}",
    )


async def _run_mcp_handshake(client: _McpClient, name: str) -> ProbeResult:
    try:
        await client.connect()
        tools = await client.list_tools()
        return ProbeResult(
            ok=True,
            status="ok",
            detail=f"Connected to {name} — {len(tools)} tool(s) exposed.",
            tool_count=len(tools),
        )
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        TimeoutError,
    ) as exc:
        return ProbeResult(
            ok=False,
            status="unreachable",
            detail=f"Couldn't reach the MCP server: {type(exc).__name__}.",
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - MCP SDK may raise exception groups
        return _mcp_failure(exc)
    finally:
        try:
            await client.close()
        except BaseException:  # noqa: BLE001 - best-effort teardown
            pass


async def _test_mcp_probe(body: McpTestBody, store: Any | None) -> ProbeResult:
    from disco.core.llm import ConfigStore

    target = _resolve_mcp_target(body, ConfigStore().load().mcp.servers)
    if isinstance(target, ProbeResult):
        return target
    approval_error = _mcp_approval_error(store, target)
    if approval_error is not None:
        return approval_error
    client = _make_mcp_client(target)
    if isinstance(client, ProbeResult):
        return client
    return await _run_mcp_handshake(client, target.name)


def make_probes_router(store: Any | None = None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/tts/test")
    async def test_tts() -> ProbeResult:
        return await _test_tts_probe()

    @router.post("/api/image-gen/test")
    async def test_image_gen() -> ProbeResult:
        return await _test_image_gen_probe()

    @router.post("/api/mcp/test")
    async def test_mcp(body: McpTestBody) -> ProbeResult:
        return await _test_mcp_probe(body, store)

    return router
