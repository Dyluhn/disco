"""Core contract helpers for the governed verification authority oracle."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

_ORACLE = "GovernedVerificationOracle"
_WORK_TERMINALS = frozenset({"FINISHED", "VERIFIED"})


def _policy(scenario: dict[str, Any] | None) -> dict[str, Any] | None:
    if not scenario:
        return None
    assertions = scenario.get("assertions")
    raw = assertions.get("governed_verification") if isinstance(assertions, dict) else None
    return raw if isinstance(raw, dict) and raw.get("required") is True else None


def _fail(link: str, reason: str, **facts: Any) -> list:
    from .. import failure_codes as fc
    from .schema import failing

    return [
        failing(
            _ORACLE,
            fc.GOVERNED_ADMISSION_BYPASSED,
            first_broken_link=link,
            facts={"reason": reason, **facts},
        )
    ]


def _seq(event: dict[str, Any]) -> int:
    value = event.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _terminal_segment(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int] | None:
    terminals = sorted(
        (
            event
            for event in events
            if event.get("kind") == "status"
            and event.get("status") in _WORK_TERMINALS
            and not str(event.get("detail") or "").startswith("host_revision:")
            and _seq(event) >= 0
        ),
        key=_seq,
    )
    if not terminals:
        return None
    final_seq = _seq(terminals[-1])
    prior_seq = _seq(terminals[-2]) if len(terminals) > 1 else -1
    return (
        [event for event in events if prior_seq < _seq(event) <= final_seq],
        prior_seq,
        final_seq,
    )


def _claim_contract(claim: dict[str, Any]) -> tuple[object, ...]:
    return (
        claim.get("claim_id"),
        claim.get("kind"),
        claim.get("required", True),
        claim.get("expected", ""),
        claim.get("source_authority"),
        claim.get("reference_image_sha256"),
    )


def _active_requirement_directive(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    active: dict[str, Any] | None = None
    for event in sorted(
        (
            event
            for event in events
            if event.get("kind") == "message"
            and event.get("source") == "user"
            and _seq(event) <= through_seq
        ),
        key=_seq,
    ):
        directive = event.get("verification_requirements")
        if not isinstance(directive, dict):
            continue
        expected = active.get("id") if active is not None else None
        if directive.get("supersedes_event_id") == expected:
            active = event
    if active is None:
        return None
    directive = active.get("verification_requirements")
    assert isinstance(directive, dict)
    return active, directive


def _reference_image_hashes(images: Any) -> dict[int, str]:
    references: dict[int, str] = {}
    if isinstance(images, list):
        for index, image in enumerate(images):
            if not isinstance(image, str):
                continue
            _header, separator, payload = image.partition(",")
            if not separator:
                continue
            try:
                raw = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                continue
            references[index] = hashlib.sha256(raw).hexdigest()
    return references


def _external_claim_contracts(
    active: dict[str, Any],
    directive: dict[str, Any],
    references: dict[int, str],
) -> tuple[tuple[object, ...], ...]:
    out: list[tuple[object, ...]] = []
    claims = directive.get("claims")
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or claim.get("required", True) is not True:
            continue
        reference_index = claim.get("reference_image_index")
        reference_hash = (
            references.get(reference_index) if isinstance(reference_index, int) else None
        )
        out.append(
            (
                claim.get("claim_id"),
                claim.get("kind"),
                claim.get("required", True),
                claim.get("expected", ""),
                f"user_event:{active.get('id')}",
                reference_hash,
            )
        )
    return tuple(out)


def _active_external_claims(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> tuple[tuple[object, ...], ...]:
    active_directive = _active_requirement_directive(
        events,
        through_seq=through_seq,
    )
    if active_directive is None:
        return ()
    active, directive = active_directive
    references = _reference_image_hashes(directive.get("reference_images"))
    return _external_claim_contracts(active, directive, references)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt_authority_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: receipt.get(key)
        for key in (
            "conversation_id",
            "run_intent_id",
            "run_identity",
            "target_id",
            "delivery_shape",
            "delivery_entry_reference",
            "verification_contract_digest",
            "check_id",
            "receipt_kind",
            "issuer_id",
            "operation",
            "delegated_issuer_ids",
            "execution_identity",
            "artifact_identity",
            "agent_view_id",
            "deliverable_event_id",
            "artifact_path",
            "artifact_kind",
            "observed_url",
            "preview_selection",
            "workspace_revision",
            "workspace_generation",
            "workspace_epoch",
            "observed_after_seq",
        )
    }
    payload["delegated_issuer_ids"] = sorted(receipt.get("delegated_issuer_ids") or [])
    observed_facts = receipt.get("observed_facts")
    if isinstance(observed_facts, list) and observed_facts:
        payload["observed_facts"] = observed_facts
    return payload


def _receipt_requirement_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    claims = receipt.get("claim_results")
    return {
        "verification_contract_digest": receipt.get("verification_contract_digest"),
        "check_id": receipt.get("check_id"),
        "receipt_kind": receipt.get("receipt_kind"),
        "issuer_id": receipt.get("issuer_id"),
        "operation": receipt.get("operation"),
        "delegated_issuer_ids": sorted(receipt.get("delegated_issuer_ids") or []),
        "claims": [
            {
                key: claim.get(key)
                for key in (
                    "claim_id",
                    "kind",
                    "required",
                    "expected",
                    "source_authority",
                    "reference_image_sha256",
                )
            }
            for claim in claims or []
            if isinstance(claim, dict)
        ],
    }


def _effect_receipt_is_exact(receipt: dict[str, Any]) -> bool:
    effect = receipt.get("effect_receipt")
    subject = effect.get("subject") if isinstance(effect, dict) else None
    resource = subject.get("resource") if isinstance(subject, dict) else None
    return bool(
        isinstance(effect, dict)
        and effect.get("kind") == "verification"
        and effect.get("capability") == "artifact.verify"
        and effect.get("passed") is True
        and effect.get("failure_fingerprint") is None
        and effect.get("verifier_id") == receipt.get("verifier_id")
        and isinstance(subject, dict)
        and subject.get("algorithm") == "sha256"
        and subject.get("digest") == _canonical_digest(_receipt_authority_payload(receipt))
        and isinstance(resource, dict)
        and resource.get("namespace") == "verification.subject"
        and effect.get("requirement_fingerprint")
        == _canonical_digest(_receipt_requirement_payload(receipt))
    )


def _artifact_identity_is_exact(
    identity: object,
    *,
    scheme: str,
    entry_reference: object,
    producer_id: object,
) -> bool:
    if not isinstance(identity, dict):
        return False
    digest = identity.get("digest")
    try:
        digest_bytes = bytes.fromhex(
            digest.removeprefix("sha256:") if isinstance(digest, str) else ""
        )
    except ValueError:
        return False
    return bool(
        identity.get("scheme") == scheme
        and isinstance(digest, str)
        and digest.startswith("sha256:")
        and len(digest_bytes) == 32
        and identity.get("entry_reference") == entry_reference
        and identity.get("producer_id") == producer_id
    )


def _event_matches_run_intent(
    segment: list[dict[str, Any]],
    event: dict[str, Any],
    run_intent: dict[str, Any],
) -> bool:
    event_seq = _seq(event)
    intent_seq = _seq(run_intent)
    if event_seq <= intent_seq or not event.get("agent_view_id"):
        return False
    producing_admission = max(
        (
            candidate
            for candidate in segment
            if candidate.get("kind") == "workspace_mutation"
            and candidate.get("operation") == "agent.view-admitted"
            and candidate.get("run_intent_id") == run_intent.get("id")
            and intent_seq < _seq(candidate) < event_seq
        ),
        key=_seq,
        default=None,
    )
    return bool(
        producing_admission is not None
        and producing_admission.get("agent_view_id") == event.get("agent_view_id")
    )
