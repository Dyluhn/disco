"""TitleService — auto-titling from the first user message.

Covers the pure helpers (extraction, sanitization, fallback) + the service's
idempotency / first-message-gating / model-then-fallback behavior with a fake store
and a fake router.
"""

from __future__ import annotations

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
        ('"Tetris Game"', "Tetris Game"),
        ("Title: Personal Finance Dashboard", "Personal Finance Dashboard"),
        ("  A   macOS   Desktop   Clone  ", "A macOS Desktop Clone"),
        ("Recipe App.", "Recipe App"),
        ("“Weather Widget”", "Weather Widget"),
    ],
)
def test_sanitize_title(raw: str, expected: str) -> None:
    assert sanitize_title(raw) == expected


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
async def test_run_noop_when_no_first_message_yet() -> None:
    store = _FakeStore([_env("ctx only")])
    router = _FakeRouter(text="X")
    svc = TitleService(store, lambda *a, **k: router)
    await svc._run("cid")  # type: ignore[attr-defined]
    assert store.updated_to is None
    assert router.calls == 0
