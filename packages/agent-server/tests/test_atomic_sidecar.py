"""T5/E1: sidecar _save_* methods must be crash-safe atomic.

A partial/failed write must NOT truncate the live sidecar file. The reference
atomic pattern is `_save_assist` (added by T1): write to a temp file in the
same directory, then `os.replace` it onto the target. The plain
`open(path, "w") -> json.dump(...)` form truncates the target the moment the
file is opened, so any exception (disk full, signal, bug) leaves a 0-byte /
truncated file. These tests pin the guarantee for the three sibling saves
that the plan calls out: `_save_autonomous`, `_save_overrides`, `_save_surfaces`.
"""

from __future__ import annotations

import json
import os
from operator import attrgetter
from unittest.mock import MagicMock

import pytest
from disco.agent_server.runtime import ConversationRuntime


def _new_runtime(tmp_path, monkeypatch) -> ConversationRuntime:
    """A runtime that points all sidecars under tmp_path."""
    monkeypatch.setenv("PMX_DB", str(tmp_path / "disco.db"))
    return ConversationRuntime(store=MagicMock())


def _seed(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f)


def _crash_json_dump(monkeypatch):
    """Make `json.dump` raise on the very next call. Restored on teardown.

    We patch the module-attribute `json.dump` on the json module itself
    (runtime.py references json.dump, not a local re-bind). pytest's
    monkeypatch.setattr auto-restores the real json.dump at test teardown so
    the failure mode stays scoped to the test that requested it.
    """

    def boom(*a, **kw):
        raise RuntimeError("simulated mid-write crash")

    monkeypatch.setattr(json, "dump", boom)


@pytest.mark.parametrize(
    "sidecar_attr,mutate_state,seed_payload",
    [
        (
            "_autonomous_path",
            lambda rt: rt.set_autonomous("conv-A", False),
            {"conv-A": True, "conv-B": False},
        ),
        (
            "_override_path",
            lambda rt: rt.set_model_override("conv-A", "anthropic/claude-3-opus"),
            {"conv-A": "anthropic/claude-3-haiku"},
        ),
        (
            "_surface_path",
            lambda rt: rt.set_surface("conv-A", "research"),
            {"conv-A": "build", "conv-B": "research"},
        ),
    ],
)
def test_save_preserves_existing_file_on_failure(
    tmp_path, monkeypatch, sidecar_attr, mutate_state, seed_payload
):
    """If a write crashes mid-flight, the live sidecar must remain byte-identical
    to its pre-call state. The pre-fix implementation opens the target with "w"
    (truncates immediately), so a raised exception inside json.dump leaves the
    file empty. The post-fix atomic pattern leaves the target untouched and
    only ever swaps in a complete new file via os.replace."""
    rt = _new_runtime(tmp_path, monkeypatch)
    owner_attr = {
        "_autonomous_path": "_settings._autonomous_path",
        "_override_path": "_settings.model_binding._override_path",
        "_surface_path": "_settings._surface_settings._path",
    }[sidecar_attr]
    path = attrgetter(owner_attr)(rt)
    assert path, f"runtime did not configure {sidecar_attr}"

    _seed(path, seed_payload)
    with open(path, "rb") as f:
        expected_bytes = f.read()
    assert expected_bytes, "precondition: seed must have produced non-empty bytes"

    _crash_json_dump(monkeypatch)
    try:
        mutate_state(rt)
    except Exception:
        # If anything escapes the best-effort wrapper that is itself a bug,
        # but the assertion below (bytes intact) is the real contract.
        pass

    assert os.path.exists(path), f"sidecar {path} vanished after failed save"
    with open(path, "rb") as f:
        post_bytes = f.read()
    assert post_bytes == expected_bytes, (
        f"sidecar {path} was TRUNCATED by a failed save - write is not atomic. "
        f"Before: {expected_bytes!r}  After: {post_bytes!r}"
    )

    with open(path) as f:
        roundtrip = json.load(f)
    assert roundtrip == seed_payload


def test_save_assist_is_already_atomic(tmp_path, monkeypatch):
    """Regression guard: the T1 _save_assist pattern is the reference. A future
    refactor of the sibling saves must not be allowed to quietly drop the
    temp+os.replace pattern; this test pins it for the assist sidecar."""
    rt = _new_runtime(tmp_path, monkeypatch)
    path = rt._settings._assist_path
    assert path

    _seed(path, {"conv-A": True})
    with open(path, "rb") as f:
        expected_bytes = f.read()
    assert expected_bytes

    _crash_json_dump(monkeypatch)
    try:
        rt.set_assist("conv-A", False)
    except Exception:
        pass

    with open(path, "rb") as f:
        assert f.read() == expected_bytes


def test_save_autonomous_succeeds_on_happy_path(tmp_path, monkeypatch):
    """Sanity: the atomic rewrite still produces a valid file on the happy path.
    If we break the temp+os.replace plumbing, this catches it."""
    rt = _new_runtime(tmp_path, monkeypatch)
    rt.set_autonomous("conv-X", True)
    rt.set_autonomous("conv-Y", False)
    with open(rt._settings._autonomous_path) as f:
        data = json.load(f)
    assert data == {"conv-X": True, "conv-Y": False}


def test_save_surfaces_succeeds_on_happy_path(tmp_path, monkeypatch):
    rt = _new_runtime(tmp_path, monkeypatch)
    rt.set_surface("conv-X", "build")
    with open(rt._settings._surface_settings._path) as f:
        data = json.load(f)
    assert data == {"conv-X": "build"}


def test_save_overrides_succeeds_on_happy_path(tmp_path, monkeypatch):
    rt = _new_runtime(tmp_path, monkeypatch)
    rt.set_model_override("conv-X", "anthropic/claude-3-haiku")
    with open(rt._settings.model_binding._override_path) as f:
        data = json.load(f)
    assert data == {"conv-X": "anthropic/claude-3-haiku"}


def test_effective_autonomous_gated_by_surface(tmp_path, monkeypatch):
    """Autonomous auto-approve applies to surfaces WITH a plan gate — build,
    agent, AND deep_research — but NOT plain research (no plan gate).
    Regression: deep_research was excluded, so a headless/autonomous DR run
    stalled forever at AWAITING_PLAN_APPROVAL."""
    rt = _new_runtime(tmp_path, monkeypatch)
    cases = [
        ("c-build", "build", True),
        ("c-agent", "agent", True),
        ("c-dr", "deep_research", True),  # the fix
        ("c-research", "research", False),
    ]
    for conv, surface, expected in cases:
        rt.set_autonomous(conv, True)
        rt.set_surface(conv, surface)
        assert rt._effective_autonomous(conv) is expected, f"{surface}: {expected}"
    # autonomous never set → False even on an eligible surface
    rt.set_surface("c-unset", "deep_research")
    assert rt._effective_autonomous("c-unset") is False
