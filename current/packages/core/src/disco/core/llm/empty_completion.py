"""Classifying a provider call that came back with nothing in it.

A completion with no content is a completed HTTP exchange: the socket closed,
the status was 200, usage was reported. Recorded on those facts alone it is
indistinguishable from a working call — which is how a review call could spend
its whole output ceiling inside the reasoning channel three times in a row and
leave a trail saying three calls succeeded.

The provider adapter already names the class in ``response_metadata``
(``_openai_response.empty_reasoning_only_metadata``). This is the one place that
reads it back, so the router, the attempt trail and the inspect trace all say
the same word for the same event.

Classification only — no retry, no repair, no behaviour. The re-ask belongs to
the caller that owns the ceiling, and the loop's own empty-response repair
belongs to the loop.
"""

from __future__ import annotations

from .types import (
    CEILING_HIT_EMPTY_METADATA_KEY,
    EMPTY_REASONING_ONLY_METADATA_KEY,
    CompletionResponse,
)

#: Checked in order; a response carries at most one of them.
EMPTY_COMPLETION_KEYS: tuple[str, ...] = (
    CEILING_HIT_EMPTY_METADATA_KEY,
    EMPTY_REASONING_ONLY_METADATA_KEY,
)


def empty_completion_class(resp: CompletionResponse) -> str | None:
    """The name of the empty-completion class this response carries, or None."""
    metadata = resp.response_metadata or {}
    return next((key for key in EMPTY_COMPLETION_KEYS if key in metadata), None)


def completion_outcome(resp: CompletionResponse) -> tuple[str, str | None]:
    """``(outcome, error_class)`` for the attempt record of a completed call.

    ``"empty"`` rather than ``"success"``, because a call that returned nothing
    is not a success by any reading the operator cares about — and a class name
    beside it makes the failure countable instead of anecdotal.
    """
    empty_class = empty_completion_class(resp)
    return ("empty" if empty_class else "success"), empty_class


__all__ = [
    "EMPTY_COMPLETION_KEYS",
    "completion_outcome",
    "empty_completion_class",
]
