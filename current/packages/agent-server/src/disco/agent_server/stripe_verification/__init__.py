"""Stripe live verification package — cohesive owners for the ``stripe.security.v1`` live verifier.

This package owns the immutable evidence helpers, workerd lifecycle, probe
script, and webhook-lifecycle pieces extracted from the original monolithic
``stripe_live_verifier.py``.  The public verifier entry point and run
orchestrator remain in ``stripe_live_verifier.py``; this package holds the
implementation owners so no single module exceeds the architecture budget.
"""

from __future__ import annotations

from .evidence import StripeLiveVerifierError

__all__ = [
    "StripeLiveVerifierError",
]
