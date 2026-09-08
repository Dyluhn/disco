"""TitleService — auto-titling from the first user message.

Covers the pure helpers (extraction, sanitization, fallback) + the service's
idempotency / first-message-gating / model-then-fallback behavior with a fake store
and a fake router.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server.title_service import (
    TitleService,
    fallback_title,
    first_user_text,
    sanitize_title,
)
from disco.core.events import EventSource, MessageEvent
from disco.core.llm.types import LLMMessage


def _user(content: str, *, steer: bool = False) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=content),
        meta={"steer": True} if steer else {},
    )


def _env(content: str) -> MessageEvent:
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content=content),
    )


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_first_user_text_skips_environment_and_steer() -> None:
    events = [
        _env("HIDDEN: a giant piped Deep Research report ..."),
        _user("Build a macOS desktop clone with a working dock", steer=True),  # a steer
        _user("Build a Tetris game in one HTML file"),
    ]
    # ENVIRONMENT skipped, steer skipped → the first genuine task.
    assert first_user_text(events) == "Build a Tetris game in one HTML file"


def test_first_user_text_none_when_no_user_message() -> None:
    assert first_user_text([_env("ctx only")]) is None
    assert first_user_text([]) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        # clean
        ('"Tetris Game"', "Tetris Game"),
        ("Title: Personal Finance Dashboard", "Personal Finance Dashboard"),
        ("  A   macOS   Desktop   Clone  ", "A macOS Desktop Clone"),
        ("Recipe App.", "Recipe App"),
        ("“Weather Widget”", "Weather Widget"),
        # markdown emphasis / heading / code
        ("**Tetris Game**", "Tetris Game"),
        ("## Budget Tracker", "Budget Tracker"),
        ("`Markdown Editor`", "Markdown Editor"),
        # list / bullet markers
        ("1. Kanban Board", "Kanban Board"),
        ("- Todo App", "Todo App"),
        ("• Notes App", "Notes App"),
        # conversational preamble (mid-string)
        ("Sure! Here's a title: Recipe Finder App", "Recipe Finder App"),
        ("Project Name: Foo Bar Baz", "Foo Bar Baz"),
        # multi-line — title on line 1, explanation after (must NOT be merged in)
        ("Weather Dashboard\nThis title captures the essence of the app", "Weather Dashboard"),
        ("Title: Pomodoro Timer\n\nThis is concise and descriptive.", "Pomodoro Timer"),
        # leading emoji / stray symbols
        ("🚀 Rocket Launch Tracker", "Rocket Launch Tracker"),
        # MUST preserve mid-token '#' and '_' (regression: don't break C#/F#/snake_case)
        ("C# Game Engine", "C# Game Engine"),
        ("F# Web Server", "F# Web Server"),
        ("my_cool_app dashboard", "my_cool_app dashboard"),
        # degenerate
        ("", ""),
        ("   \n  ", ""),
    ],
)
def test_sanitize_title(raw: str, expected: str) -> None:
    assert sanitize_title(raw) == expected


def test_sanitize_title_does_not_overstrip_legit_titles_with_colon() -> None:
    # A real title that merely contains a colon but no title:/name: preamble stays whole.
    assert sanitize_title("AI: A Modern Approach") == "AI: A Modern Approach"


def test_sanitize_title_clamps_long_output_on_word_boundary() -> None:
    out = sanitize_title("A " * 80)  # very long
    assert len(out) <= 60
    assert not out.endswith(" ")


def test_fallback_title_uses_first_words() -> None:
    t = fallback_title(
        "Build a fully featured kanban board with drag and drop and labels and filters"
    )
    assert t.startswith("Build a fully featured kanban")
    assert len(t) <= 60


# ── service behavior ──────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, events: list[MessageEvent], title: str | None = None) -> None:
        self._events = events
        self.title = title
        self.updated_to: str | None = None

    async def get_title(self, cid: str) -> str | None:
        return self.title

    async def get_events(self, cid: str, *a, **k) -> list[MessageEvent]:
        return self._events

    async def update_title(self, cid: str, title: str) -> None:
        self.title = title
        self.updated_to = title


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeRouter:
    def __init__(self, text: str | None = None, raises: bool = False) -> None:
        self._text = text
        self._raises = raises
        self.calls = 0

    async def complete(self, req):  # noqa: ANN001
        self.calls += 1
        if self._raises:
            raise RuntimeError("provider down")
        return _FakeResp(self._text or "")


class _ScriptedRouter:
    """Returns scripted texts in order; records each request's max_tokens."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0
        self.max_tokens_seen: list[int | None] = []

    async def complete(self, req):  # noqa: ANN001
        self.calls += 1
        self.max_tokens_seen.append(getattr(req, "max_tokens", None))
        return _FakeResp(self._texts.pop(0) if self._texts else "")


