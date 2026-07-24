"""The deterministic classifier (guidelines §13, §16).

Runs the oracle layers in the §16 order; the FIRST broken link wins. The classifier
is a pure function of durable evidence — it MUST NOT call an LLM, and it ignores any
agent/Claude/Codex prose ("agent_comments_ignored_for_adjudication": true, §13).

Outcome mapping:
  * a HarnessValidity / Contract failure (an evidence/contract defect, not a product
    defect) -> INVALID_RUN (§8): the harness couldn't adjudicate; never a pass.
  * any product-oracle failure -> FAIL with the code's severity (§12).
  * no failure -> PASS.

It also implements the §17 no-fluke replay policy as a pure function: a run that
failed once then passed on exact replay is recorded as FAIL /
INTERMITTENT_<original_code> (same severity), never PASS — "fluke" has no
operational meaning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import failure_codes as fc
from .events import NormalizationError, normalize_events
from .evidence import EvidenceManifest, load_manifest, sha256_file, verify_evidence_unchanged
from .oracles import (
    BROWSER_EVIDENCE_ORACLES,
    TARGETED_EDIT_ORACLES,
    ContextPressureOracle,
    ContractOracle,
    EventChainOracle,
    GovernedAdmissionOracle,
    HarnessValidityOracle,
    OutputTruthOracle,
    ProviderLedgerOracle,
    RevisionOracle,
    ScenarioBrowserVerificationOracle,
    ScenarioLifecycleOracle,
    ThrashOracle,
    ToolScopeOracle,
)
from .oracles.schema import OracleResult
from .product_evidence import PROVIDER_LEDGER_NAME

CLASSIFICATION_NAME = "classification.json"

# Default "likely files" for the most common Build chain breaks (§16 example).
# Advisory only — surfaced to the repair loop, never adjudicated on.
_LIKELY_FILES: dict[str, list[str]] = {
    fc.NO_REPLAN_AFTER_REVISION: [
        "packages/core/src/disco/core/loop/plans.py",
        "packages/core/src/disco/core/loop/engine.py",
        "packages/agent-server/src/disco/agent_server/control_ops.py",
    ],
    fc.WRITE_TOOL_ATTEMPTED_IN_PLANNING: [
        "packages/core/src/disco/core/loop/engine.py",  # _gate_planning_mode
        "packages/core/src/disco/core/loop/driver.py",
    ],
    fc.WRITE_TOOL_ALLOWED_IN_PLANNING: [
        "packages/core/src/disco/core/loop/engine.py",
        "packages/core/src/disco/core/loop/driver.py",
    ],
    fc.APPROVE_PLAN_NO_EXECUTION: [
        "packages/core/src/disco/core/loop/finish.py",
        "packages/core/src/disco/core/loop/engine.py",
    ],
    fc.ACTION_NO_OBSERVATION: [
        "packages/core/src/disco/core/loop/observe.py",
    ],
}


def _tool_scope_from_inspect(inspect_trace: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Extract tool-scope proof from the frozen inspect trace, fail-closed.

    ``ConversationTrace.snapshot`` exposes both a convenience projection and an
    interleaved event stream.  Requiring them to agree prevents a partial or
    hand-edited projection from becoming admissible evidence.  A malformed
    capture is returned as a sentinel entry for ``ToolScopeOracle`` to classify
    as INVALID; total absence remains ``None`` so ``ContractOracle`` reports the
    selected assertion's missing evidence.
    """
    if inspect_trace is None or "tool_scopes" not in inspect_trace:
        return None
    scopes = inspect_trace.get("tool_scopes")
    events = inspect_trace.get("events")
    if not isinstance(scopes, list) or not isinstance(events, list):
        return [{"__malformed__": "tool_scopes/events must be lists"}]
    event_count = inspect_trace.get("event_count")
    if not isinstance(event_count, int) or event_count != len(events):
        return [{"__malformed__": "event_count does not match events"}]
    dropped_event_count = inspect_trace.get("dropped_event_count")
    if not isinstance(dropped_event_count, int) or dropped_event_count != 0:
        return [{"__malformed__": "inspect trace is truncated"}]

    projected: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            return [{"__malformed__": "inspect event is not an object"}]
        if event.get("kind") != "tool_scope":
            continue
        item = {k: v for k, v in event.items() if k not in {"seq", "kind"}}
        projected.append(item)
    if scopes != projected:
        return [{"__malformed__": "tool_scopes projection disagrees with events"}]
    return [
        dict(scope) if isinstance(scope, dict) else {"__malformed__": "scope is not an object"}
        for scope in scopes
    ]


