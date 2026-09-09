"""Structured web verification result builders — private implementation.

Converts trusted structured browser output into exact claim results.  Missing
fields are unavailable or failed; they are never filled from model prose.
Screenshot provenance is retained, but visual claims stay unavailable until a
separate vision-capable verifier explicitly judges the pixels.

Extracted from ``verification.py`` to reduce module/callable complexity; the
public facade re-imports the public functions unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..verification import (
    HostVerificationClaim,
    HostVerificationClaimResult,
    HostVerificationObservedFact,
    HostVerificationResult,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationEvidenceModality,
    default_structured_web_claims,
    normalized_required_text,
    validated_image_data_url,
)
from ._authority import with_verification_effect_receipt
from ._results import _result, _target_binding_fields


@dataclass(frozen=True)
class _StructuredWebEvidence:
    deliverable: Any
    verdict: dict[str, Any]
    verifier_id: str
    basis: str
    refs: tuple[str, ...]
    deterministic_pass: bool
    url: str
    http_status: Any
    rendered_text: Any
    visible_dom_text: Any
    console_errors: Any
    network_failures: Any
    meaningful: bool


def _basic_structured_claim_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
) -> HostVerificationClaimResult | None:
    common = {
        "verifier_id": evidence.verifier_id,
        "basis": evidence.basis,
        "refs": evidence.refs,
    }
    if claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY:
        return _artifact_identity_result(claim, evidence, common)
    if claim.kind is VerificationClaimKind.HTTP_READY:
        return _http_ready_result(claim, evidence, common)
    if claim.kind is VerificationClaimKind.RENDERED_CONTENT:
        return _rendered_content_result(claim, evidence, common)
    if claim.kind is VerificationClaimKind.VISIBLE_TEXT:
        return _visible_text_result(claim, evidence, common)
    if claim.kind is VerificationClaimKind.CONSOLE_CLEAN:
        return _console_clean_result(claim, evidence, common)
    if claim.kind is VerificationClaimKind.NETWORK_CLEAN:
        return _network_clean_result(claim, evidence, common)
    return None


def _identity_fields_match(identity: dict[str, Any], evidence: _StructuredWebEvidence) -> bool:
    """True iff the identity dict's scalar fields match the deliverable."""

    deliverable = evidence.deliverable
    return (
        identity.get("conversation_id") == deliverable.conversation_id
        and identity.get("artifact_path") == deliverable.artifact_path
        and identity.get("artifact_kind") == deliverable.artifact_kind
        and identity.get("requested_url") == deliverable.deployment_url
        and identity.get("observed_url") == evidence.url
    )


def _artifact_identity_matches(
    identity: Any, evidence: _StructuredWebEvidence, selected: Any
) -> bool:
    """True iff the verdict's artifact_identity binds the selected preview."""

    if not isinstance(identity, dict) or not _identity_fields_match(identity, evidence):
        return False
    if identity.get("preview_selection") != (
        selected.model_dump(mode="json") if selected is not None else None
    ):
        return False
    if identity.get("preview_live_match") is not True:
        return False
    if evidence.deliverable.preview_binding_required and selected is None:
        return False
    if selected is not None:
        if not selected.contains_artifact(evidence.deliverable.artifact_path):
            return False
        if selected.verification_target_url(evidence.deliverable.artifact_path) != evidence.url:
            return False
    return bool(evidence.url and evidence.deterministic_pass)


def _artifact_identity_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    identity = evidence.verdict.get("artifact_identity")
    selected = evidence.deliverable.preview_selection
    ok = _artifact_identity_matches(identity, evidence, selected)
    return _result(
        claim,
        VerificationClaimStatus.PASS if ok else VerificationClaimStatus.FAIL,
        "host bound the selected artifact to the observed preview URL"
        if ok
        else "host could not bind the artifact to an observed preview URL",
        modalities=(VerificationEvidenceModality.ARTIFACT_BINDING,),
        **common,
    )


