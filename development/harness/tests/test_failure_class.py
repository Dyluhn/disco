"""Lane L26 — the harness records the class the PRODUCT named, never a guess.

`errors` (the sentences) stays exactly as it was. `failures` is the structured
half: the ``failure_class`` and the four parts the product's own raising code
path put on the ``ErrorEvent``. A batch tally reads those fields instead of
bucketing prose by regex — which is what it had to do before, and which
silently re-buckets every time a message is reworded.
"""

from __future__ import annotations

from disco.core.events import RunFailureClass
from harness.research_harness import Observation


def _error_frame(**failure: object) -> dict[str, object]:
    """One terminal error frame in the shape the WS wire really carries it."""
    return {
        "type": "event",
        "event": {
            "kind": "error",
            "code": "deep_research_failed",
            "detail": "ResearchAgentError: research exhausted its turn budget…",
            "failure": failure,
        },
    }


_FAILURE = {
    "failure_class": "extraction_infrastructure",
    "why": (
        "research exhausted its turn budget without usable evidence — extraction "
        "provider failing: 59/60 attempts failed (upstream_http_500)"
    ),
    "state": "no sources were admitted; 8 of 8 research turns used",
    "next": "the extraction service did not answer in time — restart it",
    "allowed": "the question and its settings stay on this conversation",
}


def test_the_structured_failure_is_recorded_beside_the_prose() -> None:
    observer = Observation()
    observer.record(_error_frame(**_FAILURE))

    assert observer.errors == ["ResearchAgentError: research exhausted its turn budget…"]
    assert observer.failures == [_FAILURE]


def test_an_error_without_a_declared_class_contributes_no_guess() -> None:
    """A zero here means "the product did not classify it", which is a finding.

    Inventing a bucket from the message is exactly the regex classification this
    field exists to remove.
    """
    observer = Observation()
    observer.record(
        {
            "type": "event",
            "event": {"kind": "status", "status": "ERROR", "detail": "provider rejected"},
        }
    )

    assert observer.errors == ["provider rejected"]
    assert observer.failures == []


def test_one_failure_is_recorded_once_however_many_frames_carry_it() -> None:
    """The ErrorEvent and the ERROR status arrive as two frames, one failure."""
    observer = Observation()
    observer.record(_error_frame(**_FAILURE))
    observer.record(_error_frame(**_FAILURE))

    assert len(observer.failures) == 1


def test_every_recorded_class_is_one_the_product_declares() -> None:
    """The harness never invents a class name of its own."""
    import typing

    declared = set(typing.get_args(RunFailureClass))
    observer = Observation()
    for name in sorted(declared):
        observer.record(_error_frame(**{**_FAILURE, "failure_class": name}))

    assert {row["failure_class"] for row in observer.failures} == declared
