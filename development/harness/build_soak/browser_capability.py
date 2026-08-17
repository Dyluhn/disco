"""Amendment A6.1 — gauge honesty: declare IN THE ARTIFACT which scenario claims
a run could and could not evaluate.

Deferred register #6 recorded four consecutive canaries in which
``p4_appkit_restart`` failed because no browser daemon could be started, while
the batch summary still presented the scenario as governed and the exclusion
lived only in receipt prose. A red on a scenario that *cannot* pass carries no
information, and prose is not an artifact.

This module computes a machine-readable scope block from two inputs that are
already authoritative elsewhere, and invents neither:

* the scenario's OWN declared assertions — ``browser_verification.required`` and
  the browser-dependent members of
  ``governed_verification.required_claim_kinds``;
* a browser-capability verdict recorded by the recipe's end-to-end probe, which
  measures the daemon exactly as the sandbox ships it.

Three properties keep this a gauge and not a loophole:

* It never marks a claim as passed and never suppresses the failure of a
  scenario that was admitted. It only ever says "this claim was not evaluated".
* It names no scenario. A claim is excluded only when the scenario itself
  declares that claim browser-dependent AND the recorded verdict proves the
  capability absent.
* It is fail-closed. With no verdict, or an unreadable one, the capability is
  ``unproven`` and browser-dependent claims read ``not_evaluated`` — never
  ``evaluated``. Absence of a verdict never licenses a claim.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Mirrors ``disco.core.verification._STRUCTURED_BROWSER_RUNTIME_CLAIM_KINDS``.
# That name is private, and a private cross-package import is exactly the hidden
# coupling this campaign hunts, so the set is restated here and PINNED against
# the product's own authority by ``test_browser_capability.py``. If the product
# adds or removes a browser-runtime claim kind, that test fails rather than this
# gauge silently under-reporting what a headless run left unproven.
BROWSER_RUNTIME_CLAIM_KINDS: frozenset[str] = frozenset(
    {
        "http_ready",
        "rendered_content",
        "visible_text",
        "console_clean",
        "network_clean",
        "interaction",
        "route",
    }
)

CAPABILITY_AVAILABLE = "available"
CAPABILITY_UNAVAILABLE = "unavailable"
CAPABILITY_UNPROVEN = "unproven"

_STATES = frozenset({CAPABILITY_AVAILABLE, CAPABILITY_UNAVAILABLE, CAPABILITY_UNPROVEN})

EVALUATED = "evaluated"
NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class BrowserCapability:
    """A recorded verdict about whether this host can start a browser daemon.

    ``state`` is deliberately three-valued. Collapsing ``unproven`` into
    ``unavailable`` would let a missing probe silently excuse browser claims,
    and collapsing it into ``available`` would let a missing probe silently
    assert them. Neither is honest, so the absence of evidence stays visible.
    """

    state: str
    engine: str
    evidence: str

    @property
    def proven_present(self) -> bool:
        """Only a positive measurement licenses a browser-dependent claim.

        Deliberately not ``state != "unavailable"``. An ``unproven`` capability
        means nothing was measured, and a run that measured nothing has not
        evaluated the claim — treating silence as success is precisely the
        gauge dishonesty A6.1 exists to end.
        """

        return self.state == CAPABILITY_AVAILABLE

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state, "engine": self.engine, "evidence": self.evidence}


def unproven_capability(evidence: str) -> BrowserCapability:
    """The fail-closed verdict: nothing was measured, so nothing is claimed."""

    return BrowserCapability(state=CAPABILITY_UNPROVEN, engine="", evidence=evidence)


def capability_from_mapping(payload: Mapping[str, Any]) -> BrowserCapability:
    """Read a probe verdict, degrading to ``unproven`` on any unknown shape."""

    state = str(payload.get("state") or "")
    if state not in _STATES:
        return unproven_capability(f"probe verdict carried unknown state {state!r}")
    return BrowserCapability(
        state=state,
        engine=str(payload.get("engine") or ""),
        evidence=str(payload.get("evidence") or ""),
    )


def load_capability(path: str | None) -> BrowserCapability:
    """Load a recorded probe verdict from ``path``; never raise.

    A probe that could not be read is indistinguishable from a probe that was
    never run, and both must read ``unproven`` rather than failing the batch:
    the gauge reports what is unknown, it does not decide the run's verdict.
    """

    if not path:
        return unproven_capability("no browser-capability probe was supplied")
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        return unproven_capability(f"browser-capability probe unreadable: {exc!r}")
    if not isinstance(payload, Mapping):
        return unproven_capability("browser-capability probe was not a JSON object")
    return capability_from_mapping(payload)


def _required_claim_kinds(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    assertions = scenario.get("assertions")
    governed = assertions.get("governed_verification") if isinstance(assertions, Mapping) else None
    kinds = governed.get("required_claim_kinds") if isinstance(governed, Mapping) else None
    if not isinstance(kinds, Mapping):
        return ()
    return tuple(sorted(str(kind) for kind in kinds))


def browser_verification_required(scenario: Mapping[str, Any]) -> bool:
    assertions = scenario.get("assertions")
    declared = assertions.get("browser_verification") if isinstance(assertions, Mapping) else None
    return isinstance(declared, Mapping) and declared.get("required") is True


def declared_browser_claims(scenario: Mapping[str, Any]) -> tuple[str, ...]:
    """The scenario's own required claim kinds that need a browser runtime."""

    return tuple(
        kind for kind in _required_claim_kinds(scenario) if kind in BROWSER_RUNTIME_CLAIM_KINDS
    )


def scenario_scope(
    scenario: Mapping[str, Any], capability: BrowserCapability
) -> dict[str, Any]:
    """The per-scenario scope record written into the batch summary."""

    browser_claims = declared_browser_claims(scenario)
    needs_browser = bool(browser_claims) or browser_verification_required(scenario)
    evaluable = capability.proven_present if needs_browser else True
    return {
        "browser_verification_required": browser_verification_required(scenario),
        "browser_dependent_claim_kinds": list(browser_claims),
        "browser_dependent_claims_evaluability": EVALUATED if evaluable else NOT_EVALUATED,
        "structurally_evaluable_headless": evaluable,
    }


def scope_declaration(
    selected: Mapping[str, Mapping[str, Any]], capability: BrowserCapability
) -> dict[str, Any]:
    """The batch-summary ``scope`` block.

    ``not_evaluated_claim_kinds`` is the union across selected scenarios of the
    declared browser-dependent claims this run did not evaluate. ``full_scope``
    is true exactly when that set is empty — either because the capability was
    measured present, or because no selected scenario declared a
    browser-dependent claim in the first place.
    """

    scenarios = {name: scenario_scope(body, capability) for name, body in selected.items()}
    not_evaluated: set[str] = set()
    for name, body in selected.items():
        if scenarios[name]["browser_dependent_claims_evaluability"] == NOT_EVALUATED:
            not_evaluated.update(declared_browser_claims(body))
    return {
        "schema_version": 1,
        "browser_capability": capability.to_dict(),
        "scenarios": scenarios,
        "not_evaluated_claim_kinds": sorted(not_evaluated),
        "full_scope": not not_evaluated,
    }