@pytest.mark.anyio
async def test_run_summarizes_and_persists() -> None:
    store = _FakeStore([_user("Build a Tetris game in one HTML file")])
    router = _FakeRouter(text="Single-File Tetris Game")
    svc = TitleService(store, lambda *a, **k: router)
    await svc._run("cid")  # type: ignore[attr-defined]
    assert store.updated_to == "Single-File Tetris Game"
    assert router.calls == 1


@pytest.mark.anyio
async def test_run_is_idempotent_when_already_titled() -> None:
    store = _FakeStore([_user("Build something")], title="Already Named")
    router = _FakeRouter(text="New Title")
    svc = TitleService(store, lambda *a, **k: router)
    await svc._run("cid")  # type: ignore[attr-defined]
    assert store.updated_to is None  # never overwrote
    assert router.calls == 0  # never even called the model


@pytest.mark.anyio
async def test_run_falls_back_when_model_fails() -> None:
    store = _FakeStore([_user("Build a personal finance dashboard with charts")])
    router = _FakeRouter(raises=True)
    svc = TitleService(store, lambda *a, **k: router)
    await svc._run("cid")  # type: ignore[attr-defined]
    assert store.updated_to is not None
    assert store.updated_to.startswith("Build a personal finance")  # fallback snippet


@pytest.mark.anyio
async def test_a_failed_summarizer_call_says_so_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The title it falls back to is permanent, so the failure has to be findable.

    It was logged at DEBUG, which is why a 14,976-line acceptance log covering
    299 conversations carried exactly one line about titling at all.
    """
    store = _FakeStore([_user("Build a personal finance dashboard with charts")])
    svc = TitleService(store, lambda *a, **k: _FakeRouter(raises=True))

    with caplog.at_level("WARNING", logger="disco.agent_server.title_service"):
        await svc._run("cid")  # type: ignore[attr-defined]

    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "auto-title: summarizer call failed (RuntimeError: provider down); "
        "storing the first-words fallback"
    ]


@pytest.mark.anyio
async def test_a_summarizer_that_returns_nothing_usable_says_so_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two empty passes is the other failure shape, and it raised nothing."""
    store = _FakeStore([_user("Build a personal finance dashboard with charts")])
    svc = TitleService(store, lambda *a, **k: _ScriptedRouter(["", ""]))

    with caplog.at_level("WARNING", logger="disco.agent_server.title_service"):
        await svc._run("cid")  # type: ignore[attr-defined]

    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "auto-title: summarizer '' produced no usable title in two passes; "
        "storing the first-words fallback"
    ]


@pytest.mark.anyio
async def test_run_noop_when_no_first_message_yet() -> None:
    store = _FakeStore([_env("ctx only")])
    router = _FakeRouter(text="X")
    svc = TitleService(store, lambda *a, **k: router)
    await svc._run("cid")  # type: ignore[attr-defined]
    assert store.updated_to is None
    assert router.calls == 0


