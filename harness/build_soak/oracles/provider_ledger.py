"""ProviderLedgerOracle (HARN-1a / HARN-2) — enforces the soak provider constraint
over the provider-call-ledger evidence.

Zero-opinion: it reads the ledger (the list of provider calls made during the run)
and the scenario's provider assertion, and fails on a concrete, observed violation:
- a call to a FORBIDDEN host (default: openrouter) or a host that isn't the required one
  → PROVIDER_FORBIDDEN
- a call using the WRONG model (not the pinned soak model) → PROVIDER_WRONG_MODEL
- a call that fired AFTER the conversation went terminal (runaway burn)
  → PROVIDER_CALL_AFTER_TERMINAL

Enforcement is OPT-IN per scenario: it runs only when the scenario declares
``assertions.provider`` (so ordinary dev scenarios are not failed for using other
providers). When enforcement is required but no ledger was captured, it SKIPS with a
reason rather than passing or failing — absent evidence is not a verdict. (The final
P17 soak requires the ledger present + enforcement on.)

scenario.assertions.provider (all keys optional):
    {"forbid_host_substr": ["openrouter"],   # default ["openrouter"] if provider asserted
     "require_host_substr": "minimax",        # every call's host must contain this
     "model": "MiniMax-M3"}                    # every call must use exactly this model
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "provider_ledger"
_DEFAULT_FORBID = ("openrouter",)


class ProviderLedgerOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        provider_ledger: list[dict[str, Any]] | None = None,
    ) -> list[OracleResult]:
        prov = (scenario or {}).get("assertions", {}).get("provider")
        if not prov:
            return [skipping(_ORACLE, reason="scenario asserts no provider enforcement")]

        # FAIL-CLOSED evidence posture (default). When provider enforcement is asserted
        # the ledger is REQUIRED evidence: absent (None = no file), empty (captured zero
        # records), or malformed (a record missing a host) is an adjudication gap, NOT a
        # silent pass — it maps to MISSING_REQUIRED_EVIDENCE → INVALID_RUN. A scenario may
        # opt out with `require_ledger: false` (then absent/empty → SKIP).
        require_ledger = prov.get("require_ledger", True)

        if provider_ledger is None:
            if require_ledger:
                return [
                    failing(
                        _ORACLE,
                        fc.MISSING_REQUIRED_EVIDENCE,
                        first_broken_link="soak -> provider_ledger_captured",
                        facts={"captured": False, "reason": "no provider-call ledger file"},
                    )
                ]
            return [skipping(_ORACLE, reason="no provider-call ledger captured (require_ledger=false)")]

        if len(provider_ledger) == 0:
            if require_ledger:
                return [
                    failing(
                        _ORACLE,
                        fc.MISSING_REQUIRED_EVIDENCE,
                        first_broken_link="soak -> provider_ledger_nonempty",
                        facts={"captured": True, "records": 0},
                    )
                ]
            return [skipping(_ORACLE, reason="provider-call ledger captured but empty (require_ledger=false)")]

        # Validate record shape: a non-dict or hostless record means corrupt/under-captured
        # evidence — fail-closed rather than under-report a possibly-forbidden call.
        for i, rec in enumerate(provider_ledger):
            if not isinstance(rec, dict) or not str(rec.get("host", "")).strip():
                return [
                    failing(
                        _ORACLE,
                        fc.MISSING_REQUIRED_EVIDENCE,
                        first_broken_link="soak -> provider_ledger_wellformed",
                        facts={
                            "bad_index": i,
                            "record": rec if isinstance(rec, dict) else str(type(rec).__name__),
                        },
                    )
                ]

        forbid = [s.lower() for s in (prov.get("forbid_host_substr") or _DEFAULT_FORBID)]
        require_host = prov.get("require_host_substr")
        require_host = require_host.lower() if isinstance(require_host, str) else None
        model = prov.get("model")

        for rec in provider_ledger:
            host = str(rec.get("host", "")).lower()
            if rec.get("after_terminal"):
                return [
                    failing(
                        _ORACLE,
                        fc.PROVIDER_CALL_AFTER_TERMINAL,
                        first_broken_link="terminal -> no_provider_calls",
                        facts={"host": host, "model": rec.get("model"), "ts": rec.get("ts")},
                    )
                ]
            for bad in forbid:
                if bad in host:
                    return [
                        failing(
                            _ORACLE,
                            fc.PROVIDER_FORBIDDEN,
                            first_broken_link="soak -> provider_allowed",
                            facts={"host": host, "forbidden_substr": bad},
                        )
                    ]
            if require_host is not None and require_host not in host:
                return [
                    failing(
                        _ORACLE,
                        fc.PROVIDER_FORBIDDEN,
                        first_broken_link="soak -> provider_allowed",
                        facts={"host": host, "required_substr": require_host},
                    )
                ]
            if model and str(rec.get("model", "")) != str(model):
                return [
                    failing(
                        _ORACLE,
                        fc.PROVIDER_WRONG_MODEL,
                        first_broken_link="soak -> model_pinned",
                        facts={"model": rec.get("model"), "expected": model, "host": host},
                    )
                ]

        return [passing(_ORACLE, facts={"calls": len(provider_ledger)})]