def classify(
    events_raw: list[Any],
    *,
    scenario: dict[str, Any] | None = None,
    run_id: str = "",
    conversation_id: str = "",
    commit: str = "",
    seed: int | None = None,
    evidence_intact: bool = True,
    available_evidence: set[str] | None = None,
    workspace_manifest: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
    tool_scope: list[dict[str, Any]] | None = None,
    autonomous: bool | None = None,
    revision_meta: dict[str, Any] | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
    inspect_trace: dict[str, Any] | None = None,
    product_evidence: dict[str, Any] | None = None,
    browser_evidence_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Classify one run from its raw event log (full-event dicts OR DB rows) plus
    optional captured evidence. Returns the §13 classification dict.

    `autonomous`, when given (e.g. from the run manifest), overrides the scenario's
    autonomy: an autonomous build legitimately skips the AWAITING_PLAN_APPROVAL gate
    (it auto-approves inline), so the event-chain approval-ordering check relaxes the
    awaiting link for it."""
    # An explicit autonomous flag (manifest) overrides the scenario declaration.
    if autonomous is not None:
        scenario = {**(scenario or {}), "autonomous": autonomous}

    tool_scope_asserted = bool(((scenario or {}).get("assertions") or {}).get("tool_scope"))
    if tool_scope is None and tool_scope_asserted:
        tool_scope = _tool_scope_from_inspect(inspect_trace)

    # Normalize the event log; a parse failure is a harness-validity defect.
    parse_ok = True
    try:
        events = normalize_events(events_raw)
    except (NormalizationError, ValueError, TypeError):
        events, parse_ok = [], False

    # The evidence categories actually captured for this run — derived from the
    # supplied kwargs so the ContractOracle judges what we really have (a caller
    # may also pass an explicit set, which we union in).
    present: set[str] = set(available_evidence or set())
    present.discard("browser_verification")  # H191 proof is oracle-derived below
    present.add("events")
    if workspace_manifest is not None:
        present.add("workspace")
    # Current preview evidence is admissible only when it came through the
    # product's capability-isolated boundary. Historical/unrecorded or harness-
    # local fallback bytes cannot satisfy a preview-required contract.
    if preview is not None and preview.get("source") == "isolated_path_capability":
        present.add("preview")
    if tool_scope is not None:
        present.add("tool_scope")
    if product_evidence is not None:
        present.add("product_evidence")
    verification_paths = _successful_browser_verification_paths(events)
    captured_browser_paths = set(browser_evidence_paths or set())

    results: list[OracleResult] = []
    verified_claim_results: list[dict[str, Any]] | None = None
    verified_observed_facts: list[dict[str, Any]] | None = None
    verified_artifact_paths: list[str] | None = None

    # 1. harness validity (evidence integrity + parse + presence).
    results += HarnessValidityOracle().check(
        events, evidence_intact=evidence_intact, parse_ok=parse_ok
    )
    first_fail = _first_fail(results)
    if first_fail is None:
        # 2. scenario contract.
        results += ContractOracle().check(scenario, available_evidence=present)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 3. event chain.
        results += EventChainOracle().check(events, scenario=scenario)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 3a. model/tool thrash. Cost valves may eventually stop or recover a
        # repeated bad call; the soak still treats that interaction as broken.
        # Ordered BEFORE the terminal-shape oracles (governed admission, tool
        # scope): the live monitor kills a thrashing run at the moment of the
        # repeated error, so "no successful work terminal exists" is downstream
        # of the thrash — reporting the terminal shape first would obscure the
        # earliest broken link.
        results += ThrashOracle().check(events, scenario=scenario, inspect_trace=inspect_trace)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 3b. governed admission — Phase-4 governed Freeform scenarios must have
        # platform admission with claims and a typed PASS receipt. Inline browser
        # evidence cannot substitute for governed completion authority.
        governed_results = GovernedAdmissionOracle().check(
            events,
            scenario=scenario,
            conversation_id=conversation_id,
        )
        results += governed_results
        for result in governed_results:
            claims = result.facts.get("verified_claim_results")
            if result.passed and isinstance(claims, list):
                verified_claim_results = [
                    dict(claim) for claim in claims if isinstance(claim, dict)
                ]
                facts = result.facts.get("verified_observed_facts")
                verified_observed_facts = (
                    [dict(fact) for fact in facts if isinstance(fact, dict)]
                    if isinstance(facts, list)
                    else []
                )
                artifact_paths = result.facts.get("verified_artifact_paths")
                verified_artifact_paths = (
                    [path for path in artifact_paths if isinstance(path, str)]
                    if isinstance(artifact_paths, list)
                    else []
                )
                break
        first_fail = _first_fail(results)
    if first_fail is None:
        # 4. tool scope.
        results += ToolScopeOracle().check(events, scenario=scenario, tool_scope=tool_scope)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 6. revision.
        results += RevisionOracle().check(events, scenario=scenario, meta=revision_meta)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 7. scenario-directed lifecycle and context-pressure actions.
        results += ScenarioLifecycleOracle().check(
            events,
            scenario=scenario,
            product_evidence=product_evidence,
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        results += ContextPressureOracle().check(events, scenario=scenario)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 8. output truth.
        results += OutputTruthOracle().check(
            events,
            scenario=scenario,
            workspace_manifest=workspace_manifest,
            preview=preview,
            verified_claim_results=verified_claim_results,
            verified_observed_facts=verified_observed_facts,
            verified_artifact_paths=verified_artifact_paths,
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        # 9. provider ledger (HARN-1a) — MiniMax-only / no-OpenRouter / zero-calls-after-terminal.
        results += ProviderLedgerOracle().check(
            events,
            scenario=scenario,
            provider_ledger=provider_ledger,
            conversation_id=conversation_id,
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        # 10. H191 scenario-owned browser-verification promise.  Zero passing
        # verifier observations is an adjudicable PRODUCT failure; a verifier that
        # did pass but whose referenced bytes are not locked is harness INVALID.
        results += ScenarioBrowserVerificationOracle().check(
            scenario=scenario,
            verification_paths=verification_paths,
            browser_evidence_paths=captured_browser_paths,
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        # 11. browser product-harness oracles (HARN-2). Each SKIPs without its evidence
        # slice, so a headless run (product_evidence is None) is unaffected; a product-
        # harness run enforces the real UI path.
        for _oracle_cls in BROWSER_EVIDENCE_ORACLES:
            results += _oracle_cls().check(product_evidence=product_evidence)
            first_fail = _first_fail(results)
            if first_fail is not None:
                break
    if first_fail is None:
        # 12. targeted/manual-edit oracles (P8D). SKIP-safe: each SKIPs without its
        # product_evidence slice, so non-edit runs are unaffected; an edit-harness run
        # enforces targeted-edit + manual-preservation discipline. Producer = P1B-LIVE.
        for _oracle_cls in TARGETED_EDIT_ORACLES:
            results += _oracle_cls().check(product_evidence=product_evidence)
            first_fail = _first_fail(results)
            if first_fail is not None:
                break

    required_evidence_present = not any(
        r.failed and r.code in fc.HARNESS_VALIDITY_CODES for r in results
    )

    if first_fail is None:
        status, severity, code, broken, facts = fc.PASS, fc.NONE, None, None, {}
    elif first_fail.code in fc.HARNESS_VALIDITY_CODES:
        status, severity = fc.INVALID_RUN, fc.NONE
        code, broken, facts = first_fail.code, first_fail.first_broken_link, dict(first_fail.facts)
    else:
        status = fc.FAIL
        code = first_fail.code or fc.UNKNOWN_FAILURE
        severity = fc.severity_for(code)
        broken, facts = first_fail.first_broken_link, dict(first_fail.facts)

    classification: dict[str, Any] = {
        "status": status,
        "severity": severity,
        "code": code,
        "first_broken_link": broken,
        "scenario_id": (scenario or {}).get("id") if scenario else None,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "commit": commit,
        "seed": seed,
        "facts": facts,
        "oracle_results": [r.to_dict() for r in results],
        "required_evidence_present": required_evidence_present,
        "replay": {"attempted": False, "result": "not_attempted", "run_id": None},
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
    }
    if code and code in _LIKELY_FILES:
        classification["likely_files"] = list(_LIKELY_FILES[code])
    return classification


def _first_fail(results: list[OracleResult]) -> OracleResult | None:
    for r in results:
        if r.failed:
            return r
    return None


_BROWSER_VERIFICATION_TOOLS = frozenset({"verify_web_app", "verify_appkit_app"})
_DELIVERABLE_MUTATION_TOOLS = frozenset(
    {
        "file_write",
        "file_edit",
        "file_append",
        "file_replace_lines",
        "file_insert_lines",
        "file_str_replace",
        "exact_replace",
        "safe_write_file",
        "write_file",
        "app_create",
        "app_update_content",
        "app_add_section",
        "app_remove_section",
        "app_reorder_section",
        "app_set_design",
        "app_set_tweak",
        "app_add_primitive",
    }
)


def _admissible_browser_screenshot_path(path: str) -> bool:
    """Mirror H190's exact product-owned screenshot namespace."""
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and "\x00" not in path
        and len(parts) == 3
        and parts[:2] == [".pmx", "screenshots"]
        and not any(part in {"", ".", ".."} for part in parts)
        and parts[2].endswith(".png")
    )


