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
import os

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
    """Which MCP server to handshake. ``name`` resolves a configured server from
    the shared config; ``url``/``transport`` allow testing an ad-hoc/unsaved one."""

    name: str | None = None
    url: str | None = None
    transport: str | None = None


def _resolve_key(api_key_env: str) -> str | None:
    """Decrypt the named secret (or fall back to the live env) — the same order
    the runtime uses to overlay a provider key at build time."""
    if not api_key_env:
        return None
    try:
        from disco.core.llm.secrets import SecretStore

        val = SecretStore().get_secret(api_key_env)
    except Exception:  # noqa: BLE001 — store unavailable; fall back to env
        val = None
    return val or os.environ.get(api_key_env) or None


def make_probes_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/tts/test")
    async def test_tts() -> ProbeResult:
        """T4.3 — synthesize the single word "Disco" via the configured TTS tier.
        Non-empty audio = ok; disabled/unreachable/bad-key fail honestly."""
        from disco.core.llm import ConfigStore

        tts = ConfigStore().load().tts
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
            if provider == "bundled":
                from disco.tools.builtin.audio_overview import _synthesize_local

                audio = await _synthesize_local(word, voice)
            else:
                from disco.agent_server.audio_config import SPEACHES_URL
                from disco.tools.builtin.audio_overview import _synthesize_remote

                if provider == "speaches":
                    base = (tts.base_url or SPEACHES_URL).rstrip("/")
                    audio = await _synthesize_remote(word, voice, base)
                else:  # openai (paid, OpenAI-compatible)
                    base = (tts.base_url or "https://api.openai.com").rstrip("/")
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
                    audio = await _synthesize_remote(
                        word, voice, base, api_key=key, model=tts.model or "tts-1"
                    )
        except ImportError as exc:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=(
                    "The bundled Kokoro TTS extra isn't installed on this server "
                    f"({exc}). Install it or use a self-host/paid tier."
                ),
                provider=provider,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            status = "unauthorized" if code in (401, 403) else "error"
            return ProbeResult(
                ok=False,
                status=status,
                detail=f"TTS endpoint answered {code}.",
                provider=provider,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            return ProbeResult(
                ok=False,
                status="unreachable",
                detail=f"Couldn't reach the TTS endpoint: {type(exc).__name__}.",
                provider=provider,
            )
        except Exception as exc:  # noqa: BLE001 — surface anything else honestly
            return ProbeResult(
                ok=False, status="error", detail=f"TTS failed: {exc}", provider=provider
            )
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

    @router.post("/api/image-gen/test")
    async def test_image_gen() -> ProbeResult:
        """T4.4 — one tiny 64×64 generation via the configured tier. If a real
        tier is selected but the run falls back to the keyless procedural
        placeholder (#76), SAY so (procedural-fallback) instead of faking success."""
        from disco.core.llm import ConfigStore
        from disco.tools.builtin.image_gen import select_image_backend

        provider = ConfigStore().load().image_gen.provider
        backend = select_image_backend()
        is_procedural = not bool(getattr(backend, "is_remote", False))
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
                procedural=is_procedural,
            )
        n = len(data) if data else 0
        if n <= 0:
            return ProbeResult(
                ok=False,
                status="error",
                detail="The image backend returned no bytes.",
                provider=provider,
                procedural=is_procedural,
            )
        if provider != "procedural" and is_procedural:
            # A real tier is selected but select_image_backend() fell back.
            return ProbeResult(
                ok=False,
                status="procedural-fallback",
                detail=(
                    f"‘{provider}’ is selected but image-gen fell back to the "
                    "keyless procedural placeholder — its required config (base URL / "
                    "stored key / model) is missing, so no real diffusion ran."
                ),
                provider=provider,
                procedural=True,
                byte_count=n,
            )
        if is_procedural:
            return ProbeResult(
                ok=True,
                status="ok",
                detail=(
                    f"Procedural placeholder produced {n} bytes — keyless in-process "
                    "art (no real diffusion backend is configured)."
                ),
                provider=provider,
                procedural=True,
                byte_count=n,
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
        from disco.core.events import SecurityRisk
        from disco.core.llm import ConfigStore

        servers = ConfigStore().load().mcp.servers
        # Resolve the target: a saved server by name, else an ad-hoc url.
        srv = servers.get(body.name) if body.name else None
        if srv is not None:
            name = body.name or "probe"
            transport = body.transport or srv.get("transport") or "streamable_http"
            url = body.url or srv.get("url") or ""
            command = srv.get("command")
        elif body.url:
            name = "probe"
            transport = body.transport or "streamable_http"
            url = body.url
            command = None
        else:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail="No MCP server to test — provide a saved server name or a URL.",
            )

        client: object | None = None
        try:
            if transport == "stdio":
                from disco.tools.mcp.stdio import McpStdioClient

                cmd = list(command) if command else ([url] if url else [])
                if not cmd:
                    return ProbeResult(
                        ok=False,
                        status="misconfigured",
                        detail="Stdio MCP server has no command to launch.",
                    )
                client = McpStdioClient(command=cmd, args=[], env=None)
            else:
                from disco.tools.mcp.config import McpServerConfig
                from disco.tools.mcp.http import McpHttpClient

                if not url:
                    return ProbeResult(
                        ok=False,
                        status="misconfigured",
                        detail="Streamable-HTTP MCP server has no URL.",
                    )
                server = McpServerConfig(
                    name="probe",  # validator-safe ([a-z0-9_]); risk irrelevant to a handshake
                    transport="streamable_http",
                    url=url,
                    risk_tier=SecurityRisk.MEDIUM,
                )
                client = McpHttpClient(server)
            await client.connect()  # type: ignore[attr-defined]
            tools = await client.list_tools()  # type: ignore[attr-defined]
            count = len(tools)
            return ProbeResult(
                ok=True,
                status="ok",
                detail=f"Connected to {name} — {count} tool(s) exposed.",
                tool_count=count,
            )
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
            unreachable = any(
                tok in text for tok in ("Connect", "connection", "refused", "timeout")
            )
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

    return router
