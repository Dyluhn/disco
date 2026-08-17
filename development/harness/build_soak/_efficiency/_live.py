"""Live progress — bounded, zero provider calls."""

from __future__ import annotations

from typing import Any

from ._report import _fmt


class LiveEfficiencyProgress:
    """Bounded live counters for one in-flight run.

    Emits ONLY when a meaningful counter changes or ``interval_s`` has elapsed
    since the last emission, so a fast poll loop cannot flood the log. Makes no
    provider calls and reads nothing the harness had not already collected —
    polling samples update the clock, never the counters.
    """

    def __init__(
        self,
        *,
        scenario_id: str,
        seed: Any,
        emit: Any = print,
        interval_s: float = 30.0,
    ) -> None:
        self._scenario_id = scenario_id
        self._seed = seed
        self._emit = emit
        self._interval_s = interval_s
        self._last_signature: tuple[Any, ...] | None = None
        self._last_emit_at: float | None = None

    def update(
        self,
        *,
        now: float,
        elapsed_s: float,
        actions: int | None = None,
        planning_turns: int | None = None,
        execution_turns: int | None = None,
        provider_calls: int | None = None,
        compactions: int | None = None,
        repairs: int | None = None,
        state: str = "",
    ) -> bool:
        """Emit a progress line when warranted. Returns True when it emitted.

        ``now`` is passed in rather than read from the clock so the caller owns
        the time source and this stays testable without sleeping.
        """
        signature = (
            actions,
            planning_turns,
            execution_turns,
            provider_calls,
            compactions,
            repairs,
            state,
        )
        counters_changed = signature != self._last_signature
        interval_elapsed = (
            self._last_emit_at is None or (now - self._last_emit_at) >= self._interval_s
        )
        if not counters_changed and not interval_elapsed:
            return False
        self._last_signature = signature
        self._last_emit_at = now
        self._emit(
            f"[eff] {self._scenario_id} · seed {self._seed} · {elapsed_s:.0f}s · "
            f"act {_fmt(actions)} · plan {_fmt(planning_turns)} · "
            f"exec {_fmt(execution_turns)} · calls {_fmt(provider_calls)} · "
            f"cmpct {_fmt(compactions)} · rep {_fmt(repairs)} · {state or '?'}"
        )
        return True
