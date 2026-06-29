"""Product-harness scenarios + the classify wrapper (P1B-LIVE-2).

A ProductScenario materializes the exact classifier scenario dict (event-chain + MiniMax-only
provider enforcement) AND declares which product_evidence slices it REQUIRES. `classify_dossier`
enforces that completeness FIRST — a product-harness run missing a required slice is INVALID_RUN
(missing evidence), NOT a PASS via the browser oracles' skip-when-absent behavior — then delegates
to the existing `classify_run_folder`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.build_soak import failure_codes as fc
from harness.build_soak.classify import classify_run_folder
from harness.build_soak.product_evidence import PRODUCT_EVIDENCE_NAME


@dataclass(frozen=True)
class ProductScenario:
    """A browser product-harness scenario: the build prompt + the classifier contract it
    must satisfy + the product_evidence slices it requires to be present."""

    id: str
    build_prompt: str
    kind: str
    requires_export: bool = False
    required_slices: tuple[str, ...] = ()

    def to_classifier_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "assertions": {
                "event_chain": {"require_plan_before_execution": True},
                # MiniMax-only provider enforcement (P17 constraint): a ledger is required, every
                # host must contain "minimax", and an openrouter host is forbidden.
                "provider": {
                    "require_ledger": True,
                    "require_host_substr": "minimax",
                    "forbid_host_substr": ["openrouter"],
                    "model": "MiniMax-M3",
                },
            },
        }


# The §6.8 minimal scenario: a static site build that exercises the real UI/WS/preview/finish/
# cleanup path. No export step (requires_export=False); a disco-kernel run has no sidecar — those
# oracles SKIP legitimately, so they are NOT in required_slices.
STATIC_SITE_SMOKE = ProductScenario(
    id="static_site_smoke",
    build_prompt="Build a simple one-page static website for a neighbourhood coffee shop.",
    kind="static.site",
    requires_export=False,
    required_slices=("browser_ws", "lifecycle", "preview", "shown", "verification", "cleanup"),
)


def _missing(reason: str, facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": fc.INVALID_RUN,
        "severity": fc.NONE,
        "code": fc.MISSING_REQUIRED_EVIDENCE,
        "first_broken_link": "product_harness -> required_evidence",
        "facts": {"reason": reason, **facts},
    }


def classify_dossier(run_dir: str | Path, scenario: ProductScenario) -> dict[str, Any]:
    """Enforce product-harness completeness, then classify. A required slice (or, when
    requires_export, export.requested) absent from product-evidence.json → INVALID_RUN, never
    a PASS via oracle SKIP."""
    base = Path(run_dir)
    pe_path = base / PRODUCT_EVIDENCE_NAME
    if not pe_path.is_file():
        return _missing("no product-evidence.json", {"required_slices": list(scenario.required_slices)})
    try:
        ev = json.loads(pe_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return _missing(f"product-evidence.json unreadable: {exc}", {})
    if not isinstance(ev, dict):
        return _missing("product-evidence.json is not an object", {})

    missing = [s for s in scenario.required_slices if not isinstance(ev.get(s), dict)]
    if missing:
        return _missing("required product slice(s) absent", {"missing_slices": missing})
    if scenario.requires_export:
        export = ev.get("export")
        if not isinstance(export, dict) or export.get("requested") is not True:
            return _missing("scenario requires an export but none was requested/captured", {})

    return classify_run_folder(base, scenario=scenario.to_classifier_dict())
