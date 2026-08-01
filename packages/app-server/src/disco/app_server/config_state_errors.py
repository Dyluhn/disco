"""The typed validation-failure exception shared by ConfigState's domain
repositories (PY-0365 decomposition).

A tiny standalone module — not defined inside `config_state.py` or any one
domain-repository module — because both `config_sandbox_admin.py` and
`config_features.py` raise it, and both are imported BY `config_state.py`;
defining it in either of them would make `config_state.py` import a module
that (transitively, via the OTHER repository) imports back `config_state`
before the name exists. `ConfigState` re-exports it at its historical import
path — `from disco.app_server.config_state import ConfigValidationError` is a
real, tested import several route modules and tests use directly.
"""

from __future__ import annotations


class ConfigValidationError(Exception):
    """Raised when a candidate config value fails validation. The endpoint maps
    this to a 400 with a typed `reason`."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
