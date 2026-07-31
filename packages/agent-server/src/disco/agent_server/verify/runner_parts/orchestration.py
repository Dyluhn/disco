"""File/app/report validators + dossier writer + scenario orchestration (PKG-08 extraction).

Owns the IO-bearing validators (download + inspect real bytes), the live-app
reachability checks, the report-export byte check, the redacted dossier writer,
and the ``run_scenario`` entry point. Extracted from ``runner.py`` so the
orchestration logical-LOC is scoped to this module.

The public symbols are re-exported by ``runner.py`` so existing imports are
unchanged.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import disco.tools.verify.artifact_validators as _av
from disco.core.evidence.schema import redact

from ..probe import app_body_problem, validate_app_deliverables
from ..reliability import run_reliability_metrics
from ..schema import Scenario, VerifyResult
from .discovery import (
    _VALIDATABLE_EXTS,
    _deliverable_type_satisfied,
    _locate_deliverables,
)
from .policy_checks import (
    _run_appkit_verify_check,
    _run_forbid_checks,
    _run_tool_checks,
)
from .transport import _TERMINAL, AbstractVerifyClient, HttpVerifyClient

log = logging.getLogger(__name__)


async def _run_file_validators(
    deliverables: list[dict[str, Any]],
    *,
    client: AbstractVerifyClient,
    cid: str,
    dest_dir: Path,
) -> list[str]:
    """codex P0: DOWNLOAD each file deliverable from the app and validate the ACTUAL bytes —
    not a path that happens to exist in the runner's cwd (the old check silently skipped every
    live artifact). This is what catches a procedural-image deck or a corrupt artifact live."""
    problems: list[str] = []
    file_deliverables = [
        d for d in deliverables if str(d.get("path", "")).lower().endswith(_VALIDATABLE_EXTS)
    ]
    if file_deliverables:
        dest_dir.mkdir(parents=True, exist_ok=True)
    for d in file_deliverables:
        path = str(d["path"])
        data = await client.download_artifact(cid, path)
        if data is None:
            problems.append(f"deliverable not downloadable from the app: {path}")
            continue
        local = dest_dir / Path(path).name
        local.write_bytes(data)
        low = path.lower()
        if low.endswith(".pdf"):
            problems.extend(_av.validate_pdf(str(local)))
        elif low.endswith((".mp3", ".wav")):
            problems.extend(_av.validate_audio(str(local)))
        elif low.endswith(".xlsx"):
            problems.extend(_av.validate_sheet(str(local)))
        elif low.endswith((".pptx", ".html", ".htm")):
            # deck: detect procedural placeholder images in the real file (W3 live proof)
            problems.extend(_av.validate_deck_file(str(local)))
            if low.endswith(".pptx"):
                # codex round-3: also RENDER the pptx with LibreOffice — a zip-valid deck that
                # won't actually open would otherwise pass. Skips on hosts without soffice (the
                # PR tier); renders for real on the VM 201 nightly host.
                from disco.tools.verify.heavy_validators import validate_pptx_renders

                problems.extend(
                    p for p in validate_pptx_renders(str(local)) if "unavailable" not in p
                )
        elif low.endswith((".png", ".jpg", ".jpeg")):
            # codex round-2: a standalone image deliverable must also be checked for the
            # procedural placeholder signature (not only images embedded in a deck).
            if _av._looks_procedural(data):
                problems.append(f"procedural_placeholder_image: {path}")
    return problems


def _validate_report_export(
    result: tuple[int, bytes] | None, fmt: str, *, dest_dir: Path
) -> list[str]:
    """Gap #54: validate the REAL bytes returned by POST /report/export. Report export
    bypasses the event-log deliverable path (it's a direct blob/FSA download in the UI), so
    _locate_deliverables never sees it — this is what makes it discoverable + validatable by
    disco-verify. Must be reachable, 2xx, non-empty, and pass the format's byte check:
      • pdf  → validate_pdf on the downloaded bytes
      • md   → non-empty, UTF-8-decodable text
    """
    label = f"report.export[{fmt}]"
    if result is None:
        return [f"{label}: export endpoint not reachable"]
    status, body = result
    if not (200 <= status < 300):
        return [f"{label}: HTTP {status}"]
    if not body:
        return [f"{label}: empty body"]
    low = fmt.lower()
    if low == "pdf":
        dest_dir.mkdir(parents=True, exist_ok=True)
        local = dest_dir / "report_export.pdf"
        local.write_bytes(body)
        return [f"{label}: {p}" for p in _av.validate_pdf(str(local))]
    if low == "md":
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return [f"{label}: not UTF-8-decodable markdown"]
        if not text.strip():
            return [f"{label}: markdown body is blank"]
        return []
    # Unknown format → only the reachable+non-empty checks above apply.
    return []


async def _validate_app_deliverables(
    deliverables: list[dict[str, Any]], *, client: AbstractVerifyClient, cid: str
) -> list[str]:
    return await validate_app_deliverables(deliverables, client=client, cid=cid)


def _app_body_problem(result: tuple[int, bytes] | None, *, label: str) -> str | None:
    return app_body_problem(result, label=label)


async def _run_validators(
    scenario: Scenario,
    deliverables: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    client: AbstractVerifyClient,
    cid: str,
    dest_dir: Path,
) -> list[str]:
    """Forbid checks (no IO) + file validators on the real downloaded bytes + live-app
    reachability checks + EPIC M AppKit tool-path / verifier checks (no IO)."""
    problems = _run_forbid_checks(scenario, deliverables, events)
    problems.extend(_run_tool_checks(scenario, events))
    if scenario.expect_appkit_verify:
        problems.extend(_run_appkit_verify_check(events))
    problems.extend(
        await _run_file_validators(deliverables, client=client, cid=cid, dest_dir=dest_dir)
    )
    problems.extend(await _validate_app_deliverables(deliverables, client=client, cid=cid))
    return problems


def _write_dossier(
    dossier: Path,
    cid: str,
    scenario: Scenario,
    final_state: dict[str, Any],
    events: list[dict[str, Any]],
    trace: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    result: VerifyResult,
) -> None:
    """Write the redacted evidence dossier to *dossier* (created if absent).

    Files written:
    - ``result.json``    — scenario + VerifyResult, redacted
    - ``events.jsonl``  — one event per line, redacted
    - ``state.json``    — final state snapshot, redacted
    - ``trace.json``    — DISCO_INSPECT trace (if available), redacted
    - ``manifest.json`` — project manifest (if available), redacted
    """
    dossier.mkdir(parents=True, exist_ok=True)

    (dossier / "result.json").write_text(
        json.dumps(
            redact(
                {
                    "run_id": dossier.name,
                    "conversation_id": cid,
                    "scenario": scenario.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    with (dossier / "events.jsonl").open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(redact(evt)) + "\n")

    (dossier / "state.json").write_text(
        json.dumps(redact(final_state), indent=2),
        encoding="utf-8",
    )

    if trace is not None:
        (dossier / "trace.json").write_text(
            json.dumps(redact(trace), indent=2),
            encoding="utf-8",
        )

    if manifest is not None:
        (dossier / "manifest.json").write_text(
            json.dumps(redact(manifest), indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_scenario(
    scenario: Scenario,
    *,
    agent_base: str = "http://127.0.0.1:8000",
    timeout_s: float = 600,
    dossier_base: Path | None = None,
    _client: AbstractVerifyClient | None = None,
) -> VerifyResult:
    """Run one scenario against the live app and return a ``VerifyResult``.

    Drives the app through its HTTP/WS API (no browser, no internal calls):

    (a) POST /conversations with the scenario's surface/model_override.
    (b) Open the WS, send {"type":"send_message",...}; if ``approve_plan`` is
        True, wait for AWAITING_PLAN_APPROVAL and send {"type":"approve_plan"}.
    (c) Poll /conversations/{cid}/state until FINISHED/ERROR/STUCK/IDLE or timeout.
    (d) Fetch evidence: events, state, /api/debug/trace/{cid}, /api/projects/{cid}/manifest.
    (e) Locate deliverables from the event log.
    (f) Run forbid checks and file validators (pdf/audio/sheet from artifact_validators).
    (g) Write a redacted dossier under ``dossier_base/<run-id>/``.

    Parameters
    ----------
    scenario:
        The scenario to execute.
    agent_base:
        Base URL of the agent-server (e.g. ``http://127.0.0.1:8000``).
    timeout_s:
        Hard wall-clock timeout; effective timeout = min(scenario.timeout_s, timeout_s).
    dossier_base:
        Root directory for evidence output. Defaults to ``./test-record/disco-verify``.
    _client:
        Injectable transport for testing. Pass a ``FakeVerifyClient`` to skip live IO.
    """
    if dossier_base is None:
        dossier_base = Path("test-record") / "disco-verify"

    effective_timeout = min(float(scenario.timeout_s), timeout_s)
    client: AbstractVerifyClient = _client if _client is not None else HttpVerifyClient(agent_base)

    run_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_id = f"run_{run_ts}_{uuid.uuid4().hex[:8]}"

    log.info("[%s] scenario=%r surface=%s", run_id, scenario.id, scenario.surface)

    # (a) create conversation (EPIC M: appkit_mode → strict AppKit tool allowlist)
    cid = await client.create_conversation(
        scenario.surface, scenario.model_override, appkit_mode=scenario.appkit_mode
    )
    log.info("[%s] cid=%s", run_id, cid)

    # (b) WS exchange: send message; optionally wait for plan approval; then send
    # any scripted extra command frames (gap #3 — steer/stop/resume/…).
    await client.run_ws_exchange(
        cid,
        scenario.prompt,
        approve_plan=scenario.approve_plan,
        auto_answer=scenario.auto_answer,
        timeout_s=effective_timeout,
        ws_commands=scenario.ws_commands,
        send_build_brief=scenario.send_build_brief,
    )

    # (c) poll until terminal
    final_state = await client.poll_until_terminal(cid, timeout_s=effective_timeout)
    terminal_status = str(final_state.get("execution_status", "UNKNOWN"))
    log.info("[%s] cid=%s terminal_status=%s", run_id, cid, terminal_status)

    # (d) fetch evidence — fresh snapshots after the run terminates
    events = await client.get_events(cid)
    # Re-fetch state for the dossier (clean post-run snapshot distinct from the
    # poll's last-seen state, which may have been fetched mid-sleep).
    evidence_state = await client.get_state(cid)
    trace = await client.get_trace(cid)
    manifest = await client.get_manifest(cid)

    # (e) locate deliverables
    deliverables = _locate_deliverables(events)
    log.info("[%s] deliverables=%d", run_id, len(deliverables))

    # (f) validate — download + inspect the REAL delivered bytes
    artifacts_dir = dossier_base / run_id / "artifacts"
    validator_problems = await _run_validators(
        scenario,
        deliverables,
        events,
        client=client,
        cid=cid,
        dest_dir=artifacts_dir,
    )
    validator_problems.extend(
        await _run_report_export_check(scenario, cid, client=client, dest_dir=artifacts_dir)
    )
    validator_problems.extend(await _run_schedule_fire_check(scenario, cid, client=client))

    if validator_problems:
        log.warning("[%s] %d problem(s): %s", run_id, len(validator_problems), validator_problems)

    # compute passed
    passed, validator_problems = _compute_passed(
        scenario, terminal_status, final_state, deliverables, validator_problems
    )

    # build result with the dossier path already set
    dossier_path = dossier_base / run_id
    result = VerifyResult(
        scenario_id=scenario.id,
        terminal_status=terminal_status,
        deliverables=deliverables,
        validator_problems=validator_problems,
        passed=passed,
        reliability_metrics=dict(run_reliability_metrics(events)),
        dossier_path=str(dossier_path.resolve()),
    )

    # (g) write dossier — use evidence_state (fresh post-run snapshot) for state.json
    _write_dossier(dossier_path, cid, scenario, evidence_state, events, trace, manifest, result)
    log.info("[%s] dossier=%s passed=%s", run_id, dossier_path, passed)

    return result


async def _run_report_export_check(
    scenario: Scenario,
    cid: str,
    *,
    client: AbstractVerifyClient,
    dest_dir: Path,
) -> list[str]:
    """Gap #54: report export bypasses the event log — call it explicitly + validate bytes."""
    if not scenario.report_export:
        return []
    export_result = await client.export_report(cid, scenario.report_export)
    return _validate_report_export(export_result, scenario.report_export, dest_dir=dest_dir)


async def _run_schedule_fire_check(
    scenario: Scenario,
    cid: str,
    *,
    client: AbstractVerifyClient,
) -> list[str]:
    """Gap #98 (REGRESSION seam): fire a schedule deterministically (no wall-clock) and
    assert it produced a schedule_run event."""
    if not scenario.fire_schedule_id:
        return []
    fired = await client.fire_schedule_now(cid, scenario.fire_schedule_id)
    if not fired:
        return [f"schedule fire-now hook did not accept schedule {scenario.fire_schedule_id!r}"]
    post_fire_events = await client.get_events(cid)
    if not any(e.get("kind") == "schedule_run" for e in post_fire_events):
        return [f"fired schedule {scenario.fire_schedule_id!r} produced no schedule_run event"]
    return []


def _compute_passed(
    scenario: Scenario,
    terminal_status: str,
    final_state: dict[str, Any],
    deliverables: list[dict[str, Any]],
    validator_problems: list[str],
) -> tuple[bool, list[str]]:
    """Compute the final passed verdict and append any deliverable-type problem.

    codex P0: a run that never reached a real terminal status — a timeout, or left
    at RUNNING / AWAITING_* — must FAIL, even when the scenario declares no
    expected status (otherwise a silent hang would PASS, defeating the whole point).
    codex round-2: a scenario that EXPECTS a deliverable type (e.g. a deck) must
    actually produce one — a FINISHED run with zero matching deliverables is a
    false pass.
    """
    expected_status = scenario.expect.get("terminal_status")
    reached_terminal = terminal_status in _TERMINAL and not final_state.get("_timed_out", False)
    status_ok = reached_terminal and (
        expected_status is None or terminal_status == str(expected_status)
    )
    expected_dtype = scenario.expect.get("deliverable_type")
    deliverable_ok = _deliverable_type_satisfied(deliverables, expected_dtype)
    if not deliverable_ok:
        validator_problems.append(
            f"expected deliverable_type={expected_dtype!r} but no matching deliverable was produced"
        )
    passed = not validator_problems and status_ok and deliverable_ok
    return passed, validator_problems


__all__ = [
    "_app_body_problem",
    "_run_file_validators",
    "_run_validators",
    "_validate_app_deliverables",
    "_validate_report_export",
    "_write_dossier",
    "run_scenario",
]
