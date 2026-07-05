"""Generative splash suggestions for the Research/Build/Agent entry surfaces.

Suggestions are request-time only: startup never calls the model. A short-lived
disk cache keeps the splash fresh without turning every mount into an LLM call,
and callers always have a curated fallback when generation is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    LLMMessage,
    ModelRole,
)
from disco.core.think import strip_think_spans

_LOG = logging.getLogger(__name__)

SuggestionSurface = Literal["research", "build", "agent"]
SuggestionSource = Literal["generated", "curated"]

_TTL_S = 24 * 60 * 60
_MAX_SUGGESTION_CHARS = 90
# A reasoning-class SUMMARIZER thinks at length before emitting the 8 lines; a
# tight budget dies inside <think> and strips to nothing (the title-service
# lesson). Give reasoning room to FINISH — the cache amortizes the cost.
_MAX_OUTPUT_TOKENS = 1400
_GENERATION_TIMEOUT_S = 60.0
# How long a REQUEST waits on generation before serving the curated pool. The
# background task keeps running and caches, so the next view gets generated.
_REQUEST_WAIT_S = 8.0
_QUOTE_CHARS = "\"'`“”‘’"
_LEADER_RE = re.compile(r"^\s*(?:[-•>*]|\d+[.)])\s+")

CURATED_SUGGESTIONS: dict[SuggestionSurface, tuple[str, ...]] = {
    "research": (
        "What changed in US nuclear power permitting since 2020?",
        "Compare sodium-ion and LFP batteries for grid storage",
        "Map the evidence for and against GLP-1 use in adolescents",
        "Trace how NASA's Artemis schedule shifted and why",
        "What are the strongest critiques of carbon offset registries?",
        "Compare the EU AI Act with US state AI laws for model developers",
        "Which wildfire mitigation programs have measurable results?",
        "How do housing supply reforms in Auckland and Minneapolis compare?",
    ),
    "build": (
        "a kanban board with drag and drop",
        "a landing page for a coffee roastery",
        "a 2048 clone",
        "an invoice PDF generator",
        "a habit tracker with weekly streak charts",
        "a personal finance dashboard from CSV uploads",
        "a classroom quiz game with score tracking",
        "a docs site with search and dark mode",
    ),
    "agent": (
        "summarize the files I upload into a brief",
        "fill this form from my notes",
        "browse example.com and extract pricing",
        "turn my meeting transcript into decisions and next steps",
        "find broken links on this site and list fixes",
        "extract deadlines from these PDFs",
        "check whether this CSV has duplicate customer rows",
        "turn my rough spec into implementation tickets",
    ),
}

_SURFACE_PROMPTS: dict[SuggestionSurface, str] = {
    "research": (
        "questions worth researching. They should invite comparison, evidence review, "
        "timelines, tradeoffs, or source-backed explanation"
    ),
    "build": (
        "things to build: sites, decks, sheets, games, apps, dashboards, documents, "
        "and small tools"
    ),
    "agent": (
        "small multi-step computer tasks involving files, browsing, forms, extraction, "
        "cleanup, comparison, or planning"
    ),
}


def curated_suggestions(surface: SuggestionSurface) -> list[str]:
    return list(CURATED_SUGGESTIONS[surface])


def sanitize_suggestion(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _LEADER_RE.sub("", text)
    text = re.sub(r"^#+\s*", "", text)
    text = text.replace("*", "").replace("`", "")
    text = " ".join(text.split())
    text = text.strip().strip(_QUOTE_CHARS).strip()
    if len(text) > _MAX_SUGGESTION_CHARS:
        clipped = text[:_MAX_SUGGESTION_CHARS].rsplit(" ", 1)[0].strip()
        text = clipped or text[:_MAX_SUGGESTION_CHARS].strip()
    return text


def parse_suggestions(raw: str) -> list[str]:
    text = strip_think_spans(raw or "")
    suggestions: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        item = sanitize_suggestion(line)
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(item)
        if len(suggestions) >= 8:
            break
    if len(suggestions) < 4:
        raise ValueError("not enough usable generated suggestions")
    return suggestions


def _prompt(surface: SuggestionSurface) -> str:
    return (
        "Generate 8 diverse, concrete, ONE-LINE example prompts for the Disco "
        f"{surface} splash.\n"
        f"Surface meaning: {_SURFACE_PROMPTS[surface]}.\n"
        "Rules: output exactly 8 lines, one prompt per line, no numbering required, "
        "no markdown, no quotes, no categories, no explanations. Each line must be "
        "specific, user-ready, and at most 90 characters."
    )


class SuggestionService:
    def __init__(
        self,
        router_now: Callable[..., Any],
        projects_root_now: Callable[[], str | Path],
    ) -> None:
        self._router_now = router_now
        self._projects_root_now = projects_root_now
        self._inflight: dict[SuggestionSurface, asyncio.Task[list[str]]] = {}

    async def get_generated(self, surface: SuggestionSurface) -> list[str]:
        cached = self._read_cache(surface)
        if cached:
            return cached

        task = self._inflight.get(surface)
        if task is None or task.done():
            task = asyncio.create_task(self._generate_and_cache(surface))
            self._inflight[surface] = task
            task.add_done_callback(lambda t, s=surface: self._on_done(s, t))
        # Shielded short wait: a slow generation must not stall the splash —
        # this request falls back to curated while the task finishes + caches.
        return await asyncio.wait_for(asyncio.shield(task), timeout=_REQUEST_WAIT_S)

    def _on_done(self, surface: SuggestionSurface, task: asyncio.Task[list[str]]) -> None:
        if self._inflight.get(surface) is task:
            self._inflight.pop(surface, None)
        if not task.cancelled() and task.exception() is not None:
            _LOG.debug("suggestion generation failed for %s", surface, exc_info=task.exception())

    async def _generate_and_cache(self, surface: SuggestionSurface) -> list[str]:
        suggestions = await asyncio.wait_for(
            self._generate(surface), timeout=_GENERATION_TIMEOUT_S
        )
        self._write_cache(surface, suggestions)
        return suggestions

    async def _generate(self, surface: SuggestionSurface) -> list[str]:
        router = self._router_now()
        resp = await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
                messages=[LLMMessage(role="user", content=_prompt(surface))],
                temperature=0.7,
                max_tokens=_MAX_OUTPUT_TOKENS,
            )
        )
        return parse_suggestions(getattr(resp, "text", "") or "")

    def _cache_path(self, surface: SuggestionSurface) -> Path:
        root = Path(self._projects_root_now()).expanduser()
        return root / ".disco" / "suggestions" / f"{surface}.json"

    def _read_cache(self, surface: SuggestionSurface) -> list[str] | None:
        try:
            path = self._cache_path(surface)
            data = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(data.get("created_at") or 0)
            if time.time() - created_at > _TTL_S:
                return None
            if data.get("surface") != surface:
                return None
            suggestions = data.get("suggestions")
            if not isinstance(suggestions, list):
                return None
            parsed = parse_suggestions("\n".join(str(s) for s in suggestions))
            return parsed if len(parsed) >= 4 else None
        except FileNotFoundError:
            return None
        except Exception:
            _LOG.debug("suggestion cache read failed for %s", surface, exc_info=True)
            return None

    def _write_cache(self, surface: SuggestionSurface, suggestions: list[str]) -> None:
        try:
            path = self._cache_path(surface)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "surface": surface,
                "created_at": time.time(),
                "suggestions": suggestions[:8],
            }
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception:
            _LOG.debug("suggestion cache write failed for %s", surface, exc_info=True)


__all__ = [
    "SuggestionService",
    "SuggestionSource",
    "SuggestionSurface",
    "curated_suggestions",
    "parse_suggestions",
    "sanitize_suggestion",
]
