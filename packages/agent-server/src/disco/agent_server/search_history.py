"""Durable Standard Search results, independent of agent workspace authority.

Only server-produced final frames become saved answers. Request identities make
socket replay idempotent; interrupted requests require an explicit fresh retry.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from disco.core import ConversationStatus, EventSource, LLMMessage, MessageEvent, StatusEvent
from disco.core.store.sqlite import SqliteEventStore
from pydantic import BaseModel, Field

from .redaction import redact_frame
from .title_service import fallback_title

_KEY = "standard_search"


class SearchScope(BaseModel):
    query: str = Field(min_length=1)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    parent_request_id: str | None = Field(default=None, max_length=128)
    conversation_id: str
    model_override: str | None = None
    think: bool = False
    drop_weak: bool = False
    domains_deny: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    space_ids: list[str] = Field(default_factory=list)


@contextlib.asynccontextmanager
async def close_search_stream(stream: AsyncIterator[dict[str, Any]]):
    try:
        yield stream
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


async def read_search_history(store: SqliteEventStore, cid: str) -> dict[str, Any]:
    turns, requests = [], []
    for event in await store.get_events(cid):
        if not isinstance(event, MessageEvent):
            continue
        record = event.meta.get(_KEY)
        if not isinstance(record, dict):
            continue
        if event.source is EventSource.USER:
            requests.append(record)
        elif event.source is EventSource.AGENT and isinstance(record.get("answer"), dict):
            turns.append(record)
    return {
        "conversation_id": cid,
        "turns": turns,
        "latest_request": requests[-1] if requests else None,
    }


class SearchHistory:
    def __init__(self, store: SqliteEventStore) -> None:
        self.store = store
        self.active: set[str] = set()

    async def stream(
        self, scope: SearchScope, run: Callable[[], AsyncIterator[dict[str, Any]]]
    ) -> AsyncIterator[dict[str, Any]]:
        cid = scope.conversation_id
        if self.store.conversation_origin(cid) == "imported":
            raise ValueError("Imported searches are read-only")
        if self.store.conversation_surface_sync(cid) != "research":
            raise ValueError("This conversation is not a Standard Search")
        if cid in self.active:
            raise ValueError("A search is already running in this conversation")
        self.active.add(cid)
        try:
            async with close_search_stream(self._stream(scope, run)) as frames:
                async for frame in frames:
                    yield frame
        finally:
            self.active.discard(cid)

    async def _stream(
        self, scope: SearchScope, run: Callable[[], AsyncIterator[dict[str, Any]]]
    ) -> AsyncIterator[dict[str, Any]]:
        cid = scope.conversation_id
        history = await read_search_history(self.store, cid)
        payload = scope.model_dump(mode="json")
        for turn in history["turns"]:
            if turn["scope"]["request_id"] == scope.request_id:
                if turn["scope"] != payload:
                    raise ValueError("Search request identity already used with a different scope")
                yield {"type": "final", "answer": turn["answer"]}
                yield {"type": "state", "status": "finished"}
                return
        await self._require_new_request(scope, history["turns"])
        await self.store.append_many(
            cid,
            [
                MessageEvent(
                    source=EventSource.USER,
                    message=LLMMessage(role="user", content=scope.query),
                    meta={_KEY: {"scope": payload}},
                ),
                StatusEvent(status=ConversationStatus.RUNNING, detail="standard_search"),
            ],
            require_existing=True,
        )
        if not await self.store.get_title(cid):
            await self.store.update_title(cid, fallback_title(scope.query))
        complete = False
        try:
            async with close_search_stream(run()) as frames:
                async for frame in frames:
                    if frame.get("type") == "final":
                        await self._save_answer(scope, redact_frame(frame)["answer"])
                        complete = True
                    # FINISHED is meaningful only once the complete answer is durable.
                    if frame == {"type": "state", "status": "finished"} and not complete:
                        raise ValueError("Search ended without a complete answer")
                    yield frame
        finally:
            if not complete:
                await self._record_interrupted(cid)

    async def _require_new_request(self, scope: SearchScope, turns: list[dict[str, Any]]) -> None:
        if any(
            isinstance(event, MessageEvent)
            and event.meta.get(_KEY, {}).get("scope", {}).get("request_id") == scope.request_id
            for event in await self.store.get_events(scope.conversation_id)
        ):
            raise ValueError("This search was interrupted; retry explicitly to start a new request")
        if scope.parent_request_id and not any(
            turn["scope"]["request_id"] == scope.parent_request_id for turn in turns
        ):
            raise ValueError("The parent answer does not belong to this conversation")

    async def _record_interrupted(self, cid: str) -> None:
        # Deleting a running Search must not resurrect its row when
        # the socket/generator later unwinds.
        with contextlib.suppress(KeyError):
            await self.store.append_many(
                cid,
                [
                    StatusEvent(
                        status=ConversationStatus.ERROR,
                        detail="standard_search_interrupted",
                    )
                ],
                require_existing=True,
            )

    async def _save_answer(self, scope: SearchScope, answer: dict[str, Any]) -> None:
        if not isinstance(answer, dict) or not isinstance(answer.get("blocks"), list):
            raise ValueError("Search final frame is missing structured blocks")
        content = "\n\n".join(str(block.get("text", "")) for block in answer["blocks"])
        await self.store.append_many(
            scope.conversation_id,
            [
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=content),
                    meta={_KEY: {"scope": scope.model_dump(mode="json"), "answer": answer}},
                ),
                StatusEvent(status=ConversationStatus.FINISHED, detail="standard_search_complete"),
            ],
            require_existing=True,
        )
