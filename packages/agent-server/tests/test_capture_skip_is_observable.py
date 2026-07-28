"""A skipped workspace capture must not be silent.

Certified-lane evidence 2026-07-27 (`p4_ff_react_steer` seed 621005, INVALID_RUN
/ MISSING_REQUIRED_EVIDENCE): 68 actions over 1104s, then the conversation ended
with NO `<projects_root>/<cid>/` at all — no tree, no versions, no manifest. Its
browser screenshots were referenced as durable evidence and had nowhere to live.

`_do_capture_workspace` returns None when no capture session resolves, and did so
SILENTLY, so nothing distinguished "captured nothing because there was nothing"
from "lost the entire workspace". Best-effort on this path is deliberate — the
strict `seal_fence` path raises instead — but silence is not.
"""

from __future__ import annotations

import logging

import pytest
from disco.agent_server.workspace_persistence import WorkspacePersistence, _skipped_capture


def test_the_reporter_returns_the_skip_value_so_the_call_site_stays_one_line():
    # The call site is `return _skipped_capture(...)`, which keeps the reporting
    # out of WorkspacePersistence's size budget entirely.
    assert _skipped_capture("conv_x", "terminal") is None


def test_it_names_the_conversation_and_the_trigger(caplog):
    with caplog.at_level(logging.WARNING):
        _skipped_capture("conv_seed_621005", "terminal")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "SKIPPED" in joined
    assert "conv_seed_621005" in joined, "name the conversation that lost its workspace"
    assert "terminal" in joined, "name the trigger, to locate the path that skipped"


def test_it_says_nothing_was_persisted(caplog):
    # The operator-facing point: this is not a partial capture.
    with caplog.at_level(logging.WARNING):
        _skipped_capture("conv_x", "paused")
    assert "nothing was persisted" in " ".join(r.getMessage() for r in caplog.records)


def test_it_is_WARNING_not_INFO(caplog):
    # INFO would sit beside the routine "workspace capture started" line and read
    # as normal operation. Losing a workspace is not routine.
    with caplog.at_level(logging.INFO):
        _skipped_capture("conv_x", "stuck")
    levels = {r.levelno for r in caplog.records if "SKIPPED" in r.getMessage()}
    assert levels == {logging.WARNING}


@pytest.mark.asyncio
async def test_the_capture_path_reports_through_it(monkeypatch, caplog):
    persistence = WorkspacePersistence.__new__(WorkspacePersistence)

    async def _unresolvable(*_a, **_k):
        return None

    monkeypatch.setattr(persistence, "_resolve_capture_session", _unresolvable, raising=False)
    with caplog.at_level(logging.WARNING):
        result = await persistence._do_capture_workspace(
            "conv_seed_621005", trigger="terminal", snapshot_fn=None
        )
    assert result is None
    assert "conv_seed_621005" in " ".join(r.getMessage() for r in caplog.records)
