"""OpenAI-compatible chat-completions transport for the turn-script pipeline.

Extracted from ``audio_overview.py``. ``_call_llm`` and ``_call_llm_with_key``
are re-exported as module-level attributes of ``audio_overview`` (and imported
directly by ``report_audio.py`` / tests) precisely so
``monkeypatch.setattr(audio_overview, "_call_llm", fake)`` and
``mock.patch("disco.tools.builtin.audio_overview._call_llm", ...)`` keep
working: every caller that matters (``AudioOverviewTool._generate_turn_script``'s
inner closure, and ``report_audio.py``'s qualified ``audio_overview._call_llm``
access) resolves the name at call time through ``audio_overview``'s own module
globals, not through this module's.
"""

from __future__ import annotations

import httpx

from ._types import LLMResponse


async def _call_llm(payload: dict, llm_url: str) -> LLMResponse:
    """Call the LLM without discarding the provider's completion status."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0), trust_env=False, follow_redirects=False
    ) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        return LLMResponse(
            content=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason"),
        )


async def _call_llm_with_key(payload: dict, llm_url: str, api_key: str) -> LLMResponse:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0), trust_env=False, follow_redirects=False
    ) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        return LLMResponse(
            content=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason"),
        )