def _successful_browser_verification_paths(events: list[dict[str, Any]]) -> set[str]:
    """Return screenshot paths claimed by strict passing browser evidence.

    A tool-level success is insufficient: ``verify_web_app`` also uses successful
    tool transport for an ``unverifiable`` verdict.  Requiring ``structured.passed``
    to be exactly true keeps that branch from satisfying a browser-verification
    contract. A direct ``browser`` observation is admissible only when it carries
    the same strict facts the product finish gate consumes: successful render on an
    explicit loopback preview port, clean console/network, meaningful content, and
    a screenshot path. Screenshot keys may be nested (notably for AppKit
    interactions), so collect the same explicit path-key family retained by the
    H190 capture. Host ``verifier_verdict`` events are accepted only with exact
    verified/pass fields and one admissible top-level screenshot path.
    """
    # A restore changes the exact workspace generation being adjudicated. Older
    # receipts remain in the audit log but cannot prove the restored tree, and
    # their product-owned screenshot bytes need not exist in the current
    # ProjectStore snapshot. Fence on the mutation itself so even a partial
    # restore invalidates earlier proof.
    restore_fence = max(
        (
            event["seq"]
            for event in events
            if (
                (
                    event.get("kind") == "workspace_mutation"
                    and event.get("operation") == "version.restore"
                )
                or event.get("kind") == "workspace_restored"
            )
            and type(event.get("seq")) is int
        ),
        default=0,
    )
    if restore_fence:
        events = [
            event
            for event in events
            if type(event.get("seq")) is int and event["seq"] > restore_fence
        ]

    paths: set[str] = set()
    actions_by_id: dict[str, dict[str, Any]] = {}
    active_previews: dict[str, tuple[int, int]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "screenshot_path" or key.endswith("_screenshot_path"):
                    if isinstance(child, str) and child:
                        paths.add(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    def local_url_port(value: Any) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = urlsplit(value)
            explicit_port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or explicit_port is None
        ):
            return None
        return explicit_port

    def paired_action(event: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
        action_id = event.get("action_id")
        if not isinstance(action_id, str):
            return None
        candidate = actions_by_id.get(action_id)
        if candidate is None:
            return None
        tool_call = candidate.get("tool_call")
        if not isinstance(tool_call, dict):
            return None
        call_id = tool_call.get("call_id")
        if not isinstance(call_id, str) or result.get("call_id") != call_id:
            return None
        if result.get("tool_name") != tool_call.get("tool_name"):
            return None
        return candidate

    mutation_actions: dict[str, dict[str, Any]] = {}
    last_mutation_observation_seq = 0
    for event in events:
        if event.get("kind") == "action":
            event_id = event.get("id")
            if isinstance(event_id, str):
                mutation_actions[event_id] = event
            continue
        if event.get("kind") != "observation":
            continue
        result = event.get("tool_result")
        action_id = event.get("action_id")
        seq = event.get("seq")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or not isinstance(action_id, str)
            or not isinstance(seq, int)
        ):
            continue
        action_event = mutation_actions.get(action_id)
        tool_call = action_event.get("tool_call") if isinstance(action_event, dict) else None
        if not isinstance(tool_call, dict):
            continue
        tool_name = tool_call.get("tool_name")
        if (
            tool_name in _DELIVERABLE_MUTATION_TOOLS
            and result.get("tool_name") == tool_name
            and result.get("call_id") == tool_call.get("call_id")
        ):
            last_mutation_observation_seq = max(last_mutation_observation_seq, seq)

    def strict_direct_browser(
        structured: dict[str, Any], action_event: dict[str, Any], selected_port: int | None
    ) -> bool:
        if structured.get("ok") is not True:
            return False
        tool_call = action_event.get("tool_call")
        if not isinstance(tool_call, dict) or tool_call.get("tool_name") != "browser":
            return False
        arguments = tool_call.get("arguments")
        if not isinstance(arguments, dict):
            return False
        action = arguments.get("action")
        if action not in {"navigate", "screenshot"}:
            return False
        action_url = arguments.get("url")
        action_port = local_url_port(action_url)
        observed_port = local_url_port(structured.get("url"))
        if selected_port is None or observed_port != selected_port:
            return False
        if action == "navigate" or (isinstance(action_url, str) and action_url):
            if action_port != selected_port:
                return False
        if action == "screenshot":
            # Screenshot observes the current page rather than always issuing a
            # navigation. Admit it only with the browser daemon's current-workspace
            # synchronization receipt; this preserves realistic final visual checks
            # without accepting a stale pre-mutation page.
            freshness = structured.get("freshness")
            if not isinstance(freshness, dict):
                return False
            requested_epoch = freshness.get("requested_epoch")
            synchronized_epoch = freshness.get("synchronized_epoch")
            if (
                not isinstance(requested_epoch, int)
                or isinstance(requested_epoch, bool)
                or not isinstance(synchronized_epoch, int)
                or isinstance(synchronized_epoch, bool)
                or requested_epoch != synchronized_epoch
                or not isinstance(freshness.get("sync_performed"), bool)
                or freshness.get("page_kind") != "local_preview"
            ):
                return False
        console = structured.get("console")
        network = structured.get("network")
        if not isinstance(console, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("level"), str)
            and isinstance(item.get("text"), str)
            for item in console
        ):
            return False
        if any(item["level"].lower() == "error" for item in console):
            return False
        if not isinstance(network, list) or network:
            return False
        title = structured.get("title")
        text = structured.get("text")
        if not isinstance(title, str) or not isinstance(text, str):
            return False
        semantic_count = structured.get("visible_semantic_elements")
        if not isinstance(semantic_count, int) or isinstance(semantic_count, bool):
            return False
        semantic = semantic_count > 0
        elements = structured.get("elements")
        if not isinstance(elements, list) or not all(
            isinstance(item, str) and bool(item.strip()) for item in elements
        ):
            return False
        meaningful = len((title + " " + text).strip()) >= 20 or semantic or bool(elements)
        path = structured.get("screenshot_path")
        return meaningful and isinstance(path, str) and bool(path)

    for event in events:
        if event.get("kind") == "action":
            event_id = event.get("id")
            if isinstance(event_id, str):
                actions_by_id[event_id] = event
            continue
        if event.get("kind") == "verifier_verdict":
            path = event.get("screenshot_path")
            if (
                isinstance(event.get("seq"), int)
                and event["seq"] > last_mutation_observation_seq
                and event.get("verified") is True
                and event.get("verdict") == "pass"
                and isinstance(path, str)
                and _admissible_browser_screenshot_path(path)
            ):
                paths.add(path)
            continue
        if event.get("kind") != "observation":
            continue
        event_seq = event.get("seq")
        proof_is_fresh = isinstance(event_seq, int) and event_seq > last_mutation_observation_seq
        result = event.get("tool_result")
        if not isinstance(result, dict):
            continue
        if result.get("success") is not True:
            continue
        structured = result.get("structured")
        if not isinstance(structured, dict):
            continue
        tool_name = result.get("tool_name")
        action_event = paired_action(event, result)
        if action_event is None:
            continue
        if tool_name == "preview_start":
            name = structured.get("name")
            port = structured.get("port")
            seq = event.get("seq")
            if (
                isinstance(name, str)
                and bool(name)
                and isinstance(port, int)
                and not isinstance(port, bool)
                and 1 <= port <= 65535
                and isinstance(seq, int)
                and structured.get("status") == "running"
            ):
                active_previews[name] = (seq, port)
            continue
        if tool_name == "preview_stop":
            stopped = structured.get("stopped")
            if isinstance(stopped, list) and all(
                isinstance(name, str) and bool(name) for name in stopped
            ):
                for name in stopped:
                    active_previews.pop(name, None)
            else:
                # A successful but malformed stop result cannot leave stale preview
                # state admissible as proof. A later strict preview_start re-arms it.
                active_previews.clear()
            continue
        if tool_name == "browser":
            selected_port = (
                max(active_previews.values(), key=lambda preview: preview[0])[1]
                if active_previews
                else None
            )
            if proof_is_fresh and strict_direct_browser(structured, action_event, selected_port):
                visit(structured)
            continue
        if (
            not proof_is_fresh
            or tool_name not in _BROWSER_VERIFICATION_TOOLS
            or structured.get("passed") is not True
        ):
            continue
        visit(structured)
    return paths


