"""Audio overview tool — two-voice TTS from a finished deep-research report.

Pipeline:
  0. Resolve TTS settings from ConfigStore (toggle / backend / voices). If the
     feature is disabled, fail soft so the model never loads (saves RAM).
  1. Accept the report text.
  2. Call an LLM (OpenAI-compatible chat completions) for bounded, indexed JSON
     turn batches and retain its completion status.
  3. Assemble contiguous batches; shrink on explicit truncation and retry ONCE
     on ordinary malformed output.
  4. Synthesise each turn to float32 PCM @ 24 kHz — bundled in-process Kokoro
     (default, ONNX/CPU) or a remote Speaches endpoint, per Settings → Audio.
  5. Mix the per-turn PCM with inter-turn silence, then encode the whole
     overview to MP3 ONCE via _audio_mixer (one sample rate end to end).
  6. Write MP3 + transcript through ctx.sandbox.write_file (jailed).
  7. Return ToolOutcome with artifact paths.

Fail-soft: if the chosen TTS backend is unreachable/disabled, return
success=False with a clear message — never crash the loop.

The turn-script pipeline's implementation (payload construction, the LLM
transport, JSON validation, the segmented-assembly loop, and TTS synthesis)
lives in the sibling ``_audio_overview_parts`` package; this module re-imports
and re-exports every one of those names unchanged so that
``disco.tools.builtin.audio_overview.<name>`` keeps resolving exactly as it did
before the split — including for ``monkeypatch.setattr``/``mock.patch`` targets
used across the test suite, since every real caller of these names
(``AudioOverviewTool``'s own methods) still lives physically in this module and
therefore still resolves them at call time through this module's globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ._audio_mixer import encode_mp3, mix_pcm
from ._audio_overview_parts._llm_client import _call_llm, _call_llm_with_key
from ._audio_overview_parts._payload import (
    _SCRIPT_BATCH_TURNS as _SCRIPT_BATCH_TURNS,
)
from ._audio_overview_parts._payload import (
    _SCRIPT_REASONING_TOKEN_RESERVE as _SCRIPT_REASONING_TOKEN_RESERVE,
)
from ._audio_overview_parts._payload import (
    _SCRIPT_TOKENS_PER_TURN as _SCRIPT_TOKENS_PER_TURN,
)
from ._audio_overview_parts._payload import (
    _SINGLE_SCRIPT_PROMPT as _SINGLE_SCRIPT_PROMPT,
)
from ._audio_overview_parts._payload import (
    _TURN_SCRIPT_PROMPT as _TURN_SCRIPT_PROMPT,
)

# --- compatibility re-exports (PKG-10-MEDIA) --------------------------------
# Names this module exposed BEFORE the parts split. Consumers and the test
# suite reach several of them through this module object (including via
# monkeypatch), so the facade must keep exposing every one. Restored
# programmatically by diffing this module's top-level names against its
# parent commit; PKG-13-FACADES owns their eventual deletion.
from ._audio_overview_parts._payload import (
    Sequence as Sequence,
)
from ._audio_overview_parts._payload import (
    _build_llm_payload as _build_llm_payload,
)
from ._audio_overview_parts._payload import (
    _build_llm_payload_for_model as _build_llm_payload_for_model,
)
from ._audio_overview_parts._payload import (
    _build_segment_payload_for_model as _build_segment_payload_for_model,
)
from ._audio_overview_parts._payload import (
    _segment_output_tokens as _segment_output_tokens,
)
from ._audio_overview_parts._payload import (
    json as json,
)
from ._audio_overview_parts._script_generation import (
    _SCRIPT_MAX_PROVIDER_CALLS as _SCRIPT_MAX_PROVIDER_CALLS,
)
from ._audio_overview_parts._script_generation import (
    _SCRIPT_MAX_TRUNCATIONS_PER_BATCH as _SCRIPT_MAX_TRUNCATIONS_PER_BATCH,
)
from ._audio_overview_parts._script_generation import (
    Awaitable as Awaitable,
)
from ._audio_overview_parts._script_generation import (
    Callable as Callable,
)
from ._audio_overview_parts._script_generation import (
    _coerce_llm_response as _coerce_llm_response,
)
from ._audio_overview_parts._script_generation import (
    _extract_script_json as _extract_script_json,
)
from ._audio_overview_parts._script_generation import _generate_segmented_turn_script
from ._audio_overview_parts._script_generation import (
    _malformed_retry_payload as _malformed_retry_payload,
)
from ._audio_overview_parts._script_generation import (
    _validate_script_batch as _validate_script_batch,
)
from ._audio_overview_parts._script_generation import (
    _validate_turn_script as _validate_turn_script,
)
from ._audio_overview_parts._script_generation import (
    _validate_turn_script_single as _validate_turn_script_single,
)
from ._audio_overview_parts._synthesis import (
    TTS_SAMPLE_RATE,
    _synthesize_local,
    _synthesize_remote,
)
from ._audio_overview_parts._synthesis import (
    _text_for_synthesis as _text_for_synthesis,
)
from ._audio_overview_parts._synthesis import (
    io as io,
)
from ._audio_overview_parts._synthesis import (
    normalize_tts_text as normalize_tts_text,
)
from ._audio_overview_parts._synthesis import (
    wave as wave,
)
from ._audio_overview_parts._types import (
    AudioScriptGenerationError,
    LLMResponseLike,
    Turn,
)
from ._audio_overview_parts._types import (
    LLMResponse as LLMResponse,
)
from ._audio_overview_parts._types import (
    _ScriptBatch as _ScriptBatch,
)
from ._audio_overview_parts._validation import (
    _extract_json as _extract_json,
)
from ._audio_overview_parts._validation import (
    re as re,
)


@dataclass
class _ResolvedTts:
    """Resolved TTS settings for one overview run (the output of Step 0)."""

    provider: str
    backend: str
    voice_a: str
    voice_b: str
    is_remote: bool
    remote_base: str
    remote_key: str
    remote_model: str


# ---- tool implementation ----------------------------------------------------


# --- the tracked upward edge lives HERE, and only here -----------------------
# `.importlinter` whitelists `disco.tools.builtin.audio_overview -> agent_server.*`
# by EXACT module name ("Remove these lines when fixed"). The `_audio_overview_parts`
# modules must therefore never import agent_server themselves — they route through
# these two accessors, so the tracked debt stays exactly the three declared edges
# instead of multiplying across every new parts module. Both imports stay
# function-local to dodge the load-time cycle, as before.


def _audio_llm_model() -> str:
    """The audio LLM constant, through this module's tracked upward edge."""
    from disco.agent_server.audio_config import LLM_MODEL

    return LLM_MODEL


