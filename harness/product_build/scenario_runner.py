"""Product-harness scenarios + the classify wrapper (P1B-LIVE-2).

A ProductScenario materializes the exact classifier scenario dict (event-chain + MiniMax-only
provider enforcement) AND declares which product_evidence slices it REQUIRES. `classify_dossier`
enforces that completeness FIRST — a product-harness run missing a required slice is INVALID_RUN
(missing evidence), NOT a PASS via the browser oracles' skip-when-absent behavior — then delegates
to the existing `classify_run_folder`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    # The provider identity this scenario pins. The defaults are the P17 MiniMax-DIRECT
    # topology (relay -> api.minimaxi.chat) the first product scenarios were authored
    # against. A scenario driven through a DIFFERENT DEPLOYMENT of the same owner-settled
    # model declares its own host/model here rather than relaxing the constraint: the
    # enforcement shape is identical (ledger required, exact model, openrouter forbidden,
    # every host must match), only the pinned identity differs.
    provider_host_substr: str = "minimax"
    provider_model: str = "MiniMax-M3"

    def to_classifier_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "assertions": {
                "event_chain": {"require_plan_before_execution": True},
                # Single-provider enforcement (P17 constraint): a ledger is required, every
                # host must contain the pinned substring, the model must match EXACTLY, and
                # an openrouter host is forbidden.
                "provider": {
                    "require_ledger": True,
                    "require_host_substr": self.provider_host_substr,
                    "forbid_host_substr": ["openrouter"],
                    "model": self.provider_model,
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


# A4-CLOSURE: the GOVERNED-topology static smoke. Byte-for-byte the same evidence contract as
# STATIC_SITE_SMOKE — the same six required slices, so the same four browser oracles adjudicate
# rather than SKIP — driven through the campaign's OWNER-SETTLED canary identity
# (opencode.ai -> minimax-m3, standing handover §0, 2026-07-31) instead of the retired
# MiniMax-direct relay. This is NOT a relaxation: `require_ledger` stays on, the model is still
# matched exactly, openrouter is still forbidden, and every ledger host must still match. It
# exists because the governed isolated stack reaches the same owner-settled model over a
# different deployment, and pinning the OLD host would fail an honest run for a topology the
# campaign left behind by owner decision.
STATIC_SITE_SMOKE_GOVERNED = ProductScenario(
    id="static_site_smoke_governed",
    build_prompt=STATIC_SITE_SMOKE.build_prompt,
    kind="static.site",
    requires_export=False,
    required_slices=STATIC_SITE_SMOKE.required_slices,
    provider_host_substr="opencode.ai",
    provider_model="minimax-m3",
)


# P1B-LIVE-STABILITY: the STRICT static smoke. Identical to STATIC_SITE_SMOKE but ENFORCES the
# `sidecar` slice {stopped_at_terminal, provider_calls_after_terminal} — so the SidecarStopOracle
# adjudicates (not SKIPs) "the provider/sidecar stopped at the terminal state, zero calls after".
# The stability runner captures sidecar via a ledger-native per-run relay-offset window; this is
# the scenario the x10 consecutive STATIC_SMOKE matrix classifies against. A NEW scenario (not a
# change to STATIC_SITE_SMOKE) so the existing durable spec — which does not capture sidecar —
# is unaffected.
STATIC_SMOKE_STRICT = ProductScenario(
    id="static_smoke_strict",
    build_prompt="Build a simple one-page static website for a neighbourhood coffee shop.",
    kind="static.site",
    requires_export=False,
    required_slices=(
        "browser_ws",
        "lifecycle",
        "sidecar",
        "preview",
        "shown",
        "verification",
        "cleanup",
    ),
)


# P10 (Export/Handoff): an EXPORT-requiring scenario. The build must ALSO hand off a
# downloadable artifact (serve kind='files'), so `classify_dossier` enforces export.requested
# (absent ⇒ INVALID_RUN) and the ExportDownloadOracle requires a REAL non-empty download
# (download_present + download_bytes>0). Same UI/preview/verify/cleanup path as the static
# smoke, plus the export slice — exercising disco's serve(kind='files') → DeliverableEvent →
# /conversations/{cid}/artifacts/{path} download path end to end (the live capture is P10b).
EXPORT_SMOKE = ProductScenario(
    id="export_smoke",
    build_prompt=(
        "Build a one-page static website for a neighbourhood coffee shop, then serve its "
        "source files as a downloadable bundle the user can download."
    ),
    kind="static.site",
    requires_export=True,
    required_slices=(
        "browser_ws",
        "lifecycle",
        "preview",
        "shown",
        "verification",
        "export",
        "cleanup",
    ),
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
        return _missing(
            "no product-evidence.json", {"required_slices": list(scenario.required_slices)}
        )
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
