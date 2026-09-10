"""Auto-titling — derive a short, human display title for a conversation from its
first user message, so History/Projects show meaningful names instead of a wall of
``(untitled)``.

Design (runthru-v2): fire EARLY + ASYNC the moment the first user message lands — that
message IS the task, so there's nothing to wait for. Summarize it with the cheap
``SUMMARIZER``-role model into a 3-6 word Title-Case label and persist it. It NEVER
blocks the build loop (a detached task) and is NEVER worse than today (one retry, then a
few-word label derived from the first message if the model call fails / there's no key).
Idempotent: it only acts when the title is still unset, so it's safe to call from ``kick``
on every message. The name is claimed through the store's de-duplicating write, so two
conversations can't end up with the identical label.
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
from disco.core.think import strip_think_spans

_LOG = logging.getLogger(__name__)

# Built by concatenation (NOT str.format) so a task containing literal { } braces can't
# break formatting or inject placeholders.
_PROMPT_PREFIX = (
    "Generate a concise, specific title for the project or task described below.\n"
    "Rules: 3 to 6 words, Title Case, plain text only — NO markdown, NO quotes, NO "
    "preamble or explanation, NO trailing punctuation. Output ONLY the title.\n\nTask:\n"
)

_MAX_TITLE_CHARS = 60
# UI-13: the no-LLM fallback is a LABEL, not a summary of the task. Its own, much
# harder cap (below) is what stops a failed summarizer from writing the first
# message across the History row and the finish screen's title slot.
_MAX_FALLBACK_WORDS = 5
_MAX_FALLBACK_CHARS = 42
_MAX_TASK_CHARS = 800  # cap the prompt input — first messages can be very long
_TITLE_ENSURE_TIMEOUT_S = 15.0
_QUOTE_CHARS = "\"'`“”‘’"  # straight + smart quotes + backtick

# A leading "…title:" / "…name:" preamble (≤40 chars before the colon so it can't fire
# on a legit title that merely contains the word later). Captures what follows.
_PREAMBLE_RE = re.compile(r"^.{0,40}?\b(?:title|name)\s*[:\-]\s*(.+)$", re.IGNORECASE)
# Leading list / blockquote / bullet markers ("1. ", "- ", "• ", "> ", "* ").
_LEADER_RE = re.compile(r"^\s*(?:[-•>*]|\d+[.)])\s+")
# Leading junk before the first real character (emoji, stray symbols), while KEEPING a
# leading letter/digit/quote/paren so real titles survive.
_LEAD_JUNK_RE = re.compile(r"^[^\w(\"'“‘]+")


def _clamp(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` chars on a word boundary (whole word if the first
    word alone already overflows)."""
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return clipped or text[:limit].strip()


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
    # 0. A reasoning model can leak <think>… into the summary; reasoning is never a
    #    title. Closed spans are cut; an unclosed leading think consumes everything
    #    (returns "" → caller falls back to the first-message heuristic).
    text = strip_think_spans(text)
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
    return _clamp(t, _MAX_TITLE_CHARS)


def fallback_title(task: str) -> str:
    """A no-LLM title: the first FEW words of the task, cleaned + hard-capped. The
    floor when the model call fails — still far better than ``(untitled)``.

    UI-13: what this returns is stored PERMANENTLY (``_run`` is idempotent), so the
    old 8-word / 60-char clamp let a whole short prompt land as the conversation's
    title — the one build in fifteen whose History row and finish screen showed the
    full task and pushed the deliverable card down. A few words, always: at most
    ``_MAX_FALLBACK_WORDS`` words AND ``_MAX_FALLBACK_CHARS`` characters."""
    one_line = " ".join((task or "").split())
    if not one_line:
        return ""
    words = one_line.split(" ")[:_MAX_FALLBACK_WORDS]
    return _clamp(sanitize_title(" ".join(words)), _MAX_FALLBACK_CHARS)