def _http_ready_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    ok = (
        isinstance(evidence.http_status, int)
        and not isinstance(evidence.http_status, bool)
        and 200 <= evidence.http_status < 400
    )
    return _result(
        claim,
        VerificationClaimStatus.PASS if ok else VerificationClaimStatus.FAIL,
        f"host HTTP probe returned {evidence.http_status!r}",
        modalities=(VerificationEvidenceModality.HTTP,),
        **common,
    )


def _rendered_content_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    return _result(
        claim,
        VerificationClaimStatus.PASS if evidence.meaningful else VerificationClaimStatus.FAIL,
        "rendered DOM contained meaningful visible content"
        if evidence.meaningful
        else "rendered DOM did not prove meaningful visible content",
        modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,),
        **common,
    )


def _collapse_whitespace(value: str) -> str:
    """Every run of whitespace (line breaks included) is one space."""

    return " ".join(value.split())


def _visible_text_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    rendered_text = evidence.rendered_text if isinstance(evidence.rendered_text, str) else None
    visible_dom_text = (
        evidence.visible_dom_text if isinstance(evidence.visible_dom_text, str) else None
    )
    available = rendered_text is not None or visible_dom_text is not None
    # PROD-3: the required PHRASE is compared against the AUTHORED DOM text
    # case-insensitively and with runs of whitespace (line breaks included)
    # collapsed — a brief that quoted 'beans of the month' is satisfied by a
    # page heading that reads "Beans of the Month" across two wrapped lines,
    # and the agent no longer burns end-of-run turns editing correct copy.
    #
    # `rendered_text` stays case-EXACT on purpose: it is post-CSS, so its
    # casing belongs to `text-transform`, not to the author. Seed 405115 is
    # already handled by consulting the authored `visible_dom_text` instead
    # of casefolding the rendered projection.
    expected_exact = _collapse_whitespace(claim.expected)
    expected_folded = normalized_required_text(claim.expected)
    ok = (
        rendered_text is not None
        and expected_exact in _collapse_whitespace(rendered_text)
        or visible_dom_text is not None
        and expected_folded in normalized_required_text(visible_dom_text)
    )
    return _result(
        claim,
        VerificationClaimStatus.PASS
        if ok
        else VerificationClaimStatus.FAIL
        if available
        else VerificationClaimStatus.UNAVAILABLE,
        f"structured visible DOM/accessibility contains required text "
        f"{claim.expected!r} (authored DOM text matched ignoring case and "
        f"whitespace runs)"
        if ok
        else f"structured visible DOM/accessibility does not contain required text "
        f"{claim.expected!r} (authored DOM text matched ignoring case and "
        f"whitespace runs)"
        if available
        else "structured browser receipt omitted visible DOM/accessibility text",
        modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,) if available else (),
        **common,
    )


def _console_clean_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    available = isinstance(evidence.console_errors, list)
    ok = available and not evidence.console_errors
    return _result(
        claim,
        VerificationClaimStatus.PASS
        if ok
        else VerificationClaimStatus.FAIL
        if available
        else VerificationClaimStatus.UNAVAILABLE,
        "browser runtime reported zero console errors"
        if ok
        else "browser runtime reported console errors"
        if available
        else "structured browser receipt omitted console diagnostics",
        modalities=(VerificationEvidenceModality.RUNTIME_CONSOLE,) if available else (),
        **common,
    )


def _network_clean_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    available = isinstance(evidence.network_failures, list)
    ok = available and not evidence.network_failures
    return _result(
        claim,
        VerificationClaimStatus.PASS
        if ok
        else VerificationClaimStatus.FAIL
        if available
        else VerificationClaimStatus.UNAVAILABLE,
        "browser runtime reported zero critical network failures"
        if ok
        else "browser runtime reported critical network failures"
        if available
        else "structured browser receipt omitted network diagnostics",
        modalities=(VerificationEvidenceModality.RUNTIME_NETWORK,) if available else (),
        **common,
    )


