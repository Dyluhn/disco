"""F8 — render-time shrink of confirmed `file_write` bodies (assist tier).

Moved out of ``view_render`` verbatim so that module stays under its size budget;
``view_render`` re-exports the name, so every import path is unchanged.
"""

from __future__ import annotations

from ..events import Event, LLMMessage
from .dedup import _F8_PREFIX_CHARS, _F8_TRUNCATION_MARKER_TEMPLATE, _f8_confirmed_file_writes


def f8_shrink_file_write_args(messages: list[LLMMessage], events: list[Event]) -> list[LLMMessage]:
    """F8 — GATED render-time transform. For every assistant message
    whose tool_call is a ``file_write`` that was CONFIRMED successful
    (per :func:`_f8_confirmed_file_writes`), replace the long
    ``content`` argument with a short prefix + a recoverable marker.

    Render-time only: the persisted event log is unchanged. The
    transform runs AFTER ``View.of`` (and the existing
    ``_ARG_SNIP_CHARS`` shaper in events.py) so it OVERRIDES the
    generic "[[DISCO-ELIDED: N chars ...]]" marker with a more
    useful form: a real 200-char prefix + a path-aware history marker. The
    always-fresh workspace snapshot remains the normal grounding surface.

    The marker does not command a full reread: it identifies the resource and
    asks for only the minimal range if a future action actually needs more
    context. The event log remains lossless because the event's
    ``tool_call.arguments["content"]`` is never modified.

    Assist OFF (the default) → caller does not invoke this method;
    messages are byte-identical to today.

    Returns a new list; the input ``messages`` is not mutated. Each
    modified message is a new LLMMessage (LLMMessage is frozen, so
    ``model_copy`` is required); each modified tool_call dict is a
    new dict.
    """
    confirmed = _f8_confirmed_file_writes(events)
    if not confirmed:
        return messages
    out: list[LLMMessage] = []
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            out.append(msg)
            continue
        new_tcs: list[dict] = []
        mutated = False
        for tc in msg.tool_calls:
            if not isinstance(tc, dict):
                new_tcs.append(tc)
                continue
            cid = tc.get("id")
            if tc.get("name") == "file_write" and isinstance(cid, str) and cid in confirmed:
                path, content = confirmed[cid]
                # Only shrink when the ORIGINAL content is long
                # enough that a prefix is meaningful. Short writes
                # (≤ _F8_PREFIX_CHARS) pass through unchanged —
                # the snip shaper in events.py did not elide them
                # either, and the F8 prefix would be the full
                # content + marker (no reclaim, no value).
                if len(content) > _F8_PREFIX_CHARS:
                    args = tc.get("arguments")
                    if isinstance(args, dict):
                        new_args = dict(args)
                        new_args["content"] = content[
                            :_F8_PREFIX_CHARS
                        ] + _F8_TRUNCATION_MARKER_TEMPLATE.format(path=path)
                        new_tc = dict(tc)
                        new_tc["arguments"] = new_args
                        new_tcs.append(new_tc)
                        mutated = True
                        continue
            new_tcs.append(tc)
        if mutated:
            out.append(msg.model_copy(update={"tool_calls": new_tcs}))
        else:
            out.append(msg)
    return out
