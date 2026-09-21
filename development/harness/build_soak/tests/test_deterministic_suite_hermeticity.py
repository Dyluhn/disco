"""The provider-free gate must not depend on the operator's ambient shell.

Observed 2026-07-26: F0 was launched from a shell that still exported the live
campaign's ``DISCO_PROVIDER_LEDGER``. ``run_once`` resolves that env var through
``_relay_log_path()`` for every run, so five deterministic tests adjudicated a
real, unrelated 1068-record ledger as their own evidence and flipped their
classifications. A gate whose verdict depends on who invoked it is not a gate.

These tests pin the fix at the resolver, not at one variable name.
"""

from __future__ import annotations

import os

import pytest
from harness.build_soak.run import _relay_log_path
from harness.build_soak.tests.conftest import _LEDGER_ENV_VARS


def test_no_ambient_ledger_leaks_into_a_deterministic_test():
    """The autouse fixture has already cleared the whole resolver family."""
    assert _relay_log_path() is None
    for name in _LEDGER_ENV_VARS:
        assert name not in os.environ


@pytest.mark.parametrize("name", _LEDGER_ENV_VARS)
def test_every_resolver_variable_is_cleared(name: str, monkeypatch: pytest.MonkeyPatch):
    """Each variable `_relay_log_path` consults is neutralized, not just one.

    Setting one inside a test still works — the fixture runs first, so an
    explicit opt-in is honoured while the ambient default stays empty.
    """
    assert _relay_log_path() is None
    monkeypatch.setenv(name, "/tmp/explicit-opt-in.jsonl")
    assert _relay_log_path() == "/tmp/explicit-opt-in.jsonl"


def test_the_fixture_covers_the_resolvers_full_variable_list():
    """If `_relay_log_path` learns a new variable, this fails until it is cleared.

    Guards against the fix silently rotting: a future env var added to the
    resolver would otherwise reopen exactly the hole that was just closed.
    """
    import inspect

    source = inspect.getsource(_relay_log_path)
    quoted = {
        line.strip().strip('",') for line in source.splitlines() if line.strip().startswith('"')
    }
    referenced = {name for name in quoted if name.isupper() and "_" in name}
    assert referenced == set(_LEDGER_ENV_VARS), (
        "_relay_log_path consults env vars the hermeticity fixture does not clear: "
        f"{sorted(referenced - set(_LEDGER_ENV_VARS))}"
    )