def _extended_structured_claim_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
) -> HostVerificationClaimResult:
    common = {
        "verifier_id": evidence.verifier_id,
        "basis": evidence.basis,
        "refs": evidence.refs,
    }
    if claim.kind is VerificationClaimKind.INTERACTION:
        return _interaction_result(claim, evidence, common)
    if claim.kind is VerificationClaimKind.ROUTE:
        return _route_result(claim, evidence, common)
    visual = claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
    return _result(
        claim,
        VerificationClaimStatus.UNAVAILABLE,
        "visual-semantic claim requires a configured vision-capable verifier receipt"
        if visual
        else "claim requires an independent target/semantic verifier receipt",
        **common,
    )


def _interaction_steps_pass(steps: Any) -> bool:
    """True iff all interaction steps succeeded without errors."""

    return bool(steps) and all(
        isinstance(step, dict) and step.get("success") is True and not step.get("error")
        for step in steps
    )


def _interaction_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    interactions = evidence.verdict.get("interaction_claims")
    interaction = interactions.get(claim.claim_id) if isinstance(interactions, dict) else None
    interaction_data = interaction if isinstance(interaction, dict) else {}
    steps = interaction_data.get("steps")
    available = bool(interaction_data) and isinstance(steps, list)
    exact_contract = available and interaction_data.get("expected") == claim.expected
    ok = (
        available
        and exact_contract
        and interaction_data.get("passed") is True
        and _interaction_steps_pass(steps)
    )
    return _result(
        claim,
        VerificationClaimStatus.PASS
        if ok
        else VerificationClaimStatus.FAIL
        if available
        else VerificationClaimStatus.UNAVAILABLE,
        "host interaction sequence completed without errors"
        if ok
        else "host interaction evidence did not prove the exact required outcome"
        if available
        else "no host-owned interaction evidence was available",
        modalities=(VerificationEvidenceModality.INTERACTION,) if available else (),
        **common,
    )


def _route_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
    common: dict[str, Any],
) -> HostVerificationClaimResult:
    ok = evidence.url.rstrip("/") == claim.expected.rstrip("/")
    return _result(
        claim,
        VerificationClaimStatus.PASS if ok else VerificationClaimStatus.FAIL,
        f"observed route was {evidence.url!r}",
        modalities=(VerificationEvidenceModality.NAVIGATION,),
        **common,
    )


def _build_structured_evidence(
    deliverable: Any,
    verdict: dict[str, Any],
    resolved_verifier_id: str,
    basis: str,
    refs: tuple[str, ...],
) -> _StructuredWebEvidence:
    label = str(verdict.get("verdict") or "").lower()
    deterministic_pass = verdict.get("passed") is True and label == "pass"
    return _StructuredWebEvidence(
        deliverable=deliverable,
        verdict=verdict,
        verifier_id=resolved_verifier_id,
        basis=basis,
        refs=refs,
        deterministic_pass=deterministic_pass,
        url=str(verdict.get("url") or ""),
        http_status=verdict.get("http_status"),
        rendered_text=verdict.get("rendered_text"),
        visible_dom_text=verdict.get("visible_dom_text"),
        console_errors=verdict.get("console_errors"),
        network_failures=verdict.get("network_failures"),
        meaningful=verdict.get("meaningful_content") is True,
    )


def _screenshot_provenance(verdict: dict[str, Any]) -> tuple[str, str | None]:
    """Return ``(screenshot_path, screenshot_sha256)`` from the verdict."""

    raw_screenshot_path = str(verdict.get("screenshot_path") or "")
    screenshot_path = (
        raw_screenshot_path
        if len(raw_screenshot_path) <= 512
        and not any(ord(char) < 32 or ord(char) == 127 for char in raw_screenshot_path)
        else ""
    )
    screenshot_b64 = verdict.get("screenshot_b64")
    screenshot_sha256: str | None = None
    if isinstance(screenshot_b64, str) and screenshot_b64:
        validated = validated_image_data_url(f"data:image/png;base64,{screenshot_b64}")
        if validated is not None:
            _, screenshot_sha256 = validated
    return screenshot_path, screenshot_sha256


def _workspace_epoch(freshness: dict[str, Any]) -> int | None:
    raw_workspace_epoch = freshness.get("synchronized_epoch")
    return (
        raw_workspace_epoch
        if isinstance(raw_workspace_epoch, int)
        and not isinstance(raw_workspace_epoch, bool)
        and raw_workspace_epoch > 0
        else None
    )


