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

import json

from disco.core import EventSource, LLMMessage, MessageEvent
from disco.core.appkit import BuildBrief
from disco.core.verification import VerificationRequirementsDirective


def _user_message(
    content: str,
    *,
    steer: bool = False,
    verification_requirements: VerificationRequirementsDirective | None = None,
) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=content),
        meta={"steer": True} if steer else {},
        verification_requirements=verification_requirements,
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


# --- injection-safety caps for the persisted brief ---------------------------
# The brief is server-derived, but these caps keep a future classifier change from
# making the hidden environment wrapper unbounded.
_BRIEF_GOAL_MAX = 200
_BRIEF_LIST_MAX = 16
_BRIEF_ELEM_MAX = 64


def _bounded_brief_payload(brief: BuildBrief) -> dict[str, object]:
    """A length-clamped plain-dict view of the brief for safe serialization."""

    def _clamp_list(items: list[str]) -> list[str]:
        return [str(x)[:_BRIEF_ELEM_MAX] for x in items[:_BRIEF_LIST_MAX]]

    return {
        "app_kind": str(brief.app_kind)[:_BRIEF_ELEM_MAX],
        "primary_goal": str(brief.primary_goal)[:_BRIEF_GOAL_MAX],
        "audience": str(brief.audience)[:_BRIEF_ELEM_MAX],
        "key_entities": _clamp_list(brief.key_entities),
        "must_have_sections": _clamp_list(brief.must_have_sections),
    }


def _build_brief_message(brief: BuildBrief) -> MessageEvent:
    """Hidden ENVIRONMENT message carrying the deterministic AppKit Build Brief.

    The JSON payload is wrapped in ``<build_brief>`` tags for the model, while any
    angle brackets inside user-influenced string values are escaped so the payload
    cannot forge markup or close the wrapper.
    """
    payload = json.dumps(_bounded_brief_payload(brief), ensure_ascii=True)
    safe = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content=f"<build_brief>{safe}</build_brief>"),
        # Provenance marker for the contract fold: only THIS constructor writes it,
        # so a user-supplied WS `context` block that happens to contain the same
        # wrapper text cannot be mistaken for a declaration (codex finding #5).
        meta={"build_brief": True},
    )


_BRIEF_OPEN = "<build_brief>"
_BRIEF_CLOSE = "</build_brief>"


_BRIEF_KEYS = frozenset(
    {"app_kind", "primary_goal", "audience", "key_entities", "must_have_sections"}
)


def parse_build_brief_content(
    content: object, *, meta: dict[str, object] | None = None
) -> dict[str, object] | None:
    """Inverse of :func:`_build_brief_message`: the bounded payload dict from a
    persisted ``<build_brief>…</build_brief>`` ENVIRONMENT message content, or
    None when the content is not a brief message / fails to parse. Used by the
    runtime's contract fold-on-load (restart durability) — the persisted brief
    is the durable record of the declaration `activate_contract_for_brief` made.

    Provenance (codex finding #5): events written by `_build_brief_message` carry
    the ``build_brief`` meta marker and are trusted outright. LEGACY events
    (pre-marker) are accepted only when the payload is the exact bounded shape —
    all five keys, nothing else — so a free-form context block that merely
    contains the wrapper text cannot masquerade as a declaration."""
    if not isinstance(content, str):
        return None
    if not (content.startswith(_BRIEF_OPEN) and content.endswith(_BRIEF_CLOSE)):
        return None
    try:
        payload = json.loads(content[len(_BRIEF_OPEN) : -len(_BRIEF_CLOSE)])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not (meta or {}).get("build_brief") and set(payload.keys()) != _BRIEF_KEYS:
        return None
    return payload