class TitleService:
    """Idempotent, fire-and-forget auto-titler. ``schedule(cid)`` is called from the
    runtime's ``kick`` (every message), so it must be cheap + self-gating: it no-ops
    when a title already exists or the first user message isn't present yet."""

    def __init__(self, store: Any, router_now: Callable[..., Any]) -> None:
        self._store = store
        self._router_now = router_now
        self._inflight: dict[str, asyncio.Task[None]] = {}

    def schedule(self, conversation_id: str) -> None:
        """Kick off auto-titling (no-op if one is already in flight this process).
        Detached — never awaited by the caller, so it can't delay the build."""
        task = self._inflight.get(conversation_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._run_guarded(conversation_id))
        self._inflight[conversation_id] = task

    async def ensure(self, conversation_id: str) -> str | None:
        """Wait for this conversation's canonical title, generating it once.

        Normal UI startup remains fire-and-forget through :meth:`schedule`.
        Consumers that permanently render the title (notably report export)
        use this join point so they cannot race the same title lifecycle.
        """

        existing = await self._store.get_title(conversation_id)
        if existing:
            return existing
        task = self._inflight.get(conversation_id)
        if task is None or task.done():
            task = asyncio.create_task(self._run_guarded(conversation_id))
            self._inflight[conversation_id] = task
        try:
            async with asyncio.timeout(_TITLE_ENSURE_TIMEOUT_S):
                await asyncio.shield(task)
        except TimeoutError:
            # Export must not inherit an indefinitely stalled model call.  The
            # detached canonical-title task stays alive for History/UI, while
            # this caller receives the same deterministic fallback immediately.
            events = await self._store.get_events(conversation_id)
            fallback = fallback_title(first_user_text(events) or "")
            return fallback or None
        return await self._store.get_title(conversation_id)

    async def _run_guarded(self, cid: str) -> None:
        try:
            await self._run(cid)
        except Exception:  # auto-title is best-effort; never surface to the user
            # Best-effort to the user, not to the log: the title this conversation
            # ends up with is permanent (see _run's idempotence), so a failure
            # here has to be findable afterwards.
            _LOG.warning("auto-title failed for %s", cid, exc_info=True)
        finally:
            # Clear in-flight LAST. If the first message wasn't present yet, a later
            # kick retries (the title is still unset).
            task = asyncio.current_task()
            if self._inflight.get(cid) is task:
                self._inflight.pop(cid, None)

    async def _run(self, cid: str) -> None:
        if await self._store.get_title(cid):
            return  # already titled — idempotent
        events = await self._store.get_events(cid)
        task = first_user_text(events)
        if not task:
            return  # first user message not present yet; a later kick retries
        title = await self._summarize(task) or fallback_title(task)
        if title:
            stored = await self._store_title(cid, title)
            _LOG.info("auto-titled %s -> %r", cid, stored)

    async def _store_title(self, cid: str, title: str) -> str:
        """Persist the title, DE-DUPLICATED against the owner's other conversations
        (UI-31: two builds of the same prompt both landed on "Coffee Shop Subscription
        Website", leaving two History rows and two Projects cards nothing but the file
        count told apart). The store claims the name and appends " (2)" / " (3)" in one
        locked transaction, so two builds finishing titling at the same moment can't
        both take it. Returns what was actually stored.

        Stores without the claim method (older fakes) keep the plain overwrite."""
        claim = getattr(self._store, "update_title_unique", None)
        if claim is not None:
            return str(await claim(cid, title))
        await self._store.update_title(cid, title)
        return title

    async def _summarize(self, task: str) -> str:
        """Model-summarized title via the cheap SUMMARIZER role. Returns '' on any
        failure (the caller falls back to a truncated snippet).

        A reasoning-class SUMMARIZER can spend the whole tiny budget inside
        <think> — sanitize_title strips that to "", and the fallback clamp then
        gets stored PERMANENTLY (idempotence blocks retitling). So on an empty
        first pass, retry ONCE with room for the reasoning to finish AND emit
        the title (same shape as the synthesis empty-retry).

        UI-13: a RAISING call gets the same second pass. A one-off provider blip
        used to burn the title for good — one build in fifteen kept its prompt as
        its name — because the exception skipped straight to the fallback.

        Both failure shapes are logged at WARNING with their reason (once, after
        the retry). This is not a fallback anybody sees: the caller stores a short
        derived label as the conversation's permanent title and nothing revisits
        it, so a silent '' here is a defect that leaves no trace at all."""
        model = ""
        failure: Exception | None = None
        for max_tokens in (24, 384):
            try:
                router = self._router_now()
                resp = await router.complete(
                    CompletionRequest(
                        profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
                        messages=[
                            LLMMessage(role="user", content=_PROMPT_PREFIX + task[:_MAX_TASK_CHARS])
                        ],
                        temperature=0.0,
                        max_tokens=max_tokens,
                    )
                )
            except Exception as exc:
                failure = exc
                continue
            failure = None
            model = str(getattr(resp, "model_used", "") or "")
            title = sanitize_title(getattr(resp, "text", "") or "")
            if title:
                return title
        if failure is not None:
            _LOG.warning(
                "auto-title: summarizer call failed (%s: %s); storing the first-words fallback",
                type(failure).__name__,
                failure,
            )
        else:
            _LOG.warning(
                "auto-title: summarizer %r produced no usable title in two passes; "
                "storing the first-words fallback",
                model,
            )
        return ""

    async def backfill(
        self, conversation_ids: Sequence[str], *, retitle_fallbacks: bool = False
    ) -> dict[str, str]:
        """Title a batch of existing conversations (the one-off pass for the wall of
        pre-feature ``(untitled)`` rows). Sequential to avoid hammering the model;
        returns ``{cid: title}`` for the ones it set.

        ``retitle_fallbacks=True`` also revisits conversations whose stored title
        is exactly the no-LLM fallback clamp of their first message (the scar a
        failed summarizer call leaves behind — e.g. a Deep Research question cut
        off mid-sentence on the PDF cover) and replaces it when the model now
        produces a real title."""
        result: dict[str, str] = {}
        for cid in conversation_ids:
            try:
                existing = await self._store.get_title(cid)
                events = await self._store.get_events(cid)
                task = first_user_text(events)
                if not task:
                    continue
                if existing:
                    if not (retitle_fallbacks and existing == fallback_title(task)):
                        continue
                    title = await self._summarize(task)  # only a REAL title replaces
                else:
                    title = await self._summarize(task) or fallback_title(task)
                if title and title != existing:
                    result[cid] = await self._store_title(cid, title)
            except Exception:
                _LOG.debug("backfill title failed for %s", cid, exc_info=True)
        return result
