"""Make the in-repo `harness` package importable regardless of the PYTHONPATH the
test runner was launched with (the build-soak verify command sets only the
package-src dirs, not the repo root). Insert the repo root at the front of
sys.path so `import harness.build_soak...` resolves."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Every env var `run.py::_relay_log_path()` consults, in its own order. Clearing
# the WHOLE family (not just the one that bit us) keeps the fix attached to the
# resolver rather than to a single variable name.
_LEDGER_ENV_VARS = (
    "DISCO_PROVIDER_LEDGER",
    "MINIMAX_RELAY_LOG",
    "PMX_RELAY_LOG",
    "DISCO_RELAY_LOG",
)


@pytest.fixture(autouse=True)
def _hermetic_provider_ledger_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach the deterministic suite from any AMBIENT provider-call ledger.

    Root cause this exists for (observed 2026-07-26): F0 was invoked from a shell
    that still exported the live campaign's
    ``DISCO_PROVIDER_LEDGER=<evidence>/epic4/provider-ledger.jsonl``. ``run_once``
    resolves that path through ``_relay_log_path()`` for EVERY run, including the
    synthetic ones in this suite, and ``record_applies_to_conversation`` treats a
    record with NO conversation id as applying to every conversation — correct
    fail-closed behaviour for a serial relay log. That real file held 1068 such
    unscoped records, so five tests adjudicated a stranger's provider traffic as
    their own and flipped BUILD_DID_NOT_FINISH/FAIL into RUN_INTERRUPTED/
    INVALID_RUN.

    The verdicts were not wrong — the INPUT was. A provider-free deterministic
    gate whose result depends on the operator's ambient shell is not a gate, so
    the suite now starts from a known-empty ledger environment. Tests that need a
    ledger still set one explicitly (``monkeypatch.setenv`` / patching
    ``_relay_log_path``); this fixture runs first, so those keep working.
    """
    for name in _LEDGER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
