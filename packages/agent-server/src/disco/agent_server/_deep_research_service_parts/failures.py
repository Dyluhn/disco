"""What ended a deep-research run, as fields rather than as a sentence.

Every terminal deep-research error used to reach the UI and the acceptance
harness as prose in ``ErrorEvent.detail`` / ``StatusEvent.detail``. Anything
that wanted to act on the failure — an error panel that renders the next action
separately, a batch tally that counts outages apart from research results — had
to read English back out of that sentence, and reading English back out is how
a classifier starts guessing.

So the class comes from the code path that RAISED, never from the message:

* a research wall carries its own :class:`~disco.core.events.RunFailure` on the
  exception (``_exhaustion`` built it when it decided which provider had
  failed), and it is used verbatim;
* a provider failure is an ``LLMError`` BY TYPE;
* preflight failed before the run started, and its reason already names what to
  fix;
* anything else is ``internal_error`` — honestly unclassified rather than
  guessed at, which is what keeps the set closed and the counts meaningful.

The prose is unchanged: ``detail`` still carries the same sentence it always
did, and for the research walls that sentence is now rendered FROM these fields
so the two cannot disagree.
"""

from __future__ import annotations

from disco.core.events import RunFailure
from disco.core.llm import LLMError

#: What survives any terminal deep-research failure. The conversation keeps the
#: question and the settings it was asked under, so the repair is followed by
#: asking again rather than by rebuilding the run.
_ALLOWED = (
    "the question and its depth, recency and source settings stay on this "
    "conversation, so asking again after the fix starts from them."
)


def _provider_failure(exc: LLMError) -> RunFailure:
    """A driver call the provider refused, filtered, or failed.

    ``LLMError`` subclasses carry the provider and model they were routed to,
    and the router contract guarantees the provider's REAL message survives to
    here — so the failure names the boundary and quotes it rather than
    flattening it into "the model failed".
    """
    where = " / ".join(part for part in (exc.provider, exc.model) if part)
    return RunFailure(
        failure_class="model_provider",
        why=(
            "the deep-research driver call failed at the provider: "
            f"{type(exc).__name__}" + (f" [{where}]" if where else "") + f": {exc}"
        ),
        state=(
            "the run stopped at that call; whatever it had gathered is in its "
            "trace and no report was written"
        ),
        next=(
            "check that driver's provider, key and quota in the model settings — "
            "the provider's own message above says which — then ask this question "
            "again, or pick a different deep-research driver"
        ),
        allowed=_ALLOWED,
    )


def _internal_failure(exc: BaseException) -> RunFailure:
    """An exception no code path claimed.

    Deliberately not classified further. Guessing a boundary from an exception
    type nobody mapped would put wrong counts in the batch tallies, and a wrong
    count is worse than an admitted unknown.
    """
    return RunFailure(
        failure_class="internal_error",
        why=f"the deep research run failed: {type(exc).__name__}: {exc}",
        state=(
            "the run stopped where it was; no report was written and no failing "
            "boundary named itself"
        ),
        next=(
            "report this run with its conversation id — the server log carries "
            "the traceback for this exception — then ask this question again"
        ),
        allowed=_ALLOWED,
    )


def preflight_failure(reason: str) -> RunFailure:
    """A required dependency was dead before the run started.

    ``reason`` is the preflight's own verbose sentence, which already names the
    exact driver or encoder and where to set it; it becomes ``why`` unchanged
    so the text an operator reads does not move.
    """
    return RunFailure(
        failure_class="preflight",
        why=reason,
        state=(
            "nothing ran: the run was refused before any search or model call, so "
            "no budget and no quota were spent"
        ),
        next="fix what the reason above names, then ask this question again",
        allowed=_ALLOWED,
    )


def run_failure_for(exc: BaseException) -> RunFailure:
    """The typed failure for one terminal run exception."""
    declared = getattr(exc, "failure", None)
    if isinstance(declared, RunFailure):
        return declared
    if isinstance(exc, LLMError):
        return _provider_failure(exc)
    return _internal_failure(exc)


__all__ = ["preflight_failure", "run_failure_for"]
