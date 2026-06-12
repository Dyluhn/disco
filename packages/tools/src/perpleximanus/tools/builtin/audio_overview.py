"""Audio overview tool — two-voice TTS from a finished deep-research report.

Pipeline:
  1. Accept the report text.
  2. Call an LLM (OpenAI-compatible chat completions) to generate a JSON
     turn-script: [{"speaker": "A"|"B", "text": "..."}].
  3. Validate the JSON schema + retry ONCE on malformed output.
  4. Call Speaches per turn to synthesise each voice line as MP3.
  5. Concatenate per-turn MP3s with inter-turn silence via _audio_mixer.
  6. Write MP3 + transcript through ctx.sandbox.write_file (jailed).
  7. Return ToolOutcome with artifact paths.

Fail-soft: if Speaches is unreachable, return success=False with a clear
message — never crash the loop.
"""

from __future__ import annotations

import json
import re

import httpx
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ._audio_mixer import mix_turns

# ---- turn-script schema -----------------------------------------------------


class Turn(BaseModel):
    speaker: str = Field(pattern=r"^(A|B)$")
    text: str = Field(min_length=1)


_TURN_SCRIPT_PROMPT = """You are a podcast scriptwriter. Given a research report, write a short 
two-host audio script covering the key insights. Host A is the lead analyst; 
Host B is the co-host who asks follow-ups and adds colour.

Output ONLY a JSON array of turns. Each turn is an object with:
  "speaker": "A" or "B"
  "text": one or two sentences (natural spoken language, no markdown)

Keep turns conversational. ~12-20 turns total. Each turn's text should be 
~100-200 tokens (a comfortable spoken sentence or two). Start with Host A 
introducing the topic.

Report to convert:
{report_text}

Output ONLY valid JSON array, nothing else:"""


def _build_llm_payload(report_text: str) -> dict:
    from perpleximanus.agent_server.audio_config import LLM_MODEL

    return {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": _TURN_SCRIPT_PROMPT.format(report_text=report_text),
            }
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
    }


async def _call_llm(payload: dict, llm_url: str) -> str:
    """Call the LLM to generate the turn-script. Returns raw response text."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> str:
    """Extract a JSON array from LLM output that may have surrounding text."""
    # Try direct parse first
    text = text.strip()
    if text.startswith("["):
        # Find the matching closing bracket
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            return text[:end]
    # Try markdown code block
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Last resort: find first [ and last ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _validate_turn_script(raw_json: str) -> tuple[list[Turn] | None, str | None]:
    """Validate the JSON turn-script. Returns (turns, error_message)."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    if not isinstance(data, list):
        return None, "Turn script must be a JSON array of turns"

    if len(data) < 2:
        return None, "Turn script must have at least 2 turns"

    turns: list[Turn] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"Turn {i} is not an object"
        speaker = item.get("speaker")
        if speaker not in ("A", "B"):
            return None, f"Turn {i}: speaker must be 'A' or 'B', got {speaker!r}"
        text = item.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return None, f"Turn {i}: text must be a non-empty string"
        turns.append(Turn(speaker=speaker, text=text.strip()))

    return turns, None


