"""Shared ownership cleanup for core test helpers."""

import pytest
from loop_fakes import close_implicit_test_stores


@pytest.fixture(autouse=True)
def _close_implicit_loop_stores():
    """Release every in-memory event store owned by ``build_loop``."""
    yield
    close_implicit_test_stores()
