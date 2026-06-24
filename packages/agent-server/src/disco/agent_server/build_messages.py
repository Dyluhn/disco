"""User/context turn constructors for the Build seam.

These two helpers build the `MessageEvent`s a user turn appends to the event
store: the visible USER message and the optional HIDDEN ENVIRONMENT context
block. They live HERE — a plain agent-server module, NOT under `routes._common`
— so the kernel/runtime seam (`build_kernel.DiscoKernel`, which appends a user
turn) does not have to depend on the HTTP/WS route layer (Disco Pi Build Kernel
Campaign, codex finding #3). Both the routes (`routes._common` re-exports these)
and `DiscoKernel` import them from this single source.

Behaviour is byte-identical to the original definitions previously inlined in
`routes._common`.
"""

from __future__ import annotations

from disco.core import EventSource, LLMMessage, MessageEvent


def _user_message(content: str, *, steer: bool = False) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=content),
        meta={"steer": True} if steer else {},
    )


def _context_message(content: str) -> MessageEvent:
    """R3: a hidden ENVIRONMENT message carrying large context (e.g. a full DR
    report) that the MODEL receives but the USER doesn't see as a chat bubble.
    EventSource.ENVIRONMENT is filtered out of the build feed (buildTrace) except
    ⚠-prefixed / 'User uploaded:' ones, so this stays hidden — used to keep the
    DR→slides handoff message short ("Make slides for …") instead of dumping the
    whole report into the visible history."""
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content=content),
    )