def classify_run_folder(
    folder: str | Path, *, scenario: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Classify a frozen run folder: load the manifest, verify evidence integrity
    (a mismatch -> INVALID_RUN), read the event log, classify, and write
    classification.json into the folder. Returns the classification dict."""
    base = Path(folder)
    manifest: EvidenceManifest | None = None
    evidence_intact = True
    try:
        manifest = load_manifest(base)
        integrity = verify_evidence_unchanged(base, manifest)
        evidence_intact = integrity.intact
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        manifest = None

    if manifest is not None:
        scenario_rel = manifest.evidence_files.get("scenario.json")
        if scenario_rel:
            try:
                scenario_path = base / scenario_rel
                loaded_scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
                if not isinstance(loaded_scenario, dict):
                    raise ValueError("persisted scenario is not an object")
                if (
                    manifest.scenario_sha256
                    and sha256_file(scenario_path) != manifest.scenario_sha256
                ):
                    raise ValueError("persisted scenario hash does not match manifest")
                if scenario is not None and scenario != loaded_scenario:
                    evidence_intact = False
                scenario = loaded_scenario
            except (OSError, json.JSONDecodeError, ValueError):
                evidence_intact = False

    events_raw: list[Any] = []
    events_rel = (manifest.evidence_files.get("events") if manifest else None) or "events.jsonl"
    events_path = base / events_rel
    if not events_path.is_file():
        # try the conventional conversation-scoped path
        for candidate in base.rglob("events.jsonl"):
            events_path = candidate
            break
    if events_path.is_file():
        events_raw = _read_jsonl(events_path)
    folder_conversation_id = ""
    if events_path.name == "events.jsonl" and events_path.parent.parent.name == "conversations":
        folder_conversation_id = events_path.parent.name

    # H179: replay output truth from the same locked artifacts used by the live
    # in-memory classification. Integrity-only checking is insufficient: without
    # reloading these files, a frozen preview/workspace verdict cannot be reproduced.
    workspace_manifest: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    if manifest is not None:
        workspace_rel = manifest.evidence_files.get("workspace-manifest.json")
        if workspace_rel:
            try:
                loaded_workspace = json.loads((base / workspace_rel).read_text(encoding="utf-8"))
                if not isinstance(loaded_workspace, dict):
                    raise ValueError("workspace manifest is not an object")
                workspace_manifest = loaded_workspace
            except (OSError, json.JSONDecodeError, ValueError):
                evidence_intact = False

        preview_health_rel = manifest.evidence_files.get("preview/health.json")
        preview_body_rel = manifest.evidence_files.get("preview/served.html")
        preview_meta_rel = manifest.evidence_files.get("preview/metadata.json")
        if preview_health_rel or preview_body_rel or preview_meta_rel:
            try:
                if not preview_health_rel or not preview_body_rel:
                    raise ValueError("preview evidence set is incomplete")
                loaded_health = json.loads((base / preview_health_rel).read_text(encoding="utf-8"))
                if not isinstance(loaded_health, dict):
                    raise ValueError("preview health is not an object")
                metadata: dict[str, Any] = {
                    "available": False,
                    "source": "legacy_unrecorded",
                }
                if preview_meta_rel:
                    loaded_metadata = json.loads(
                        (base / preview_meta_rel).read_text(encoding="utf-8")
                    )
                    if not isinstance(loaded_metadata, dict):
                        raise ValueError("preview metadata is not an object")
                    metadata = loaded_metadata
                preview = {
                    **metadata,
                    "health": loaded_health,
                    "content": (base / preview_body_rel).read_text(encoding="utf-8"),
                }
            except (OSError, json.JSONDecodeError, ValueError):
                evidence_intact = False
                preview = None

    # HARN-1a: the provider-call ledger (MiniMax-only / no-OpenRouter enforcement).
    # Read it if present anywhere in the run folder; absent → the oracle SKIPs (or
    # fails closed if the scenario requires it). A malformed line is NOT silently
    # dropped (that would under-report a forbidden call) nor crashes classification —
    # it becomes a hostless record the ProviderLedgerOracle fails on as an evidence gap.
    provider_ledger: list[dict[str, Any]] | None = None
    ledger_rel = (
        manifest.evidence_files.get(PROVIDER_LEDGER_NAME)
        or manifest.evidence_files.get("provider_ledger")
        if manifest
        else None
    )
    ledger_path = base / ledger_rel if ledger_rel else base / PROVIDER_LEDGER_NAME
    if manifest is None and not ledger_path.is_file():
        ledger_path = next(iter(base.rglob(PROVIDER_LEDGER_NAME)), ledger_path)
    if (manifest is None or ledger_rel is not None) and ledger_path.is_file():
        provider_ledger = _read_ledger(ledger_path)

    inspect_trace: dict[str, Any] | None = None
    trace_path = base / "inspect-trace.json"
    if not trace_path.is_file():
        trace_path = next(iter(base.rglob("inspect-trace.json")), trace_path)
    if trace_path.is_file():
        try:
            loaded_trace = json.loads(trace_path.read_text(encoding="utf-8"))
            inspect_trace = loaded_trace if isinstance(loaded_trace, dict) else None
        except (json.JSONDecodeError, ValueError):
            inspect_trace = None

    # HARN-2: the browser product-harness evidence dossier (a single JSON object).
    # Absent → the browser oracles SKIP (headless run unaffected).
    product_evidence: dict[str, Any] | None = None
    pe_path = base / "product-evidence.json"
    if not pe_path.is_file():
        pe_path = next(iter(base.rglob("product-evidence.json")), pe_path)
    if pe_path.is_file():
        try:
            loaded = json.loads(pe_path.read_text(encoding="utf-8"))
            product_evidence = loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, ValueError):
            product_evidence = None

    # H191: only browser evidence named in the manifest is admissible on frozen
    # replay.  These files are already covered by ``evidence_intact`` above; an
    # unlisted screenshot sitting beside the dossier cannot satisfy the contract.
    browser_evidence_paths: set[str] = set()
    if manifest is not None:
        prefix = "browser-evidence/"
        for label, rel in manifest.evidence_files.items():
            if not label.startswith(prefix) or label == prefix:
                continue
            proof_path = label.removeprefix(prefix)
            expected_rel = f"conversations/{folder_conversation_id}/browser-evidence/{proof_path}"
            if (
                folder_conversation_id
                and rel == expected_rel
                and label in manifest.evidence_hashes
                and manifest.evidence_hashes[label] != "MISSING"
            ):
                browser_evidence_paths.add(proof_path)

    classification = classify(
        events_raw,
        scenario=scenario,
        run_id=manifest.run_id if manifest else base.name,
        conversation_id=folder_conversation_id,
        commit=manifest.repo_commit if manifest else "",
        seed=manifest.seed if manifest else None,
        evidence_intact=evidence_intact,
        autonomous=manifest.autonomous if manifest else None,
        provider_ledger=provider_ledger,
        inspect_trace=inspect_trace,
        product_evidence=product_evidence,
        workspace_manifest=workspace_manifest,
        preview=preview,
        browser_evidence_paths=browser_evidence_paths,
    )
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(classification, indent=2, sort_keys=True), encoding="utf-8"
    )
    return classification


def _read_jsonl(path: str | Path) -> list[Any]:
    out: list[Any] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _read_ledger(path: str | Path) -> list[dict[str, Any]]:
    """Tolerant provider-call-ledger reader: never crashes on a malformed line and
    never silently drops one. A non-JSON / non-dict line becomes a hostless
    ``{"__malformed__": ...}`` record so the ProviderLedgerOracle surfaces it as an
    evidence gap (rather than under-reporting a possibly-forbidden call)."""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            out.append({"__malformed__": line[:160]})
            continue
        out.append(obj if isinstance(obj, dict) else {"__malformed__": str(obj)[:160]})
    return out


# ---- §17 no-fluke replay policy (pure function) -----------------------------


def intermittent_classification(
    original: dict[str, Any],
    *,
    replay_status: str,
    replay_code: str | None = None,
    replay_run_id: str | None = None,
) -> dict[str, Any]:
    """Apply the no-fluke replay policy (§17) to an originally-FAILED run.

    A failed run that PASSES on exact replay is NOT a pass — it is an intermittent
    failure: status stays FAIL, code becomes INTERMITTENT_<original_code> with the
    SAME severity. A replay that fails the same way is a deterministic failure
    (unchanged). The word "fluke" never yields PASS.

    Returns a NEW classification dict; the input is not mutated."""
    if original.get("status") != fc.FAIL:
        raise ValueError("no-fluke policy only applies to an originally-FAILED run")
    original_code = str(original.get("code"))
    updated = dict(original)

    if replay_status == fc.PASS:
        intermittent_code = fc.INTERMITTENT_PREFIX + original_code
        updated["status"] = fc.FAIL
        updated["code"] = intermittent_code
        updated["severity"] = fc.severity_for(intermittent_code)
        updated["replay"] = {
            "attempted": True,
            "result": "passed",
            "run_id": replay_run_id,
        }
        return updated

    if replay_status == fc.FAIL and replay_code == original_code:
        updated["replay"] = {"attempted": True, "result": "same_failure", "run_id": replay_run_id}
        return updated

    if replay_status == fc.FAIL:
        updated["replay"] = {
            "attempted": True,
            "result": "different_failure",
            "run_id": replay_run_id,
        }
        return updated

    # INVALID_RUN / INFRA_FAILURE on replay: cannot prove a pass; keep the failure
    # and record the inconclusive replay.
    updated["replay"] = {"attempted": True, "result": "not_attempted", "run_id": replay_run_id}
    return updated
