"""Target-neutral governed verification authority oracle."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
from collections import Counter
from typing import Any
from urllib.parse import quote

from .. import failure_codes as fc
from ..events import action_executed
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "GovernedVerificationOracle"
_WORK_TERMINALS = frozenset({"FINISHED", "VERIFIED"})
_MUTATING_TOOLS = frozenset(
    {
        "file_write",
        "file_append",
        "file_edit",
        "file_replace_lines",
        "shell",
        "code_exec",
        "app_create",
        "app_mutate",
        "app_eject",
        "workspace_restore",
    }
)
_NON_PRODUCTIVE_TOOLS = frozenset(
    {
        "submit_plan",
        "think",
        "plan_step",
        "update_plan_progress",
        "ask_user",
        "questions_v2",
        "propose_plan_update",
        "notify_user",
        "finish",
        "remember",
        "serve",
        "file_read",
        "file_list",
        "search",
        "extract",
        "server_status",
        "preview_start",
        "preview_status",
        "preview_logs",
        "preview_stop",
        "shell_view",
        "shell_wait",
        "browser",
        "verify_web_app",
        "verify_appkit_app",
        "design_lint",
        "app_snapshot_version",
    }
)


def _policy(scenario: dict[str, Any] | None) -> dict[str, Any] | None:
    if not scenario:
        return None
    assertions = scenario.get("assertions")
    raw = assertions.get("governed_verification") if isinstance(assertions, dict) else None
    return raw if isinstance(raw, dict) and raw.get("required") is True else None


def _fail(link: str, reason: str, **facts: Any) -> list[OracleResult]:
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


def _active_external_claims(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> tuple[tuple[object, ...], ...]:
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
        return ()
    directive = active.get("verification_requirements")
    assert isinstance(directive, dict)
    images = directive.get("reference_images")
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
    out: list[tuple[object, ...]] = []
    claims = directive.get("claims")
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or claim.get("required", True) is not True:
            continue
        reference_index = claim.get("reference_image_index")
        out.append(
            (
                claim.get("claim_id"),
                claim.get("kind"),
                claim.get("required", True),
                claim.get("expected", ""),
                f"user_event:{active.get('id')}",
                references.get(reference_index) if isinstance(reference_index, int) else None,
            )
        )
    return tuple(out)


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
    """Mirror core's durable same-intent output authority for raw soak events."""

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