def _observed_visible_text(verdict: dict[str, Any]) -> str | None:
    visible_dom_text = verdict.get("visible_dom_text")
    rendered_text = verdict.get("rendered_text")
    return (
        visible_dom_text
        if isinstance(visible_dom_text, str) and visible_dom_text
        else rendered_text
    )


def _aggregate_status(
    results: list[HostVerificationClaimResult],
) -> VerificationClaimStatus:
    return (
        VerificationClaimStatus.FAIL
        if any(
            result.required and result.status is VerificationClaimStatus.FAIL for result in results
        )
        else VerificationClaimStatus.UNAVAILABLE
        if any(
            result.required and result.status is VerificationClaimStatus.UNAVAILABLE
            for result in results
        )
        else VerificationClaimStatus.PASS
    )


def _overall_reason(
    status: VerificationClaimStatus,
    results: list[HostVerificationClaimResult],
) -> str:
    return (
        "all mandatory verification claims are covered by current trusted receipts"
        if status is VerificationClaimStatus.PASS
        else next(
            result.reason for result in results if result.required and result.status is status
        )
    )


def _build_claim_results(
    claims: tuple[HostVerificationClaim, ...],
    evidence: _StructuredWebEvidence,
    infrastructure_unavailable: bool,
    verdict: dict[str, Any],
    resolved_verifier_id: str,
    basis: str,
    refs: tuple[str, ...],
) -> list[HostVerificationClaimResult]:
    """Build the per-claim result list from the structured evidence."""

    results: list[HostVerificationClaimResult] = []
    for claim in claims:
        if infrastructure_unavailable:
            results.append(
                _result(
                    claim,
                    VerificationClaimStatus.UNAVAILABLE,
                    str(verdict.get("summary") or "structured browser verification unavailable"),
                    verifier_id=resolved_verifier_id,
                    basis=basis,
                    refs=refs,
                )
            )
            continue
        result = _basic_structured_claim_result(claim, evidence)
        results.append(result or _extended_structured_claim_result(claim, evidence))
    return results


def _observed_facts(
    evidence: _StructuredWebEvidence,
    observed_visible_text: str | None,
    resolved_verifier_id: str,
    basis: str,
    refs: tuple[str, ...],
) -> tuple[HostVerificationObservedFact, ...]:
    """Build the visible-text observed fact when deterministic pass + real text."""

    if (
        evidence.deterministic_pass
        and isinstance(observed_visible_text, str)
        and 0 < len(observed_visible_text) <= 131_072
    ):
        return (
            HostVerificationObservedFact(
                fact_id="web.observed.visible_text",
                kind=VerificationClaimKind.VISIBLE_TEXT,
                value=observed_visible_text,
                verifier_id=resolved_verifier_id,
                capability_basis=basis,
                evidence_modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,),
                evidence_refs=refs,
            ),
        )
    return ()


def structured_web_verification_result(
    *,
    deliverable: Any,
    verdict: dict[str, Any],
    verifier_id: str | None = None,
    tool_id: str | None = None,
) -> HostVerificationResult:
    """Convert trusted structured browser output into exact claim results.

    Missing fields are unavailable or failed; they are never filled from model
    prose.  Screenshot provenance is retained, but visual claims stay unavailable
    until a separate vision-capable verifier explicitly judges the pixels.
    """

    check = getattr(deliverable, "verification_check", None)
    resolved_verifier_id = verifier_id or (
        check.issuer_id if check is not None else "host.verify_web_app@1"
    )
    resolved_tool_id = tool_id or (check.operation if check is not None else "verify_web_app@1")
    label = str(verdict.get("verdict") or "").lower()
    infrastructure_unavailable = label in {"unavailable", "unverifiable"}
    url = str(verdict.get("url") or "")
    basis = "deterministic host structured-browser receipt"
    screenshot_path, screenshot_sha256 = _screenshot_provenance(verdict)
    refs = tuple(
        ref
        for ref in (
            f"artifact:{deliverable.artifact_path}",
            f"url:{url}" if url else "",
            f"screenshot:{screenshot_path}" if screenshot_path else "",
        )
        if ref
    )

    claims = deliverable.required_claims or default_structured_web_claims()
    evidence = _build_structured_evidence(
        deliverable, verdict, resolved_verifier_id, basis, refs
    )
    results = _build_claim_results(
        claims, evidence, infrastructure_unavailable, verdict, resolved_verifier_id, basis, refs
    )

    status = _aggregate_status(results)
    reason = _overall_reason(status, results)
    raw_freshness = verdict.get("freshness")
    freshness: dict[str, Any] = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}
    workspace_epoch = _workspace_epoch(freshness)
    observed_visible_text = _observed_visible_text(verdict)
    facts = _observed_facts(evidence, observed_visible_text, resolved_verifier_id, basis, refs)
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url=url,
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=str(freshness.get("executor_generation") or ""),
        workspace_epoch=workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=resolved_verifier_id,
        tool_id=resolved_tool_id,
        status=status,
        reason=reason,
        claim_results=tuple(results),
        observed_facts=facts,
        screenshot_path=screenshot_path,
        screenshot_sha256=screenshot_sha256,
    )
    return with_verification_effect_receipt(receipt)