async def _synthesize(text: str, voice: str, speaches_url: str) -> bytes | None:
    """Call Speaches to synthesise text as MP3. Returns MP3 bytes or None on failure."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            f"{speaches_url}/v1/audio/speech",
            json={"input": text, "voice": voice, "response_format": "mp3"},
        )
        resp.raise_for_status()
        return resp.content


# ---- tool implementation ----------------------------------------------------


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
    )

    async def run(self, args: AudioOverviewArgs, ctx: ToolContext) -> ToolOutcome:
        from perpleximanus.agent_server.audio_config import (
            LLM_URL,
            SILENCE_MS_DEFAULT,
            SPEACHES_URL,
            VOICE_A,
            VOICE_B,
        )

        report_text = args.report_text
        filename = args.filename

        # --- Step 1: Generate turn-script via LLM ---------------------------
        payload = _build_llm_payload(report_text)
        try:
            raw_response = await _call_llm(payload, LLM_URL)
        except Exception as e:
            return ToolOutcome(
                success=False,
                content=f"LLM call failed while generating turn-script: {e}",
                error=str(e),
            )

        raw_json = _extract_json(raw_response)
        turns, error = _validate_turn_script(raw_json)

        # --- Step 2: One retry on malformed output --------------------------
        if error is not None:
            retry_payload = _build_llm_payload(report_text)
            retry_payload["messages"].append(
                {"role": "assistant", "content": raw_response}
            )
            retry_payload["messages"].append(
                {
                    "role": "user",
                    "content": f"Your JSON output was invalid: {error}\n\n"
                    f"Please fix the errors and output ONLY a valid JSON array "
                    f"of turns. Each turn must have 'speaker' (A or B) and "
                    f"'text' (non-empty string).",
                }
            )
            try:
                raw_response2 = await _call_llm(retry_payload, LLM_URL)
            except Exception as e:
                return ToolOutcome(
                    success=False,
                    content=(
                        f"Turn-script validation failed: {error}\n"
                        f"Retry LLM call also failed: {e}"
                    ),
                    error=error,
                )

            raw_json2 = _extract_json(raw_response2)
            turns, error2 = _validate_turn_script(raw_json2)
            if error2 is not None:
                return ToolOutcome(
                    success=False,
                    content=(
                        f"Turn-script validation failed after retry.\n"
                        f"First error: {error}\n"
                        f"Retry error: {error2}"
                    ),
                    error=error2,
                )

        # --- Step 3: Synthesise each turn via Speaches ----------------------
        assert turns is not None  # validated above
        turn_mp3s: list[bytes] = []
        for i, turn in enumerate(turns):
            voice = VOICE_A if turn.speaker == "A" else VOICE_B
            try:
                mp3_bytes = await _synthesize(turn.text, voice, SPEACHES_URL)
            except httpx.ConnectError:
                return ToolOutcome(
                    success=False,
                    content=(
                        f"Speaches is unreachable at {SPEACHES_URL}. "
                        f"Turn {i + 1}/{len(turns)} could not be synthesised. "
                        f"Audio overview cannot be generated."
                    ),
                    error=f"Speaches offline: {SPEACHES_URL}",
                )
            except Exception as e:
                return ToolOutcome(
                    success=False,
                    content=(
                        f"Speaches call failed for turn {i + 1}/{len(turns)} "
                        f"(speaker {turn.speaker}): {e}"
                    ),
                    error=str(e),
                )
            if mp3_bytes is None or len(mp3_bytes) == 0:
                return ToolOutcome(
                    success=False,
                    content=(
                        f"Speaches returned empty audio for turn {i + 1}/{len(turns)} "
                        f"(speaker {turn.speaker})"
                    ),
                    error="Empty audio from Speaches",
                )
            turn_mp3s.append(mp3_bytes)

        # --- Step 4: Mix with inter-turn silence ---------------------------
        mixed_mp3 = mix_turns(turn_mp3s, silence_ms=SILENCE_MS_DEFAULT)

        # --- Step 5: Build transcript ---------------------------------------
        transcript_lines: list[str] = [
            "# Audio Overview Transcript",
            "",
            f"Voices: Host A = {VOICE_A}, Host B = {VOICE_B}",
            f"Turns: {len(turns)}",
            "",
        ]
        for turn in turns:
            label = "Host A" if turn.speaker == "A" else "Host B"
            transcript_lines.append(f"**{label}:** {turn.text}")
            transcript_lines.append("")
        transcript_text = "\n".join(transcript_lines)

        # --- Step 6: Write through sandbox (jailed) -------------------------
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
                f"  Turns: {len(turns)} | Voices: {VOICE_A} (A) + {VOICE_B} (B)\n"
                f"  Silence between turns: {SILENCE_MS_DEFAULT}ms"
            ),
            artifacts=[mp3_path, transcript_path],
            structured={
                "mp3_path": mp3_path,
                "transcript_path": transcript_path,
                "turn_count": len(turns),
                "mp3_bytes": len(mixed_mp3),
                "voice_a": VOICE_A,
                "voice_b": VOICE_B,
                "silence_ms": SILENCE_MS_DEFAULT,
            },
        )
