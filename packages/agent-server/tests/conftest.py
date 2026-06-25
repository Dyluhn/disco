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
