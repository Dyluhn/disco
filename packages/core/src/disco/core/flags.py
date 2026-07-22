"""Deployment-level feature flags — single-variable kill switches.

Each flag is ONE environment variable, read at call time (never cached at
import), so a deployment flips a feature with a config change + service
restart, and tests flip it with ``monkeypatch.setenv``. Feature flags default
ON: absence of configuration must never silently remove a shipped surface —
but an explicit disable must remove ALL of it (tools, routes, catalog
entries), never leave a surface that looks usable and refuses.
"""

from __future__ import annotations

import os

_FALSY = frozenset({"0", "false", "no", "off", "disabled"})

APPKIT_ENABLED_ENV = "DISCO_APPKIT_ENABLED"


def appkit_enabled() -> bool:
    """The AppKit kill switch: ``DISCO_APPKIT_ENABLED=0`` fully separates the
    AppKit track from a deployment.

    OFF means: the ``app_*`` v2 mutators leave the default tool registry, the
    create route refuses ``appkit_mode`` requests (409, honest — never a silent
    downgrade), and existing AppKit conversations fail closed instead of being
    opened with a Freeform executor. The ``lead_form`` starter refuses with a
    free-form alternative. Freeform Build, starter kits, contracts, and the
    legacy governed app surface (``app_set_tweak``/``app_snapshot_version``)
    are untouched.
    """
    return os.environ.get(APPKIT_ENABLED_ENV, "1").strip().lower() not in _FALSY
