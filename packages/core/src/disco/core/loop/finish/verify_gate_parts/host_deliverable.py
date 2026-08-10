"""Host-verification deliverable reconstruction and evidence reading.

Owns: reconstructing the web-like `HostVerificationDeliverable` for host
shadow/authoritative verify (REL-1c), the REL-2a artifact-manifest reader,
workspace evidence reads (HTML medium detection, manifest presence, related
game texts), verdict-shape builders, and the verifier context seed used by
both the raw host verifier and the semantic judge.
"""

from __future__ import annotations

import posixpath
from typing import Any

from ....verification import unavailable_verification_result
from ....verify_medium import (
    VerifierMediumHint,
    detect_html_medium,
    html_manifest_hrefs,
    html_script_srcs,
)
from ..common import (
    _LOG,
    AgentStep,
    Event,
    FileExistsPredicate,
    HostVerificationClaim,
    HostVerificationDeliverable,
    VerificationClaimKind,
    VerifierContextSeed,
    _artifact_record_kind,
    _bounded_verifier_check_results,
    _deliverable_event_paths,
    _is_web_deliverable,
    _latest_app_deliverable_event,
    _latest_deliverable_event,
    _plan_file_exists_paths,
    _safe_deliverable_file_path,
    _screenshot_from_verdict,
    artifact_manifest_reader_enabled,
    artifact_paths_from_events,
    artifact_paths_from_manifest_records,
    manifest_path_divergence,
)
from .host_authority import bind_host_verification_authority
from .host_claims import governed_verification_required, user_verification_material

# Compatibility re-exports only. Medium detection no longer synthesizes this
# requirement; an admitted target may still supply the exact claim explicitly.
_GAME_INTERACTION_CLAIM_ID = "web.interaction:canvas-keyboard-smoke"
_GAME_INTERACTION_EXPECTED = (
    "host browser completed canvas click, Space, ArrowRight, and post-interaction capture"
)


async def host_artifact_file_exists(gate: Any, path: str) -> bool | None:
    sbx = getattr(gate._loop.executor, "sandbox", None)
    file_exists: Any = getattr(sbx, "file_exists", None)
    if file_exists is None:
        return None
    try:
        return bool(await file_exists(path))
    except Exception:  # noqa: BLE001 — path resolution is advisory; skip only on known absence
        return None


async def artifact_manifest_records(
    gate: Any, events: list[Event], *, consumer: str
) -> tuple[Any, ...] | None:
    """Read REL-2a artifact manifest records for promoted readers.

    Empty manifests are NOT agreement: the REL-1e flip exposed how easy it is
    to bank a shadow claim when no shadow data was actually recorded. This
    helper only returns records when the manifest has at least one path, and
    always logs the event-projection comparison while the reader soaks.
    """

    if not artifact_manifest_reader_enabled():
        return None
    sbx = getattr(gate._loop.executor, "sandbox", None)
    if sbx is None:
        _LOG.info(
            "artifact-manifest reader skipped for %s:%s: sandbox unavailable",
            gate._loop.conversation_id,
            consumer,
        )
        return None
    try:
        from ....context import ArtifactMemoryStore

        records = tuple(await ArtifactMemoryStore(sbx).read_artifacts())
    except Exception:  # noqa: BLE001 — manifest reader must fail open to legacy readers
        _LOG.warning(
            "artifact-manifest reader failed for %s:%s",
            gate._loop.conversation_id,
            consumer,
            exc_info=True,
        )
        return None

    manifest_paths = artifact_paths_from_manifest_records(records)
    projected = artifact_paths_from_events(events)
    if not manifest_paths:
        _LOG.info(
            "artifact-manifest reader skipped for %s:%s: no manifest data (projected=%d)",
            gate._loop.conversation_id,
            consumer,
            len(projected),
        )
        return None

    missing, extra = manifest_path_divergence(projected, manifest_paths)
    _LOG.info(
        "artifact-manifest reader compare for %s:%s: projected=%d manifest=%d "
        "missing=%s extra=%s",
        gate._loop.conversation_id,
        consumer,
        len(projected),
        len(manifest_paths),
        sorted(missing),
        sorted(extra),
    )
    return records


async def host_verify_artifact_path(gate: Any, raw_path: str) -> str | None:
    path = _safe_deliverable_file_path(raw_path)
    legacy_index = _safe_deliverable_file_path(raw_path, app_root=True)
    if path is None:
        path = legacy_index
    if path is None:
        return None
    exists = await host_artifact_file_exists(gate, path)
    if exists is not False:
        return path
    if legacy_index is not None and legacy_index != path:
        if await host_artifact_file_exists(gate, legacy_index) is not False:
            return legacy_index
    return None


