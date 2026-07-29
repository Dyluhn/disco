"""Bounded Build Soak ledger owner."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..adapters.disco_api import (
    CollectedRun,
)
from ..provider_ledger import parse_relay_log, record_applies_to_conversation
from .temporal import (
    _min_event_epoch,
    _terminal_status_epoch,
)
from .thrash import (
    _confirmed_live_thrash_stop,
)


def _diagnostic_stop_is_supported(run: CollectedRun) -> bool:
    if run.diagnostic_stop not in (None, "progressing_hard_cap"):
        return False
    if run.diagnostic_stop != "progressing_hard_cap":
        return True
    return (
        isinstance(run.diagnostic_stop_epoch, (int, float))
        and not isinstance(run.diagnostic_stop_epoch, bool)
        and isinstance(run.diagnostic_stop_seq, int)
        and not isinstance(run.diagnostic_stop_seq, bool)
    )


def _provider_slice(
    records: list[dict[str, Any]],
    run: CollectedRun,
    *,
    start_epoch: float,
    terminal_epoch: float,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for record in records:
        timestamp = record.get("ts")
        if not isinstance(timestamp, (int, float)) or float(timestamp) < start_epoch:
            continue
        if not record_applies_to_conversation(record, run.conversation_id):
            continue
        selected.append(
            {
                **record,
                "after_terminal": float(timestamp) > terminal_epoch
                and bool(record.get("has_tools", True)),
            }
        )
    return selected


def _provider_ledger_for_run(
    run: CollectedRun,
    *,
    relay_log_path: Callable[[], str | Path | None] | None = None,
) -> list[dict[str, Any]] | None:
    """Capture this run's exact, terminal-annotated provider slice for live and replay use."""
    relay_log = (relay_log_path or _relay_log_path)()
    if not relay_log or not os.path.exists(relay_log):
        return None
    if not _diagnostic_stop_is_supported(run):
        return None
    try:
        with open(relay_log, encoding="utf-8") as handle:
            records = parse_relay_log(handle.read())
        terminal_epoch = (
            run.diagnostic_stop_epoch
            if run.diagnostic_stop == "progressing_hard_cap"
            else _terminal_status_epoch(
                run.events,
                allow_killed_idle=_confirmed_live_thrash_stop(run),
            )
        )
        start_epoch = _min_event_epoch(run.events)
        if terminal_epoch is None or start_epoch is None:
            return None
        # after_terminal is the BUILD-runaway signal. Tool-less summarizer/title
        # calls are benign; unmarked records default has_tools=True and remain
        # fail-closed.
        return _provider_slice(
            records,
            run,
            start_epoch=float(start_epoch),
            terminal_epoch=float(terminal_epoch),
        )
    except Exception:  # noqa: BLE001 — required-provider oracle fails closed on None
        return None


def _relay_log_path() -> str | None:
    """[codex] ONE canonical resolver for the MiniMax relay-ledger path — the SINGLE source for
    BOTH reading the ledger AND the `_live_measure` fail-closed gate, so the rule can never key on
    a different env var than the one that actually carries the ledger. The relay WRITES
    MINIMAX_RELAY_LOG (minimax_relay.py); direct drivers write DISCO_PROVIDER_LEDGER. We also
    accept the legacy PMX_RELAY_LOG (Playwright live specs) and DISCO_RELAY_LOG so no driver path
    can drift into a silent SKIP-as-PASS."""
    for name in (
        "DISCO_PROVIDER_LEDGER",
        "MINIMAX_RELAY_LOG",
        "PMX_RELAY_LOG",
        "DISCO_RELAY_LOG",
    ):
        v = os.environ.get(name)
        if v:
            return v
    return None