def apply_semantic_verifier_result(
    receipt: HostVerificationResult,
    *,
    verified: bool,
    verdict: str,
    detail: str,
    verifier_id: str = "model_role.verifier@1",
    claim_id: str | None = None,
) -> HostVerificationResult:
    """Apply one bounded independent judge to one exact semantic claim.

    The no-id form is retained for the standalone one-claim adapter.  It refuses
    to fan one aggregate judgement across multiple semantic requirements.
    """

    semantic_kinds = {
        VerificationClaimKind.CONTRACT_SEMANTIC,
        VerificationClaimKind.VISUAL_SEMANTIC,
    }
    label = str(verdict).lower()
    status = (
        VerificationClaimStatus.PASS
        if verified and label == "pass"
        else VerificationClaimStatus.UNAVAILABLE
        if label in {"unavailable", "unverifiable"}
        else VerificationClaimStatus.FAIL
    )
    semantic_results = [result for result in receipt.claim_results if result.kind in semantic_kinds]
    target_id = claim_id or (semantic_results[0].claim_id if len(semantic_results) == 1 else None)
    updated: list[HostVerificationClaimResult] = []
    for result in receipt.claim_results:
        if result.kind not in semantic_kinds or result.claim_id != target_id:
            updated.append(result)
            continue
        updated.append(
            _apply_semantic_to_result(
                result, status, verdict, detail, verifier_id, receipt.screenshot_sha256
            )
        )
    overall = _aggregate_status(updated)
    reason = _overall_reason(overall, updated)
    updated_receipt = receipt.model_copy(
        update={
            "status": overall,
            "reason": reason,
            "claim_results": tuple(updated),
            "effect_receipt": None,
        }
    )
    return with_verification_effect_receipt(updated_receipt)


def _apply_semantic_to_result(
    result: HostVerificationClaimResult,
    status: VerificationClaimStatus,
    label: str,
    detail: str,
    verifier_id: str,
    screenshot_sha256: str | None,
) -> HostVerificationClaimResult:
    visual = result.kind is VerificationClaimKind.VISUAL_SEMANTIC
    applied_status = (
        VerificationClaimStatus.UNAVAILABLE
        if visual
        and status is VerificationClaimStatus.PASS
        and screenshot_sha256 is None
        else status
    )
    return result.model_copy(
        update={
            "status": applied_status,
            "reason": (
                "vision verifier cannot pass without captured pixel evidence"
                if applied_status is VerificationClaimStatus.UNAVAILABLE
                and status is VerificationClaimStatus.PASS
                else detail or f"independent verifier returned {label}"
            ),
            "verifier_id": verifier_id,
            "capability_basis": (
                "configured independent vision-capable verifier"
                if visual
                else "configured independent semantic verifier"
            ),
            "evidence_modalities": (
                (VerificationEvidenceModality.SCREENSHOT_PIXELS,)
                if visual and applied_status is not VerificationClaimStatus.UNAVAILABLE
                else ()
            ),
        }
    )