async def _app_record_deliverable(
    gate: Any, step: AgentStep, records: tuple[Any, ...]
) -> HostVerificationDeliverable | None:
    app_records = [r for r in records if _artifact_record_kind(r) == "app"]
    for record in reversed(app_records):
        raw_path = getattr(record, "path", None)
        if not isinstance(raw_path, str):
            continue
        path = await host_verify_artifact_path(gate, raw_path)
        if path is None:
            continue
        return HostVerificationDeliverable(
            conversation_id=gate._loop.conversation_id,
            artifact_path=path,
            artifact_kind="app",
            deployment_url="",
            requested_verification=step.requested_verification,
        )
    return None


def _file_record_deliverable(
    gate: Any, step: AgentStep, records: tuple[Any, ...]
) -> HostVerificationDeliverable | None:
    file_records = [r for r in records if _artifact_record_kind(r) != "app"]
    shown_records = [r for r in file_records if bool(getattr(r, "shown", False))]
    for record in reversed(shown_records or file_records):
        raw_path = getattr(record, "path", None)
        if not isinstance(raw_path, str):
            continue
        path = _safe_deliverable_file_path(raw_path)
        if path is None:
            continue
        return HostVerificationDeliverable(
            conversation_id=gate._loop.conversation_id,
            artifact_path=path,
            artifact_kind=_artifact_record_kind(record),
            deployment_url="",
            requested_verification=step.requested_verification,
        )
    return None


async def host_verify_manifest_deliverable(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    include_unverifiable: bool,
) -> HostVerificationDeliverable | None:
    records = await gate._artifact_manifest_records(events, consumer="host_verify")
    if records is None:
        return None
    app_deliverable = await _app_record_deliverable(gate, step, records)
    if app_deliverable is not None:
        return app_deliverable
    if not include_unverifiable:
        return None
    return _file_record_deliverable(gate, step, records)


async def _manifest_deliverable_if_unhandled(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    governed_target: bool,
    current_handoff: Any,
    include_unverifiable: bool,
) -> HostVerificationDeliverable | None:
    if governed_target and current_handoff is not None:
        return None
    return await gate._host_verify_manifest_deliverable(
        step,
        events,
        include_unverifiable=include_unverifiable and not governed_target,
    )


def _selected_any_event(
    *,
    governed_target: bool,
    current_handoff: Any,
    include_unverifiable: bool,
    events: list[Event],
    app_event: Any,
) -> Any:
    if governed_target:
        return current_handoff
    if include_unverifiable:
        return _latest_deliverable_event(events)
    return app_event


async def _resolved_artifact_path(gate: Any, any_event: Any) -> str | None:
    if any_event is not None and any_event.artifact_kind != "app":
        return _safe_deliverable_file_path(any_event.path)
    return await host_verify_artifact_path(
        gate, any_event.path if any_event is not None else "index.html"
    )


async def host_verify_deliverable(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    include_unverifiable: bool = False,
) -> HostVerificationDeliverable | None:
    """REL-1c — reconstruct the web-like deliverable for host shadow verify.

    The host verifier is advisory in this PR, so missing/ambiguous delivery
    evidence simply skips the shadow path. A first-class app handoff wins;
    otherwise the existing web-finish convention (index.html/server_status) is
    treated as index.html. App-root handoffs such as "." or "dist" are
    resolved to their primary index.html-style file, never verified as the
    directory path itself.
    """

    governed_target = governed_verification_required(events)
    current_handoff = _latest_deliverable_event(events)
    manifest_deliverable = await _manifest_deliverable_if_unhandled(
        gate,
        step,
        events,
        governed_target=governed_target,
        current_handoff=current_handoff,
        include_unverifiable=include_unverifiable,
    )
    if manifest_deliverable is not None:
        bound = bind_host_verification_authority(gate, manifest_deliverable, events)
        return await gate._with_host_verification_profile(bound, events)

    app_event = _latest_app_deliverable_event(events)
    if governed_target and current_handoff is None:
        return None
    any_event = _selected_any_event(
        governed_target=governed_target,
        current_handoff=current_handoff,
        include_unverifiable=include_unverifiable,
        events=events,
        app_event=app_event,
    )
    if any_event is None and not _is_web_deliverable(events):
        return None
    path = await _resolved_artifact_path(gate, any_event)
    if path is None:
        return None
    deployment_url = any_event.deployment_url if any_event is not None else ""
    artifact_kind = any_event.artifact_kind if any_event is not None else "app"
    deliverable = HostVerificationDeliverable(
        conversation_id=gate._loop.conversation_id,
        artifact_path=path,
        artifact_kind=artifact_kind,
        deployment_url=deployment_url or "",
        requested_verification=step.requested_verification,
    )
    bound = bind_host_verification_authority(gate, deliverable, events)
    return await gate._with_host_verification_profile(bound, events)


