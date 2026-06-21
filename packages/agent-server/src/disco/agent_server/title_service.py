"""Auto-titling — derive a short, human display title for a conversation from its
first user message, so History/Projects show meaningful names instead of a wall of
``(untitled)``.

Design (runthru-v2): fire EARLY + ASYNC the moment the first user message lands — that
message IS the task, so there's nothing to wait for. Summarize it with the cheap
``SUMMARIZER``-role model into a 3-6 word Title-Case label and persist it. It NEVER
blocks the build loop (a detached task) and is NEVER worse than today (falls back to a
cleaned first-message snippet if the model call fails / there's no key). Idempotent: it
only acts when the title is still unset, so it's safe to call from ``kick`` on every
message.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from disco.core.events import Event, EventSource, MessageEvent
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    LLMMessage,
    ModelRole,
)

_LOG = logging.getLogger(__name__)

# Built by concatenation (NOT str.format) so a task containing literal { } braces can't
# break formatting or inject placeholders.
_PROMPT_PREFIX = (
    "Generate a concise, specific title for the project or task described below.\n"
    "Rules: 3 to 6 words, Title Case, plain text only — NO markdown, NO quotes, NO "
    "preamble or explanation, NO trailing punctuation. Output ONLY the title.\n\nTask:\n"
)

_MAX_TITLE_CHARS = 60
_MAX_TASK_CHARS = 800  # cap the prompt input — first messages can be very long
_QUOTE_CHARS = "\"'`“”‘’"  # straight + smart quotes + backtick

# A leading "…title:" / "…name:" preamble (≤40 chars before the colon so it can't fire
# on a legit title that merely contains the word later). Captures what follows.
_PREAMBLE_RE = re.compile(r"^.{0,40}?\b(?:title|name)\s*[:\-]\s*(.+)$", re.IGNORECASE)
# Leading list / blockquote / bullet markers ("1. ", "- ", "• ", "> ", "* ").
_LEADER_RE = re.compile(r"^\s*(?:[-•>*]|\d+[.)])\s+")
# Leading junk before the first real character (emoji, stray symbols), while KEEPING a
# leading letter/digit/quote/paren so real titles survive.
_LEAD_JUNK_RE = re.compile(r"^[^\w(\"'“‘]+")


def first_user_text(events: Sequence[Event]) -> str | None:
    """The first genuine USER message's text (the task). Skips ENVIRONMENT messages
    (hidden seed-context like a piped Deep Research report) and steer messages."""
    for ev in events:
        if not isinstance(ev, MessageEvent):
            continue
        if ev.source != EventSource.USER:
            continue
        if (ev.meta or {}).get("steer"):
            continue
        text = (ev.message.content or "").strip()
        if text:
            return text
    return None


def sanitize_title(raw: str) -> str:
    """Make a model-produced title presentable + CONSISTENT across chatty / markdown /
    multi-line / preamble outputs (different SUMMARIZER models misbehave differently).

    Steps, in order: take the first non-empty line (any explanation usually follows on
    later lines); peel a leading ``…title:`` / ``name:`` preamble; strip list/heading
    markers + markdown emphasis (``*`` and `` ` `` — but NOT ``#``, so ``C#``/``F#``
    survive); strip leading emoji/symbols; collapse whitespace; strip wrapping quotes +
    trailing punctuation; clamp to the char budget on a word boundary."""
    text = (raw or "").strip()
    if not text:
        return ""
    # 1. First non-empty line — a chatty model puts the title on line 1, prose after.
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    # 2. Pull the title out of a leading "…title: X" / "…name: X" preamble.
    m = _PREAMBLE_RE.match(first.strip())
    t = m.group(1) if m else first
    # 3. Strip leading list/blockquote markers + leading markdown heading (#…), then
    #    markdown emphasis. KEEP '#' mid-token (C#, F#) and '_' (snake_case names).
    t = _LEADER_RE.sub("", t)
    t = re.sub(r"^#+\s*", "", t)
    t = t.replace("*", "").replace("`", "")
    # 4. Strip leading emoji / stray symbols (keeps a leading letter/digit/quote/paren).
    t = _LEAD_JUNK_RE.sub("", t)
    # 5. Collapse internal whitespace; strip wrapping quotes + trailing punctuation.
    t = " ".join(t.split())
    t = t.strip().strip(_QUOTE_CHARS).strip()
    t = t.rstrip(".,;:!?—-").strip()
    # 6. Clamp on a word boundary.
    if len(t) > _MAX_TITLE_CHARS:
        clipped = t[:_MAX_TITLE_CHARS].rsplit(" ", 1)[0].strip()
        t = clipped or t[:_MAX_TITLE_CHARS].strip()
    return t


def fallback_title(task: str) -> str:
    """A no-LLM title: the first words of the task, cleaned + clamped. The floor when
    the model call fails — still far better than ``(untitled)``."""
    one_line = " ".join((task or "").split())
    if not one_line:
        return ""
    words = one_line.split(" ")[:8]
    return sanitize_title(" ".join(words))


class TitleService:
    """Idempotent, fire-and-forget auto-titler. ``schedule(cid)`` is called from the
    runtime's ``kick`` (every message), so it must be cheap + self-gating: it no-ops
    when a title already exists or the first user message isn't present yet."""

    def __init__(self, store: Any, router_now: Callable[..., Any]) -> None:
        self._store = store
        self._router_now = router_now
        self._inflight: set[str] = set()

    def schedule(self, conversation_id: str) -> None:
        """Kick off auto-titling (no-op if one is already in flight this process).
        Detached — never awaited by the caller, so it can't delay the build."""
        if conversation_id in self._inflight:
            return
        self._inflight.add(conversation_id)
        asyncio.create_task(self._run_guarded(conversation_id))

    async def _run_guarded(self, cid: str) -> None:
        try:
            await self._run(cid)
        except Exception:  # auto-title is best-effort; never surface to the user
            _LOG.debug("auto-title failed for %s", cid, exc_info=True)
        finally:
            # Clear in-flight LAST. If the first message wasn't present yet, a later
            # kick retries (the title is still unset).
            self._inflight.discard(cid)

    async def _run(self, cid: str) -> None:
        if await self._store.get_title(cid):
            return  # already titled — idempotent
        events = await self._store.get_events(cid)
        task = first_user_text(events)
        if not task:
            return  # first user message not present yet; a later kick retries
        title = await self._summarize(task) or fallback_title(task)
        if title:
            await self._store.update_title(cid, title)
            _LOG.info("auto-titled %s -> %r", cid, title)

    async def _summarize(self, task: str) -> str:
        """Model-summarized title via the cheap SUMMARIZER role. Returns '' on any
        failure (the caller falls back to a truncated snippet)."""
        try:
            router = self._router_now()
            resp = await router.complete(
                CompletionRequest(
                    profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
                    messages=[
                        LLMMessage(
                            role="user", content=_PROMPT_PREFIX + task[:_MAX_TASK_CHARS]
                        )
                    ],
                    temperature=0.0,
                    max_tokens=24,
                )
            )
            return sanitize_title(getattr(resp, "text", "") or "")
        except Exception:
            _LOG.debug("title summarization call failed", exc_info=True)
            return ""

    async def backfill(self, conversation_ids: Sequence[str]) -> dict[str, str]:
        """Title a batch of existing conversations (the one-off pass for the wall of
        pre-feature ``(untitled)`` rows). Sequential to avoid hammering the model;
        returns ``{cid: title}`` for the ones it set."""
        result: dict[str, str] = {}
        for cid in conversation_ids:
            try:
                if await self._store.get_title(cid):
                    continue
                events = await self._store.get_events(cid)
                task = first_user_text(events)
                if not task:
                    continue
                title = await self._summarize(task) or fallback_title(task)
                if title:
                    await self._store.update_title(cid, title)
                    result[cid] = title
            except Exception:
                _LOG.debug("backfill title failed for %s", cid, exc_info=True)
        return result
