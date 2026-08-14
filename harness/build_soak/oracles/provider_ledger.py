"""ProviderLedgerOracle (HARN-1a / HARN-2) — enforces the soak provider constraint
over the provider-call-ledger evidence.

Zero-opinion: it reads the ledger (the list of provider calls made during the run)
and the scenario's provider assertion, and fails on a concrete, observed violation:
- a call to a FORBIDDEN host (default: openrouter) or a host that isn't the required one
  → PROVIDER_FORBIDDEN
- a call using the WRONG model for its pinned main or visual-observer route
  → PROVIDER_WRONG_MODEL
- a call that fired AFTER the conversation went terminal (runaway burn)
  → PROVIDER_CALL_AFTER_TERMINAL

Enforcement is OPT-IN per scenario: it runs only when the scenario declares
``assertions.provider`` (so ordinary dev scenarios are not failed for using other
providers). When enforcement is required but no ledger was captured, it SKIPS with a
reason rather than passing or failing — absent evidence is not a verdict. (The final
P17 soak requires the ledger present + enforcement on.)

scenario.assertions.provider (all keys optional):
    {"forbid_host_substr": ["openrouter"],   # default ["openrouter"] if provider asserted
     "require_host_substr": "minimax",        # ordinary calls use this host/model
     "model": "MiniMax-M3",
     "visual_observer": {                      # optional tagged visual route
       "require_host_substr": "ollama.com",
       "model": "minimax-m3"}}
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..provider_ledger import records_for_conversation
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "provider_ledger"
_DEFAULT_FORBID = ("openrouter",)


def _check_ledger_presence(
    provider_ledger: list[dict[str, Any]] | None,
    require_ledger: bool,
) -> list[OracleResult] | None:
    """Check ledger presence and emptiness. Returns results or None to continue."""
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
        return [
            skipping(
                _ORACLE, reason="provider-call ledger captured but empty (require_ledger=false)"
            )
        ]
    return None


def _check_ledger_shape(provider_ledger: list[dict[str, Any]]) -> OracleResult | None:
    """Validate record shape: a non-dict or hostless record is corrupt evidence."""
    for i, rec in enumerate(provider_ledger):
        if not isinstance(rec, dict) or not str(rec.get("host", "")).strip():
            return failing(
                _ORACLE,
                fc.MISSING_REQUIRED_EVIDENCE,
                first_broken_link="soak -> provider_ledger_wellformed",
                facts={
                    "bad_index": i,
                    "record": rec if isinstance(rec, dict) else str(type(rec).__name__),
                },
            )
    return None


def _visual_policy(prov: dict[str, Any]) -> tuple[dict[str, Any] | None, OracleResult | None]:
    visual = prov.get("visual_observer")
    if visual is None:
        return None, None
    if (
        isinstance(visual, dict)
        and isinstance(visual.get("require_host_substr"), str)
        and visual["require_host_substr"].strip()
        and isinstance(visual.get("model"), str)
        and visual["model"].strip()
    ):
        return visual, None
    return None, failing(
        _ORACLE,
        fc.MISSING_REQUIRED_EVIDENCE,
        first_broken_link="soak -> provider_policy_valid",
        facts={"reason": "visual_observer provider policy is malformed"},
    )


def _visual_shape_error(rec: dict[str, Any]) -> OracleResult | None:
    bounded = (
        rec.get("call_kind") == "observer"
        and rec.get("has_tools") is False
        and rec.get("stream") is False
        and rec.get("image_count") == 1
        and rec.get("tool_count") == 0
    )
    if bounded:
        return None
    return failing(
        _ORACLE,
        fc.MISSING_REQUIRED_EVIDENCE,
        first_broken_link="soak -> visual_observer_bounded",
        facts={
            "purpose": rec.get("purpose"),
            "call_kind": rec.get("call_kind"),
            "has_tools": rec.get("has_tools"),
            "image_count": rec.get("image_count"),
            "tool_count": rec.get("tool_count"),
            "stream": rec.get("stream"),
        },
    )


def _route_expectation(
    rec: dict[str, Any],
    *,
    main_host: str | None,
    main_model: object,
    visual: dict[str, Any] | None,
) -> tuple[str | None, object, str, OracleResult | None]:
    is_visual = rec.get("purpose") == "visual_inspection"
    if not is_visual:
        if visual is not None and rec.get("image_count") != 0:
            return (
                None,
                None,
                "",
                failing(
                    _ORACLE,
                    fc.MISSING_REQUIRED_EVIDENCE,
                    first_broken_link="soak -> visual_route_identified",
                    facts={
                        "purpose": rec.get("purpose"),
                        "image_count": rec.get("image_count"),
                        "reason": "dedicated visual routing requires exact image-call evidence",
                    },
                ),
            )
        return main_host, main_model, "soak -> model_pinned", None
    if shape_error := _visual_shape_error(rec):
        return None, None, "", shape_error
    if visual is None:
        return main_host, main_model, "soak -> visual_model_pinned", None
    return (
        str(visual["require_host_substr"]).lower(),
        visual["model"],
        "soak -> visual_model_pinned",
        None,
    )


def _check_provider_record(
    rec: dict[str, Any],
    *,
    forbid: list[str],
    main_host: str | None,
    main_model: object,
    visual: dict[str, Any] | None,
) -> OracleResult | None:
    host = str(rec.get("host", "")).lower()
    if rec.get("after_terminal"):
        return failing(
            _ORACLE,
            fc.PROVIDER_CALL_AFTER_TERMINAL,
            first_broken_link="terminal -> no_provider_calls",
            facts={
                "host": host,
                "model": rec.get("model"),
                "ts": rec.get("ts"),
                "conversation_id": rec.get("conversation_id"),
            },
        )
    if forbidden := next((item for item in forbid if item in host), None):
        return failing(
            _ORACLE,
            fc.PROVIDER_FORBIDDEN,
            first_broken_link="soak -> provider_allowed",
            facts={"host": host, "forbidden_substr": forbidden},
        )
    expected_host, expected_model, model_link, route_error = _route_expectation(
        rec, main_host=main_host, main_model=main_model, visual=visual
    )
    if route_error is not None:
        return route_error
    if expected_host is not None and expected_host not in host:
        return failing(
            _ORACLE,
            fc.PROVIDER_FORBIDDEN,
            first_broken_link="soak -> provider_allowed",
            facts={"host": host, "required_substr": expected_host},
        )
    if expected_model and str(rec.get("model", "")) != str(expected_model):
        return failing(
            _ORACLE,
            fc.PROVIDER_WRONG_MODEL,
            first_broken_link=model_link,
            facts={
                "model": rec.get("model"),
                "expected": expected_model,
                "host": host,
                "purpose": rec.get("purpose"),
            },
        )
    return None


def _check_ledger_records(
    provider_ledger: list[dict[str, Any]],
    prov: dict[str, Any],
) -> OracleResult | None:
    """Check each record for forbidden hosts, wrong models, and post-terminal calls."""
    forbid = [s.lower() for s in (prov.get("forbid_host_substr") or _DEFAULT_FORBID)]
    required_host = prov.get("require_host_substr")
    main_host = required_host.lower() if isinstance(required_host, str) else None
    visual, policy_error = _visual_policy(prov)
    if policy_error is not None:
        return policy_error
    for rec in provider_ledger:
        if error := _check_provider_record(
            rec,
            forbid=forbid,
            main_host=main_host,
            main_model=prov.get("model"),
            visual=visual,
        ):
            return error
    return None


class ProviderLedgerOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        provider_ledger: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> list[OracleResult]:
        prov = (scenario or {}).get("assertions", {}).get("provider")
        if not prov:
            return [skipping(_ORACLE, reason="scenario asserts no provider enforcement")]

        require_ledger = prov.get("require_ledger", True)
        presence_result = _check_ledger_presence(provider_ledger, require_ledger)
        if presence_result is not None:
            return presence_result

        assert provider_ledger is not None
        provider_ledger = records_for_conversation(provider_ledger, conversation_id)
        presence_result = _check_ledger_presence(provider_ledger, require_ledger)
        if presence_result is not None:
            return presence_result

        shape_error = _check_ledger_shape(provider_ledger)
        if shape_error is not None:
            return [shape_error]

        record_error = _check_ledger_records(provider_ledger, prov)
        if record_error is not None:
            return [record_error]

        facts = {"calls": len(provider_ledger)}
        visual_calls = sum(
            record.get("purpose") == "visual_inspection" for record in provider_ledger
        )
        if visual_calls:
            facts["visual_observer_calls"] = visual_calls
        return [passing(_ORACLE, facts=facts)]