async def with_host_verification_profile(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
) -> HostVerificationDeliverable:
    """Select the observed medium without inventing behavioral requirements.

    Interaction claims come only from an admitted target/check contract. A canvas
    heuristic can select the browser probe implementation, but it cannot infer that
    every game must accept a canvas click followed by Space and ArrowRight.
    """

    check = deliverable.verification_check
    contract = deliverable.verification_contract
    governed_web = bool(check is not None and check.receipt_kind == "disco.web_functional@1")
    if contract is not None and not governed_web:
        return deliverable.model_copy(update={"verification_medium": contract.preview_modality})
    # Medium selection belongs to the selected artifact only. Historical
    # handoffs/plan paths are useful bounded context for a semantic judge,
    # but reading them here can classify a stale app (or traverse an alias)
    # as the current target.
    paths = [deliverable.artifact_path]
    hint = await gate._verifier_medium_hint(paths)
    medium = hint.kind if hint is not None else "web"
    return deliverable.model_copy(update={"verification_medium": medium})


def verdict_label(verdict: dict | None) -> str | None:
    if not verdict:
        return None
    label = verdict.get("verdict")
    if label is not None:
        return str(label)
    if "passed" in verdict:
        return "pass" if verdict.get("passed") is True else "fail"
    return None


def verdict_failures(verdict: dict) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    for item in verdict.get("failures") or []:
        if isinstance(item, dict):
            failures.append(dict(item))
    for err in verdict.get("console_errors") or []:
        if isinstance(err, dict):
            failures.append({"kind": "console_error", **err})
    for err in verdict.get("network_failures") or []:
        if isinstance(err, dict):
            failures.append({"kind": "network_failure", **err})
    if verdict.get("passed") is not True and not failures:
        summary = str(verdict.get("summary") or verdict.get("detail") or "").strip()
        if summary:
            failures.append({"kind": "summary", "message": summary})
    return failures


def host_unavailable_verdict(
    deliverable: HostVerificationDeliverable, detail: str
) -> dict[str, object]:
    verdict: dict[str, object] = {
        "passed": False,
        "verdict": "unavailable",
        "url": "",
        "http_status": 0,
        "summary": detail,
        "next_action": "",
        "console_errors": [],
        "network_failures": [],
        "failure_fingerprint": "host_verifier_unavailable",
    }
    if (
        deliverable.verification_contract is not None
        and deliverable.verification_check is not None
        and deliverable.required_claims
    ):
        verdict["verification_result"] = unavailable_verification_result(
            deliverable=deliverable,
            reason=detail,
        ).model_dump(mode="json")
    return verdict


def host_unverifiable_verdict(
    deliverable: HostVerificationDeliverable,
) -> dict[str, object]:
    return {
        "passed": False,
        "verdict": "unverifiable",
        "url": deliverable.deployment_url,
        "http_status": 0,
        "summary": (
            f"No host validator is available for artifact kind "
            f"{deliverable.artifact_kind!r}; verification was not claimed."
        ),
        "next_action": "",
        "console_errors": [],
        "network_failures": [],
        "failure_fingerprint": f"host_verifier_unverifiable:{deliverable.artifact_kind}",
    }


def verdict_first_failure(verdict: dict) -> str:
    errs = verdict.get("console_errors") or []
    nets = verdict.get("network_failures") or []
    if errs:
        e0 = errs[0]
        if isinstance(e0, dict):
            where = f" @ {e0.get('source')}" if e0.get("source") else ""
            return f"{e0.get('text', '')}{where}".strip()
    if nets:
        n0 = nets[0]
        if isinstance(n0, dict):
            marker = n0.get("status") or n0.get("failure") or "failed"
            return f"{n0.get('method', 'GET')} {n0.get('url', '')} -> {marker}".strip()
    failures = verdict_failures(verdict)
    if failures:
        message = failures[0].get("message")
        if message:
            return str(message)
    return ""


def verifier_contract_payload(gate: Any) -> dict[str, Any]:
    alias = getattr(gate._loop, "_finish_alias", None)
    if not alias:
        return {}
    try:
        from ....contract.registry import BuildContractRegistry

        reg = BuildContractRegistry.default()
        for kind in reg.kinds():
            c = reg.get(kind)
            if c is not None and c.verify.finalizer == alias:
                return c.model_dump(mode="json")
    except Exception:  # noqa: BLE001 — verifier seed degrades to finalizer-only
        pass
    return {"verify": {"finalizer": alias}}