async def _tts_local_synthesize(text: str, voice: str) -> Any:
    """Bundled in-process Kokoro synthesis, through the tracked upward edge."""
    from disco.agent_server.tts_local import synthesize

    return await synthesize(text, voice)


class AudioOverviewArgs(BaseModel):
    report_text: str = Field(
        description="The full text of the finished research report to convert to audio."
    )
    filename: str = Field(
        default="audio_overview",
        description="Base filename (without extension) for MP3 + transcript outputs.",
    )


class AudioOverviewTool:
    definition = ToolDef(
        name="audio_overview",
        description=(
            "Generate a two-voice audio overview (MP3 + transcript) from a finished "
            "deep-research report. Two AI hosts discuss the key findings in a "
            "conversational back-and-forth. Produces an MP3 file and a transcript "
            "text file in the workspace."
        ),
        args_model=AudioOverviewArgs,
        needs=frozenset({Capability.NETWORK, Capability.FILESYSTEM}),
        base_risk=None,
        runs_in="sandbox",  # HTTP calls + sandbox-backed file writes
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    def execution_scope(self, args: AudioOverviewArgs) -> str:
        return "in_process"

    async def _resolve_tts_settings(
        self, tts: Any, speaches_url: str
    ) -> tuple[_ResolvedTts | None, ToolOutcome | None]:
        """Step 0: resolve the toggle / backend / voices from ConfigStore TTS
        settings. Returns (resolved, None) when enabled, or (None, fail_soft) when
        disabled — the disabled path unloads the bundled model first (RAM gate)."""
        if not tts.enabled:
            # Wire (D5): the "Off" toggle is supposed to free the Kokoro model
            # immediately, not wait for the agent-server's idle-TTL sweep
            # (PMX_TTS_IDLE_TTL_S, default 30 min). The bundled engine is
            # process-global in `disco.agent_server.tts_local`; `unload()` is a
            # no-op when nothing is loaded, so it's safe to call here. Lazy-
            # imported + suppressed so the tools package stays importable even
            # where the agent-server (which hosts the bundled engine) isn't.
            try:
                from disco.agent_server import tts_local as _tts_local

                await _tts_local.unload()
            except ImportError:
                pass  # agent-server / bundled TTS not importable — nothing to unload
            return None, ToolOutcome(
                success=False,
                content=(
                    "Audio overview is disabled in Settings → Audio. Enable it "
                    "(bundled in-process, self-host, or paid endpoint) to generate "
                    "overviews."
                ),
                error="tts disabled",
            )
        # Three-tier provider resolution. `speaches` (self-host) and `openai` (paid)
        # share the OpenAI-compatible HTTP client; they differ only by the base_url
        # default and whether a Bearer key + model are sent. `bundled` uses none of
        # these (in-process Kokoro). The key is resolved from the ENV-VAR NAME the
        # user set in Settings (never the raw key) — same convention as search/
        # extraction/model providers.
        _OPENAI_DEFAULT_BASE = "https://api.openai.com"
        remote_base = remote_key = remote_model = ""
        if tts.provider == "speaches":
            remote_base = (tts.base_url or speaches_url).rstrip("/")
        elif tts.provider == "openai":
            remote_base = (tts.base_url or _OPENAI_DEFAULT_BASE).rstrip("/")
        if remote_base:
            from disco.core.llm import ConfigStore

            store = ConfigStore()
            secret_ref = tts.api_key_env if tts.provider == "openai" else ""
            if not store.origin_approved(remote_base, f"tts:{tts.provider}", secret_ref):
                return None, ToolOutcome(
                    success=False,
                    content=(
                        "Audio overview remote endpoint is not operator-approved. "
                        "Save the Audio settings to approve this exact origin."
                    ),
                    error="tts endpoint not approved",
                )
        if tts.provider == "openai":
            if tts.api_key_env:
                from disco.core.llm.secret_refs import (
                    resolve_provider_secret,
                    secret_ref_allowed_for_origin,
                )
                from disco.core.llm.secrets import SecretStore

                if not secret_ref_allowed_for_origin(tts.api_key_env, remote_base):
                    return None, ToolOutcome(
                        success=False,
                        content=(
                            "Audio overview secret_ref is not allowed for this endpoint origin."
                        ),
                        error="tts secret_ref origin mismatch",
                    )
                remote_key = resolve_provider_secret(tts.api_key_env, SecretStore()) or ""
            remote_model = tts.model or "tts-1"
        _BACKEND_LABEL = {
            "bundled": "bundled Kokoro",
            "speaches": "self-host Speaches",
            "openai": "paid OpenAI-compatible",
        }
        resolved = _ResolvedTts(
            provider=tts.provider,
            backend=_BACKEND_LABEL.get(tts.provider, tts.provider),
            voice_a=tts.voice_a,
            voice_b=tts.voice_b,
            is_remote=tts.provider != "bundled",
            remote_base=remote_base,
            remote_key=remote_key,
            remote_model=remote_model,
        )
        return resolved, None

    async def _generate_turn_script(
        self,
        report_text: str,
        llm_url: str,
        llm_model: str,
        llm_key: str = "",
        max_output_tokens: int | None = None,
    ) -> tuple[list[Turn] | None, ToolOutcome | None]:
        """Generate a complete script through the bounded shared batch protocol."""

        async def call(payload: dict[str, Any]) -> LLMResponseLike:
            if llm_key:
                return await _call_llm_with_key(payload, llm_url, llm_key)
            return await _call_llm(payload, llm_url)

        try:
            turns = await _generate_segmented_turn_script(
                report_text,
                llm_model,
                mode="podcast",
                call_llm=call,
                max_output_tokens=max_output_tokens,
            )
        except AudioScriptGenerationError as exc:
            return None, ToolOutcome(
                success=False,
                content=str(exc),
                error=str(exc),
            )
        return turns, None

    async def _synthesize_turns(
        self, turns: list[Turn], cfg: _ResolvedTts
    ) -> tuple[list[Any] | None, ToolOutcome | None]:
        """Step 3: synthesise each turn to float32 mono PCM @ 24 kHz via the bundled
        (in-process Kokoro) or remote (Speaches / OpenAI-compatible) backend. Returns
        (pcm_turns, None) on success or (None, failure_outcome) on the first error."""
        pcm_turns: list[Any] = []
        for i, turn in enumerate(turns):
            voice = cfg.voice_a if turn.speaker == "A" else cfg.voice_b
            try:
                if cfg.is_remote:
                    pcm = await _synthesize_remote(
                        turn.text,
                        voice,
                        cfg.remote_base,
                        api_key=cfg.remote_key,
                        model=cfg.remote_model,
                    )
                else:
                    pcm = await _synthesize_local(turn.text, voice)
            except httpx.ConnectError:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"The TTS endpoint is unreachable at {cfg.remote_base}. "
                        f"Turn {i + 1}/{len(turns)} could not be synthesised. "
                        f"Switch Settings → Audio to bundled, or start/fix the endpoint."
                    ),
                    error=f"tts endpoint offline: {cfg.remote_base}",
                )
            except Exception as e:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"TTS ({cfg.backend}) failed for turn {i + 1}/{len(turns)} "
                        f"(speaker {turn.speaker}): {e}"
                    ),
                    error=str(e),
                )
            if pcm is None or getattr(pcm, "size", len(pcm) if pcm is not None else 0) == 0:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"TTS ({cfg.backend}) returned empty audio for turn "
                        f"{i + 1}/{len(turns)} (speaker {turn.speaker})"
                    ),
                    error="Empty audio from TTS",
                )
            pcm_turns.append(pcm)
        return pcm_turns, None

    def _mix_and_encode(self, pcm_turns: list[Any], silence_ms: int) -> bytes:
        """Step 4: mix the per-turn PCM with inter-turn silence (one sample rate end
        to end), then encode the whole overview to MP3 exactly once."""
        mixed_pcm = mix_pcm(pcm_turns, silence_ms=silence_ms, sample_rate=TTS_SAMPLE_RATE)
        return encode_mp3(mixed_pcm, sample_rate=TTS_SAMPLE_RATE)

    async def _write_outputs(
        self,
        ctx: ToolContext,
        filename: str,
        mixed_mp3: bytes,
        transcript_text: str,
        cfg: _ResolvedTts,
        *,
        turn_count: int,
        silence_ms: int,
    ) -> ToolOutcome:
        """Step 6: write the MP3 + transcript through the jailed sandbox and build the
        success ToolOutcome (artifact paths + structured metadata)."""
        mp3_path = f"{filename}.mp3"
        transcript_path = f"{filename}.md"
        assert ctx.sandbox is not None, "audio_overview requires a sandbox for file writes"
        await ctx.sandbox.write_file(mp3_path, mixed_mp3)
        await ctx.sandbox.write_file(transcript_path, transcript_text.encode("utf-8"))

        return ToolOutcome(
            success=True,
            content=(
                f"Audio overview generated.\n"
                f"  MP3: {mp3_path} ({len(mixed_mp3)} bytes)\n"
                f"  Transcript: {transcript_path}\n"
                f"  Turns: {turn_count} | Voices: {cfg.voice_a} (A) + {cfg.voice_b} (B)\n"
                f"  Backend: {cfg.backend} | Silence between turns: {silence_ms}ms"
            ),
            artifacts=[mp3_path, transcript_path],
            structured={
                "mp3_path": mp3_path,
                "transcript_path": transcript_path,
                "turn_count": turn_count,
                "mp3_bytes": len(mixed_mp3),
                "voice_a": cfg.voice_a,
                "voice_b": cfg.voice_b,
                "backend": cfg.provider,
                "silence_ms": silence_ms,
            },
        )

    def _build_transcript(self, turns: list[Turn], voice_a: str, voice_b: str) -> str:
        """Step 5: render the turn-script to a Markdown transcript."""
        transcript_lines: list[str] = [
            "# Audio Overview Transcript",
            "",
            f"Voices: Host A = {voice_a}, Host B = {voice_b}",
            f"Turns: {len(turns)}",
            "",
        ]
        for turn in turns:
            label = "Host A" if turn.speaker == "A" else "Host B"
            transcript_lines.append(f"**{label}:** {turn.text}")
            transcript_lines.append("")
        return "\n".join(transcript_lines)

    def _resolve_script_llm(self) -> tuple[str, str, str, int | None, str | None]:
        from disco.core.llm import ConfigStore, ModelRole
        from disco.core.llm.secret_refs import (
            resolve_provider_secret,
            secret_ref_allowed_for_origin,
        )
        from disco.core.llm.secrets import SecretStore

        store = ConfigStore()
        cfg = store.load()
        key = cfg.assignments.get(ModelRole.RAG_ANSWERER) or cfg.default_model
        entry = cfg.models.get(key)
        if entry is None or not entry.base_url:
            return "", "local-model", "", None, "LLM endpoint is not configured"
        llm_url = entry.base_url.rstrip("/")
        llm_model = entry.model_id
        api_key_env = entry.api_key_env
        if not store.origin_approved(llm_url, f"model:{entry.provider}", api_key_env or ""):
            return "", llm_model, "", entry.max_output_tokens, "LLM origin not approved"
        if not secret_ref_allowed_for_origin(api_key_env, llm_url):
            return (
                "",
                llm_model,
                "",
                entry.max_output_tokens,
                "LLM secret_ref not allowed for this origin",
            )
        key = resolve_provider_secret(api_key_env, SecretStore()) if api_key_env else None
        if api_key_env and not key:
            return (
                "",
                llm_model,
                "",
                entry.max_output_tokens,
                "LLM secret_ref is not decryptable",
            )
        return llm_url, llm_model, key or "", entry.max_output_tokens, None

    async def run(self, args: AudioOverviewArgs, ctx: ToolContext) -> ToolOutcome:
        from disco.agent_server.audio_config import (
            SILENCE_MS_DEFAULT,
            SPEACHES_URL,
        )
        from disco.core.llm import ConfigStore

        report_text = args.report_text
        filename = args.filename

        # --- Step 0: Resolve TTS settings (toggle + backend + voices) -------
        # ConfigStore is the single source of truth; the agent-server reloads it
        # per request, so a Settings change drives the NEXT overview. `enabled`
        # is the RAM gate — when off we fail soft rather than loading the model.
        tts = ConfigStore().load().tts
        tts_cfg, disabled_outcome = await self._resolve_tts_settings(tts, SPEACHES_URL)
        if disabled_outcome is not None:
            return disabled_outcome
        assert tts_cfg is not None  # enabled path resolves a config
        voice_a = tts_cfg.voice_a
        voice_b = tts_cfg.voice_b

        # --- Steps 1 & 2: Generate turn-script via LLM (+ one retry) ---------
        llm_url, llm_model, llm_key, max_output_tokens, llm_error = self._resolve_script_llm()
        if llm_error is not None:
            return ToolOutcome(success=False, content=llm_error, error=llm_error)
        turns, gen_failure = await self._generate_turn_script(
            report_text, llm_url, llm_model, llm_key, max_output_tokens
        )
        if gen_failure is not None:
            return gen_failure

        # --- Step 3: Synthesise each turn to PCM ----------------------------
        assert turns is not None  # validated above
        pcm_turns, synth_failure = await self._synthesize_turns(turns, tts_cfg)
        if synth_failure is not None:
            return synth_failure
        assert pcm_turns is not None

        # --- Step 4: Mix in PCM, then encode the whole overview to MP3 once --
        mixed_mp3 = self._mix_and_encode(pcm_turns, SILENCE_MS_DEFAULT)

        # --- Step 5: Build transcript ---------------------------------------
        transcript_text = self._build_transcript(turns, voice_a, voice_b)

        # --- Step 6: Write through sandbox (jailed) + return outcome ---------
        return await self._write_outputs(
            ctx,
            filename,
            mixed_mp3,
            transcript_text,
            tts_cfg,
            turn_count=len(turns),
            silence_ms=SILENCE_MS_DEFAULT,
        )
