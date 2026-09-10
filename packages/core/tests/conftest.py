"""Shared ownership cleanup for core test helpers."""

import pytest
from disco.core.llm import routing as routing_module
from loop_fakes import close_implicit_test_stores


@pytest.fixture(autouse=True)
def _close_implicit_loop_stores():
    """Release every in-memory event store owned by ``build_loop``."""
    yield
    close_implicit_test_stores()


@pytest.fixture(autouse=True)
def _no_router_retry_backoff(monkeypatch: pytest.MonkeyPatch):
    """Collapse the router's transient-retry backoff to zero suite-wide.

    The backoff is real and deliberate in production — five same-model attempts
    firing back-to-back give a blipping provider nothing to recover in. But a
    suite that exercises retry paths would otherwise spend minutes of genuine
    wall-clock asleep, and none of those tests are about the delay.

    The schedule itself is proved directly, against the pure function and
    against a patched ``asyncio.sleep``, in ``test_router_errors.py``. Tests
    that want the live schedule restore ``_RETRY_BACKOFF_BASE_S`` themselves.
    """
    monkeypatch.setattr(routing_module, "_RETRY_BACKOFF_BASE_S", 0.0)
