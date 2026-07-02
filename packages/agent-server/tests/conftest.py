"""Test-support for the Pi tool-bridge suites.

Defensive per-test wall-clock guard (scoped — see ``_GUARDED_MODULES``). The bridge
gates park a held request on an ``asyncio.Future`` and ``await`` it until a SEPARATE
control op resolves it (pi_kernel ``_park`` / ``_park_confirm``). A test that drives
such a gate but forgets to RESOLVE it would otherwise block ``await fut`` forever and
hang the whole suite. This wraps each async test in those two modules in
``asyncio.wait_for`` so a future gate hang FAILS FAST (TimeoutError) instead of
wedging the run. It is event-loop-native (no signals), touches no shared venv, and
is scoped by module name so the other ~90 agent-server suites are unaffected.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Iterable

import pytest

# Only these modules are guarded — every other suite in this directory is untouched.
_GUARDED_MODULES = {"test_pi_tool_bridge", "test_pi_tool_bridge_unit"}

# Generous vs. the suites' own 5s gate-status waits, tight enough to fail fast.
_PER_TEST_TIMEOUT_S = 20.0


def pytest_collection_modifyitems(items: Iterable[pytest.Item]) -> None:
    for item in items:
        module = getattr(item, "module", None)
        short = module.__name__.rsplit(".", 1)[-1] if module is not None else ""
        if short not in _GUARDED_MODULES:
            continue
        func = getattr(item, "obj", None)
        if func is None or not inspect.iscoroutinefunction(func):
            continue  # sync tests can't await-hang

        @functools.wraps(func)
        async def _guarded(*args: object, __func=func, **kwargs: object) -> object:
            return await asyncio.wait_for(__func(*args, **kwargs), timeout=_PER_TEST_TIMEOUT_S)

        item.obj = _guarded  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _isolated_projects_root(monkeypatch, tmp_path):
    """[REL-RC-J fallout] Isolate the durable ProjectStore per test.

    Without this, every scripted test that shares CID "c1" reads/writes ONE
    persistent workspace at ~/.local/share/disco/projects/c1/ — files written by
    any test in any PAST run leak into every future run (a file_write to a path
    that "already exists" is refused read_before_write, silently zeroing the
    test's productive work). The old attempt-counting finish gate masked this for
    weeks; the outcome-counting gate (REL-RC-J) surfaced it. DISCO_DATA_DIR is
    the highest-priority root override (default_projects_root, projects/store.py).
    """
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path / "disco-data"))
