"""Live Settings "test" probes that need the disco-tools provider clients
(TTS / image-gen / MCP) — T4.3 / T4.4 / T4.5.

These reuse the EXISTING provider clients (audio_overview synth, image_gen
backend factory, the MCP http/stdio clients) so no transport is reinvented. Each
does one real, cheap operation and classifies the outcome into ProbeResult. An
expected failure (disabled, unreachable, bad key, procedural fallback) returns
ok=False at HTTP 200 — never a fake green, never a 500 for an expected condition.

Config is read from the SHARED ConfigStore (the same file the app-server settings
write), so a probe reflects exactly what the user just saved.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel


class ProbeResult(BaseModel):
    """Mirror of the app-server ProbeResult (same wire shape) — the outcome of a
    real provider probe. ``status`` ∈ ok|unauthorized|unreachable|misconfigured|
    disabled|procedural-fallback|error. Extras are probe-specific facts."""

    ok: bool
    status: str
    detail: str = ""
    provider: str | None = None
    tool_count: int | None = None
    byte_count: int | None = None
    procedural: bool | None = None


class McpTestBody(BaseModel):
    """Which saved MCP server to handshake.

    Legacy ``url``/``transport`` fields remain parseable for old clients, but
    W4 deliberately refuses ad-hoc probes: connecting is itself a host/network
    side effect and may only use the exact operator-approved saved config.
    """

    name: str | None = None
    url: str | None = None
    transport: str | None = None


def _resolve_key(api_key_env: str) -> str | None:
    """Resolve a provider key by secret-ref id from SecretStore only."""
    if not api_key_env:
        return None
    try:
        from disco.core.llm.secret_refs import resolve_provider_secret
        from disco.core.llm.secrets import SecretStore

        val = resolve_provider_secret(api_key_env, SecretStore())
    except Exception:  # noqa: BLE001 — store unavailable/locked means no key
        val = None
    return val or None


# ---- T4.3 TTS probe ------------------------------------------------------


async def _tts_synthesize_bundled(word: str, voice: str) -> Any:
    from disco.tools.builtin.audio_overview import _synthesize_local

    return await _synthesize_local(word, voice)


async def _tts_synthesize_speaches(
    store: Any, tts: Any, provider: str, word: str, voice: str
) -> Any:
    from disco.agent_server.audio_config import SPEACHES_URL
    from disco.tools.builtin.audio_overview import _synthesize_remote

    base = (tts.base_url or SPEACHES_URL).rstrip("/")
    if not store.approvals.origin_approved(base, "tts:speaches", ""):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="TTS endpoint origin is not operator-approved.",
            provider=provider,
        )
    return await _synthesize_remote(word, voice, base)


async def _tts_synthesize_openai(store: Any, tts: Any, provider: str, word: str, voice: str) -> Any:
    from disco.core.llm.secret_refs import secret_ref_allowed_for_origin
    from disco.tools.builtin.audio_overview import _synthesize_remote

    base = (tts.base_url or "https://api.openai.com").rstrip("/")
    if not store.approvals.origin_approved(base, "tts:openai", tts.api_key_env):
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
    return await _synthesize_remote(word, voice, base, api_key=key, model=tts.model or "tts-1")


async def _tts_synthesize(store: Any, tts: Any, provider: str, word: str, voice: str) -> Any:
    """Dispatch to the provider-specific synthesis path. May return raw audio
    data, or an early ``ProbeResult`` (misconfigured) when the tier isn't
    ready to run."""
    if provider == "bundled":
        return await _tts_synthesize_bundled(word, voice)
    if provider == "speaches":
        return await _tts_synthesize_speaches(store, tts, provider, word, voice)
    return await _tts_synthesize_openai(store, tts, provider, word, voice)


def _tts_classify_exception(exc: Exception, provider: str | None) -> ProbeResult:
    """Classify a synthesis failure. Mirrors the original except-clause order
    (most specific first) so behaviour is identical to a single inline try."""
    if isinstance(exc, ImportError):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail=(
                "The bundled Kokoro TTS couldn't be imported on this server "
                f"({exc}). It ships in core deps — re-run `uv sync` — or use a "
                "self-host/paid tier."
            ),
            provider=provider,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        status = "unauthorized" if code in (401, 403) else "error"
        return ProbeResult(
            ok=False,
            status=status,
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
    return ProbeResult(ok=False, status="error", detail=f"TTS failed: {exc}", provider=provider)


def _tts_result_from_audio(audio: Any, provider: str | None) -> ProbeResult:
    n = int(getattr(audio, "size", 0)) or (len(audio) if audio is not None else 0)
    if n <= 0:
        return ProbeResult(
            ok=False,
            status="error",
            detail="The TTS tier returned empty audio.",
            provider=provider,
        )
    return ProbeResult(
        ok=True,
        status="ok",
        detail=f"{provider} synthesized {n} samples for “Disco”.",
        provider=provider,
        byte_count=n,
    )


async def _test_tts_probe() -> ProbeResult:
    """T4.3 — synthesize the single word "Disco" via the configured TTS tier."""
    from disco.core.llm import ConfigStore

    store = ConfigStore()
    cfg = store.load()
    tts = cfg.tts
    provider = tts.provider
    if not tts.enabled:
        return ProbeResult(
            ok=False,
            status="disabled",
            detail="Audio overview is off in Settings → Audio. Enable a tier to test it.",
            provider=provider,
        )
    word = "Disco"
    voice = tts.voice_a or "af_heart"
    try:
        audio = await _tts_synthesize(store, tts, provider, word, voice)
    except Exception as exc:  # noqa: BLE001 — classified below, never a 500
        return _tts_classify_exception(exc, provider)
    if isinstance(audio, ProbeResult):
        return audio
    return _tts_result_from_audio(audio, provider)


# ---- T4.5 MCP probe -------------------------------------------------------


def _mcp_secret_refs(srv: dict[str, Any] | None) -> tuple[str, ...]:
    headers = srv.get("headers") if isinstance(srv, dict) else None
    if not isinstance(headers, dict):
        return ("",)
    refs = tuple(sorted(str(v).strip() for v in headers.values() if str(v).strip()))
    return refs or ("",)


def _mcp_origin_approved(url: str, name: str, refs: tuple[str, ...]) -> bool:
    from disco.core.llm import ConfigStore
    from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

    store = ConfigStore()
    purpose = f"mcp:{name}"
    return all(
        store.approvals.origin_approved(url, purpose, ref)
        and secret_ref_allowed_for_origin(ref, url)
        for ref in refs
    )


_McpTarget = tuple[str, str, str, Any, dict[str, Any], tuple[str, ...]]


def _mcp_resolve_target(servers: dict[str, Any], body: McpTestBody) -> _McpTarget | ProbeResult:
    """Resolve the target: a saved server by name only — never let request
    fields override the config whose fingerprint was approved. Otherwise a
    caller could approve one transport/URL and probe a different executable
    or endpoint under its authority."""
    srv = servers.get(body.name) if body.name else None
    if srv is None:
        if body.url:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=(
                    "Save and operator-approve the MCP server configuration before testing it."
                ),
            )
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="No MCP server to test — provide a saved server name or a URL.",
        )
    name = body.name or "probe"
    transport = srv.get("transport") or "streamable_http"
    url = srv.get("url") or ""
    command = srv.get("command")
    secret_refs = _mcp_secret_refs(srv)
    return name, transport, url, command, srv, secret_refs


def _mcp_check_approval(store: Any, name: str, srv: dict[str, Any]) -> ProbeResult | None:
    from disco.tools.mcp.approval import compute_config_hash
    from disco.tools.mcp.migrations import get_mcp_config_approval

    conn = getattr(store, "_conn", None)
    approval = get_mcp_config_approval(conn, name) if conn is not None else None
    current_config_hash = compute_config_hash({"name": name, **srv})
    if approval is None or approval["config_hash"] != current_config_hash:
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="Approve the current MCP server configuration before testing it.",
        )
    return None


def _mcp_build_client(
    transport: str, url: str, command: Any, name: str, secret_refs: tuple[str, ...]
) -> Any | ProbeResult:
    if transport == "stdio":
        from disco.tools.mcp.stdio import McpStdioClient

        cmd = list(command) if command else ([url] if url else [])
        if not cmd:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail="Stdio MCP server has no command to launch.",
            )
        return McpStdioClient(command=cmd, args=[], env=None)

    from disco.core.events import SecurityRisk
    from disco.tools.mcp.config import McpServerConfig
    from disco.tools.mcp.http import McpHttpClient

    if not url:
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="Streamable-HTTP MCP server has no URL.",
        )
    if not _mcp_origin_approved(url, name, secret_refs):
        return ProbeResult(
            ok=False,
            status="misconfigured",
            detail="MCP HTTP origin is not operator-approved.",
        )
    server = McpServerConfig(
        name="probe",  # validator-safe ([a-z0-9_]); risk irrelevant to a handshake
        transport="streamable_http",
        url=url,
        risk_tier=SecurityRisk.MEDIUM,
    )
    return McpHttpClient(server)


async def _mcp_handshake(client: Any, name: str) -> ProbeResult:
    await client.connect()  # type: ignore[attr-defined]
    tools = await client.list_tools()  # type: ignore[attr-defined]
    count = len(tools)
    return ProbeResult(
        ok=True,
        status="ok",
        detail=f"Connected to {name} — {count} tool(s) exposed.",
        tool_count=count,
    )


async def _run_mcp_probe(store: Any, body: McpTestBody) -> ProbeResult:
    """T4.5 — handshake the configured MCP server (initialize + tools/list)
    and report the tool count, or the real connection error."""
    from disco.core.llm import ConfigStore

    servers = ConfigStore().load().mcp.servers
    target = _mcp_resolve_target(servers, body)
    if isinstance(target, ProbeResult):
        return target
    name, transport, url, command, srv, secret_refs = target

    approval_error = _mcp_check_approval(store, name, srv)
    if approval_error is not None:
        return approval_error

    client: Any | None = None
    try:
        client_or_error = _mcp_build_client(transport, url, command, name, secret_refs)
        if isinstance(client_or_error, ProbeResult):
            return client_or_error
        client = client_or_error
        return await _mcp_handshake(client, name)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, TimeoutError) as exc:
        return ProbeResult(
            ok=False,
            status="unreachable",
            detail=f"Couldn't reach the MCP server: {type(exc).__name__}.",
        )
    except asyncio.CancelledError:
        raise  # never swallow real cancellation
    except BaseException as exc:  # noqa: BLE001 — the MCP SDK may raise an
        # anyio BaseExceptionGroup on a failed handshake (and a cross-task
        # teardown RuntimeError); classify a connection refusal honestly.
        text = str(exc) or type(exc).__name__
        unreachable = any(tok in text for tok in ("Connect", "connection", "refused", "timeout"))
        return ProbeResult(
            ok=False,
            status="unreachable" if unreachable else "error",
            detail=f"MCP handshake failed: {text}",
        )
    finally:
        if client is not None:
            try:
                await client.close()  # type: ignore[attr-defined]
            except BaseException:  # noqa: BLE001 — best-effort teardown
                pass


def make_probes_router(store: Any | None = None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/tts/test")
    async def test_tts() -> ProbeResult:
        return await _test_tts_probe()

    @router.post("/api/image-gen/test")
    async def test_image_gen() -> ProbeResult:
        """T4.4 — one tiny 64×64 generation via the configured tier. W-50: if the
        selected tier isn't fully configured, select_image_backend() raises
        ImageGenNotConfigured → report `misconfigured` (no procedural fallback)."""
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
        except Exception as exc:  # noqa: BLE001 — real backends may raise on net/auth
            return ProbeResult(
                ok=False,
                status="error",
                detail=f"{provider} image generation failed: {exc}",
                provider=provider,
                procedural=False,
            )
        n = len(data) if data else 0
        if n <= 0:
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
            detail=f"{provider} produced {n} bytes of real generated image data.",
            provider=provider,
            procedural=False,
            byte_count=n,
        )

    @router.post("/api/mcp/test")
    async def test_mcp(body: McpTestBody) -> ProbeResult:
        """T4.5 — handshake the configured MCP server (initialize + tools/list)
        and report the tool count, or the real connection error."""
        return await _run_mcp_probe(store, body)

    return router
