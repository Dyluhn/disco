"""harness.product_build — the BROWSER product-harness producer (P1B-LIVE).

The live Playwright run (P1B-LIVE-3) captures real UI/WS/preview/lifecycle observations; this
package turns them into a classifiable run-folder dossier (via the existing build_soak
evidence writers + classifier) and defines the product scenarios. Pure assembly + a thin
classify wrapper — it does NOT drive a browser (that is the e2e-live spec).
"""

from __future__ import annotations

from .evidence_writer import write_dossier
from .scenario_runner import STATIC_SITE_SMOKE, ProductScenario, classify_dossier

__all__ = ["write_dossier", "ProductScenario", "STATIC_SITE_SMOKE", "classify_dossier"]
