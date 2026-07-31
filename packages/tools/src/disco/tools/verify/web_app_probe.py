"""Pure helpers for the ``verify_web_app`` structured verdict — compatibility facade.

The tool drives the browser/sandbox. This module classifies the structured
probe payload into deterministic diagnostics and a verdict, so server-side host
gates can reuse the same judgement without importing the tool implementation.

The cohesive implementation lives in the private ``web_app_probe_parts`` package
(classification, fingerprint, probe collection, verdict). This module is a
state-free compatibility facade that re-exports the public names so existing
importers (the tool, the agent-server verifier probe, tests) keep working
unchanged.

Tools are evidence producers, not verdict authorities: the output here is
immutable evidence bound for later host verification. It never manufactures or
upgrades a typed ``HostVerificationResult`` — that typed receipt is host
authority (``disco.core.verification``), owned elsewhere.
"""

from __future__ import annotations

from .web_app_probe_parts._classification import (  # noqa: F401 — re-exported
    _VISION_REVIEW_CHECKLIST,
)
from .web_app_probe_parts._fingerprint import _failure_fingerprint  # noqa: F401 — re-exported
from .web_app_probe_parts._probe import collect_web_app_probe  # noqa: F401 — re-exported
from .web_app_probe_parts._verdict import compute_verdict  # noqa: F401 — re-exported

__all__ = [
    "collect_web_app_probe",
    "compute_verdict",
]