async def verifier_deliverable_paths(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
) -> list[str]:
    paths: list[str] = [deliverable.artifact_path]
    paths.extend(_deliverable_event_paths(events))
    paths.extend(_plan_file_exists_paths(events))
    paths.extend(gate._contract_required_deliverable_paths())
    spec = await gate._loop.store.get_external_dod_spec(gate._loop.conversation_id)
    if spec is not None:
        for pred in spec.predicates:
            if isinstance(pred, FileExistsPredicate):
                p = _safe_deliverable_file_path(pred.path)
                if p is not None:
                    paths.append(p)

    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        safe = _safe_deliverable_file_path(str(p))
        if safe is None or safe in seen:
            continue
        seen.add(safe)
        out.append(safe)
    return out


async def read_workspace_bytes(gate: Any, path: str) -> bytes | None:
    sbx = getattr(getattr(gate._loop, "executor", None), "sandbox", None)
    if sbx is None:
        return None
    try:
        data = await sbx.read_file(path)
    except Exception:
        return None
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return None


async def manifest_present_for_html(
    gate: Any, html_path: str, html_text: str, deliverable_paths: list[str]
) -> bool:
    base_dir = posixpath.dirname(html_path)
    candidates: list[str] = []
    for href in html_manifest_hrefs(html_text):
        clean = href.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean or "://" in clean or clean.startswith("//"):
            continue
        safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, clean)))
        if safe is not None:
            candidates.append(safe)
    if not candidates:
        safe_default = _safe_deliverable_file_path(
            posixpath.normpath(posixpath.join(base_dir, "manifest.json"))
        )
        if safe_default is not None:
            candidates.append(safe_default)
    candidates.extend(
        p for p in deliverable_paths if posixpath.basename(p).lower() == "manifest.json"
    )
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if await read_workspace_bytes(gate, path) is not None:
            return True
    return False


async def related_game_texts_for_html(gate: Any, html_path: str, html_text: str) -> list[str]:
    base_dir = posixpath.dirname(html_path)
    candidates: list[str] = []
    for src in html_script_srcs(html_text):
        clean = src.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean or "://" in clean or clean.startswith("//"):
            continue
        safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, clean)))
        if safe is not None:
            candidates.append(safe)
    for name in ("README.md", "NOTES.md"):
        safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, name)))
        if safe is not None:
            candidates.append(safe)

    texts: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        raw = await read_workspace_bytes(gate, path)
        if raw is not None:
            texts.append(raw.decode("utf-8", errors="replace"))
    return texts


async def verifier_medium_hint(
    gate: Any,
    deliverable_paths: list[str],
) -> VerifierMediumHint | None:
    for path in deliverable_paths:
        if not path.lower().endswith((".html", ".htm")):
            continue
        raw = await read_workspace_bytes(gate, path)
        if raw is None:
            continue
        text = raw.decode("utf-8", errors="replace")
        manifest_present = await manifest_present_for_html(gate, path, text, deliverable_paths)
        hint = detect_html_medium(
            text,
            manifest_present=manifest_present,
            related_texts=await related_game_texts_for_html(gate, path, text),
        )
        if hint is not None:
            return hint
    return None


async def verifier_context_seed(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    check_verdict: dict[str, Any],
    *,
    contract: dict[str, Any],
    claims: tuple[HostVerificationClaim, ...] | None = None,
) -> VerifierContextSeed:
    deliverable_paths = await gate._verifier_deliverable_paths(deliverable, events)
    selected_claims = claims if claims is not None else deliverable.required_claims
    screenshot = _screenshot_from_verdict(check_verdict)
    if not any(claim.kind is VerificationClaimKind.VISUAL_SEMANTIC for claim in selected_claims):
        # A screenshot file is still useful provenance, but attaching its
        # pixels would manufacture a VISION requirement for non-visual work.
        screenshot = screenshot.model_copy(update={"image_data_url": ""})
    _, all_references = user_verification_material(events)
    reference_images = tuple(
        reference
        for reference in all_references
        if any(
            claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
            and claim.reference_image_sha256 == reference.sha256
            for claim in selected_claims
        )
    )
    return VerifierContextSeed(
        contract=contract,
        deliverable_paths=deliverable_paths,
        check_results=_bounded_verifier_check_results(check_verdict),
        screenshot=screenshot,
        reference_images=reference_images,
        medium=await gate._verifier_medium_hint(deliverable_paths),
        claims=selected_claims,
    )
