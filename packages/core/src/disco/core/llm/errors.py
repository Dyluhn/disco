"""Typed errors + context-window classification — llm-router-contract.md §6.

Callers never parse raw provider strings (principle 7). Providers raise this
hierarchy; the router catches, retries/escalates, and re-raises terminal
failures to the caller.

This module also delivers `is_context_window_exceeded()` — the function the
event/state contract (§5.3) declared and left for this subsystem. The burden of
recognizing each provider's context-window error shape lives in that provider's
adapter (where the raw error is seen), which raises `LLMContextWindowExceeded`;
this function is then a simple isinstance check.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base. Carries the provider and model for diagnostics."""

    def __init__(self, message: str = "", *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class LLMTransientError(LLMError):
    """Retryable: timeouts, 429 rate limits, 5xx, transient network."""

    def __init__(
        self,
        message: str = "",
        *,
        provider: str = "",
        model: str = "",
        retry_after_s: float | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model)
        self.retry_after_s = retry_after_s
        # Inert diagnostic attribute (like retry_after_s): the HTTP status the
        # provider answered with (429 usage/rate limit, 5xx outage), or None for
        # timeouts/network failures that never got a response. Nothing in the
        # retry/classification path reads it — it exists so a landing can label
        # the outage honestly in non-semantic event meta (UI display only).
        self.http_status = http_status


class LLMProviderUnavailable(LLMTransientError):
    """A provider-routing rejection: the upstream (e.g. Chutes via OpenRouter)
    refused the request because of auth/availability, NOT because the model
    payload was malformed. Subclasses LLMTransientError so it inherits the
    back-off path; the driver catches it FIRST (before LLMTransientError) to
    escalate provider_prefs instead of sleeping on the same upstream."""


class LLMContextWindowExceeded(LLMError):
    """The input exceeded the model's context window. THIS is the case the
    condenser's hard-reset path depends on (event contract §5.3)."""


class LLMAuthError(LLMError):
    """Bad/missing key — terminal."""


class LLMContentFiltered(LLMError):
    """Provider refused on policy grounds — terminal."""


class NoEligibleModel(LLMError):
    """No model satisfies the requested requirements (§4.1) — terminal."""


class BudgetExceeded(LLMError):
    """Hard cost cap hit (§5.2) — terminal for the overflow path."""


def is_context_window_exceeded(err: Exception) -> bool:
    """[CONTRACT] True iff `err` indicates context-window overflow. The condenser
    calls this to raise a HARD condensation request (event contract §5.2/§5.3).

    Per-provider error-shape detection is enumerated inside the adapters (which
    raise LLMContextWindowExceeded); this is then simply an isinstance check.
    That per-provider detection is the [VERIFY]/maintenance surface BoD §7.3
    warns about — provider error formats drift."""
    return isinstance(err, LLMContextWindowExceeded)
