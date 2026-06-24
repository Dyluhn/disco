"""PR C1 — the ephemeral, run-scoped token model for the DiscoInferenceGateway.

The gateway (PR C2, ``routes/pi_inference.py``) lets the Pi sidecar drive the
UI-SELECTED model over a local OpenAI-compatible endpoint **without ever seeing a
provider API key**. The token here is the capability that authorizes one Pi kernel
to call that endpoint, bound to exactly one conversation + one selected model, for
a bounded lifetime and token budget.

Security properties (campaign §4.3 / §11.1):

* **Run-scoped & ephemeral.** A token is issued for one ``kernel_id`` /
  ``conversation_id`` and one ``model_key`` (the catalogue key the UI picked). It
  expires (``expires_at``) and is revoked on cancel / finish / error.
* **Random 256-bit value.** ``secrets.token_urlsafe(32)`` = 32 random bytes.
* **Model-pinned.** The selected model lives in the token, NOT in the request — the
  gateway always uses ``token.model_key`` and ignores any ``model`` Pi sends, so a
  model-switch attempt cannot change which model answers.
* **Budgeted.** ``budget_tokens`` caps total tokens; over-budget → reject.
* **Never logged.** The raw token value is the dict KEY and is never stored inside a
  record (so a record ``repr`` can't leak it) and never written to a log line. Only
  a non-reversible ``fingerprint`` (sha256 prefix) is ever emitted for correlation.

This module is pure (no FastAPI / httpx / network); it owns issue / validate /
revoke / budget only. The endpoint composes it with the provider+secrets layer.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# 32 random bytes = 256 bits of entropy, URL-safe so it rides a bearer header.
_TOKEN_BYTES = 32


def fingerprint(token: str) -> str:
    """A short, non-reversible id for a token, safe to log/trace. NEVER the token
    itself — a sha256 prefix, so a trace can correlate calls to one token without
    exposing the bearer secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class InvalidGatewayToken(Exception):
    """A bearer token that is not a live, unrevoked, unexpired gateway token.

    ``reason`` ∈ {"unknown", "expired", "revoked"} — all map to HTTP 401 at the
    endpoint (we never reveal which, to avoid an oracle; the reason is for the
    server's own honest classification/trace, never the token value)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class GatewayToken:
    """The fields a token binds (campaign §4.3). The raw 256-bit value is NOT a
    field — it is the store's key — so this record can never leak the secret via a
    ``repr``/log. ``used_tokens`` accrues for the budget check."""

    kernel_id: str
    conversation_id: str
    model_key: str  # the SELECTED model's catalogue key (what the UI picked)
    expires_at: float  # epoch seconds; past → expired
    budget_tokens: int = 0  # total token cap; <= 0 means "no budget cap"
    revoked: bool = False
    used_tokens: int = 0
    # opaque, NON-secret correlation id (the token fingerprint) for traces/logs.
    fingerprint: str = ""

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def budget_remaining(self) -> int | None:
        """Tokens left, or None when no cap is set (``budget_tokens <= 0``)."""
        if self.budget_tokens <= 0:
            return None
        return max(0, self.budget_tokens - self.used_tokens)

    def is_over_budget(self) -> bool:
        """True when a cap is set and it is already reached/exceeded. Calls are
        rejected at the threshold (used >= cap) — a request that would start past
        the cap does not run."""
        return self.budget_tokens > 0 and self.used_tokens >= self.budget_tokens


@dataclass
class PiInferenceTokenStore:
    """In-memory, process-local store of live gateway tokens.

    Run-scoped: tokens live only for a build and are revoked when it ends. There is
    no persistence by design — a token must not survive a restart (an ephemeral
    capability), and it must never touch disk (defense-in-depth like the secrets
    store). The raw token is the dict key; records hold no secret.

    ``clock`` is injectable for deterministic tests (default wall clock)."""

    clock: Callable[[], float] = time.time
    _tokens: dict[str, GatewayToken] = field(default_factory=dict)

    # -- issue ----------------------------------------------------------------

    def issue(
        self,
        *,
        kernel_id: str,
        conversation_id: str,
        model_key: str,
        ttl_s: float,
        budget_tokens: int = 0,
    ) -> str:
        """Mint a new 256-bit token bound to one kernel/conversation/model with a
        TTL and (optional) token budget. Returns the raw bearer value — the ONLY
        time it is exposed; the caller hands it to the Pi kernel and never logs it."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._tokens[token] = GatewayToken(
            kernel_id=kernel_id,
            conversation_id=conversation_id,
            model_key=model_key,
            expires_at=self.clock() + ttl_s,
            budget_tokens=budget_tokens,
            fingerprint=fingerprint(token),
        )
        return token

    # -- validate -------------------------------------------------------------

    def validate(self, token: str | None) -> GatewayToken:
        """Return the live record for ``token`` or raise InvalidGatewayToken.

        A token is valid only if it is known, not revoked, and not expired. This
        does NOT check the budget (that is a separate, per-request gate) and does
        NOT check loopback (the transport boundary owns that)."""
        if not token:
            raise InvalidGatewayToken("unknown")
        rec = self._tokens.get(token)
        if rec is None:
            raise InvalidGatewayToken("unknown")
        if rec.revoked:
            raise InvalidGatewayToken("revoked")
        if rec.is_expired(self.clock()):
            raise InvalidGatewayToken("expired")
        return rec

    def peek(self, token: str) -> GatewayToken | None:
        """The record for ``token`` without validation (None if unknown). For the
        store's own bookkeeping; the endpoint uses ``validate``."""
        return self._tokens.get(token)

    # -- revoke ---------------------------------------------------------------

    def revoke(self, token: str) -> bool:
        """Revoke one token. Returns True if it existed (idempotent — revoking an
        already-revoked or unknown token is harmless). A revoked token stays in the
        map so ``validate`` can report "revoked" rather than "unknown"."""
        rec = self._tokens.get(token)
        if rec is None:
            return False
        rec.revoked = True
        return True

    def revoke_conversation(self, conversation_id: str) -> int:
        """Revoke EVERY token bound to a conversation (called on cancel / finish /
        error so no kernel can keep driving the model after the run ends). Returns
        the count revoked."""
        n = 0
        for rec in self._tokens.values():
            if rec.conversation_id == conversation_id and not rec.revoked:
                rec.revoked = True
                n += 1
        return n

    def revoke_kernel(self, kernel_id: str) -> int:
        """Revoke every token bound to a kernel id. Returns the count revoked."""
        n = 0
        for rec in self._tokens.values():
            if rec.kernel_id == kernel_id and not rec.revoked:
                rec.revoked = True
                n += 1
        return n

    def purge_expired(self) -> int:
        """Drop expired/revoked records from the map (housekeeping). Returns the
        count removed. Not required for correctness (validate already rejects them)
        — keeps the map from growing unboundedly across many short runs."""
        now = self.clock()
        dead = [t for t, r in self._tokens.items() if r.revoked or r.is_expired(now)]
        for t in dead:
            del self._tokens[t]
        return len(dead)

    # -- budget ---------------------------------------------------------------

    def record_usage(self, token: str, *, input_tokens: int, output_tokens: int) -> None:
        """Accrue a completed call's token usage against the token's budget. Unknown
        tokens are ignored (the call already validated; this is best-effort
        accounting). Never logs the token value."""
        rec = self._tokens.get(token)
        if rec is None:
            return
        rec.used_tokens += max(0, int(input_tokens)) + max(0, int(output_tokens))
