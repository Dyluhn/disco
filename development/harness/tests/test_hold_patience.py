"""``--hold-patience-s`` — the observer's patience, not a product wall.

A product ``hold`` means every search engine is inside its rate-limit cooldown,
so the run waits instead of burning its budget on queries that cannot go out.
The product puts no wall clock on that on purpose. A BATCH harness has other
runs to get to, so it is allowed to stop watching — by pressing the same Stop a
user presses, which yields the product's own resumable checkpoint. The run is
then labelled ``stopped_during_hold`` so no tally can read it as a product or
provider failure.
"""

from __future__ import annotations

from typing import Any

from harness.research_harness_parts._cli import _parse_args
from harness.research_harness_parts._observe import (
    HOLD_PATIENCE_REASON,
    HoldPatience,
    Observation,
)
from harness.research_harness_parts._run import _stopped_during_hold


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def _action(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "type": "event",
        "event": {
            "kind": "action",
            "tool_call": {"tool_name": name, "arguments": arguments},
        },
    }


def _stream(patience: HoldPatience, frames: list[dict[str, Any]], clock: _Clock):
    """Replay a frame stream, reporting when the observer would press Stop."""
    tripped_at: float | None = None
    for delay, frame in frames:
        clock.t += delay
        patience.observe(frame)
        if patience.expired():
            patience.trip()
            tripped_at = clock.t
    return tripped_at


def test_patience_is_unlimited_by_default() -> None:
    """Matching the product: a hold has no wall clock, so neither does watching it."""
    clock = _Clock()
    patience = HoldPatience(None, now=clock.now)

    tripped = _stream(
        patience, [(0.0, _action("hold")), (10_000.0, _action("hold"))], clock
    )

    assert tripped is None and patience.tripped is False
    assert patience.holding is True


def test_a_hold_that_outlasts_the_patience_trips_exactly_once() -> None:
    clock = _Clock()
    patience = HoldPatience(60.0, now=clock.now)

    tripped = _stream(
        patience,
        [
            (0.0, _action("turn", n=3, of=16)),
            (5.0, _action("hold", reason="search_pool_cooling")),
            (30.0, _action("hold", reason="search_pool_cooling")),
            (40.0, _action("hold", reason="search_pool_cooling")),
        ],
        clock,
    )

    assert tripped == 75.0  # 5s in, plus the 60s it agreed to wait
    assert patience.tripped is True


def test_a_hold_the_product_resumes_by_itself_never_trips_the_observer() -> None:
    clock = _Clock()
    patience = HoldPatience(60.0, now=clock.now)

    tripped = _stream(
        patience,
        [
            (0.0, _action("hold")),
            (30.0, _action("hold_resumed", waited_s=30.0)),
            (600.0, _action("turn", n=4, of=16)),
        ],
        clock,
    )

    assert tripped is None and patience.tripped is False
    assert patience.holding is False


def test_the_clock_restarts_on_a_second_hold_rather_than_accumulating() -> None:
    """Two short waits are not one long one — each hold is its own decision."""
    clock = _Clock()
    patience = HoldPatience(60.0, now=clock.now)

    tripped = _stream(
        patience,
        [
            (0.0, _action("hold")),
            (50.0, _action("hold_resumed")),
            (1.0, _action("hold")),
            (50.0, _action("hold")),
        ],
        clock,
    )

    assert tripped is None


def test_a_non_action_frame_does_not_disturb_the_hold_clock() -> None:
    """Status/state chatter during a hold is not the product making progress."""
    clock = _Clock()
    patience = HoldPatience(60.0, now=clock.now)

    tripped = _stream(
        patience,
        [
            (0.0, _action("hold")),
            (30.0, {"type": "state", "state": {"execution_status": "RUNNING"}}),
            (40.0, _action("hold")),
        ],
        clock,
    )

    assert tripped == 70.0


def test_the_stop_is_recorded_as_a_checkpoint_not_a_failure() -> None:
    observer = Observation()
    observer.record(_action("hold"))
    observer.record(
        {
            "type": "harness_control",
            "command": "cancel",
            "reason": HOLD_PATIENCE_REASON,
        }
    )

    assert _stopped_during_hold(observer) is True
    # Nothing about it is an error: the product answers a cancel with a
    # resumable checkpoint, so the observed run carries no error.
    assert observer.errors == []


def test_an_ordinary_run_is_not_labelled_stopped_during_hold() -> None:
    observer = Observation()
    observer.record(_action("turn", n=1, of=16))

    assert _stopped_during_hold(observer) is False


def test_the_flag_defaults_to_unlimited_and_says_it_is_observer_policy() -> None:
    args = _parse_args(["--query", "q"])
    assert args.hold_patience_s is None

    args = _parse_args(["--query", "q", "--hold-patience-s", "90"])
    assert args.hold_patience_s == 90.0
