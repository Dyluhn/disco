"""harness.product_build — the BROWSER product-harness producer (P1B-LIVE).

The live Playwright run (P1B-LIVE-3) captures real UI/WS/preview/lifecycle observations; this
package turns them into a classifiable run-folder dossier (via the existing build_soak evidence
writers + classifier), defines the product scenarios, and hosts the MiniMax relay.

Re-exports are LAZY (PEP 562 ``__getattr__``) so importing a dependency-light member — e.g.
``harness.product_build.minimax_relay`` (no fastapi/disco at import) — does not pull the
disco-dependent scenario_runner/evidence_writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # static visibility for __all__ without a runtime disco import
    from .evidence_writer import write_dossier
    from .scenario_runner import (
        EXPORT_SMOKE,
        STATIC_SITE_SMOKE,
        STATIC_SMOKE_STRICT,
        ProductScenario,
        classify_dossier,
    )

__all__ = [
    "write_dossier",
    "ProductScenario",
    "STATIC_SITE_SMOKE",
    "STATIC_SMOKE_STRICT",
    "EXPORT_SMOKE",
    "classify_dossier",
]

_SCENARIO_NAMES = ("ProductScenario", "STATIC_SITE_SMOKE", "STATIC_SMOKE_STRICT", "EXPORT_SMOKE", "classify_dossier")


def __getattr__(name: str) -> Any:
    if name == "write_dossier":
        from .evidence_writer import write_dossier

        return write_dossier
    if name in _SCENARIO_NAMES:
        from . import scenario_runner

        return getattr(scenario_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
