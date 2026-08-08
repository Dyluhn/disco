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
class EditContract:
    """What a scenario's EDIT phase is scoped to, and the bound its churn is judged against.

    ``max_churn_ratio`` is evidence-supplied BY DESIGN: RewriteAvoidanceOracle deliberately
    reads the bound from the dossier rather than holding a hidden opinion, so it has to be
    declared somewhere governed and visible — here — and merely MEASURED by the producer.
    The producer may not invent it.
    """

    target_path: str
    """The workspace file whose churn, anchors and labels are measured."""

    expected_files: tuple[str, ...]
    """The files the edit is scoped to; touching any other file is a targeting failure."""

    edited_sections: tuple[str, ...]
    """The sections the edit targets; every OTHER section's screen label must hold."""

    edit_scope: str = "small"
    """Only ``small`` is adjudicated for churn (RewriteAvoidanceOracle's own rule)."""

    max_churn_ratio: float = 0.25
    """A one-section in-place text edit measures a few percent; a full rewrite measures
    ~1.0. 0.25 sits far from both, so the bound discriminates rewrite-vs-edit rather than
    grading how tidy the edit was."""


@dataclass(frozen=True)
class ProductScenario:
    """A browser product-harness scenario: the build prompt + the classifier contract it
    must satisfy + the product_evidence slices it requires to be present."""

    id: str
    build_prompt: str
    kind: str
    requires_export: bool = False
    required_slices: tuple[str, ...] = ()
    # Present only on scenarios with an EDIT phase; None everywhere else, so no existing
    # scenario changes shape or behaviour.
    edit_contract: EditContract | None = None
    override_prompt: str = ""
    edit_prompt: str = ""
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


# PKG-19-CERT-REQUAL5: owner-authorized replacement for the exhausted OpenCode topology.
# This is a separate exact identity contract, not a relaxation of the historical governed one:
# ledger presence, exact host/model matching, and the OpenRouter prohibition remain unchanged.
STATIC_SITE_SMOKE_DEEPSEEK_DIRECT = ProductScenario(
    id="static_site_smoke_deepseek_direct",
    build_prompt=STATIC_SITE_SMOKE.build_prompt,
    kind="static.site",
    requires_export=False,
    required_slices=STATIC_SITE_SMOKE.required_slices,
    provider_host_substr="api.deepseek.com",
    provider_model="deepseek-v4-flash",
)


# PKG-03-EDIT-EVIDENCE (2026-08-05n) — the governed EDIT scenario. The first scenario in either
# campaign whose required slices include the five P8D edit slices, so TargetedEdit /
# RewriteAvoidance / ManualEditPreservation / CommentAnchor / ScreenLabel ADJUDICATE instead of
# SKIPping for want of a producer that was never built.
#
# It declares ONLY evidence its runner actually produces — the no-false-affordances rule applies
# to scenario declarations exactly as to product surfaces. `edit_evidence_run.py` drives the
# product over HTTP and reads workspace SOURCE bytes, so it observes the five edit slices plus
# lifecycle and cleanup. It opens no browser session and requests no export, so browser_ws /
# preview / shown / verification / export / sidecar are legitimately ABSENT and those oracles SKIP
# under the documented optional-slice contract. Declaring them here would be exactly the false
# affordance that left five slices with no producer in the first place.
#
# Provider identity: the owner-settled canary pin, with the same enforcement shape as the governed
# static smoke (ledger required, model matched exactly, openrouter forbidden, every host checked).
STATIC_SITE_EDIT_GOVERNED = ProductScenario(
    id="static_site_edit_governed",
    build_prompt=(
        "Build a simple one-page static website for a neighbourhood coffee shop as "
        "`index.html` (plain HTML and CSS, no framework). Give it four sections, each a "
        '`<section>` with an `id` and an `<h2>` heading: `id="about"`, `id="hours"`, '
        '`id="menu"` and `id="contact"`. Immediately before each of those four section '
        "tags, put an HTML comment of exactly this form naming that section: "
        "`<!-- anchor: about -->`, `<!-- anchor: hours -->`, `<!-- anchor: menu -->`, "
        "`<!-- anchor: contact -->`. Choose the headings and the copy yourself. "
        "Serve it on the preview and finish."
    ),
    # The USER'S OWN change, made through the product's own follow-up surface. It has to be a
    # product surface: there is no user-facing path to hand-edit workspace source between
    # turns (measured — run r3, 2026-08-05o: a host-side edit to the ProjectStore workspace is
    # silently discarded when the next turn materializes from its own durable capture).
    override_prompt=(
        "Please add this line to the `about` section of `index.html`, exactly as written, "
        "as the last paragraph of that section, and change nothing else:\n"
        '<p class="owner-note">Hand-written by the owner — MANUAL-OVERRIDE-7Q4X — '
        "please keep this line exactly as it is.</p>\n"
        "Then finish."
    ),
    edit_prompt=(
        "Now change ONLY the copy inside the `hours` section of `index.html`, editing "
        "precisely IN PLACE — do not rewrite the whole file, and do not touch any other "
        "section. Read the file first, apply the change with a targeted edit tool, then finish."
    ),
    kind="static.site",
    requires_export=False,
    required_slices=(
        "lifecycle",
        "cleanup",
        "targeted_edit",
        "rewrite_avoidance",
        "manual_edit",
        "comment_anchors",
        "screen_labels",
    ),
    edit_contract=EditContract(
        target_path="index.html",
        expected_files=("index.html",),
        edited_sections=("hours",),
    ),
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