@pytest.mark.anyio
async def test_ensure_joins_scheduled_title_generation_once() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingRouter(_FakeRouter):
        async def complete(self, req):  # noqa: ANN001
            self.calls += 1
            started.set()
            await release.wait()
            return _FakeResp("Open Source Release Survey")

    store = _FakeStore([_user("What open-source models were released this week?")])
    router = _BlockingRouter()
    service = TitleService(store, lambda *args, **kwargs: router)
    service.schedule("cid")
    await started.wait()
    waiters = [
        asyncio.create_task(service.ensure("cid")),
        asyncio.create_task(service.ensure("cid")),
    ]
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(*waiters) == [
        "Open Source Release Survey",
        "Open Source Release Survey",
    ]
    assert router.calls == 1


@pytest.mark.anyio
async def test_ensure_times_out_to_fallback_without_cancelling_canonical_title(
    monkeypatch,
) -> None:
    import disco.agent_server.title_service as title_service

    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingRouter(_FakeRouter):
        async def complete(self, req):  # noqa: ANN001
            self.calls += 1
            started.set()
            await release.wait()
            return _FakeResp("Open Source Release Survey")

    task_text = "What open-source models were released this week?"
    store = _FakeStore([_user(task_text)])
    router = _BlockingRouter()
    service = TitleService(store, lambda *args, **kwargs: router)
    monkeypatch.setattr(title_service, "_TITLE_ENSURE_TIMEOUT_S", 0.01)

    ensure = asyncio.create_task(service.ensure("cid"))
    await started.wait()
    assert await ensure == fallback_title(task_text)
    canonical = service._inflight["cid"]  # type: ignore[attr-defined]
    assert not canonical.done()

    release.set()
    await canonical
    assert store.title == "Open Source Release Survey"
    assert router.calls == 1


@pytest.mark.anyio
async def test_think_only_summary_retries_with_wider_budget() -> None:
    """A reasoning SUMMARIZER can spend the whole 24-token budget inside <think>
    (sanitize → ""), which used to store the fallback clamp PERMANENTLY — the
    cut-off Deep Research PDF cover title. The retry must widen and win."""
    store = _FakeStore([_user("How did the transatlantic telegraph cable change global finance?")])
    router = _ScriptedRouter(
        [
            "<think>The user wants a concise title for",  # budget died mid-think
            "<think>ok</think>Transatlantic Telegraph and Global Finance",
        ]
    )
    svc = TitleService(store, lambda *a, **k: router)
    await svc._run("cid")  # type: ignore[attr-defined]
    assert store.updated_to == "Transatlantic Telegraph and Global Finance"
    assert router.max_tokens_seen == [24, 384]


@pytest.mark.anyio
async def test_backfill_retitles_stored_fallback_clamp() -> None:
    task = "How did the transatlantic telegraph cable change global finance and news?"
    store = _FakeStore([_user(task)], title=fallback_title(task))
    router = _FakeRouter(text="Transatlantic Telegraph History")
    svc = TitleService(store, lambda *a, **k: router)
    out = await svc.backfill(["cid"], retitle_fallbacks=True)
    assert out == {"cid": "Transatlantic Telegraph History"}
    assert store.title == "Transatlantic Telegraph History"


@pytest.mark.anyio
async def test_backfill_leaves_real_titles_alone_even_when_forced() -> None:
    store = _FakeStore([_user("Build a Tetris game")], title="Single-File Tetris")
    router = _FakeRouter(text="Different Title")
    svc = TitleService(store, lambda *a, **k: router)
    out = await svc.backfill(["cid"], retitle_fallbacks=True)
    assert out == {}
    assert store.title == "Single-File Tetris"
    assert router.calls == 0


def test_sanitize_title_strips_leaked_think() -> None:
    assert sanitize_title(
        "<think>The user wants a concise title</think>Container Shipping Overview"
    ) == ("Container Shipping Overview")
    # Unclosed think consumes everything → empty → caller falls back honestly.
    assert sanitize_title("<think>The user wants a concise title (3-6 words, Title") == ""
