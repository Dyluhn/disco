"""Cohesive helper modules extracted from the disco-verify runner (PKG-08).

The flat ``runner.py`` module exceeded the architecture budget on seven rows.
These sub-modules split it into cohesive, independently-scoped units while
``runner.py`` remains a state-free compatibility/export facade that preserves
the exact public import surface (``AbstractVerifyClient``, ``HttpVerifyClient``,
``run_scenario``, and the private helpers the tests import).

Modules:
- :mod:`transport`       — injectable transport contract + HTTP/WS production client.
- :mod:`discovery`        — event-log deliverable discovery + deliverable-type checks.
- :mod:`policy_checks`    — forbid/tool/AppKit policy checks over the event log (no IO).
- :mod:`orchestration`    — file/app/report validators + dossier writer + ``run_scenario``.
"""

from __future__ import annotations