def _preview_identity_from_pair(
    action: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    call = action.get("tool_call")
    result = observation.get("tool_result")
    tool_name = call.get("tool_name") if isinstance(call, dict) else None
    outer = result.get("structured") if isinstance(result, dict) else None
    if (
        not isinstance(call, dict)
        or tool_name not in {"preview_start", "verify_appkit_app"}
        or not isinstance(result, dict)
        or result.get("tool_name") != tool_name
        or result.get("success") is not True
        or observation.get("action_id") != action.get("id")
        or result.get("call_id") != call.get("call_id")
    ):
        return None
    structured = outer
    if tool_name == "verify_appkit_app":
        if not isinstance(outer, dict) or outer.get("passed") is not True:
            return None
        structured = outer.get("preview_runtime")
    if not isinstance(structured, dict) or structured.get("status") not in {
        "running",
        "unavailable",
    }:
        return None
    intent = structured.get("intent")
    command = structured.get("command")
    exec_dir = structured.get("exec_dir")
    name = structured.get("name")
    port = structured.get("port")
    launch_kind = structured.get("launch_kind")
    if (
        not isinstance(intent, dict)
        or not isinstance(command, str)
        or not command
        or exec_dir is not None
        and not isinstance(exec_dir, str)
        or not isinstance(name, str)
        or not name
        or type(port) is not int
        or launch_kind not in {"static", "custom", "framework"}
        or intent.get("launch_kind") != launch_kind
    ):
        return None
    digest = _canonical_digest(
        {
            "command": command,
            "exec_dir": exec_dir,
            "intent": intent,
            "name": name,
            "port": port,
        }
    )
    if structured.get("intent_digest") != digest:
        return None
    action_seq = _seq(action)
    observation_seq = _seq(observation)
    if action_seq < 1 or observation_seq <= action_seq:
        return None
    static_serve_dir: str | None = None
    if launch_kind == "static":
        normalized_serve_dir = posixpath.normpath(str(intent.get("serve_dir") or "."))
        if normalized_serve_dir == "/workspace":
            static_serve_dir = "."
        elif normalized_serve_dir.startswith("/workspace/"):
            static_serve_dir = normalized_serve_dir.removeprefix("/workspace/")
        elif (
            not normalized_serve_dir.startswith("/")
            and normalized_serve_dir != ".."
            and not normalized_serve_dir.startswith("../")
        ):
            static_serve_dir = normalized_serve_dir
        else:
            return None
    return {
        "projection_id": structured.get("projection_id"),
        "session_name": name,
        "port": port,
        "url": str(structured.get("url") or ""),
        "launch_kind": launch_kind,
        "intent_digest": digest,
        "sandbox_instance_id": structured.get("sandbox_instance_id"),
        "sandbox_generation": structured.get("sandbox_generation"),
        "static_serve_dir": static_serve_dir,
        "source_action_id": action.get("id"),
        "source_action_seq": action_seq,
        "source_observation_id": observation.get("id"),
        "source_observation_seq": observation_seq,
    }


def _active_preview_selection(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> dict[str, Any] | None:
    actions: dict[str, dict[str, Any]] = {}
    active: dict[str, tuple[int, dict[str, Any]]] = {}
    for event in sorted(
        (event for event in events if 0 <= _seq(event) <= through_seq),
        key=_seq,
    ):
        if event.get("kind") == "action":
            actions[str(event.get("id") or "")] = event
            continue
        if event.get("kind") != "observation":
            continue
        result = event.get("tool_result")
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        if result.get("tool_name") in {"preview_start", "verify_appkit_app"}:
            action = actions.get(str(event.get("action_id") or ""))
            identity = (
                _preview_identity_from_pair(action, event) if isinstance(action, dict) else None
            )
            if identity is None or not isinstance(identity.get("session_name"), str):
                active.clear()
                continue
            active[identity["session_name"]] = (_seq(event), identity)
            continue
        if result.get("tool_name") != "preview_stop":
            continue
        action = actions.get(str(event.get("action_id") or ""))
        structured = result.get("structured")
        stopped = structured.get("stopped") if isinstance(structured, dict) else None
        call = action.get("tool_call") if isinstance(action, dict) else None
        if (
            not isinstance(call, dict)
            or call.get("tool_name") != "preview_stop"
            or result.get("call_id") != call.get("call_id")
            or not isinstance(stopped, list)
            or not stopped
            or not all(isinstance(name, str) and name for name in stopped)
        ):
            active.clear()
            continue
        for name in stopped:
            active.pop(name, None)
    return max(active.values(), key=lambda item: item[0])[1] if active else None


def _preview_selection_is_current(
    selection: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    deliverable_seq: int,
    verdict_seq: int,
) -> bool:
    source_action = next(
        (
            event
            for event in events
            if event.get("kind") == "action"
            and event.get("id") == selection.get("source_action_id")
            and _seq(event) == selection.get("source_action_seq")
        ),
        None,
    )
    source_observation = next(
        (
            event
            for event in events
            if event.get("kind") == "observation"
            and event.get("id") == selection.get("source_observation_id")
            and _seq(event) == selection.get("source_observation_seq")
        ),
        None,
    )
    reconstructed = (
        _preview_identity_from_pair(source_action, source_observation)
        if isinstance(source_action, dict) and isinstance(source_observation, dict)
        else None
    )
    operational_fields = (
        "projection_id",
        "session_name",
        "port",
        "launch_kind",
        "intent_digest",
        "sandbox_instance_id",
        "sandbox_generation",
        "url",
    )
    handoff = _active_preview_selection(events, through_seq=deliverable_seq)
    current = _active_preview_selection(events, through_seq=verdict_seq - 1)
    selected = (
        current
        if handoff is None
        else handoff
        if isinstance(current, dict)
        and tuple(handoff.get(field) for field in operational_fields)
        == tuple(current.get(field) for field in operational_fields)
        else None
    )
    return (
        reconstructed == selection
        and selection == selected
        and selection.get("source_observation_seq", verdict_seq) < verdict_seq
    )


def _preview_verification_url(
    selection: dict[str, Any],
    artifact_path: str,
) -> str | None:
    port = selection.get("port")
    if type(port) is not int:
        return None
    base = f"http://127.0.0.1:{port}"
    serve_dir = selection.get("static_serve_dir")
    if serve_dir is None:
        return f"{base}/"
    root = posixpath.normpath(str(serve_dir or "."))
    artifact = posixpath.normpath(artifact_path)
    if root != "." and artifact != root and not artifact.startswith(f"{root}/"):
        return None
    relative = artifact if root == "." else posixpath.relpath(artifact, root)
    if relative == "index.html":
        return f"{base}/"
    encoded = "/".join(quote(part, safe="") for part in relative.split("/"))
    return f"{base}/{encoded}"


def _shared_execution_authority(execution: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Identity shared by target checks after each check validates its modality."""

    return (
        execution.get("instance_id"),
        execution.get("generation"),
        execution.get("locator"),
    )


def _has_workspace_mutation_capability(event_or_result: dict[str, Any]) -> bool:
    profile = event_or_result.get("action_profile")
    capabilities = profile.get("capabilities") if isinstance(profile, dict) else None
    if isinstance(capabilities, list) and "workspace.mutate" in capabilities:
        return True
    receipts = event_or_result.get("effect_receipts")
    return any(
        isinstance(receipt, dict) and receipt.get("capability") == "workspace.mutate"
        for receipt in receipts or []
    )


def _authority_change_between(
    events: list[dict[str, Any]],
    after_seq: int,
    through_seq: int,
) -> bool:
    for event in events:
        seq = _seq(event)
        if not after_seq < seq < through_seq:
            continue
        if event.get("kind") in {"workspace_restored", "appkit_ejection"}:
            return True
        if event.get("kind") == "workspace_mutation":
            operation = str(event.get("operation") or "")
            if not operation.startswith(("agent.view-", "artifact-manifest-fold")):
                return True
        if event.get("kind") == "action":
            call = event.get("tool_call")
            if (
                isinstance(call, dict)
                and (
                    call.get("tool_name") in _MUTATING_TOOLS
                    or _has_workspace_mutation_capability(event)
                )
                and action_executed(events, str(event.get("id") or ""))
            ):
                return True
        if event.get("kind") == "observation":
            result = event.get("tool_result")
            if isinstance(result, dict) and _has_workspace_mutation_capability(result):
                return True
            if (
                isinstance(result, dict)
                and result.get("success") is True
                and result.get("tool_name") in {"preview_start", "preview_stop"}
            ):
                return True
        if event.get("kind") == "agent_error" and _has_workspace_mutation_capability(event):
            return True
        if event.get("kind") in {"deliverable", "build_platform_admission"}:
            return True
        if event.get("kind") == "message" and event.get("source") == "user":
            return True
    return False


def _last_successful_productive_action_seq(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> int:
    actions = {
        str(event.get("id") or ""): event
        for event in events
        if event.get("kind") == "action" and _seq(event) < through_seq
    }
    successful: set[str] = set()
    for event in events:
        if event.get("kind") != "observation" or _seq(event) >= through_seq:
            continue
        result = event.get("tool_result")
        action_id = str(event.get("action_id") or "")
        if action_id in actions and isinstance(result, dict) and result.get("success") is True:
            successful.add(action_id)
    return max(
        (
            _seq(action)
            for action_id, action in actions.items()
            if action_id in successful
            and isinstance(action.get("tool_call"), dict)
            and action["tool_call"].get("tool_name") not in _NON_PRODUCTIVE_TOOLS
        ),
        default=0,
    )


def _last_authority_seq(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> int:
    authority = _last_successful_productive_action_seq(
        events,
        through_seq=through_seq,
    )
    actions = {
        str(event.get("id") or ""): event
        for event in events
        if event.get("kind") == "action" and _seq(event) < through_seq
    }
    for event in events:
        seq = _seq(event)
        if not 0 <= seq < through_seq:
            continue
        if event.get("kind") in {"deliverable", "build_platform_admission"}:
            authority = max(authority, seq)
        elif event.get("kind") == "message" and event.get("source") == "user":
            authority = max(authority, seq)
        elif event.get("kind") == "observation":
            result = event.get("tool_result")
            action = actions.get(str(event.get("action_id") or ""))
            call = action.get("tool_call") if isinstance(action, dict) else None
            if isinstance(result, dict) and _has_workspace_mutation_capability(result):
                authority = max(authority, seq)
            if (
                isinstance(result, dict)
                and result.get("success") is True
                and result.get("tool_name") in {"preview_start", "preview_stop"}
                and isinstance(call, dict)
                and call.get("tool_name") == result.get("tool_name")
                and call.get("call_id") == result.get("call_id")
            ):
                authority = max(authority, seq)
        elif event.get("kind") == "agent_error" and _has_workspace_mutation_capability(event):
            authority = max(authority, seq)
    return authority


class GovernedAdmissionOracle:
    """Require exact current PASS receipts for every admitted target check."""

    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        conversation_id: str = "",
    ) -> list[OracleResult]:
        policy = _policy(scenario)
        if policy is None:
            return [skipping(_ORACLE, reason="scenario has no governed verification policy")]
        bounded = _terminal_segment(events)
        if bounded is None:
            return _fail("event_chain -> terminal", "no successful work terminal exists")
        segment, prior_terminal_seq, final_seq = bounded
        admission = next(
            (
                event
                for event in reversed(segment)
                if event.get("kind") == "build_platform_admission"
            ),
            None,
        )
        if admission is None:
            return _fail(
                "user_event -> build_platform_admission",
                "current terminal segment has no Build Platform admission",
            )
        if (
            admission.get("route") != policy.get("route", "platform")
            or admission.get("composition_authority")
            != policy.get("composition_authority", "build_platform_core")
            or not admission.get("run_identity")
        ):
            return _fail(
                "build_platform_admission -> composition_authority",
                "current admission does not carry the required Platform authority",
            )
        run_intent = next(
            (
                event
                for event in segment
                if event.get("kind") == "workspace_mutation"
                and event.get("id") == admission.get("run_intent_id")
                and str(event.get("operation") or "").startswith("agent.run-intent")
                and _seq(event) < _seq(admission)
            ),
            None,
        )
        if run_intent is None:
            return _fail(
                "run_intent -> build_platform_admission",
                "admission run intent is absent, foreign, or not causally prior",
            )
        current_view = next(
            (
                event
                for event in reversed(segment)
                if event.get("kind") == "workspace_mutation"
                and event.get("operation") == "agent.view-admitted"
                and event.get("run_intent_id") == run_intent.get("id")
                and isinstance(event.get("agent_view_id"), str)
                and bool(event["agent_view_id"])
                and _seq(event) < final_seq
            ),
            None,
        )
        if policy.get("require_agent_view_binding", True) and current_view is None:
            return _fail(
                "run_intent -> agent_view",
                "current run has no durable admitted model-view authority",
            )
        current_view_id = (
            current_view.get("agent_view_id") if isinstance(current_view, dict) else None
        )
        contract = admission.get("verification_contract")
        if not isinstance(contract, dict):
            return _fail(
                "build_platform_admission -> verification_contract",
                "current admission omitted the target verification contract",
            )
        delivery = contract.get("delivery")
        checks = contract.get("checks")
        if (
            not isinstance(delivery, dict)
            or delivery.get("mode") != policy.get("delivery_mode")
            or not isinstance(checks, list)
        ):
            return _fail(
                "verification_contract -> delivery/checks",
                "admitted target delivery or verifier checks differ from policy",
            )
        required_checks = [
            check for check in checks if isinstance(check, dict) and check.get("required", True)
        ]
        required_receipt_kinds = set(policy.get("required_receipt_kinds") or [])
        actual_receipt_kinds = {check.get("receipt_kind") for check in required_checks}
        if not required_receipt_kinds <= actual_receipt_kinds:
            return _fail(
                "verification_contract -> receipt_kind",
                "admission omitted a required target verifier receipt kind",
            )
        expected_execution_modalities = policy.get("required_execution_modalities")
        actual_execution_modalities = {
            check.get("required_execution_modality") for check in required_checks
        }
        if isinstance(expected_execution_modalities, list):
            modality_mismatch = not all(
                isinstance(modality, str) and modality for modality in expected_execution_modalities
            ) or actual_execution_modalities != set(expected_execution_modalities)
        else:
            expected_execution_modality = policy.get("required_execution_modality")
            modality_mismatch = any(
                check.get("required_execution_modality") != expected_execution_modality
                for check in required_checks
            )
        if modality_mismatch:
            return _fail(
                "verification_contract -> execution_modality",
                "admission omitted or changed the required target execution modality",
            )
        contract_claims = [
            claim
            for check in required_checks
            for claim in check.get("claims") or []
            if isinstance(claim, dict) and claim.get("required", True)
        ]
        required_kind_counts = Counter(policy.get("required_claim_kinds") or {})
        actual_kind_counts = Counter(claim.get("kind") for claim in contract_claims)
        if any(actual_kind_counts[kind] < count for kind, count in required_kind_counts.items()):
            return _fail(
                "verification_contract -> required_claims",
                "admission weakened the scenario's mandatory claim-kind floor",
            )
        flattened = {
            _claim_contract(claim)
            for claim in admission.get("verification_claims") or []
            if isinstance(claim, dict) and claim.get("required", True)
        }
        if flattened != {_claim_contract(claim) for claim in contract_claims}:
            return _fail(
                "verification_contract -> flattened_claims",
                "admission flattened claims differ from its durable check contract",
            )
        contract_digest = "sha256:" + _canonical_digest(contract)
        deliverable = next(
            (
                event
                for event in reversed(segment)
                if event.get("kind") == "deliverable"
                and _seq(event) > _seq(admission)
                and _event_matches_run_intent(segment, event, run_intent)
            ),
            None,
        )
        if (
            deliverable is None
            or deliverable.get("target_id") != contract.get("target_id")
            or deliverable.get("delivery_contract") != delivery
            or deliverable.get("verification_contract_digest") != contract_digest
        ):
            return _fail(
                "verification_contract -> deliverable",
                "current handoff is absent or not bound to the admitted target delivery",
            )

        # Verification directives are durable user authority. A continue/restart
        # creates a new terminal segment, but it does not erase an unsuperseded
        # directive from an earlier segment.
        external_required = set(_active_external_claims(events, through_seq=final_seq))
        covered_external: set[tuple[object, ...]] = set()
        execution_authorities: set[tuple[Any, Any, Any]] = set()
        for check in required_checks:
            verdict = next(
                (
                    event
                    for event in reversed(segment)
                    if event.get("kind") == "verifier_verdict"
                    and _seq(event) > _seq(deliverable)
                    and event.get("check_id") == check.get("check_id")
                    and event.get("receipt_kind") == check.get("receipt_kind")
                    and event.get("verification_contract_digest") == contract_digest
                ),
                None,
            )
            started = next(
                (
                    event
                    for event in segment
                    if isinstance(verdict, dict)
                    and event.get("kind") == "verifier_started"
                    and event.get("id") == verdict.get("requested_by_event_id")
                    and _seq(deliverable) < _seq(event) < _seq(verdict)
                ),
                None,
            )
            receipt = verdict.get("verification_result") if isinstance(verdict, dict) else None
            claim_results = receipt.get("claim_results") if isinstance(receipt, dict) else None
            expected_claims = {
                _claim_contract(claim)
                for claim in check.get("claims") or []
                if isinstance(claim, dict)
            }
            actual_claims = {
                _claim_contract(claim) for claim in claim_results or [] if isinstance(claim, dict)
            }
            covered_external.update(actual_claims & external_required)
            delegated = set(check.get("delegated_issuer_ids") or [])
            allowed_claim_issuers = {check.get("issuer_id"), *delegated}
            required_artifact_identity_scheme = check.get("required_artifact_identity_scheme")
            requires_artifact_identity = bool(
                isinstance(required_artifact_identity_scheme, str)
                and required_artifact_identity_scheme
            )
            if (
                not isinstance(verdict, dict)
                or verdict.get("source") != "system"
                or verdict.get("verified") is not True
                or verdict.get("verdict") != "pass"
                or not isinstance(receipt, dict)
                or receipt.get("status") != "pass"
                or bool(conversation_id)
                and receipt.get("conversation_id") != conversation_id
                or receipt.get("run_intent_id") != admission.get("run_intent_id")
                or receipt.get("run_identity") != admission.get("run_identity")
                or receipt.get("target_id") != contract.get("target_id")
                or receipt.get("delivery_shape") != delivery.get("shape")
                or receipt.get("delivery_entry_reference") != delivery.get("entry_reference")
                or receipt.get("verification_contract_digest") != contract_digest
                or receipt.get("check_id") != check.get("check_id")
                or receipt.get("receipt_kind") != check.get("receipt_kind")
                or receipt.get("issuer_id") != check.get("issuer_id")
                or receipt.get("operation") != check.get("operation")
                or set(receipt.get("delegated_issuer_ids") or []) != delegated
                or receipt.get("verifier_id") != check.get("issuer_id")
                or receipt.get("tool_id") != check.get("operation")
                or receipt.get("deliverable_event_id") != deliverable.get("id")
                or receipt.get("artifact_path") != deliverable.get("path")
                or receipt.get("artifact_kind") != deliverable.get("artifact_kind")
                or (
                    requires_artifact_identity
                    and not _artifact_identity_is_exact(
                        receipt.get("artifact_identity"),
                        scheme=required_artifact_identity_scheme,
                        entry_reference=deliverable.get("path"),
                        producer_id=check.get("issuer_id"),
                    )
                )
                or (current_view_id is not None and receipt.get("agent_view_id") != current_view_id)
                or expected_claims - actual_claims
                or any(
                    claim.get("required", True) and claim.get("status") != "pass"
                    for claim in claim_results or []
                    if isinstance(claim, dict)
                )
                or any(
                    claim.get("verifier_id") not in allowed_claim_issuers
                    for claim in claim_results or []
                    if isinstance(claim, dict) and claim.get("status") == "pass"
                )
                or not _effect_receipt_is_exact(receipt)
            ):
                return _fail(
                    "verifier_check -> current_typed_pass",
                    "a required admitted check lacks an exact current PASS receipt",
                    check_id=check.get("check_id"),
                )
            if (
                not isinstance(started, dict)
                or started.get("source") != "system"
                or started.get("target_id") != contract.get("target_id")
                or started.get("run_intent_id") != admission.get("run_intent_id")
                or started.get("run_identity") != admission.get("run_identity")
                or started.get("delivery_shape") != delivery.get("shape")
                or started.get("delivery_entry_reference") != delivery.get("entry_reference")
                or started.get("check_id") != check.get("check_id")
                or started.get("receipt_kind") != check.get("receipt_kind")
                or started.get("issuer_id") != check.get("issuer_id")
                or started.get("operation") != check.get("operation")
                or set(started.get("delegated_issuer_ids") or []) != delegated
                or started.get("verification_contract_digest") != contract_digest
                or started.get("deliverable_event_id") != deliverable.get("id")
                or started.get("artifact_path") != deliverable.get("path")
                or started.get("artifact_kind") != deliverable.get("artifact_kind")
                or (current_view_id is not None and started.get("agent_view_id") != current_view_id)
                or receipt.get("execution_identity") != started.get("execution_identity")
                or receipt.get("preview_selection") != started.get("preview_selection")
                or receipt.get("workspace_revision") != started.get("workspace_revision")
                or receipt.get("workspace_generation") != started.get("workspace_generation")
                or receipt.get("workspace_epoch") != started.get("workspace_epoch")
                or (
                    not requires_artifact_identity
                    and receipt.get("observed_after_seq") != started.get("observed_after_seq")
                )
            ):
                return _fail(
                    "verifier_started -> verifier_receipt",
                    "typed result differs from its pre-verification host authority",
                    check_id=check.get("check_id"),
                )
            started_seq = _seq(started)
            expected_workspace_revision = _last_successful_productive_action_seq(
                segment,
                through_seq=started_seq,
            )
            expected_authority_seq = _last_authority_seq(
                segment,
                through_seq=started_seq,
            )
            output_event = next(
                (event for event in segment if _seq(event) == receipt.get("observed_after_seq")),
                None,
            )
            output_identity_invalid = requires_artifact_identity and (
                started.get("observed_after_seq") != expected_authority_seq
                or not isinstance(receipt.get("observed_after_seq"), int)
                or receipt["observed_after_seq"] <= started_seq
                or receipt["observed_after_seq"] >= _seq(verdict)
                or not isinstance(output_event, dict)
                or output_event.get("kind") != "observation"
                or not isinstance(output_event.get("tool_result"), dict)
                or output_event["tool_result"].get("success") is not True
                or not any(
                    f"event:{output_event.get('id')}" in (claim.get("evidence_refs") or [])
                    for claim in claim_results or []
                    if isinstance(claim, dict)
                )
                or _authority_change_between(
                    segment,
                    int(started.get("observed_after_seq") or 0),
                    receipt["observed_after_seq"],
                )
            )
            ordinary_authority_invalid = not requires_artifact_identity and (
                not isinstance(receipt.get("observed_after_seq"), int)
                or receipt["observed_after_seq"] != expected_authority_seq
                or receipt["observed_after_seq"] >= started_seq
            )
            if (
                type(receipt.get("workspace_revision")) is not int
                or receipt.get("workspace_revision") != expected_workspace_revision
                or output_identity_invalid
                or ordinary_authority_invalid
                or (
                    policy.get("require_workspace_epoch", False)
                    and (
                        type(receipt.get("workspace_epoch")) is not int
                        or receipt["workspace_epoch"] < 1
                    )
                )
                or _authority_change_between(
                    segment,
                    receipt["observed_after_seq"],
                    _seq(verdict),
                )
            ):
                return _fail(
                    "workspace_authority -> verifier_receipt",
                    "receipt is stale or lacks exact workspace authority",
                    check_id=check.get("check_id"),
                )
            if _authority_change_between(segment, _seq(verdict), final_seq):
                return _fail(
                    "verifier_receipt -> terminal",
                    "target authority changed after the selected PASS receipt",
                    check_id=check.get("check_id"),
                )
            expected_modality = check.get("required_execution_modality")
            execution = receipt.get("execution_identity")
            if expected_modality and (
                not isinstance(execution, dict)
                or execution.get("modality") != expected_modality
                or not execution.get("instance_id")
                or not execution.get("generation")
            ):
                return _fail(
                    "target_execution -> verifier_receipt",
                    "receipt lacks the required target execution binding",
                    check_id=check.get("check_id"),
                )
            execution_authorities.add(_shared_execution_authority(execution))
            if expected_modality == "managed_preview":
                selection = receipt.get("preview_selection")
                expected_url = (
                    _preview_verification_url(selection, str(deliverable.get("path") or ""))
                    if isinstance(selection, dict)
                    else None
                )
                if (
                    not isinstance(selection, dict)
                    or not _preview_selection_is_current(
                        selection,
                        events,
                        deliverable_seq=_seq(deliverable),
                        verdict_seq=_seq(verdict),
                    )
                    or not isinstance(execution, dict)
                    or execution.get("instance_id") != selection.get("projection_id")
                    or execution.get("generation")
                    != (
                        f"{selection.get('sandbox_instance_id')}:"
                        f"{selection.get('sandbox_generation')}"
                    )
                    or execution.get("locator") != selection.get("url")
                    or receipt.get("observed_url") != expected_url
                    or not receipt.get("workspace_generation")
                ):
                    return _fail(
                        "preview_start -> verifier_receipt",
                        "managed Preview/result execution identity is absent, foreign, or stale",
                        check_id=check.get("check_id"),
                    )

        if len(execution_authorities) != 1:
            return _fail(
                "verifier_checks -> target_execution",
                "required checks did not certify one shared execution generation",
            )
        if not external_required <= covered_external:
            return _fail(
                "external_requirements -> verifier_receipts",
                "a current mandatory external verification claim was dropped",
            )
        return [
            passing(
                _ORACLE,
                facts={
                    "terminal_seq": final_seq,
                    "prior_terminal_seq": prior_terminal_seq,
                    "target_id": contract.get("target_id"),
                    "required_checks": len(required_checks),
                    "contract_digest": contract_digest,
                },
            )
        ]
