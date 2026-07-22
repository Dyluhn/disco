"""run.py — the headless live-API Build runner (PR S3, guidelines §25).

    python -m harness.build_soak.run --scenario static_html_minimal --iterations 1

Loads a scenario from scenarios.yaml, drives it against the LIVE agent-server
(http://127.0.0.1:8000) via the disco_api adapter ACTING AS THE USER (approve the
plan, send follow-ups at their trigger points), assembles the §5 evidence dossier,
freezes it under the §6 evidence lock (manifest + SHA256 hashes), runs the
deterministic classifier with events + workspace + preview + autonomy (codex #2),
writes classification.json, and exits nonzero on FAIL / INVALID_RUN / INFRA_FAILURE.

The orchestration (`drive_scenario`) and dossier assembly (`assemble_dossier`) are
importable + transport-agnostic so the deterministic tests drive them with a FAKE
transport (no live model spend); `main()` wires the live HttpTransport.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from harness.reliability.state import source_revision as _source_revision

from . import failure_codes as fc
from .adapters.disco_api import (
    AWAITING_PLAN_APPROVAL,
    AWAITING_USER_DECISION,
    AWAITING_USER_QUESTION,
    FOLLOWUP_PICKUP_TIMEOUT,
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    PAUSED_STATE,
    PROGRESSING_TIMEOUT,
    TERMINAL_STATES,
    WAITING_FOR_CONFIRMATION,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    FollowupPickupError,
    InconclusiveRunError,
    InfraProbeError,
    SnapshotNotReadyError,
    Transport,
    _referenced_screenshot_paths,
    inspect_aggregate_violations,
    validate_browser_evidence_relpath,
)
from .classify import CLASSIFICATION_NAME, classify
from .events import NormalizationError, normalize_events
from .evidence import EvidenceManifest, compute_evidence_hashes, sha256_file, write_manifest
from .oracles.thrash import ThrashOracle
from .product_evidence import (
    PRODUCT_EVIDENCE_NAME,
    PROVIDER_LEDGER_NAME,
    write_product_evidence,
    write_provider_ledger,
)
from .provider_ledger import parse_relay_log, record_applies_to_conversation
from .resources import (
    GIB,
    AdmissionPolicy,
    ResourceGate,
    ResourcePool,
    read_host_resources,
    resolve_worker_count,
    safe_worker_count,
)

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_OUT = "test-record/build-soak"
_SCENARIOS = Path(__file__).resolve().parent / "scenarios.yaml"
_MAX_GATES = 8  # bound the approve loop so a gate flap can't spin forever
_MAX_RESUMES = 3  # bound PAUSED-resume so an actionless-paused build can't spin forever
_MAX_CLARIFY = 3  # bound clarify/confirm answers so an endlessly-asking model is let go (Bug 17)
_MAX_DECISION = 3  # bound AWAITING_USER_DECISION auto-resolutions (mirror _MAX_CLARIFY)

# The runner ACTS AS THE USER (§14 runner directive): when a build asks a clarifying
# question mid-build, the runner answers it so the build proceeds — exactly what a real
# user does. The generic answer MUST instruct "do not ask further questions" so a model
# can't trap the build in a question-loop; a scenario MAY override it with its own
# `clarification_answer`. This does NOT weaken the oracle: the resulting build is still
# adjudicated (plan→approve→execute→deliver) — a build that STILL fails after a
# reasonable clarification is a genuine finding (Bug 17, §17).
_GENERIC_CLARIFY_ANSWER = (
    "Use your best judgment and proceed with sensible, conventional defaults. "
    "Do not ask further clarifying questions; build the most reasonable version."
)

# Progress-aware terminal-wait knobs (Bug 15). The terminal wait is NOT a blind
# wall-clock: `inactivity_s` is the NO-PROGRESS window (a build that keeps emitting
# events is never cut off — only genuine silence for this long ends the wait), and
# `hard_cap_s` is the generous safety ceiling that bounds a truly-hung run, set well
# above a normal build (~5min) so a slow-but-progressing build finishes on its real
# terminal rather than being frozen mid-flight + mislabeled BUILD_DID_NOT_FINISH.
_DEFAULT_INACTIVITY_S = 180.0
_DEFAULT_HARD_CAP_S = 1200.0
_TRIGGER_AFTER_FIRST_FILE_WRITE = "after_first_file_write"
_TRIGGER_AFTER_TERMINAL = "after_terminal"
_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME = "browser-evidence-collection-error.json"


# ---- scenario loading -------------------------------------------------------


def load_scenarios(path: str | Path = _SCENARIOS) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = raw.get("scenarios") or []
    defaults = raw.get("defaults") or {}

    def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out

    loaded = [merge(defaults, s) for s in scenarios if s.get("id")]
    return {str(s["id"]): s for s in loaded}


def _materialize_task_seed(value: Any, seed: int) -> Any:
    """Replace the frozen ``{{seed}}`` token throughout one scenario copy."""
    if isinstance(value, str):
        return value.replace("{{seed}}", str(seed))
    if isinstance(value, list):
        return [_materialize_task_seed(item, seed) for item in value]
    if isinstance(value, dict):
        return {key: _materialize_task_seed(item, seed) for key, item in value.items()}
    return copy.deepcopy(value)


def _driver_catalog_contains(payload: dict[str, Any], model: str) -> bool:
    """Whether the live agent reports the exact saved key as driver-eligible."""

    models = payload.get("models")
    if not isinstance(models, list):
        return False
    return any(
        isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] == model
        for entry in models
    )


def _is_terminal_driver_preflight_trace(
    run: CollectedRun,
    trace: dict[str, Any],
    routing: object,
    agent_spans: list[dict[str, Any]],
) -> bool:
    """Whether a trace proves the driver failed before the agent loop could start.

    Driver readiness calls are deliberately outside ``agent.step``.  Requiring a
    loop span after a named terminal routing failure launders a genuine product /
    provider failure into missing-evidence INVALID_RUN.  The exception is narrow:
    terminal ERROR, non-empty all-terminal routing decisions, no tool-scope event,
    and no agent span.  Once the loop starts it records a tool scope before its
    model request, so a missing span after that boundary still fails closed.
    """

    tool_scopes = trace.get("tool_scopes")
    return (
        DiscoApiClient._status_of(run.state_final) == "ERROR"
        and trace.get("dropped_event_count") == 0
        and not agent_spans
        and isinstance(routing, list)
        and bool(routing)
        and all(
            isinstance(decision, dict)
            and str(decision.get("role") or "") == "agent_driver"
            and bool(str(decision.get("chosen_model") or ""))
            and bool(str(decision.get("provider") or ""))
            and str(decision.get("reason") or "").startswith("terminal failure:")
            for decision in routing
        )
        and isinstance(tool_scopes, list)
        and not tool_scopes
    )


def _latest_durable_status(run: CollectedRun) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return the latest normalized status plus the corroborating durable log."""

    try:
        events = normalize_events(run.events)
    except NormalizationError:
        return None, []
    statuses = [event for event in events if event.get("kind") == "status"]
    return (statuses[-1] if statuses else None), events


def _named_sandbox_preflight_detail(detail: str) -> bool:
    """Match only reason strings produced by ``runtime._preflight_sandbox``."""

    if detail.startswith("sandbox backend is misconfigured:"):
        return bool(detail.removeprefix("sandbox backend is misconfigured:").strip())
    for prefix in (
        "gvisor sandbox host ",
        "local sandbox host ",
        "podman sandbox host ",
    ):
        if not detail.startswith(prefix):
            continue
        rest = detail.removeprefix(prefix)
        for marker in (" unreachable:", " error:"):
            endpoint, found, cause = rest.partition(marker)
            if found and endpoint.strip() and cause.strip():
                return True
        return False
    return any(
        detail.startswith(prefix) and bool(detail.removeprefix(prefix).strip())
        for prefix in ("process sandbox unreachable:", "process sandbox error:")
    )


def _is_terminal_sandbox_preflight_trace(
    run: CollectedRun,
    trace: dict[str, Any],
    routing: object,
    agent_spans: list[dict[str, Any]],
    scenario: dict[str, Any],
) -> bool:
    """Whether exact evidence proves sandbox readiness failed before the loop.

    Driver readiness succeeds first and records the exact chosen model/provider;
    sandbox readiness then runs before loop composition and therefore has no
    ``agent.step`` or tool scope.  Only the runtime's named, fail-closed sandbox
    error shapes are admitted.  A generic loop ERROR remains missing evidence.
    """

    latest_status, durable_events = _latest_durable_status(run)
    if latest_status is None:
        return False
    detail = str(latest_status.get("detail") or "")
    named_sandbox_failure = (
        str(latest_status.get("source") or "") == "system"
        and str(latest_status.get("status") or "").upper() == "ERROR"
        and _named_sandbox_preflight_detail(detail)
    )
    tool_scopes = trace.get("tool_scopes")
    spans = trace.get("spans")
    trace_events = trace.get("events")
    dropped = trace.get("dropped_event_count")
    expected_model = str(
        (((scenario.get("assertions") or {}).get("provider") or {}).get("model")) or ""
    )
    route: dict[str, Any] | None = (
        routing[0]
        if isinstance(routing, list) and len(routing) == 1 and isinstance(routing[0], dict)
        else None
    )
    if route is None:
        return False
    reason = str(route.get("reason") or "")
    overflow = route.get("overflow_triggers")
    successful_route = (
        str(route.get("role") or "") == "agent_driver"
        and str(route.get("path") or "") in {"manual", "pinned"}
        and bool(str(route.get("chosen_model") or ""))
        and bool(str(route.get("provider") or ""))
        and isinstance(route.get("attempt"), int)
        and int(route["attempt"]) >= 1
        and (
            (reason == "config" and overflow == [])
            or (
                reason in {"shared driver preflight success", "cached driver preflight success"}
                and overflow == ["driver_preflight"]
            )
        )
        and (not expected_model or route.get("chosen_model") == expected_model)
    )
    only_trace_event: dict[str, Any] | None = (
        trace_events[0]
        if isinstance(trace_events, list)
        and len(trace_events) == 1
        and isinstance(trace_events[0], dict)
        else None
    )
    trace_consistent = (
        dropped == 0
        and isinstance(trace_events, list)
        and trace.get("event_count") == len(trace_events)
        and only_trace_event is not None
        and only_trace_event.get("kind") == "routing"
        and all(
            only_trace_event.get(field) == route.get(field)
            for field in (
                "role",
                "chosen_model",
                "provider",
                "path",
                "reason",
                "attempt",
                "overflow_triggers",
            )
        )
        and all(event.get("kind") not in {"tool_scope", "span"} for event in trace_events)
    )
    loop_event_kinds = {"plan", "action", "observation", "agent_error", "report"}
    return (
        DiscoApiClient._status_of(run.state_final) == "ERROR"
        and named_sandbox_failure
        and not agent_spans
        and spans == []
        and successful_route
        and trace_consistent
        and isinstance(tool_scopes, list)
        and not tool_scopes
        and not any(event.get("kind") in loop_event_kinds for event in durable_events)
    )


def _declared_workspace_paths(scenario: dict[str, Any]) -> list[str]:
    files = ((scenario.get("assertions") or {}).get("workspace") or {}).get("files") or []
    return [str(f["path"]) for f in files if f.get("path")]


def _preview_required(scenario: dict[str, Any]) -> bool:
    return bool(((scenario.get("assertions") or {}).get("preview") or {}).get("required"))


def _browser_verification_required(scenario: dict[str, Any]) -> bool:
    assertion = (scenario.get("assertions") or {}).get("browser_verification") or {}
    return isinstance(assertion, dict) and assertion.get("required") is True


def _is_cancel_after_first_write(cancel_at: dict[str, Any] | None) -> bool:
    return (
        isinstance(cancel_at, dict) and cancel_at.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
    )


class CancelMissedWindowError(Exception):
    """The cancel_at watcher found its trigger only after the build had terminalized."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


class _PauseOwnership:
    """Coordinate the scenario-owned pause with the generic PAUSED driver.

    A lifecycle pause is issued from the background trigger watcher while the
    main driver polls the same conversation.  The main driver must observe that
    pause without consuming it as an actionless/user pause.  This state is local
    to one drive, so parallel conversations cannot interfere with each other.
    """

    def __init__(self) -> None:
        self.active = False
        self.observed = False
        self.consumed = False
        self.settled = asyncio.Event()


# ---- orchestration (acts as the user) ---------------------------------------


async def _inject_when_writing(
    client: DiscoApiClient,
    cid: str,
    mid_run: list[dict[str, Any]],
    cancel_at: dict[str, Any] | None,
    pause_at: dict[str, Any] | None,
    timeline: list[str],
    timeout_s: float,
    pause_ownership: _PauseOwnership | None = None,
    declared_seqs: list[int] | None = None,
    declared_requires: list[bool] | None = None,
) -> None:
    """Background trigger watcher for after-first-file-write actions.

    It handles all current users of that trigger (mid-run steer follow-ups and the
    REL-6 cancel_at kill) from one task so the runner does not race two independent
    DB pollers against the same first-write boundary.
    """
    wants_cancel = (
        isinstance(cancel_at, dict) and cancel_at.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
    )
    wants_pause = (
        isinstance(pause_at, dict) and pause_at.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
    )
    if not mid_run and not wants_cancel and not wants_pause:
        return

    seq = await client.wait_for_first_file_write(cid, timeout_s=timeout_s)
    if seq is None:
        if mid_run:
            timeline.append("mid-run steer skipped: run produced no file write to steer on")
        if wants_cancel:
            timeline.append("cancel_at skipped: run produced no file write to cancel on")
        if wants_pause:
            timeline.append("pause_at skipped: run produced no file write to pause on")
        return

    if wants_pause:
        await _pause_resume_at_trigger(
            client,
            cid,
            timeline,
            trigger_seq=seq,
            timeout_s=timeout_s,
            ownership=pause_ownership,
        )

    for f in mid_run:
        before_user_seq = client.latest_user_message_seq(cid)
        await client.send_followup(cid, str(f["text"]), kind="steer")
        timeline.append(f"steered after first file write (seq={seq}): {f['text']!r}")
        # A steer IS a DECLARED follow-up (a user turn) — record its seq + flag so the
        # RevisionOracle anchors on it (and keeps the parallel seq/flag arrays aligned).
        if declared_seqs is not None and declared_requires is not None:
            new = await client.wait_for_new_user_message_seq(
                cid, after_seq=before_user_seq, timeout_s=min(timeout_s, 30.0)
            )
            if new is not None:
                declared_seqs.append(new)
                declared_requires.append(bool(f.get("requires_plan_revision")))

    if wants_cancel:
        await _cancel_at_trigger(
            client,
            cid,
            timeline,
            trigger=_TRIGGER_AFTER_FIRST_FILE_WRITE,
            trigger_seq=seq,
            timeout_s=timeout_s,
        )


async def _wait_for_status(
    client: DiscoApiClient,
    cid: str,
    expected: str,
    *,
    timeout_s: float,
) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = DiscoApiClient._status_of(await client.get_state(cid))
        if last == expected:
            return last
        if last in TERMINAL_STATES:
            return last
        await asyncio.sleep(getattr(client, "_poll", 0.1))
    return last


async def _pause_resume_at_trigger(
    client: DiscoApiClient,
    cid: str,
    timeline: list[str],
    *,
    trigger_seq: int,
    timeout_s: float,
    ownership: _PauseOwnership | None = None,
) -> None:
    if ownership is not None:
        ownership.active = True
        ownership.settled.clear()
    try:
        await client.pause(cid)
        paused = await _wait_for_status(client, cid, PAUSED_STATE, timeout_s=min(timeout_s, 60.0))
        if paused != PAUSED_STATE:
            client.scenario_evidence["pause_resume"] = {
                "ok": False,
                "trigger_seq": trigger_seq,
                "pause_status": paused,
            }
            timeline.append(
                f"pause_at after first write (seq={trigger_seq}) did not settle PAUSED "
                f"(status={paused or 'unknown'})"
            )
            return
        if ownership is not None:
            ownership.observed = True
        response = await client.resume(cid)
        resumed = await client.wait_until_status_leaves(
            cid, PAUSED_STATE, timeout_s=min(timeout_s, 60.0)
        )
        ok = 200 <= int(response.get("http_status", 0)) < 300 and resumed != PAUSED_STATE
        client.scenario_evidence["pause_resume"] = {
            "ok": ok,
            "trigger_seq": trigger_seq,
            "pause_status": paused,
            "resume_status": resumed,
            "resume_http_status": response.get("http_status"),
        }
        timeline.append(
            f"pause_at after first write (seq={trigger_seq}) settled PAUSED then resumed "
            f"to {resumed or 'unknown'} (http {response.get('http_status')})"
        )
    finally:
        if ownership is not None:
            ownership.active = False
            ownership.settled.set()


async def _restart_isolated_stack() -> dict[str, Any]:
    """Restart the disposable App+Agent pair through its private authenticated control."""
    url = os.environ.get("DISCO_RELIABILITY_STACK_CONTROL_URL", "").rstrip("/")
    token = os.environ.get("DISCO_RELIABILITY_STACK_CONTROL_TOKEN", "")
    if not url or not token:
        return {"ok": False, "reason": "isolated_stack_control_unavailable"}

    def _post() -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            f"{url}/restart",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                raw = response.read()
                body = json.loads(raw) if raw else {}
                return response.status, body if isinstance(body, dict) else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                body = json.loads(raw)
                if isinstance(body, dict):
                    return exc.code, body
            return exc.code, {}

    try:
        status, body = await asyncio.to_thread(_post)
    except (OSError, TimeoutError, ValueError) as exc:
        return {"ok": False, "reason": type(exc).__name__}
    return {
        "ok": 200 <= status < 300 and body.get("ok") is True,
        "http_status": status,
        "reason": body.get("reason"),
    }


def _export_matches_workspace(
    archive_bytes: bytes,
    workspace: dict[str, Any],
    declared_paths: list[str],
) -> tuple[bool, dict[str, Any]]:
    """Compare required and overlapping archive entries with frozen workspace bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = [name for name in archive.namelist() if name and not name.endswith("/")]
            if len(names) != len(set(names)):
                return False, {"reason": "duplicate_archive_path"}
            archive_shas = {
                name: hashlib.sha256(archive.read(name)).hexdigest() for name in sorted(names)
            }
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False, {"reason": "invalid_zip"}
    required = [path for path in declared_paths if not path.startswith((".pmx/", ".disco/"))]
    missing = sorted(path for path in required if path not in archive_shas)
    mismatched: list[str] = []
    for path, archive_sha in archive_shas.items():
        item = workspace.get(path)
        if isinstance(item, dict) and item.get("present") is True:
            if item.get("sha256") != archive_sha:
                mismatched.append(path)
    for path in required:
        item = workspace.get(path)
        if (
            isinstance(item, dict)
            and item.get("present") is True
            and path in archive_shas
            and item.get("sha256") != archive_shas[path]
            and path not in mismatched
        ):
            mismatched.append(path)
    return not missing and not mismatched and bool(archive_shas), {
        "archive_file_count": len(archive_shas),
        "required_paths": required,
        "missing_paths": missing,
        "mismatched_paths": sorted(mismatched),
    }


async def _wait_for_cancel_idle(
    client: DiscoApiClient,
    cid: str,
    *,
    previous_status: str,
    timeout_s: float,
) -> str:
    """Wait until a kill settles into the product's resting state.

    The kill route appends `IDLE` with detail `killed`. We require two consecutive IDLE
    reads to avoid racing the state projection immediately after the POST.
    """
    deadline = time.monotonic() + timeout_s
    last = previous_status
    if previous_status and previous_status != "IDLE":
        remaining = max(0.0, deadline - time.monotonic())
        last = await client.wait_until_status_leaves(cid, previous_status, timeout_s=remaining)

    stable_idle_reads = 1 if last == "IDLE" else 0
    while time.monotonic() < deadline:
        last = DiscoApiClient._status_of(await client.get_state(cid))
        if last == "IDLE":
            stable_idle_reads += 1
            if stable_idle_reads >= 2:
                return last
        else:
            stable_idle_reads = 0
        await asyncio.sleep(getattr(client, "_poll", 0.1))
    raise TimeoutError(f"cancel did not settle to stable IDLE (last status={last!r})")


async def _cancel_at_trigger(
    client: DiscoApiClient,
    cid: str,
    timeline: list[str],
    *,
    trigger: str,
    timeout_s: float,
    trigger_seq: int | None = None,
) -> None:
    before_status = ""
    with contextlib.suppress(Exception):
        before_status = DiscoApiClient._status_of(await client.get_state(cid))
    terminal_seq = -1
    with contextlib.suppress(Exception):
        terminal_seq = client._latest_terminal_seq(cid)
    if trigger == _TRIGGER_AFTER_FIRST_FILE_WRITE and (
        (trigger_seq is not None and terminal_seq > trigger_seq)
        or (trigger_seq is None and terminal_seq >= 0)
        or before_status in TERMINAL_STATES
    ):
        reason = (
            "cancel_at after_first_file_write missed the interrupt window: "
            "the build reached a terminal status before the harness could issue kill"
        )
        facts = {
            "trigger": trigger,
            "trigger_seq": trigger_seq,
            "terminal_seq": terminal_seq if terminal_seq >= 0 else None,
            "status_before_kill": before_status or None,
        }
        timeline.append(f"{reason} (facts={facts})")
        raise CancelMissedWindowError(reason, facts)
    resp = await client.kill(cid)
    seq_note = f" (seq={trigger_seq})" if trigger_seq is not None else ""
    timeline.append(
        f"cancel_at fired at {trigger}{seq_note}: POST /conversations/{cid}/kill "
        f"(was {before_status or 'unknown'}, http {resp.get('http_status')})"
    )
    settled = await _wait_for_cancel_idle(
        client,
        cid,
        previous_status=before_status,
        timeout_s=min(timeout_s, 60.0),
    )
    timeline.append(f"cancel_at settled to stable {settled}; continuing scenario")


async def _drive_to_terminal(
    client: DiscoApiClient,
    cid: str,
    *,
    autonomous: bool,
    mid_run: list[dict[str, Any]],
    cancel_at: dict[str, Any] | None = None,
    pause_at: dict[str, Any] | None = None,
    timeline: list[str],
    inactivity_s: float,
    hard_cap_s: float,
    clarification_answer: str | None = None,
    decision_answer: str | None = None,
    decisions: list[dict[str, Any]] | None = None,
    injected_user_seqs: list[int] | None = None,
    declared_followup_seqs: list[int] | None = None,
    declared_followup_requires_revision: list[bool] | None = None,
    min_seq: int | None = None,
) -> str:
    """Drive the run to a terminal state, approving each plan gate (interactive),
    answering any mid-build clarify/confirm gate (Bug 17), and injecting any mid-run
    steer follow-up concurrently. Returns the terminal status.

    `min_seq` (H1/V2): when driving an after-terminal FOLLOW-UP, the drive must NOT return on
    the STALE pre-follow-up terminal (its event seq <= min_seq) — it keeps polling until the
    follow-up's OWN new terminal (a terminal event with seq > min_seq). This is the piece V1
    missed: V1's drive checked only the status STRING and returned on the stale FINISHED.

    The wait is PROGRESS-AWARE (Bug 15): a still-actively-progressing build is never cut
    off by a wall-clock — only a genuine terminal, genuine inactivity (INACTIVE_TIMEOUT →
    fall through to normal classification of the wedged run), or the hard cap ends it. A
    hard-cap cutoff WHILE STILL PROGRESSING (PROGRESSING_TIMEOUT) is INCONCLUSIVE, not a
    product failure → raise InconclusiveRunError so the run records INVALID_RUN (§17)."""
    injector: asyncio.Task[None] | None = None
    pause_ownership = _PauseOwnership() if pause_at else None
    if (
        mid_run
        or _is_cancel_after_first_write(cancel_at)
        or (
            isinstance(pause_at, dict)
            and pause_at.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
        )
    ):
        injector = asyncio.create_task(
            _inject_when_writing(
                client,
                cid,
                mid_run,
                cancel_at,
                pause_at,
                timeline,
                hard_cap_s,
                pause_ownership=pause_ownership,
                declared_seqs=declared_followup_seqs,
                declared_requires=declared_followup_requires_revision,
            )
        )
    gates = 0
    resumes = 0
    clarifies = 0
    decisions_done = 0
    decision_sink = decisions if decisions is not None else []
    injected_sink = injected_user_seqs if injected_user_seqs is not None else []

    def _record_injected_user_turn(before_seq: int) -> None:
        # Attribute the NEW user message the harness just injected (clarification answer /
        # decision pick) so the RevisionOracle EXCLUDES it from revision anchors. Called AFTER
        # the gate cleared, so any appended user turn is already durable — a SINGLE check (no
        # wait). A control frame that appends NO user message (confirm / pick_alternative) leaves
        # the watermark unchanged → nothing recorded, no needless wait.
        new = client.latest_user_message_seq(cid)
        if new > before_seq:
            injected_sink.append(new)

    try:
        while True:
            if autonomous:
                status = await client.poll_until_terminal(
                    cid,
                    inactivity_s=inactivity_s,
                    hard_cap_s=hard_cap_s,
                    min_terminal_seq=min_seq,
                )
            else:
                status = await client.poll_until_terminal_or_gate(
                    cid,
                    inactivity_s=inactivity_s,
                    hard_cap_s=hard_cap_s,
                    min_terminal_seq=min_seq,
                )
            if status == PROGRESSING_TIMEOUT:
                # The hard cap hit while the build was STILL emitting events — the runner
                # could not obtain a terminal verdict. This is a harness/model-speed limit,
                # NOT a product BUILD_DID_NOT_FINISH: surface it as INVALID_RUN so §17 re-runs.
                timeline.append(
                    "hard-cap reached while build was STILL PROGRESSING — "
                    "inconclusive (INVALID_RUN, not a product failure)"
                )
                raise InconclusiveRunError(
                    "terminal status not reached before the hard cap while the build "
                    "was still actively progressing",
                    {"stage": "terminal_wait", "hard_cap_s": hard_cap_s},
                )
            if (
                status != PAUSED_STATE
                and pause_ownership is not None
                and pause_ownership.observed
                and not pause_ownership.active
            ):
                # The injector completed its whole pause/resume cycle between
                # driver polls. Retire the ownership token on this first
                # non-PAUSED observation so a later actionless PAUSE remains a
                # generic user-resumable state.
                pause_ownership.consumed = True
            if status == AWAITING_PLAN_APPROVAL and gates < _MAX_GATES:
                gates += 1
                await client.approve_plan(cid)
                timeline.append(f"approved plan (gate {gates})")
                # Wait for the gate to clear so a not-yet-processed approval isn't
                # re-read as the same gate and double-approved (wastes the gate budget).
                await client.wait_until_status_leaves(
                    cid, AWAITING_PLAN_APPROVAL, timeout_s=min(inactivity_s, 60.0)
                )
                continue
            if (
                status in (AWAITING_USER_QUESTION, WAITING_FOR_CONFIRMATION)
                and clarifies < _MAX_CLARIFY
            ):
                # The runner ACTS AS THE USER: a build that asks a clarifying question (or
                # pauses for a go-ahead) mid-build is ANSWERED so it proceeds, instead of
                # false-stalling into NO_PLAN (Bug 17). Each gate is answered via its REAL
                # mechanism — a clarifying QUESTION over the send_message path a real user
                # follow-up uses; a CONFIRMATION via the dedicated `confirm` control frame
                # (the confirm analogue of approve_plan — a plain message does NOT clear it).
                # BOUNDED (≤ _MAX_CLARIFY): a model that keeps asking past the cap is let go
                # to a real terminal/inactivity and classified HONESTLY — never an infinite
                # answer-loop, never a masked failure.
                clarifies += 1
                before_user_seq = client.latest_user_message_seq(cid)
                if status == WAITING_FOR_CONFIRMATION:
                    await client.confirm(cid)
                    timeline.append(
                        f"confirmed pending action (clarify {clarifies}/{_MAX_CLARIFY})"
                    )
                else:
                    answer = clarification_answer or _GENERIC_CLARIFY_ANSWER
                    await client.send_followup(cid, answer, kind="message")
                    timeline.append(
                        "answered clarifying question "
                        f"(clarify {clarifies}/{_MAX_CLARIFY}): {answer!r}"
                    )
                # Wait for the gate to clear so the same unprocessed gate isn't re-read +
                # re-answered (wastes the clarify budget), mirroring the approval gate.
                await client.wait_until_status_leaves(
                    cid, status, timeout_s=min(inactivity_s, 60.0)
                )
                # An ANSWER (send_message) appends a user turn the oracle must NOT mis-anchor as
                # a revision follow-up; a CONFIRM appends none → nothing recorded.
                _record_injected_user_turn(before_user_seq)
                continue
            if status == AWAITING_USER_DECISION:
                # The model proposed structured alternatives (a user-choice gate) after repeated
                # tool failure. A non-interactive soak ACTS AS THE USER and auto-resolves it via
                # the REAL pick_alternative mechanism (NOT approve_plan), so a build that merely
                # asked for a choice can FINISH instead of false-stalling into BUILD_DID_NOT_FINISH.
                # BOUNDED (≤ _MAX_DECISION) + TRACEABLE (every resolution recorded). On cap-hit OR
                # an invalid/stale payload (resolve_decision returns None — state no longer
                # AWAITING_USER_DECISION, or no valid option) we do NOT continue: fall through to
                # the terminal/stop return so the run is judged HONESTLY (never a clean pass).
                if decisions_done >= _MAX_DECISION:
                    timeline.append(
                        f"AWAITING_USER_DECISION cap (_MAX_DECISION={_MAX_DECISION}) hit — "
                        "releasing to honest classification (NOT auto-finishing)"
                    )
                    return status
                before_user_seq = client.latest_user_message_seq(cid)
                resolved = await client.resolve_decision(cid, preferred_option_id=decision_answer)
                if resolved is None:
                    timeline.append(
                        "AWAITING_USER_DECISION could NOT be auto-resolved (stale/invalid "
                        "payload: gate no longer live or no valid option) — releasing to honest "
                        "classification (NOT auto-finishing)"
                    )
                    return status
                decisions_done += 1
                record = {**resolved, "attempt": decisions_done}
                decision_sink.append(record)
                timeline.append(
                    f"auto-resolved user decision (decision {decisions_done}/{_MAX_DECISION}): "
                    f"picked option {resolved['option_id']!r} for alternatives "
                    f"{resolved['alternatives_id']!r}"
                )
                # Wait for the gate to clear so the same unprocessed decision isn't re-read +
                # re-resolved, mirroring the approval/clarify gates.
                await client.wait_until_status_leaves(
                    cid, AWAITING_USER_DECISION, timeout_s=min(inactivity_s, 60.0)
                )
                # pick_alternative synthesizes an ACTION (not a user message) → normally nothing
                # to record; the watermark check captures any injected user turn defensively.
                _record_injected_user_turn(before_user_seq)
                continue
            if status == PAUSED_STATE and pause_ownership is not None:
                if pause_ownership.active:
                    try:
                        await asyncio.wait_for(
                            pause_ownership.settled.wait(), timeout=min(hard_cap_s, 125.0)
                        )
                    except TimeoutError:
                        timeline.append(
                            "scenario-owned pause did not settle within its bounded control "
                            "window — releasing to honest classification"
                        )
                        return status
                if pause_ownership.observed and not pause_ownership.consumed:
                    pause_ownership.consumed = True
                    if injector is not None and injector.done():
                        injector.result()
                    timeline.append(
                        "scenario-owned PAUSED state was resumed by the lifecycle injector"
                    )
                    continue
            if status == PAUSED_STATE and resumes < _MAX_RESUMES:
                # A cooperative / actionless PAUSE is RESUMABLE — the runner acts as the
                # user who hits Resume. BOUNDED (≤ _MAX_RESUMES) so a build that just keeps
                # actionless-pausing can't spin forever; after the budget it falls through
                # to the terminal/stop return and the oracle classifies the non-finished
                # run (BUILD_DID_NOT_FINISH), never a silent pass.
                resumes += 1
                resp = await client.resume(cid)
                http_status = int(resp.get("http_status", 0))
                timeline.append(f"resumed PAUSED run (resume {resumes}, http {http_status})")
                if http_status >= 400:
                    # not resumable (409) — stop retrying; let the oracle judge.
                    timeline.append("resume rejected (not resumable) — stopping")
                    return status
                await client.wait_until_status_leaves(
                    cid, PAUSED_STATE, timeout_s=min(inactivity_s, 60.0)
                )
                continue
            if injector is not None and _is_cancel_after_first_write(cancel_at):
                if injector.done():
                    injector.result()
                else:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            asyncio.shield(injector), timeout=min(inactivity_s, 60.0)
                        )
                    if injector.done():
                        injector.result()
            if injector is not None and injector.done():
                injector.result()
            timeline.append(f"reached terminal/stop status: {status}")
            return status
    finally:
        if injector is not None:
            if not injector.done():
                injector.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await injector
            elif not injector.cancelled():
                # Retrieve completed-task failures on every return path, not
                # only the ordinary terminal path.
                injector.result()


async def drive_scenario(
    client: DiscoApiClient,
    scenario: dict[str, Any],
    *,
    model: str | None,
    autonomous: bool,
    timeout_s: float = _DEFAULT_INACTIVITY_S,
    hard_cap_s: float = _DEFAULT_HARD_CAP_S,
) -> CollectedRun:
    """Create + drive one scenario end-to-end, then COLLECT all evidence
    (events from the DB race-free, state, workspace, preview).

    `timeout_s` is the PROGRESS-AWARE INACTIVITY window (no-new-events budget), NOT a
    blind wall-clock; `hard_cap_s` is the generous safety ceiling (Bug 15)."""
    timeline: list[str] = []
    client.enable_live_thrash_monitor(scenario)
    prompt = str(scenario["prompt"])
    appkit = bool(scenario.get("appkit"))
    surface = str(scenario.get("surface") or "build")
    import_fixture = scenario.get("import_fixture")
    if import_fixture is not None and not isinstance(import_fixture, dict):
        raise ValueError("import_fixture must be an object")
    lifecycle = scenario.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        raise ValueError("lifecycle must be an object")
    cid = await client.create_build_conversation(
        prompt,
        model=model,
        autonomous=autonomous,
        appkit=appkit,
        surface=surface,
        import_fixture=import_fixture,
    )
    timeline.append(
        f"created {surface} conversation {cid} (autonomous={autonomous}, appkit={appkit})"
    )
    state_initial = await client.get_state(cid)

    # Optional scenario-provided answer to a mid-build clarifying question (Bug 17). When
    # absent, the runner sends a generic safe default that instructs the model not to ask
    # further questions. Either way the build proceeds + is adjudicated on its real outcome.
    clarification_answer = scenario.get("clarification_answer")
    clarification_answer = str(clarification_answer) if clarification_answer is not None else None

    # Optional scenario-provided preferred option for an AWAITING_USER_DECISION gate (Part B).
    # Absent → resolve_decision picks the recommended/first valid option. Each auto-resolution
    # is accumulated here so the run record can flag a PASS that REQUIRED one.
    decision_answer = scenario.get("decision_answer")
    decision_answer = str(decision_answer) if decision_answer is not None else None
    decisions: list[dict[str, Any]] = []

    # Revision-anchor metadata (harness-only): the DECLARED follow-ups the runner sends (seq +
    # requires_plan_revision flag, parallel arrays in send order) vs the HARNESS-INJECTED
    # auto-answers (clarification answers / decision picks). The RevisionOracle anchors revision
    # checks on the declared follow-ups and EXCLUDES the injected turns — so an auto-answer is
    # never mis-anchored as a revision follow-up (the false NO_REPLAN / WRITE_BEFORE regression).
    declared_followup_seqs: list[int] = []
    declared_followup_requires_revision: list[bool] = []
    harness_injected_user_seqs: list[int] = []

    followups = scenario.get("followups") or []
    cancel_at = scenario.get("cancel_at") if isinstance(scenario.get("cancel_at"), dict) else None
    pause_trigger = lifecycle.get("pause_resume_at")
    pause_at = (
        {"trigger": pause_trigger} if isinstance(pause_trigger, str) and pause_trigger else None
    )
    mid_run = [f for f in followups if f.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE]
    after_terminal = [f for f in followups if f.get("trigger") != _TRIGGER_AFTER_FIRST_FILE_WRITE]

    async def _freeze_progress_timeout(exc: InconclusiveRunError) -> None:
        """Stop spend and retain the evidence behind an inconclusive hard-cap verdict."""
        try:
            pre_stop_events = client.collect_events(cid)
            pre_stop_watermark_trusted = True
        except Exception:  # noqa: BLE001 — the later collection records the retained gap
            pre_stop_events = []
            pre_stop_watermark_trusted = False
        pre_stop_max_seq = max((int(event.get("seq", -1)) for event in pre_stop_events), default=-1)
        kill_response: dict[str, Any] = {}
        kill_response_epoch: float | None = None
        kill_acknowledged = False
        try:
            kill_response = await client.kill(cid)
            kill_response_epoch = time.time()
            http_status = int(kill_response.get("http_status", 0))
            kill_acknowledged = (
                200 <= http_status < 300
                and kill_response.get("killed") is True
                and DiscoApiClient._status_of(kill_response.get("state") or {}) == "IDLE"
            )
            if not kill_acknowledged:
                raise RuntimeError("diagnostic stop was not acknowledged as killed IDLE")
            timeline.append(
                "progress hard-cap diagnostic stop: killed active conversation "
                f"(http {kill_response.get('http_status')})"
            )
        except Exception as kill_exc:  # noqa: BLE001 — retain partial evidence fail-closed
            timeline.append(
                "progress hard-cap diagnostic stop could not kill conversation: "
                f"{type(kill_exc).__name__}"
            )

        # Stop FIRST, then freeze: the conversation was still ACTIVE at the hard
        # cap, so a pre-kill final sample could miss trace events the model
        # emits while the stop lands — and the aggregate would still claim
        # losslessness.  The poller stays alive through the kill; the idempotent
        # owned finalizer then takes the post-stop sample and retains any prior
        # canonical prefix even if this caller is cancelled.  An unconfirmed
        # stop taints continuity instead of ever narrowing the retained prefix.
        if not kill_acknowledged:
            client.note_inspect_stop_unconfirmed(cid)
        await client.finish_inspect_collection(cid)
        inspect_trace = await client.collect_inspect_trace(cid)

        try:
            events = client.collect_events(cid)
        except Exception as events_exc:  # noqa: BLE001 — retain all other slices fail-closed
            events = pre_stop_events
            timeline.append(
                f"progress hard-cap event collection failed: {type(events_exc).__name__}"
            )
        durable_stops = [
            (int(event.get("seq", -1)), epoch)
            for event in events
            if pre_stop_watermark_trusted
            and int(event.get("seq", -1)) > pre_stop_max_seq
            and _status_value_and_detail(event) == ("IDLE", "killed")
            and (epoch := _event_epoch(event)) is not None
        ]
        durable_stop = min(durable_stops, default=None)
        diagnostic_stop_seq = durable_stop[0] if durable_stop is not None else None
        diagnostic_stop_epoch = durable_stop[1] if durable_stop is not None else None
        release_confirmed = kill_acknowledged and durable_stop is not None
        if release_confirmed and client.last_conversation_id == cid:
            # Both the endpoint acknowledgement and the exact new durable kill
            # event prove that this stop owns release.  Disarm the finally fallback.
            client.last_conversation_id = None
        try:
            state_final = await client.get_state(cid)
        except Exception as state_exc:  # noqa: BLE001
            state_final = {"evidence_error": type(state_exc).__name__}
            timeline.append(
                f"progress hard-cap final-state collection failed: {type(state_exc).__name__}"
            )
        try:
            workspace = await client.collect_workspace(cid, _declared_workspace_paths(scenario))
        except Exception as workspace_exc:  # noqa: BLE001
            workspace = {}
            timeline.append(
                f"progress hard-cap workspace collection failed: {type(workspace_exc).__name__}"
            )
        try:
            browser_evidence = client.collect_browser_evidence(
                cid,
                events,
                workspace,
                require_verified_host_screenshot=_browser_verification_required(scenario),
            )
        except Exception as browser_exc:  # noqa: BLE001
            browser_evidence = {}
            timeline.append(
                "progress hard-cap browser-evidence collection failed: "
                f"{type(browser_exc).__name__}"
            )
        preview: dict[str, Any] | None = None
        if _preview_required(scenario):
            try:
                preview = await client.collect_preview(cid)
            except Exception as preview_exc:  # noqa: BLE001
                timeline.append(
                    f"progress hard-cap preview collection failed: {type(preview_exc).__name__}"
                )
        try:
            client.observe_live_thrash_snapshot(
                events,
                inspect_trace,
                terminal_status=DiscoApiClient._status_of(state_final),
            )
        except Exception as thrash_exc:  # noqa: BLE001 — retain the remaining dossier
            timeline.append(
                f"progress hard-cap thrash snapshot failed: {type(thrash_exc).__name__}"
            )
        exc.collected_run = CollectedRun(
            conversation_id=cid,
            events=events,
            state_initial=state_initial,
            state_final=state_final,
            workspace_manifest=workspace,
            preview=preview,
            browser_evidence=browser_evidence,
            inspect_trace=inspect_trace,
            thrash_monitor=client.live_thrash_monitor,
            timeline=timeline,
            decision_resolutions=decisions,
            declared_followup_seqs=declared_followup_seqs,
            declared_followup_requires_revision=declared_followup_requires_revision,
            harness_injected_user_seqs=harness_injected_user_seqs,
            product_evidence={
                **client.scenario_evidence,
                "diagnostic_stop": {
                    "kind": "progressing_hard_cap",
                    "release_confirmed": release_confirmed,
                    "boundary_epoch": diagnostic_stop_epoch,
                    "boundary_seq": diagnostic_stop_seq,
                    "boundary_source": (
                        "durable_killed_status" if durable_stop is not None else "unconfirmed"
                    ),
                    "kill_acknowledged": kill_acknowledged,
                    "kill_response_epoch": kill_response_epoch,
                    "pre_stop_watermark_trusted": pre_stop_watermark_trusted,
                    # The kill route returns ids before runtime teardown removes
                    # them from subsequent state reads. Preserve that one scoped
                    # attribution source so a parallel wave never falls back to
                    # counting other trials' live containers as this run's leaks.
                    "sandbox_instance_ids": _extract_sandbox_instance_ids(kill_response),
                },
            },
            diagnostic_stop="progressing_hard_cap",
            diagnostic_stop_epoch=diagnostic_stop_epoch,
            diagnostic_stop_seq=diagnostic_stop_seq,
            diagnostic_release_confirmed=release_confirmed,
        )

    # Phase 1: the initial build (+ any mid-run steer) to terminal.
    try:
        initial_status = await _drive_to_terminal(
            client,
            cid,
            autonomous=autonomous,
            mid_run=mid_run,
            cancel_at=cancel_at,
            pause_at=pause_at,
            timeline=timeline,
            inactivity_s=timeout_s,
            hard_cap_s=hard_cap_s,
            clarification_answer=clarification_answer,
            decision_answer=decision_answer,
            decisions=decisions,
            injected_user_seqs=harness_injected_user_seqs,
            declared_followup_seqs=declared_followup_seqs,
            declared_followup_requires_revision=declared_followup_requires_revision,
        )
    except InconclusiveRunError as exc:
        await _freeze_progress_timeout(exc)
        raise

    terminal_snapshot_states = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK"})
    drive_status = initial_status
    drive_terminal_baseline_seq: int | None = None
    if initial_status == INACTIVE_TIMEOUT:
        # A wedged/nonterminal initial phase has no legal "after terminal" phase.
        # Sending those turns would pile work onto the very run being adjudicated
        # and could hide its decisive BUILD_DID_NOT_FINISH boundary.
        after_terminal = []

    if initial_status == LIVE_THRASH_STOP:
        timeline.append(
            "confirmed live thrash threshold stopped the conversation; "
            "skipping every remaining follow-up"
        )
        after_terminal = []

    if (
        initial_status in terminal_snapshot_states
        and cancel_at
        and cancel_at.get("trigger") == _TRIGGER_AFTER_TERMINAL
    ):
        await _cancel_at_trigger(
            client,
            cid,
            timeline,
            trigger=_TRIGGER_AFTER_TERMINAL,
            timeout_s=hard_cap_s,
        )

    if initial_status in terminal_snapshot_states and lifecycle.get("restore_version"):
        selector = str(lifecycle.get("restore_version") or "oldest")
        restored = await client.restore_workspace_version(cid, selector=selector)
        client.scenario_evidence["rollback"] = restored
        timeline.append(
            "restored workspace version after initial terminal: "
            f"selector={selector}, ok={restored.get('ok')}, "
            f"selected={restored.get('selected_seq')}, new={restored.get('new_version')}"
        )

    if initial_status in terminal_snapshot_states and lifecycle.get("restart_after_terminal"):
        declared_paths = _declared_workspace_paths(scenario)
        before_status = DiscoApiClient._status_of(await client.get_state(cid))
        before_digest = await client.workspace_snapshot_digest(cid, declared_paths)
        restarted = await _restart_isolated_stack()
        after_status = ""
        after_digest: str | None = None
        if restarted.get("ok") is True:
            with contextlib.suppress(Exception):
                after_status = DiscoApiClient._status_of(await client.get_state(cid))
                after_digest = await client.workspace_snapshot_digest(cid, declared_paths)
        restart_ok = (
            restarted.get("ok") is True
            and before_status in terminal_snapshot_states
            and after_status == before_status
            and before_digest is not None
            and after_digest == before_digest
        )
        client.scenario_evidence["restart"] = {
            **restarted,
            "ok": restart_ok,
            "before_status": before_status,
            "after_status": after_status,
            "before_digest": before_digest,
            "after_digest": after_digest,
        }
        timeline.append(
            "restarted isolated App+Agent stack after initial terminal: "
            f"ok={restart_ok}, status={before_status}->{after_status}, "
            f"snapshot_same={before_digest is not None and after_digest == before_digest}"
        )

    # Phase 2: after-terminal follow-ups — each is its own re-plan→approve→terminal
    # cycle (the user types a follow-up; the PRODUCT decides to re-plan).
    #
    # SERIALIZED (H1/V2): these follow-ups must be sent ONE AT A TIME, each fully picked up
    # AND driven to its OWN new terminal before the next is sent. The naive loop sent them
    # back-to-back because the terminal wait returned IMMEDIATELY on the STALE prior FINISHED
    # status — so follow-up 2 landed before the engine started processing follow-up 1, and the
    # engine (processing only the latest unprocessed user turn) COLLAPSED the pile into a
    # SINGLE plan revision (a false PLAN_REVISION_NOT_INCREMENTED).
    #
    # V2 REVISED (single send + hard-fail; the re-send was a live bug — on a FINISHED run
    # send_followup APPENDS the user turn again, DUPLICATING the follow-up, since there is no
    # legal kick-without-append for FINISHED, resume being illegal there). For EACH follow-up:
    #   1. snapshot a SEQ baseline BEFORE the send;
    #   2. send EXACTLY ONCE, then BLOCK on the EVENT-SEQUENCED wait_for_followup_pickup (a
    #      non-user progress event past the baseline seq — detectable even when the status stays
    #      FINISHED) for the policy-driven bound (default ~75s, the real ~37s pickup + margin);
    #   3. EXPLICIT decision: pickup observed → drive (success path). Pickup NOT observed within
    #      the bound → HARD-FAIL (FollowupPickupError → INVALID_RUN); do NOT re-send (avoids the
    #      duplicate turn) and do NOT send any later follow-up (preserves no-pile-up). Proceeding
    #      on the stale terminal is precisely what let follow-up 2 collapse into follow-up 1;
    #   4. on pickup, drive to terminal with min_seq=baseline.max_seq so the drive returns on the
    #      follow-up's OWN new terminal (seq > baseline), never the stale one.
    for f in after_terminal:
        baseline = await client.capture_followup_baseline(cid)
        baseline_seq = int(baseline.get("max_seq", -1))
        before_user_seq = client.latest_user_message_seq(cid)
        await client.send_followup(cid, str(f["text"]), kind="message")
        timeline.append(f"sent after-terminal follow-up: {f['text']!r}")
        pickup = await client.wait_for_followup_pickup(cid, baseline)
        if pickup == FOLLOWUP_PICKUP_TIMEOUT:
            # HARD-FAIL: the engine never picked the follow-up up within the bound. Do NOT
            # re-send (a second send_followup DUPLICATES the user turn on a FINISHED run) and
            # do NOT drive on the stale pre-follow-up terminal (that collapses the next follow-up
            # into this one). Record a SEQUENCING failure (INVALID_RUN), never a silent proceed.
            timeline.append(
                "follow-up NOT picked up within the bound — SEQUENCING FAILURE (INVALID_RUN); "
                "not re-sending (would duplicate the turn) and not driving on the stale terminal "
                f"(baseline status={baseline.get('status')!r}, "
                f"plan_revision={baseline.get('plan_revision')}, seq={baseline_seq})"
            )
            raise FollowupPickupError(
                "after-terminal follow-up was not picked up by the engine within the bound "
                "— cannot serialize the follow-ups",
                {
                    "stage": "followup_pickup",
                    "baseline_status": baseline.get("status"),
                    "baseline_seq": baseline_seq,
                    "baseline_plan_revision": baseline.get("plan_revision"),
                    "followup_text": str(f["text"]),
                },
            )
        timeline.append(f"follow-up picked up by the engine ({pickup}) — now serialized")
        # Record this DECLARED follow-up's user-message seq + its requires_plan_revision flag
        # (parallel arrays) so the RevisionOracle anchors on it (and ONLY the declared turns).
        # AFTER pickup the user turn is durable → a SINGLE check (no wait); a fake transport that
        # never appended it leaves the watermark unchanged → nothing recorded.
        new_user_seq = client.latest_user_message_seq(cid)
        if new_user_seq > before_user_seq:
            declared_followup_seqs.append(new_user_seq)
            declared_followup_requires_revision.append(bool(f.get("requires_plan_revision")))
        try:
            drive_terminal_baseline_seq = baseline_seq
            drive_status = await _drive_to_terminal(
                client,
                cid,
                autonomous=autonomous,
                mid_run=[],
                pause_at=None,
                timeline=timeline,
                inactivity_s=timeout_s,
                hard_cap_s=hard_cap_s,
                clarification_answer=clarification_answer,
                decision_answer=decision_answer,
                decisions=decisions,
                injected_user_seqs=harness_injected_user_seqs,
                min_seq=baseline_seq,
            )
            if drive_status in {INACTIVE_TIMEOUT, LIVE_THRASH_STOP}:
                timeline.append(
                    "after-terminal phase stopped without a durable work terminal: "
                    f"{drive_status}; skipping remaining follow-ups"
                )
                break
        except InconclusiveRunError as exc:
            await _freeze_progress_timeout(exc)
            raise

    # Collect the durable event/state boundary before release. A genuinely inactive
    # nonterminal run is already an adjudicable product failure; it can never have
    # the post-terminal atomic workspace commit required by strict live capture.
    # Waiting for that impossible commit would mask BUILD_DID_NOT_FINISH behind
    # WORKSPACE_SNAPSHOT_NOT_READY (H339). Preserve an explicit empty evidence slice
    # so ContractOracle can proceed to OutputTruthOracle's earlier terminal verdict.
    events = client.collect_events(cid)
    state_final = await client.get_state(cid)
    # Inspect is independent evidence. Finalize it before workspace/browser
    # capture can fail, and necessarily before terminal cleanup releases the
    # source daemon/server state.  A conversation that is NOT durably terminal
    # at this boundary (inactive timeout, parked gate cap, or a product stop
    # that never confirmed) may still emit trace events after a pre-stop final
    # sample, so it is stopped FIRST under the same acknowledged-kill contract
    # as the hard-cap path; an unconfirmed stop taints continuity instead of
    # letting the aggregate claim losslessness (work-order §6.2: on
    # uncertainty, lossless is false).  The durable event/state boundary above
    # was read BEFORE the stop, so the dossier keeps the true product state;
    # workspace/browser evidence reads the host ProjectStore and durable
    # bytes, which survive the kill, and the hard-cap path already set the
    # precedent of capturing preview after the stop.
    if DiscoApiClient._status_of(state_final) not in TERMINAL_STATES:
        stop_confirmed = False
        try:
            stop_response = await client.kill(cid)
            stop_confirmed = (
                200 <= int(stop_response.get("http_status", 0)) < 300
                and stop_response.get("killed") is True
                and DiscoApiClient._status_of(stop_response.get("state") or {}) == "IDLE"
            )
            timeline.append(
                "stopped nonterminal conversation before inspect freeze "
                f"(confirmed={stop_confirmed}, http {stop_response.get('http_status')})"
            )
        except Exception as stop_exc:  # noqa: BLE001 — taint instead of crash
            timeline.append(
                f"pre-freeze stop of nonterminal conversation failed: {type(stop_exc).__name__}"
            )
        if not stop_confirmed:
            client.note_inspect_stop_unconfirmed(cid)
    await client.finish_inspect_collection(cid)
    inspect_trace = await client.collect_inspect_trace(cid)
    try:
        frozen_events = normalize_events(events)
    except (NormalizationError, ValueError, TypeError):
        frozen_events = []
    # k6g F2 terminal ownership: the work terminal is the LATEST product
    # status, never merely "some terminal appeared". The canary carried a
    # STUCK at seq 516 that was answered and superseded (AWAITING_USER_
    # QUESTION -> user answer -> RUNNING -> monitor kill); treating that stale
    # marker as the run's terminal forced the strict snapshot wait onto a
    # killed, nonterminal run and masked the product outcome as INVALID_RUN.
    # IDLE markers are resting/stop states, never product supersession: the
    # harness's ``IDLE(killed)``, the product's steer-ingest bare ``IDLE`` and
    # ``IDLE(cancelled)`` all leave the latest WORK status authoritative — a
    # genuine late FINISHED stays strict (H302, verifier finding V4), while a
    # superseding RUNNING/AWAITING transition (the k6g answered-STUCK shape)
    # still routes to the carve-out. Mirrors `derive_final_workspace_fence`'s
    # latest-status principle in core events.
    latest_status_event: dict[str, Any] | None = None
    for event in frozen_events:
        if (
            event.get("kind") == "status"
            and event.get("source") == "system"
            and type(event.get("seq")) is int
        ):
            event_status, _event_detail = _status_value_and_detail(event)
            if event_status == "IDLE":
                continue
            if latest_status_event is None or int(event["seq"]) > int(latest_status_event["seq"]):
                latest_status_event = event
    frozen_work_terminal = (
        latest_status_event is not None
        and latest_status_event.get("status") in terminal_snapshot_states
        and (
            drive_terminal_baseline_seq is None
            or int(latest_status_event["seq"]) > drive_terminal_baseline_seq
        )
    )
    # Late-terminal race guard: if a real work terminal landed after the poll's
    # inactivity decision but before this frozen read, use the ordinary strict
    # terminal snapshot path. Never downgrade that terminal to a diagnostic slice.
    inactive_without_terminal = drive_status == INACTIVE_TIMEOUT and not frozen_work_terminal
    confirmed_live_thrash_without_terminal = (
        drive_status == LIVE_THRASH_STOP
        and not frozen_work_terminal
        and _strict_live_thrash_stop_boundary(
            events,
            state_final,
            client.live_thrash_monitor,
        )
    )
    nonterminal_without_snapshot = (
        inactive_without_terminal or confirmed_live_thrash_without_terminal
    )
    terminal_snapshot_available = not nonterminal_without_snapshot
    if terminal_snapshot_available:
        workspace = await client.collect_workspace(cid, _declared_workspace_paths(scenario))
    else:
        workspace = {
            "files": {},
            "_capture": {
                "status": "not_collected_nonterminal",
                "drive_status": drive_status,
                "reason": "strict atomic workspace evidence requires a durable terminal",
            },
        }
        timeline.append(
            "skipped strict workspace/browser byte capture for nonterminal drive status "
            f"{drive_status}; preserving terminal adjudication"
        )
    browser_evidence_collection_exc: BrowserEvidenceCollectionError | None = None
    browser_collection_diagnostic: dict[str, Any] | None = None
    if not terminal_snapshot_available:
        browser_evidence = {}
        referenced_paths = _referenced_screenshot_paths(
            events,
            require_verified_host_screenshot=_browser_verification_required(scenario),
        )
        if referenced_paths:
            browser_collection_diagnostic = {
                "schema_version": 1,
                "kind": "browser_evidence_not_collected_nonterminal",
                "reason": "strict browser bytes require the unavailable terminal snapshot",
                "facts": {"referenced_paths": referenced_paths},
                "conversation_id": cid,
                "preserved_for": (
                    "confirmed_live_thrash_adjudication"
                    if confirmed_live_thrash_without_terminal
                    else "nonterminal_product_adjudication"
                ),
                "admissible_as_browser_evidence": False,
            }
    else:
        try:
            browser_evidence = client.collect_browser_evidence(
                cid,
                events,
                workspace,
                require_verified_host_screenshot=_browser_verification_required(scenario),
            )
        except BrowserEvidenceCollectionError as exc:
            # Browser evidence gaps ordinarily make a run INVALID.  A confirmed live-
            # thrash stop is different: the strict monitor established the product FAIL
            # before it killed the conversation, so a screenshot that disappeared after
            # that stop must not erase the decisive event/inspect/thrash evidence.  Defer
            # admission until the complete run satisfies _confirmed_live_thrash_stop;
            # every other collection error is re-raised below unchanged.
            if drive_status != LIVE_THRASH_STOP:
                raise
            browser_evidence = {}
            browser_evidence_collection_exc = exc
    preview = await client.collect_preview(cid) if _preview_required(scenario) else None
    client.observe_live_thrash_snapshot(
        events,
        inspect_trace,
        terminal_status=DiscoApiClient._status_of(state_final),
    )
    thrash_monitor = client.live_thrash_monitor
    timeline.append(
        "live thrash monitor sampled "
        f"{thrash_monitor['sample_count']} times and detected "
        f"{len(thrash_monitor['findings'])} threshold crossing(s)"
    )
    timeline.append(
        f"collected {len(events)} events; workspace files={list(workspace)}; "
        f"browser evidence files={list(browser_evidence)}"
    )
    scenario_product_evidence: dict[str, Any] = dict(client.scenario_evidence)
    if terminal_snapshot_available and lifecycle.get("export_download"):
        export_status, archive_bytes = await client.download_project(cid)
        matches, export_facts = _export_matches_workspace(
            archive_bytes,
            workspace,
            _declared_workspace_paths(scenario),
        )
        scenario_product_evidence["export"] = {
            "requested": True,
            "download_present": 200 <= export_status < 300 and bool(archive_bytes),
            "download_bytes": len(archive_bytes),
            "workspace_match": matches,
            "http_status": export_status,
            **export_facts,
        }
        timeline.append(
            "downloaded project export after final terminal: "
            f"http={export_status}, bytes={len(archive_bytes)}, workspace_match={matches}"
        )

    nonterminal_product_evidence: dict[str, Any] = dict(scenario_product_evidence)
    if nonterminal_without_snapshot:
        nonterminal_adjudication: dict[str, Any] = {
            "schema_version": 1,
            "drive_status": drive_status,
            "terminal_baseline_seq": drive_terminal_baseline_seq,
            "frozen_max_seq": max(
                (int(event["seq"]) for event in frozen_events if type(event.get("seq")) is int),
                default=-1,
            ),
            "terminal_snapshot_available": False,
            "terminal_only_evidence_unavailable": ["workspace", "browser"],
        }
        if confirmed_live_thrash_without_terminal:
            nonterminal_adjudication.update(
                {
                    "preserved_for": "strictly_confirmed_live_thrash_stop",
                    "strict_monitor_confirmed": True,
                }
            )
        nonterminal_product_evidence["nonterminal_adjudication"] = nonterminal_adjudication
    run = CollectedRun(
        conversation_id=cid,
        events=events,
        state_initial=state_initial,
        state_final=state_final,
        workspace_manifest=workspace,
        preview=preview,
        browser_evidence=browser_evidence,
        inspect_trace=inspect_trace,
        thrash_monitor=thrash_monitor,
        timeline=timeline,
        decision_resolutions=decisions,
        declared_followup_seqs=declared_followup_seqs,
        declared_followup_requires_revision=declared_followup_requires_revision,
        harness_injected_user_seqs=harness_injected_user_seqs,
        product_evidence=nonterminal_product_evidence,
        browser_evidence_collection_error=browser_collection_diagnostic
        or (
            {
                "schema_version": 1,
                "kind": "browser_evidence_collection_error",
                "exception_type": type(browser_evidence_collection_exc).__name__,
                "reason": browser_evidence_collection_exc.reason,
                "facts": dict(browser_evidence_collection_exc.facts),
                "conversation_id": cid,
                "preserved_for": "confirmed_live_thrash_stop",
                "admissible_as_browser_evidence": False,
            }
            if browser_evidence_collection_exc is not None
            else None
        ),
    )
    if browser_evidence_collection_exc is not None:
        if not _confirmed_live_thrash_stop(run):
            raise browser_evidence_collection_exc
        timeline.append(
            "confirmed live-thrash product failure retained despite browser-evidence "
            f"collection error: {type(browser_evidence_collection_exc).__name__}"
        )
    return run


# ---- dossier assembly + evidence lock (§5, §6) ------------------------------


def _events_jsonl(events: list[dict[str, Any]]) -> str:
    """One canonical full-event dict per line. The DB rows carry the event in a
    JSON-string `payload`; write the PARSED payload so events.jsonl is the readable
    canonical shape (the classifier normalizer accepts both)."""
    lines: list[str] = []
    for row in events:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = row
        elif not isinstance(payload, dict):
            payload = row
        lines.append(json.dumps(payload, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def _timeline_md(scenario: dict[str, Any], run: CollectedRun) -> str:
    lines = [
        f"# Build Soak timeline — {scenario.get('id')}",
        "",
        f"- conversation: `{run.conversation_id}`",
        f"- prompt: {scenario.get('prompt')!r}",
        "",
        "## Steps",
    ]
    lines += [f"{i + 1}. {step}" for i, step in enumerate(run.timeline)]
    return "\n".join(lines) + "\n"


def assemble_dossier(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    run: CollectedRun,
    *,
    model: str | None,
    autonomous: bool,
    commit: str = "",
    repo_revision: str = "",
    repo_dirty: bool = False,
    seed: int | None = None,
    mode: str = "api",
    kernel: str = "disco",
    started_at: str | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
) -> Path:
    """Write the §5 dossier and freeze it under the §6 evidence lock. Returns the
    run-folder path. classification.json is written separately by classify_dossier."""
    base = Path(out_root) / run_id
    conv = base / "conversations" / run.conversation_id
    conv.mkdir(parents=True, exist_ok=True)

    (base / "prompt.txt").write_text(str(scenario.get("prompt", "")), encoding="utf-8")
    scenario_path = base / "scenario.json"
    scenario_path.write_text(
        json.dumps(scenario, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (base / "followups.json").write_text(
        json.dumps(scenario.get("followups") or [], indent=2), encoding="utf-8"
    )
    (base / "timeline.md").write_text(_timeline_md(scenario, run), encoding="utf-8")

    (conv / "events.jsonl").write_text(_events_jsonl(run.events), encoding="utf-8")
    (conv / "state.initial.json").write_text(
        json.dumps(run.state_initial, indent=2, sort_keys=True), encoding="utf-8"
    )
    (conv / "state.final.json").write_text(
        json.dumps(run.state_final, indent=2, sort_keys=True), encoding="utf-8"
    )
    (conv / "workspace-manifest.json").write_text(
        json.dumps(run.workspace_manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    # H190: retain exact screenshot bytes referenced by successful browser/verifier
    # observations.  Preserve their workspace-relative spelling beneath a dedicated,
    # conversation-scoped directory; never embed or truncate binary data in JSON.
    browser_evidence_root = (conv / "browser-evidence").resolve()
    for rel, data in sorted(run.browser_evidence.items()):
        validate_browser_evidence_relpath(rel)
        evidence_path = browser_evidence_root / rel
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        if not evidence_path.resolve(strict=False).is_relative_to(browser_evidence_root):
            raise BrowserEvidenceCollectionError(
                "browser evidence dossier destination escapes its conversation directory",
                {"path": rel},
            )
        evidence_path.write_bytes(data)
    browser_collection_error = run.browser_evidence_collection_error
    if browser_collection_error is not None:
        (conv / _BROWSER_EVIDENCE_COLLECTION_ERROR_NAME).write_text(
            json.dumps(browser_collection_error, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if run.preview is not None:
        preview_dir = conv / "preview"
        preview_dir.mkdir(exist_ok=True)
        (preview_dir / "health.json").write_text(
            json.dumps(run.preview.get("health") or {}, indent=2, sort_keys=True), encoding="utf-8"
        )
        (preview_dir / "served.html").write_text(
            str(run.preview.get("content") or ""), encoding="utf-8"
        )
        (preview_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "available": bool(run.preview.get("available")),
                    "runtime_available": bool(run.preview.get("runtime_available")),
                    "runtime_availability_status": run.preview.get("runtime_availability_status"),
                    "source": str(run.preview.get("source") or "unrecorded"),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    if run.inspect_trace is not None:
        (conv / "inspect-trace.json").write_text(
            json.dumps(run.inspect_trace, indent=2, sort_keys=True), encoding="utf-8"
        )
    (conv / "thrash-monitor.json").write_text(
        json.dumps(run.thrash_monitor, indent=2, sort_keys=True), encoding="utf-8"
    )

    # [Lane A A-B4] PERSIST product_evidence so a folder reclassify/replay ADJUDICATES instead of
    # going green-by-absence (the headless soak's REL-5 cleanup evidence was in-memory only → any
    # classify_run_folder saw product_evidence=None → all browser oracles SKIP → ungated GREEN).
    # Written into the conv dir (classify_run_folder rglobs product-evidence.json) and added to the
    # §6 hash lock so a tamper trips INVALID_RUN. strict=False: persist the truthful evidence as the
    # oracles saw it (their _int coercion already fails-closed on malformed counts).
    _pe = getattr(run, "product_evidence", None)
    if _pe:
        write_product_evidence(conv, _pe, strict=False)
    if provider_ledger is not None:
        write_provider_ledger(conv, provider_ledger)

    # Evidence lock: hash the durable §6 set (relative to the run folder).
    conv_rel = f"conversations/{run.conversation_id}"
    evidence_files = {
        "scenario.json": "scenario.json",
        "events.jsonl": f"{conv_rel}/events.jsonl",
        "state.initial.json": f"{conv_rel}/state.initial.json",
        "state.final.json": f"{conv_rel}/state.final.json",
        "workspace-manifest.json": f"{conv_rel}/workspace-manifest.json",
    }
    for rel in sorted(run.browser_evidence):
        label = f"browser-evidence/{rel}"
        evidence_files[label] = f"{conv_rel}/{label}"
    if browser_collection_error is not None:
        evidence_files[_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME] = (
            f"{conv_rel}/{_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME}"
        )
    if _pe:
        evidence_files[PRODUCT_EVIDENCE_NAME] = f"{conv_rel}/{PRODUCT_EVIDENCE_NAME}"
    if run.inspect_trace is not None:
        evidence_files["inspect-trace.json"] = f"{conv_rel}/inspect-trace.json"
    if provider_ledger is not None:
        evidence_files[PROVIDER_LEDGER_NAME] = f"{conv_rel}/{PROVIDER_LEDGER_NAME}"
    evidence_files["thrash-monitor.json"] = f"{conv_rel}/thrash-monitor.json"
    # P1 (codex): the PREVIEW dossier is preview TRUTH the oracle adjudicates on — it
    # MUST be under the hash lock too, else a preview-health/served-html tamper would
    # not trip the §6 INVALID_RUN. Hash body, health, and provenance metadata when captured.
    if run.preview is not None:
        evidence_files["preview/health.json"] = f"{conv_rel}/preview/health.json"
        evidence_files["preview/served.html"] = f"{conv_rel}/preview/served.html"
        evidence_files["preview/metadata.json"] = f"{conv_rel}/preview/metadata.json"
    provider_assertion = (scenario.get("assertions") or {}).get("provider") or {}
    provider = (
        str(provider_assertion.get("require_host_substr") or "")
        if isinstance(provider_assertion, dict)
        else ""
    )
    if not started_at:
        for event in run.events:
            candidate = event.get("created_at") or event.get("timestamp")
            if isinstance(candidate, str) and candidate:
                started_at = candidate
                break
    manifest = EvidenceManifest(
        run_id=run_id,
        scenario_id=str(scenario.get("id")),
        scenario_sha256=sha256_file(scenario_path),
        seed=seed,
        repo_commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        model=model or "",
        provider=provider,
        autonomous=autonomous,
        surface=str(scenario.get("surface") or "build"),
        kernel=kernel,
        mode=mode,
        started_at=started_at or datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        evidence_files={"events": f"{conv_rel}/events.jsonl", **evidence_files},
        evidence_hashes=compute_evidence_hashes(base, evidence_files),
    )
    write_manifest(base, manifest)
    return base


def _status_value_and_detail(event: dict[str, Any]) -> tuple[str, Any]:
    status = event.get("status")
    detail = event.get("detail")
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) or {}
        except Exception:
            payload = {}
    if isinstance(payload, dict):
        if status is None:
            status = payload.get("status")
        if detail is None:
            detail = payload.get("detail")
    return str(status or "").upper(), detail


def _first_killed_idle_epoch(events: list[dict[str, Any]]) -> float | None:
    first: float | None = None
    for event in events:
        if event.get("kind") != "status":
            continue
        status, detail = _status_value_and_detail(event)
        epoch = _event_epoch(event)
        if status == "IDLE" and detail == "killed" and epoch is not None:
            first = epoch if first is None else min(first, epoch)
    return first


def _strict_live_thrash_stop_boundary(
    events: list[dict[str, Any]],
    state_final: dict[str, Any],
    monitor: dict[str, Any],
) -> bool:
    """Whether strict retained monitor evidence proves a pre-kill thrash stop.

    The monitor stops an actively thrashing conversation through ``POST /kill``.
    The resulting durable terminal is ``IDLE(detail=killed)``, not a normal Build
    work terminal.  Accept that otherwise-ambiguous marker only when the retained
    monitor record contains a confirmed strict ThrashOracle failure.
    """

    if DiscoApiClient._status_of(state_final) != "IDLE":
        return False
    if not isinstance(monitor, dict) or monitor.get("enabled") is not True:
        return False
    minimum = monitor.get("minimum_confirmation_samples")
    sample_count = monitor.get("sample_count")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
        return False
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < minimum
    ):
        return False
    thrash_codes = {
        fc.TOOL_CALL_THRASH,
        fc.TOOL_ERROR_THRASH,
        fc.ACTIONLESS_THRASH,
        fc.MODEL_REPAIR_THRASH,
    }
    findings = monitor.get("findings")
    if not isinstance(findings, list):
        return False
    run_start = _min_event_epoch(events)
    killed_at = _first_killed_idle_epoch(events)
    if run_start is None or killed_at is None or run_start > killed_at:
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        confirmations = finding.get("confirmation_samples")
        detected_at = finding.get("detected_at_epoch")
        terminal_status = finding.get("terminal_status")
        event_count = finding.get("event_count")
        max_event_seq = finding.get("max_event_seq")
        results = finding.get("oracle_results")
        if (
            not isinstance(confirmations, int)
            or isinstance(confirmations, bool)
            or confirmations < minimum
            or confirmations > sample_count
        ):
            continue
        if (
            not isinstance(detected_at, (int, float))
            or isinstance(detected_at, bool)
            or not math.isfinite(detected_at)
            or detected_at <= 0
        ):
            continue
        if not run_start <= float(detected_at) <= killed_at:
            continue
        if not isinstance(terminal_status, str) or not terminal_status:
            continue
        if terminal_status in TERMINAL_STATES or terminal_status == PAUSED_STATE:
            continue
        if not isinstance(event_count, int) or isinstance(event_count, bool) or event_count < 0:
            continue
        if (
            not isinstance(max_event_seq, int)
            or isinstance(max_event_seq, bool)
            or max_event_seq < -1
        ):
            continue
        if not isinstance(results, list):
            continue
        if any(
            isinstance(result, dict)
            and result.get("status") == fc.FAIL
            and result.get("oracle") == "ThrashOracle"
            and result.get("code") in thrash_codes
            for result in results
        ):
            return True
    return False


def _confirmed_live_thrash_stop(run: CollectedRun) -> bool:
    """Whether the frozen run satisfies the strict pre-kill monitor boundary."""

    return _strict_live_thrash_stop_boundary(
        run.events,
        run.state_final,
        run.thrash_monitor,
    )


def _current_thrash_failure_matches_retained(run: CollectedRun, scenario: dict[str, Any]) -> bool:
    """Require current deterministic agreement with one retained monitor finding."""

    try:
        current = [
            result.to_dict()
            for result in ThrashOracle().check(
                normalize_events(run.events),
                scenario=scenario,
                inspect_trace=run.inspect_trace,
            )
            if result.failed
        ]
    except (NormalizationError, ValueError, TypeError):
        return False
    retained = [
        result
        for finding in (run.thrash_monitor or {}).get("findings") or []
        if isinstance(finding, dict)
        for result in finding.get("oracle_results") or []
        if isinstance(result, dict)
        and result.get("oracle") == "ThrashOracle"
        and result.get("status") == fc.FAIL
    ]
    for observed in retained:
        if not isinstance(observed.get("first_broken_link"), str) or not isinstance(
            observed.get("facts"), dict
        ):
            continue
        if any(
            candidate.get("code") == observed.get("code")
            and candidate.get("first_broken_link") == observed.get("first_broken_link")
            and candidate.get("facts") == observed.get("facts")
            for candidate in current
        ):
            return True
    return False


def _provider_ledger_for_run(run: CollectedRun) -> list[dict[str, Any]] | None:
    """Capture this run's exact, terminal-annotated provider slice for live and replay use."""
    relay_log = _relay_log_path()
    if not relay_log or not os.path.exists(relay_log):
        return None
    if run.diagnostic_stop not in (None, "progressing_hard_cap"):
        return None
    if run.diagnostic_stop == "progressing_hard_cap" and (
        not isinstance(run.diagnostic_stop_epoch, (int, float))
        or isinstance(run.diagnostic_stop_epoch, bool)
        or not isinstance(run.diagnostic_stop_seq, int)
        or isinstance(run.diagnostic_stop_seq, bool)
    ):
        return None
    try:
        with open(relay_log, encoding="utf-8") as handle:
            records = parse_relay_log(handle.read())
        terminal_epoch = (
            run.diagnostic_stop_epoch
            if run.diagnostic_stop == "progressing_hard_cap"
            else _terminal_status_epoch(
                run.events,
                allow_killed_idle=_confirmed_live_thrash_stop(run),
            )
        )
        start_epoch = _min_event_epoch(run.events)
        if terminal_epoch is None or start_epoch is None:
            return None
        # after_terminal is the BUILD-runaway signal. Tool-less summarizer/title calls are benign;
        # unmarked records default has_tools=True and therefore remain fail-closed.
        return [
            {
                **record,
                "after_terminal": float(record["ts"]) > terminal_epoch
                and bool(record.get("has_tools", True)),
            }
            for record in records
            if isinstance(record.get("ts"), (int, float))
            and float(record["ts"]) >= start_epoch
            and record_applies_to_conversation(record, run.conversation_id)
        ]
    except Exception:  # noqa: BLE001 — required-provider oracle fails closed on None
        return None


def classify_dossier(
    base: Path,
    scenario: dict[str, Any],
    run: CollectedRun,
    *,
    autonomous: bool,
    commit: str = "",
    seed: int | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the deterministic classifier on the collected run, passing workspace +
    preview + autonomy DIRECTLY (codex #2 — so OutputTruth/preview actually run),
    and write classification.json into the dossier."""
    # [Lane A A-M4] Wire the parsed relay ledger so the INDEPENDENT ProviderLedgerOracle runs
    # alongside the sidecar slice (defense-in-depth: two after-terminal checks, not one). Scope to
    # THIS run's window [run_start, ...] and stamp after_terminal on records past the build-terminal
    # epoch; ran AFTER _collect's grace+release so any straggler post-terminal call is already
    # logged. Live-mode only (relay env set) so deterministic unit tests are unaffected.
    if provider_ledger is None:
        provider_ledger = _provider_ledger_for_run(run)
    classification = classify(
        run.events,
        scenario=scenario,
        run_id=base.name,
        conversation_id=run.conversation_id,
        commit=commit,
        seed=seed,
        workspace_manifest=run.workspace_manifest,
        preview=run.preview,
        autonomous=autonomous,
        provider_ledger=provider_ledger,
        inspect_trace=run.inspect_trace,
        # HARN-2: browser product-harness evidence (None until HARN-1b populates it on
        # CollectedRun; the browser oracles SKIP without it, so headless runs are unaffected).
        product_evidence=getattr(run, "product_evidence", None),
        browser_evidence_paths=set(run.browser_evidence),
        revision_meta={
            "declared_followup_seqs": list(run.declared_followup_seqs),
            "declared_followup_requires_revision": list(run.declared_followup_requires_revision),
            "harness_injected_user_seqs": list(run.harness_injected_user_seqs),
        },
    )
    # Part B traceability: a PASS that REQUIRED auto-resolving an AWAITING_USER_DECISION gate
    # must be DISTINGUISHABLE from a clean PASS — surface the count + the picked options so
    # monitoring can detect "the model asked for choices unexpectedly".
    classification["auto_resolved_decisions"] = len(run.decision_resolutions)
    if run.decision_resolutions:
        classification["decision_resolutions"] = list(run.decision_resolutions)
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(classification, indent=2, sort_keys=True), encoding="utf-8"
    )
    return classification


def _infra_failure_record(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    exc: InfraProbeError,
) -> dict[str, Any]:
    """Write a §9-compliant INFRA_FAILURE record (the run never created a
    conversation). This is the ONLY non-product outcome the runner emits."""
    base = Path(out_root) / run_id
    base.mkdir(parents=True, exist_ok=True)
    record = {
        "status": fc.INFRA_FAILURE,
        "severity": fc.NONE,
        "code": exc.signature_id,
        "first_broken_link": "pre_create_probe -> agent_server/provider",
        "scenario_id": scenario.get("id"),
        "run_id": run_id,
        "conversation_id": None,
        "facts": exc.detail,
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
        "stage": "before_conversation_creation",
    }
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (base / "timeline.md").write_text(
        f"# INFRA_FAILURE (pre-create)\n\nsignature: {exc.signature_id}\ndetail: {exc.detail}\n",
        encoding="utf-8",
    )
    return record


def _invalid_run_record(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    reason: str,
    *,
    code: str = "RUN_INTERRUPTED",
    first_broken_link: str = "drive -> evidence_collection",
    facts: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    timeline_markdown: str | None = None,
) -> dict[str, Any]:
    """Write an INVALID_RUN record (§8) when the harness could not collect complete
    evidence / obtain a verdict to adjudicate — e.g. the agent-server became UNREACHABLE
    mid-run (RUN_INTERRUPTED), or the terminal wait was cut off by the hard cap while the
    build was STILL PROGRESSING (RUN_TIMEOUT_WHILE_PROGRESSING, Bug 15). This is NOT a
    product FAIL (we can't prove a product outcome) and NOT INFRA_FAILURE (pre-create only,
    §9); it blocks promotion AND signals §17 to re-run rather than recording a false fail."""
    base = Path(out_root) / run_id
    base.mkdir(parents=True, exist_ok=True)
    record_facts: dict[str, Any] = {"reason": reason, **(facts or {})}
    record = {
        "status": fc.INVALID_RUN,
        "severity": fc.NONE,
        "code": code,
        "first_broken_link": first_broken_link,
        "scenario_id": scenario.get("id"),
        "run_id": run_id,
        "conversation_id": conversation_id,
        "facts": record_facts,
        "required_evidence_present": False,
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
    }
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (base / "timeline.md").write_text(
        timeline_markdown or f"# INVALID_RUN ({code})\n\nreason: {reason}\n",
        encoding="utf-8",
    )
    return record


def _invalidation_conversation_id(
    facts: dict[str, Any] | None, client: DiscoApiClient
) -> str | None:
    """Resolve the failed run's exact conversation ID for the evidence freeze.

    The exception's own facts win (they are bound to the failing collection);
    the client's last-created conversation is the fallback for exceptions that
    carry none. Never guesses across runs: ``last_conversation_id`` is reset by
    ``run_once`` before each create.
    """

    raw = (facts or {}).get("conversation_id")
    if isinstance(raw, str) and raw:
        return raw
    return client.last_conversation_id


async def _freeze_invalidation_evidence(
    client: DiscoApiClient,
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    *,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str,
    repo_dirty: bool,
    kernel: str,
    started_at: str | None,
    seed: int | None,
    conversation_id: str | None,
    invalidation_code: str,
    invalidation_reason: str,
) -> tuple[dict[str, Any], str | None]:
    """Bounded best-effort dossier freeze for an INVALID_RUN return path.

    Every post-create invalidation must attempt this before returning: the
    durable events, final state, the in-process lossless inspect aggregate,
    the thrash-monitor record, and this run's provider-ledger slice are all
    still reachable through the client's idempotent collection APIs even when
    strict workspace capture failed. Reuses the H262 hard-cap freeze
    architecture (`assemble_dossier`, including its hash-locked manifest)
    rather than inventing a parallel dossier format.

    Never raises and never changes the caller's classification semantics: the
    return value is (`facts` update, timeline markdown). Each slice is
    collected independently; a missing slice is DISCLOSED in the
    ``evidence_freeze`` facts (present/missing lists with the failure kind),
    never fabricated and never silently omitted. Nonterminal workspace bytes
    are deliberately NOT captured here — a diagnostic freeze must never
    produce something a FINISHED deliverable/output oracle could mistake for
    terminal workspace truth (`workspace-manifest.json` carries an explicit
    ``not_collected_invalidation`` capture status instead).
    """

    freeze: dict[str, Any] = {
        "attempted": True,
        "present": [],
        "missing": [],
        "errors": {},
    }
    if not conversation_id:
        freeze["attempted"] = False
        freeze["missing"] = ["events", "state_final", "inspect_trace", "provider_ledger"]
        freeze["errors"]["conversation_id"] = "unknown before failure"
        return {"evidence_freeze": freeze}, None

    def _note(slice_name: str, present: bool, error: BaseException | None = None) -> None:
        (freeze["present"] if present else freeze["missing"]).append(slice_name)
        if error is not None:
            freeze["errors"][slice_name] = type(error).__name__

    events: list[dict[str, Any]] = []
    try:
        events = client.collect_events(conversation_id)
        _note("events", True)
    except Exception as exc:  # noqa: BLE001 — disclosed, never fabricated
        _note("events", False, exc)

    capture_marker = {
        "_capture": {
            "status": "not_collected_invalidation",
            "invalidation_code": invalidation_code,
            "reason": invalidation_reason,
        }
    }
    state_final: dict[str, Any] = dict(capture_marker)
    try:
        state_final = await client.get_state(conversation_id)
        _note("state_final", True)
    except Exception as exc:  # noqa: BLE001
        _note("state_final", False, exc)

    inspect_trace: dict[str, Any] | None = None
    try:
        await client.finish_inspect_collection(conversation_id)
        inspect_trace = await client.collect_inspect_trace(conversation_id)
        _note("inspect_trace", inspect_trace is not None)
    except Exception as exc:  # noqa: BLE001
        _note("inspect_trace", False, exc)

    try:
        thrash_monitor = client.live_thrash_monitor
    except Exception:  # noqa: BLE001
        thrash_monitor = {}

    frozen = CollectedRun(
        conversation_id=conversation_id,
        events=events,
        state_initial=dict(capture_marker),
        state_final=state_final,
        workspace_manifest={"files": {}, **capture_marker},
        preview=None,
        inspect_trace=inspect_trace,
        thrash_monitor=thrash_monitor,
        timeline=[
            f"invalidation evidence freeze for {invalidation_code}: {invalidation_reason}",
            f"slices present={freeze['present']} missing={freeze['missing']}",
        ],
    )
    provider_ledger: list[dict[str, Any]] | None = None
    try:
        provider_ledger = _provider_ledger_for_run(frozen)
        _note("provider_ledger", provider_ledger is not None)
    except Exception as exc:  # noqa: BLE001
        _note("provider_ledger", False, exc)

    try:
        assemble_dossier(
            out_root,
            run_id,
            scenario,
            frozen,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            started_at=started_at,
            seed=seed,
            provider_ledger=provider_ledger,
        )
        freeze["dossier_written"] = True
    except Exception as exc:  # noqa: BLE001 — the freeze may not mask the invalidation
        freeze["dossier_written"] = False
        freeze["errors"]["dossier"] = type(exc).__name__
        return {"evidence_freeze": freeze}, None
    return {"evidence_freeze": freeze}, _timeline_md(scenario, frozen)


# ---- runner hygiene: kill an abandoned conversation -------------------------


async def _release_conversation(
    client: DiscoApiClient, cid: str | None, timeline: list[str] | None = None
) -> None:
    """Tear down the conversation the runner is DONE with — INCLUDING its sandbox + egress
    sidecar CONTAINERS — so nothing leaks between runs. Called from run_once's `finally` AFTER
    evidence has been collected + frozen (the §6 read of the terminal events already happened in
    drive_scenario), so the release never races the dossier.

    [REL-4] We release the conversation even when it is already TERMINAL. MEASURED on the
    production podman backend: a single FINISHED build leaves TWO live containers — the sandbox
    `disco-sbx-sbx_<id>` and the egress sidecar `disco-egr-sbx_<id>` — both still "Up" after the
    terminal event (the sandbox is not destroyed at terminal; it lingers to the idle-TTL). Across
    a 95-run soak that is ~190 orphan containers and FAILS the 0-orphans acceptance. `POST /kill`
    on the terminal conversation tears down BOTH containers (measured: 2 → 0), revokes the provider
    token, and stops the preview, and is IDEMPOTENT + BEST-EFFORT (any error — server gone, already
    torn down — is swallowed so teardown never turns a real verdict into a crash). The FINISHED
    workspace snapshot is already taken (kick post-run) and evidence is frozen, so releasing here
    is safe."""
    if not cid:
        return
    # Every exit path, including cancellation and evidence-collection errors,
    # reaches this fallback.  The idempotent owned inspect finalizer is ordered
    # against the kill by the conversation's ACTIVITY, not by convenience: a
    # terminal conversation can emit no further trace events, so inspect is
    # frozen BEFORE the cleanup kill can release the runtime that owns the
    # bounded source ring; an ACTIVE/unknown conversation is stopped FIRST so
    # the final sample can observe any tail events the still-running model
    # emitted while the kill landed, and an unconfirmed stop taints continuity
    # instead of silently claiming losslessness.
    status = ""
    with contextlib.suppress(Exception):
        status = DiscoApiClient._status_of(await client.get_state(cid))
    if status in TERMINAL_STATES:
        with contextlib.suppress(Exception):
            await client.finish_inspect_collection(cid)
        # Release even though the run is TERMINAL — the orphan-container teardown.
        # kill is idempotent + suppressed so this can never crash a real verdict.
        with contextlib.suppress(Exception):
            resp = await client.kill(cid)
            if timeline is not None:
                timeline.append(
                    f"released terminal conversation {cid} — sandbox + sidecar containers "
                    f"destroyed (was {status or 'unknown'}, http {resp.get('http_status')})"
                )
        return
    kill_confirmed = False
    with contextlib.suppress(Exception):
        resp = await client.kill(cid)
        kill_confirmed = (
            200 <= int(resp.get("http_status", 0)) < 300
            and resp.get("killed") is True
            and DiscoApiClient._status_of(resp.get("state") or {}) == "IDLE"
        )
        if timeline is not None:
            timeline.append(
                f"killed abandoned conversation {cid} — sandbox + sidecar containers "
                f"destroyed (was {status or 'unknown'}, http {resp.get('http_status')})"
            )
    if not kill_confirmed:
        with contextlib.suppress(Exception):
            client.note_inspect_stop_unconfirmed(cid)
    with contextlib.suppress(Exception):
        await client.finish_inspect_collection(cid)


_DISCO_CONTAINER_PREFIXES = ("disco-sbx-", "disco-egr-")
_DISCO_VOLUME_PREFIXES = ("disco-ws-", "pmx-ws-")


def _live_disco_container_names() -> list[str] | None:
    """RUNNING disco sandbox + egress-sidecar container names, or None if unmeasurable."""
    try:
        out = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}}"],  # RUNNING only (no -a)
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names: list[str] = []
    for line in out.stdout.splitlines():
        for name in line.split(","):
            clean = name.strip()
            if clean.startswith(_DISCO_CONTAINER_PREFIXES):
                names.append(clean)
    return names


def _live_disco_container_count() -> int | None:
    """[REL-5 / Lane A A-B5/M3] Count RUNNING disco sandbox + egress-sidecar containers via the
    podman CLI — the host-side orphan signal for the CleanupOracle on the LOCAL PODMAN iteration
    backend. Uses `podman ps` (RUNNING only) NOT `podman ps -a`: `-a` includes exited-but-not-yet-
    pruned containers, so a correctly-torn-down box still matched the prefix and inflated the count
    on either side of the baseline→after delta (a real leak could net to 0, or a prune could push
    after<baseline). Counting only RUNNING containers measures actual liveness. Returns None if
    podman is unavailable (count UNKNOWN, never faked 0). The gVisor FINAL REL-6 run needs the
    gVisor-equivalent probe (tracked)."""
    names = _live_disco_container_names()
    return None if names is None else len(names)


def _podman_volume_names(args: list[str]) -> list[str] | None:
    try:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names: list[str] = []
    for line in out.stdout.splitlines():
        clean = line.strip()
        if clean:
            names.append(clean)
    return names


def _disco_volume_names() -> list[str] | None:
    """All disco workspace volumes, independent of whether their container is running."""
    names = _podman_volume_names(["podman", "volume", "ls", "--format", "{{.Name}}"])
    if names is None:
        return None
    return [name for name in names if name.startswith(_DISCO_VOLUME_PREFIXES)]


def _dangling_volume_names() -> set[str] | None:
    """Global dangling Podman volumes for unnamed-volume fallback accounting."""
    names = _podman_volume_names(
        ["podman", "volume", "ls", "--filter", "dangling=true", "--format", "{{.Name}}"]
    )
    return None if names is None else set(names)


def _scoped_disco_container_count(names: list[str], sandbox_instance_ids: list[str]) -> int:
    """Count live disco containers whose name embeds one of this conversation's sandbox ids."""
    ids = [sid for sid in sandbox_instance_ids if sid]
    return sum(1 for name in names if any(sid in name for sid in ids))


def _scoped_disco_volume_count(names: list[str], sandbox_instance_ids: list[str]) -> int:
    """Count disco workspace volumes whose name embeds one of this conversation's sandbox ids."""
    ids = [sid for sid in sandbox_instance_ids if sid]
    return sum(1 for name in names if any(sid in name for sid in ids))


def _new_dangling_volume_count(
    baseline: set[str] | None,
    after: set[str] | None,
    *,
    exclude_disco_named: bool,
) -> int | None:
    """Count volumes newly dangling since run start.

    Named disco workspace volumes are counted by sandbox id when possible; exclude
    them there to avoid double-counting the same leaked volume as both named and
    dangling.
    """
    if baseline is None or after is None:
        return None
    created = after - baseline
    if exclude_disco_named:
        created = {name for name in created if not name.startswith(_DISCO_VOLUME_PREFIXES)}
    return len(created)


def _extract_sandbox_instance_ids(*payloads: Any) -> list[str]:
    """Harvest sandbox ids from state/kill response shapes without depending on one version."""
    ids: list[str] = []

    def add(raw: Any) -> None:
        if isinstance(raw, str):
            sid = raw.strip()
            if sid and not sid.startswith("session-") and sid not in ids:
                ids.append(sid)
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(item)

    def scan(value: Any, *, sandbox_context: bool = False) -> None:
        if not isinstance(value, dict):
            return
        add(value.get("sandbox_instance_id"))
        add(value.get("sandbox_instance_ids"))
        if sandbox_context:
            add(value.get("instance_id"))
            add(value.get("instance_ids"))
            add(value.get("id"))
        scan(value.get("state"))
        scan(value.get("extras"))
        scan(value.get("sandbox"), sandbox_context=True)

    for payload in payloads:
        scan(payload)
    return ids


def _relay_log_path() -> str | None:
    """[codex] ONE canonical resolver for the MiniMax relay-ledger path — the SINGLE source for
    BOTH reading the ledger AND the `_live_measure` fail-closed gate, so the rule can never key on
    a different env var than the one that actually carries the ledger. The relay WRITES
    MINIMAX_RELAY_LOG (minimax_relay.py); direct drivers write DISCO_PROVIDER_LEDGER. We also
    accept the legacy PMX_RELAY_LOG (Playwright live specs) and DISCO_RELAY_LOG so no driver path
    can drift into a silent SKIP-as-PASS."""
    for name in (
        "DISCO_PROVIDER_LEDGER",
        "MINIMAX_RELAY_LOG",
        "PMX_RELAY_LOG",
        "DISCO_RELAY_LOG",
    ):
        v = os.environ.get(name)
        if v:
            return v
    return None


def _event_epoch(e: dict[str, Any]) -> float | None:
    """Epoch of one event from its ISO-8601 UTC timestamp ('2026-06-30T16:59:28.755769Z')."""
    from datetime import datetime

    ts = e.get("timestamp") or e.get("created_at")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# [Lane A A-M2] The BUILD-terminal set for anchoring provider-after-terminal. EXCLUDES IDLE: IDLE is
# both the pre-kick resting state AND the post-kill state, so anchoring on a (later) IDLE would push
# the boundary PAST real post-FINISHED calls and hide them. Includes VERIFIED (a clean terminal the
# LifecycleOracle accepts but TERMINAL_STATES omits) so a VERIFIED run is adjudicable, not INVALID.
_BUILD_TERMINAL_STATUSES = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK"})


def _terminal_status_epoch(
    events: list[dict[str, Any]], *, allow_killed_idle: bool = False
) -> float | None:
    """[codex/Lane A] Epoch of the LAST `status` event whose status is a BUILD terminal
    (_BUILD_TERMINAL_STATUSES — NOT IDLE) — the true instant the build went terminal. NOT the max
    timestamp across ALL events (a durable event appended AFTER the terminal status would push the
    anchor past a real post-terminal provider call), and NOT anchored on IDLE (the rest/kill state).
    None if no build-terminal status event is present."""
    best: float | None = None
    first_confirmed_kill: float | None = None
    for e in events:
        if e.get("kind") != "status":
            continue
        # run.events are raw DB rows {seq,kind,created_at,payload(JSON string)} — the status lives
        # INSIDE `payload`, not as a top-level field. Accept both shapes (flattened + DB-row).
        normalized_status, _detail = _status_value_and_detail(e)
        ep = _event_epoch(e)
        if ep is None:
            continue
        if normalized_status in _BUILD_TERMINAL_STATUSES and (best is None or ep > best):
            best = ep
        elif (
            allow_killed_idle
            and normalized_status == "IDLE"
            and _detail == "killed"
            and (first_confirmed_kill is None or ep < first_confirmed_kill)
        ):
            # The first kill is the monitor's spend stop.  Later idempotent cleanup
            # kills must not move the provider-after-terminal boundary forward.
            first_confirmed_kill = ep
    # When the caller has independently proven a monitor-owned kill, that kill is
    # the spend-stop boundary even if the conversation history contains an older
    # work terminal from a prior follow-up phase.  Ordinary terminal runs call with
    # allow_killed_idle=False and retain the latest work-terminal behavior.
    return first_confirmed_kill if first_confirmed_kill is not None else best


def _min_event_epoch(events: list[dict[str, Any]]) -> float | None:
    """Epoch of the build's FIRST event (run start) — the lower bound of this run's relay window."""
    best: float | None = None
    for e in events:
        ep = _event_epoch(e)
        if ep is not None and (best is None or ep < best):
            best = ep
    return best


async def _collect_terminal_cleanup_evidence(
    client: DiscoApiClient,
    cid: str,
    run: Any,
    *,
    baseline_containers: int | None,
    relay_log: str | None,
    timeline: list[str],
    baseline_dangling_volumes: set[str] | None = None,
    grace_s: float = 8.0,
    allow_global_cleanup_fallback: bool = True,
) -> dict[str, Any]:
    """[REL-5] Make terminal cleanup ADJUDICATED instead of SKIPPED on the headless soak. The
    build has reached terminal and its §6 evidence is already frozen into `run`; here we measure
    the post-terminal provider calls (grace window on the relay ledger), RELEASE the conversation
    (destroy the sandbox + egress-sidecar containers), and verify zero orphans — populating the
    lifecycle / sidecar / cleanup product_evidence slices from REAL signals so the reliability
    oracles RUN. Every signal is measured truthfully: an unmeasurable signal is OMITTED (its oracle
    skips), never stubbed to a passing value."""
    ev: dict[str, Any] = dict(getattr(run, "product_evidence", None) or {})

    # lifecycle — the terminal + status path (from the frozen final state + the drive timeline).
    terminal = ""
    with contextlib.suppress(Exception):
        terminal = DiscoApiClient._status_of(getattr(run, "state_final", {}) or {})
    if terminal:
        ev["lifecycle"] = {
            "terminal": terminal,
            "statuses": list(getattr(run, "timeline", []) or []),
        }

    # provider-after-terminal — [codex] anchor on the TERMINAL STATUS event's epoch (from the FROZEN
    # run.events), then count relay calls whose ts is strictly AFTER it. A post-hoc baseline or a
    # max-of-all-events anchor would hide a call made between terminal and a later durable event.
    # LIVENESS GUARD: trust "0 calls after terminal" ONLY if the relay actually captured THIS run —
    # i.e. it logged >=1 call inside the run window [run_start, terminal]. An empty/stale relay log
    # (relay not wired) yields a meaningless 0; we OMIT the slice instead (-> fail-closed INVALID in
    # live mode) so a false 0 can't classify green. Release happens AFTER the grace window so the
    # token is live during the audited window.
    events = getattr(run, "events", []) or []
    terminal_epoch = (
        getattr(run, "diagnostic_stop_epoch", None)
        if getattr(run, "diagnostic_stop", None) == "progressing_hard_cap"
        else _terminal_status_epoch(events, allow_killed_idle=_confirmed_live_thrash_stop(run))
    )
    run_start_epoch = _min_event_epoch(events)
    calls_after: int | None = None
    if (
        relay_log
        and os.path.exists(relay_log)
        and terminal_epoch is not None
        and run_start_epoch is not None
    ):
        await asyncio.sleep(grace_s)
        try:
            with open(relay_log, encoding="utf-8") as f:
                recs = parse_relay_log(f.read())
            _ts_recs = [
                (float(r["ts"]), bool(r.get("has_tools", True)))
                for r in recs
                if isinstance(r.get("ts"), (int, float)) and record_applies_to_conversation(r, cid)
            ]
            calls_during_run = sum(1 for t, _ in _ts_recs if run_start_epoch <= t <= terminal_epoch)
            if calls_during_run > 0:  # relay PROVEN live for this run → trust the after-count
                # [REL-5b] count only BUILD-driver (tool-bearing) calls after terminal. A tool-less
                # SUMMARIZER call — the async auto-title fired off kick() — is NOT a build runaway,
                # so it must not classify a clean FINISHED run as SIDECAR_NOT_STOPPED. A real
                # post-terminal driver runaway carries the tool catalog (has_tools=True) → still
                # flagged.
                calls_after = sum(1 for t, ht in _ts_recs if t > terminal_epoch and ht)
            else:
                # Relay captured nothing for this run: 0-after is meaningless, so omit it
                # (fail-closed).
                calls_after = None
                timeline.append(
                    "REL-5: relay ledger captured 0 calls in this run's window — provider-after-"
                    "terminal not adjudicable (relay not wired for this run)"
                )
        except Exception:
            calls_after = None

    # release — destroy the sandbox + sidecar containers and workspace volumes
    # (REL-4/REL-6 path), then measure orphans.  A diagnostic/live-thrash stop may
    # already have durably killed the conversation.  Treat that status event as the
    # release proof instead of issuing redundant kill requests; otherwise perform the
    # release here.  Once release is proven, disarm run_once's finally fallback.  A
    # failed/unproven release deliberately leaves it armed for one best-effort retry.
    release_resp: dict[str, Any] = {}
    diagnostic_release_reused = bool(
        getattr(run, "diagnostic_stop", None) == "progressing_hard_cap"
        and getattr(run, "diagnostic_release_confirmed", False)
    )
    live_thrash_release_reused = _confirmed_live_thrash_stop(run)
    released_ok = diagnostic_release_reused or live_thrash_release_reused
    if diagnostic_release_reused:
        timeline.append("REL-5 cleanup observed the diagnostic stop's durable killed state")
    elif live_thrash_release_reused:
        timeline.append("REL-5 cleanup observed the live-thrash monitor's durable killed state")
    else:
        with contextlib.suppress(Exception):
            release_resp = await client.kill(cid)
            http_status = int(release_resp.get("http_status", 0))
            released_ok = (
                200 <= http_status < 300
                and release_resp.get("killed") is True
                and DiscoApiClient._status_of(release_resp.get("state") or {}) == "IDLE"
            )
            if released_ok:
                timeline.append(
                    f"REL-5 released {cid} for cleanup adjudication (http {http_status})"
                )
            else:
                timeline.append(
                    f"REL-5 cleanup release was not acknowledged as killed IDLE "
                    f"(http {http_status})"
                )
    if released_ok and getattr(client, "last_conversation_id", None) == cid:
        client.last_conversation_id = None
    await asyncio.sleep(4.0)
    after_container_names = _live_disco_container_names()
    after_volume_names = _disco_volume_names()
    after_dangling_volumes = _dangling_volume_names()

    # sidecar — stopped_at_terminal iff the release tore the containers down; provider calls only if
    # we could actually measure them (else the slice is omitted → oracle skips, honest).
    if calls_after is not None:
        ev["sidecar"] = {
            "stopped_at_terminal": released_ok,
            "provider_calls_after_terminal": calls_after,
        }

    # cleanup — preferred path: count only resources whose names embed THIS conversation's sandbox
    # instance id(s). Parallel lanes may have live sandboxes after our release; those are not this
    # run's orphans. Volumes are RUNNING-independent: a leaked `disco-ws-{sandbox_id}` still counts
    # after the container is gone. If the server version cannot expose ids, keep the old global
    # baseline delta as the serial/fail-closed fallback; for unnamed image VOLUME leaks, use the
    # newly-dangling volume delta from the run window.
    sandbox_ids = _extract_sandbox_instance_ids(
        getattr(run, "state_final", {}) or {},
        release_resp,
        ev.get("diagnostic_stop"),
    )
    if sandbox_ids and after_container_names is not None:
        container_orphans = _scoped_disco_container_count(after_container_names, sandbox_ids)
        named_volume_orphans = (
            _scoped_disco_volume_count(after_volume_names, sandbox_ids)
            if after_volume_names is not None
            else None
        )
        unnamed_volume_orphans = _new_dangling_volume_count(
            baseline_dangling_volumes, after_dangling_volumes, exclude_disco_named=True
        )
        if not allow_global_cleanup_fallback:
            # A parallel trial cannot own a host-global unnamed-volume delta.
            # Keep exact sandbox-id named volumes, but refuse contaminated
            # attribution from siblings that may finish concurrently.
            unnamed_volume_orphans = None
        volume_measurement_complete = named_volume_orphans is not None and (
            not allow_global_cleanup_fallback or unnamed_volume_orphans is not None
        )
        if volume_measurement_complete:
            volume_orphans = int(named_volume_orphans or 0) + int(unnamed_volume_orphans or 0)
            total_orphans = container_orphans + volume_orphans
            ev["cleanup"] = {
                "orphans": total_orphans,
                "workspace_released": released_ok and total_orphans == 0,
                "scope": "conversation",
                "container_orphans": container_orphans,
                "volume_orphans": volume_orphans,
                "volume_scope": "conversation",
            }
        else:
            timeline.append(
                "REL-5 scoped cleanup evidence incomplete: an applicable volume "
                "probe was unavailable; omitted cleanup adjudication"
            )
    elif (
        allow_global_cleanup_fallback
        and baseline_containers is not None
        and after_container_names is not None
    ):
        after_containers = len(after_container_names)
        container_orphans = max(0, after_containers - baseline_containers)
        volume_orphans = _new_dangling_volume_count(
            baseline_dangling_volumes, after_dangling_volumes, exclude_disco_named=False
        )
        if volume_orphans is not None:
            total_orphans = container_orphans + volume_orphans
            ev["cleanup"] = {
                "orphans": total_orphans,
                "workspace_released": released_ok and total_orphans == 0,
                "scope": "global",
                "container_orphans": container_orphans,
                "volume_orphans": volume_orphans,
                "volume_scope": "global_dangling",
            }
        else:
            timeline.append(
                "REL-5 global cleanup evidence incomplete: dangling-volume probe "
                "was unavailable; omitted cleanup adjudication"
            )
    elif not allow_global_cleanup_fallback:
        timeline.append(
            "REL-5 cleanup attribution unavailable: parallel run exposed no sandbox "
            "instance id; refused contaminated global container delta"
        )

    with contextlib.suppress(Exception):
        run.product_evidence = ev  # CollectedRun is a plain dataclass — attach the populated slices
    return ev


# ---- one full run -----------------------------------------------------------


async def run_once(
    client: DiscoApiClient,
    scenario: dict[str, Any],
    *,
    run_id: str,
    out_root: str | Path,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str = "",
    repo_dirty: bool = False,
    kernel: str = "disco",
    timeout_s: float,
    hard_cap_s: float = _DEFAULT_HARD_CAP_S,
    require_inspect_trace: bool = False,
    parallel_workers: int = 1,
    seed: int | None = None,
) -> dict[str, Any]:
    run_started_at = datetime.now(UTC).isoformat()
    # §9 pre-create infra gate (the ONLY infra source). No conversation exists yet, so a
    # failure here needs no teardown (returns before the try/finally below).
    try:
        await client.pre_create_probe(model)
    except InfraProbeError as exc:
        return _infra_failure_record(out_root, run_id, scenario, exc)

    # This run has not created a conversation yet — reset the tracked cid so the finally
    # only ever kills the conversation THIS run created (the client is reused across a
    # batch of iterations).
    client.last_conversation_id = None
    client.scenario_evidence = {}
    # [REL-5] pre-run orphan baseline. The scoped sandbox-id path does not need this, but older
    # servers that cannot expose ids still fall back to the serial-era global delta.
    baseline_containers = _live_disco_container_count()
    baseline_dangling_volumes = _dangling_volume_names()
    try:
        # A mid-run transport loss (the shared server crashed / network dropped AFTER
        # create) is not adjudicable — degrade to INVALID_RUN instead of a raw traceback,
        # so a batch keeps going and the outcome is recorded honestly.
        try:
            run = await drive_scenario(
                client,
                scenario,
                model=model,
                autonomous=autonomous,
                timeout_s=timeout_s,
                hard_cap_s=hard_cap_s,
            )
        except InconclusiveRunError as exc:
            # Bug 15: the build was STILL PROGRESSING when the hard cap hit — the runner could
            # not obtain a terminal verdict. INVALID_RUN (inconclusive), NEVER a product fail.
            frozen = exc.collected_run
            if frozen is not None:
                try:
                    frozen.product_evidence = await _collect_terminal_cleanup_evidence(
                        client,
                        frozen.conversation_id,
                        frozen,
                        baseline_containers=baseline_containers,
                        relay_log=_relay_log_path(),
                        timeline=frozen.timeline,
                        baseline_dangling_volumes=baseline_dangling_volumes,
                        allow_global_cleanup_fallback=parallel_workers <= 1,
                    )
                except Exception as cleanup_exc:  # noqa: BLE001 — retain partial dossier
                    frozen.timeline.append(
                        f"progress hard-cap cleanup evidence failed: {type(cleanup_exc).__name__}"
                    )
                provider_ledger = _provider_ledger_for_run(frozen)
                assemble_dossier(
                    out_root,
                    run_id,
                    scenario,
                    frozen,
                    model=model,
                    autonomous=autonomous,
                    commit=commit,
                    repo_revision=repo_revision,
                    repo_dirty=repo_dirty,
                    kernel=kernel,
                    started_at=run_started_at,
                    seed=seed,
                    provider_ledger=provider_ledger,
                )
            hardcap_facts = exc.facts
            hardcap_cid = frozen.conversation_id if frozen is not None else None
            hardcap_timeline = _timeline_md(scenario, frozen) if frozen is not None else None
            if frozen is None:
                # No collected run survived the hard cap — attempt the same bounded
                # best-effort freeze every other invalidation path performs (F3)
                # instead of returning classification+timeline only.
                hardcap_cid = _invalidation_conversation_id(exc.facts, client)
                freeze_facts, hardcap_timeline = await _freeze_invalidation_evidence(
                    client,
                    out_root,
                    run_id,
                    scenario,
                    model=model,
                    autonomous=autonomous,
                    commit=commit,
                    repo_revision=repo_revision,
                    repo_dirty=repo_dirty,
                    kernel=kernel,
                    started_at=run_started_at,
                    seed=seed,
                    conversation_id=hardcap_cid,
                    invalidation_code=fc.RUN_TIMEOUT_WHILE_PROGRESSING,
                    invalidation_reason=exc.reason,
                )
                hardcap_facts = {**exc.facts, **freeze_facts}
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.RUN_TIMEOUT_WHILE_PROGRESSING,
                first_broken_link="terminal_wait -> no_terminal_before_hard_cap",
                facts=hardcap_facts,
                conversation_id=hardcap_cid,
                timeline_markdown=hardcap_timeline,
            )
        except FollowupPickupError as exc:
            # H1/V2: an after-terminal follow-up was never picked up by the engine (the
            # finalization dead-window) even after a re-kick. This is a HARNESS SEQUENCING
            # failure, not a product verdict — INVALID_RUN so §17 re-runs it. We must NOT drive
            # on the stale terminal (V1's silent-proceed is what collapsed the follow-ups).
            cid = _invalidation_conversation_id(exc.facts, client)
            freeze_facts, freeze_timeline = await _freeze_invalidation_evidence(
                client,
                out_root,
                run_id,
                scenario,
                model=model,
                autonomous=autonomous,
                commit=commit,
                repo_revision=repo_revision,
                repo_dirty=repo_dirty,
                kernel=kernel,
                started_at=run_started_at,
                seed=seed,
                conversation_id=cid,
                invalidation_code=fc.RUN_INTERRUPTED,
                invalidation_reason=exc.reason,
            )
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.RUN_INTERRUPTED,
                first_broken_link="followup_send -> no_pickup_before_bound",
                facts={**exc.facts, **freeze_facts},
                conversation_id=cid,
                timeline_markdown=freeze_timeline,
            )
        except CancelMissedWindowError as exc:
            cid = _invalidation_conversation_id(exc.facts, client)
            freeze_facts, freeze_timeline = await _freeze_invalidation_evidence(
                client,
                out_root,
                run_id,
                scenario,
                model=model,
                autonomous=autonomous,
                commit=commit,
                repo_revision=repo_revision,
                repo_dirty=repo_dirty,
                kernel=kernel,
                started_at=run_started_at,
                seed=seed,
                conversation_id=cid,
                invalidation_code=fc.CANCEL_MISSED_WINDOW,
                invalidation_reason=exc.reason,
            )
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.CANCEL_MISSED_WINDOW,
                first_broken_link="cancel_at -> terminal_before_kill",
                facts={**exc.facts, **freeze_facts},
                conversation_id=cid,
                timeline_markdown=freeze_timeline,
            )
        except SnapshotNotReadyError as exc:
            # The host workspace snapshot never settled on the build's AGENT-FINAL state
            # within the snapshot-wait budget (a slow/failed flush, or a multi-revision build
            # whose later bytes hadn't flushed). FAIL-FAST → INVALID_RUN rather than launder a
            # stale capture into a false ARTIFACT_TRUTH_MISMATCH or a silent stale PASS; §17
            # re-runs it. Bounded — never hangs. The snapshot verdict stays the downstream
            # evidence fact it is; the durable events / inspect aggregate / provider slice the
            # harness already holds are frozen first instead of being discarded (finding F3).
            cid = _invalidation_conversation_id(exc.facts, client)
            freeze_facts, freeze_timeline = await _freeze_invalidation_evidence(
                client,
                out_root,
                run_id,
                scenario,
                model=model,
                autonomous=autonomous,
                commit=commit,
                repo_revision=repo_revision,
                repo_dirty=repo_dirty,
                kernel=kernel,
                started_at=run_started_at,
                seed=seed,
                conversation_id=cid,
                invalidation_code=fc.WORKSPACE_SNAPSHOT_NOT_READY,
                invalidation_reason=exc.reason,
            )
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.WORKSPACE_SNAPSHOT_NOT_READY,
                first_broken_link="snapshot_flush -> snapshot_behind_agent_final_state",
                facts={**exc.facts, **freeze_facts},
                conversation_id=cid,
                timeline_markdown=freeze_timeline,
            )
        except BrowserEvidenceCollectionError as exc:
            cid = _invalidation_conversation_id(exc.facts, client)
            freeze_facts, freeze_timeline = await _freeze_invalidation_evidence(
                client,
                out_root,
                run_id,
                scenario,
                model=model,
                autonomous=autonomous,
                commit=commit,
                repo_revision=repo_revision,
                repo_dirty=repo_dirty,
                kernel=kernel,
                started_at=run_started_at,
                seed=seed,
                conversation_id=cid,
                invalidation_code=fc.MISSING_REQUIRED_EVIDENCE,
                invalidation_reason=exc.reason,
            )
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.MISSING_REQUIRED_EVIDENCE,
                first_broken_link="browser_observation -> durable_screenshot_evidence",
                facts={**exc.facts, **freeze_facts},
                conversation_id=cid,
                timeline_markdown=freeze_timeline,
            )
        except Exception as exc:  # noqa: BLE001 — surface the real reason as INVALID_RUN
            cid = _invalidation_conversation_id(None, client)
            freeze_facts, freeze_timeline = await _freeze_invalidation_evidence(
                client,
                out_root,
                run_id,
                scenario,
                model=model,
                autonomous=autonomous,
                commit=commit,
                repo_revision=repo_revision,
                repo_dirty=repo_dirty,
                kernel=kernel,
                started_at=run_started_at,
                seed=seed,
                conversation_id=cid,
                invalidation_code=fc.RUN_INTERRUPTED,
                invalidation_reason=f"{type(exc).__name__}: {exc}",
            )
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                f"{type(exc).__name__}: {exc}",
                facts=freeze_facts,
                conversation_id=cid,
                timeline_markdown=freeze_timeline,
            )

        if require_inspect_trace:
            trace = run.inspect_trace or {}
            aggregation = trace.get("aggregation")
            routing = trace.get("routing_decisions")
            spans = trace.get("spans")
            agent_spans = (
                [
                    span
                    for span in spans
                    if isinstance(span, dict) and span.get("span") == "agent.step"
                ]
                if isinstance(spans, list)
                else []
            )
            terminal_driver_preflight = _is_terminal_driver_preflight_trace(
                run, trace, routing, agent_spans
            )
            terminal_sandbox_preflight = _is_terminal_sandbox_preflight_trace(
                run, trace, routing, agent_spans, scenario
            )
            terminal_preloop = terminal_driver_preflight or terminal_sandbox_preflight
            # The gate re-derives validity from the canonical events instead of
            # trusting the caller-owned lossless/finalized/dropped assertions —
            # a forged or tampered aggregate must fail closed HERE, not surface
            # later as a bogus analyzer input.  The shared validator lives next
            # to the aggregation so producer and gate cannot drift.
            aggregate_violations = inspect_aggregate_violations(
                trace, conversation_id=str(run.conversation_id or "")
            )
            missing_trace_parts = [
                name
                for name, present in (
                    (
                        "internally consistent inspect aggregate",
                        not aggregate_violations,
                    ),
                    (
                        "lossless inspect aggregation",
                        isinstance(aggregation, dict)
                        and aggregation.get("lossless") is True
                        and aggregation.get("finalized") is True
                        and trace.get("dropped_event_count") == 0,
                    ),
                    ("routing_decisions", isinstance(routing, list) and bool(routing)),
                    ("agent.step spans", bool(agent_spans) or terminal_preloop),
                )
                if not present
            ]
            if missing_trace_parts:
                # H262: the validity gate must remain fail-closed, but the evidence that
                # explains the gap must survive isolated-stack teardown.  Freeze the
                # already-collected run (including an incomplete inspect trace, events,
                # state/workspace evidence, and any scoped provider ledger) before writing
                # the INVALID_RUN verdict.  Previously this early return kept only
                # classification.json + a one-line timeline and discarded the evidence
                # needed to root-cause the missing trace.
                run.timeline.append(
                    "required inspect evidence incomplete: missing "
                    + ", ".join(missing_trace_parts)
                )
                provider_ledger = _provider_ledger_for_run(run)
                assemble_dossier(
                    out_root,
                    run_id,
                    scenario,
                    run,
                    model=model,
                    autonomous=autonomous,
                    commit=commit,
                    repo_revision=repo_revision,
                    repo_dirty=repo_dirty,
                    kernel=kernel,
                    started_at=run_started_at,
                    seed=seed,
                    provider_ledger=provider_ledger,
                )
                return _invalid_run_record(
                    out_root,
                    run_id,
                    scenario,
                    "required per-conversation inspect trace was absent or incomplete",
                    code=fc.MISSING_REQUIRED_EVIDENCE,
                    first_broken_link="model_request -> inspect_trace",
                    facts={
                        "missing_trace_parts": missing_trace_parts,
                        "inspect_aggregate_violations": aggregate_violations,
                        "inspect_aggregation": aggregation
                        if isinstance(aggregation, dict)
                        else None,
                    },
                    conversation_id=run.conversation_id,
                    timeline_markdown=_timeline_md(scenario, run),
                )
            if terminal_driver_preflight:
                run.timeline.append(
                    "inspect trace proved a terminal driver preflight failure before "
                    "the agent loop; no agent.step span was expected"
                )
            elif terminal_sandbox_preflight:
                run.timeline.append(
                    "inspect trace and durable status proved a terminal sandbox preflight "
                    "failure before the agent loop; no agent.step span was expected"
                )

        # [REL-5] Measure terminal cleanup + release BEFORE freezing the dossier, so the
        # lifecycle / sidecar / cleanup oracles ADJUDICATE (instead of SKIP "no evidence
        # (headless run)"). The build's §6 evidence is already frozen into `run` by
        # drive_scenario, so releasing here never races it. Populates run.product_evidence.
        try:
            ev = await _collect_terminal_cleanup_evidence(
                client,
                run.conversation_id,
                run,
                baseline_containers=baseline_containers,
                relay_log=_relay_log_path(),
                timeline=getattr(run, "timeline", []),
                baseline_dangling_volumes=baseline_dangling_volumes,
                allow_global_cleanup_fallback=parallel_workers <= 1,
            )
        except Exception as exc:  # noqa: BLE001
            ev = {}
            with contextlib.suppress(Exception):
                getattr(run, "timeline", []).append(f"REL-5 cleanup measurement error: {exc}")
        # [REL-5 / codex] FAIL-CLOSED in LIVE measurement mode: a terminal run that the harness
        # could NOT adjudicate for cleanup must NOT pass via oracle SKIP (the gate forbids
        # SKIP-as-PASS). The three terminal-cleanup slices are MANDATORY whenever the cleanup-
        # measurement infra is configured (MINIMAX_RELAY_LOG set = a real soak run) — if a signal
        # is then unmeasurable (empty ledger / no container probe / unparseable terminal), that is
        # an INVALID_RUN, which the REL-6 gate ("0 INVALID_RUN in positive scenarios") forces the
        # operator to fix. The REL-6 preflight REQUIRES the relay ledger for positive scenarios, so
        # this always applies in the real gate. Deterministic unit tests (no live infra, no relay
        # env) exercise the CLASSIFIER and are not subject to the live-cleanup requirement.
        _live_measure = bool(_relay_log_path())
        _required = ("lifecycle", "sidecar", "cleanup")
        _missing = [k for k in _required if k not in ev]
        nonterminal_adjudication = ev.get("nonterminal_adjudication")
        trusted_inactive_nonterminal = (
            isinstance(nonterminal_adjudication, dict)
            and nonterminal_adjudication.get("schema_version") == 1
            and nonterminal_adjudication.get("drive_status") == INACTIVE_TIMEOUT
            and nonterminal_adjudication.get("terminal_snapshot_available") is False
            and type(nonterminal_adjudication.get("frozen_max_seq")) is int
        )
        trusted_live_thrash_nonterminal = (
            isinstance(nonterminal_adjudication, dict)
            and nonterminal_adjudication.get("schema_version") == 1
            and nonterminal_adjudication.get("drive_status") == LIVE_THRASH_STOP
            and nonterminal_adjudication.get("terminal_snapshot_available") is False
            and type(nonterminal_adjudication.get("frozen_max_seq")) is int
            and nonterminal_adjudication.get("preserved_for")
            == "strictly_confirmed_live_thrash_stop"
            and nonterminal_adjudication.get("strict_monitor_confirmed") is True
            and _confirmed_live_thrash_stop(run)
            and _current_thrash_failure_matches_retained(run, scenario)
        )
        trusted_nonterminal = trusted_inactive_nonterminal or trusted_live_thrash_nonterminal
        if _live_measure and _missing and not trusted_nonterminal:
            # Verdict-invalidating evidence gaps need the same retained diagnostic
            # dossier as the inspect-trace validity gate above.  Previously this
            # early return kept only classification.json and discarded the exact
            # events, state, inspect trace, live-thrash finding, workspace, and
            # provider slice needed to distinguish a harness gap from a masked
            # product failure.
            run.product_evidence = ev
            run.timeline.append(
                "terminal cleanup evidence incomplete: missing " + ", ".join(_missing)
            )
            provider_ledger = _provider_ledger_for_run(run)
            assemble_dossier(
                out_root,
                run_id,
                scenario,
                run,
                model=model,
                autonomous=autonomous,
                commit=commit,
                repo_revision=repo_revision,
                repo_dirty=repo_dirty,
                kernel=kernel,
                started_at=run_started_at,
                seed=seed,
                provider_ledger=provider_ledger,
            )
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                "terminal cleanup not adjudicable — missing evidence slice(s): "
                f"{', '.join(_missing)} "
                "(set MINIMAX_RELAY_LOG and ensure the container probe is available so the "
                "sidecar/cleanup oracles cannot silently SKIP into a green pass)",
                code=fc.RUN_INTERRUPTED,
                first_broken_link="terminal -> cleanup_evidence_unmeasurable",
                facts={"missing_slices": _missing},
                conversation_id=run.conversation_id,
                timeline_markdown=_timeline_md(scenario, run),
            )
        if _live_measure and _missing and trusted_nonterminal:
            # Freeze the exact exception boundary into hash-locked product evidence.
            # timeline.md is operator context only and is not part of EvidenceManifest,
            # so it cannot be the sole record of which terminal-only slices were waived.
            assert isinstance(nonterminal_adjudication, dict)
            if trusted_live_thrash_nonterminal:
                nonterminal_adjudication["terminal_cleanup_slices_unavailable"] = sorted(_missing)
            else:
                nonterminal_adjudication["terminal_cleanup_slices_not_applicable"] = sorted(
                    _missing
                )
            run.product_evidence = ev
            if trusted_live_thrash_nonterminal:
                run.timeline.append(
                    "terminal cleanup evidence unavailable after strictly confirmed live-thrash "
                    "stop; preserving the decisive retained monitor verdict without granting "
                    "cleanup proof; missing " + ", ".join(_missing)
                )
            else:
                run.timeline.append(
                    "terminal cleanup evidence not required for decisive nonterminal product "
                    "failure; missing " + ", ".join(_missing)
                )

        provider_ledger = _provider_ledger_for_run(run)
        base = assemble_dossier(
            out_root,
            run_id,
            scenario,
            run,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            started_at=run_started_at,
            seed=seed,
            provider_ledger=provider_ledger,
        )
        return classify_dossier(
            base,
            scenario,
            run,
            autonomous=autonomous,
            commit=commit,
            seed=seed,
            provider_ledger=provider_ledger,
        )
    finally:
        # RUNNER HYGIENE: kill the conversation this run created if it's still non-terminal,
        # so an abandoned RUNNING / PAUSED / AWAITING build (inconclusive cutoff, error path,
        # or simply released after evidence collection) doesn't leak and load the shared
        # server. AFTER assemble_dossier on the happy path → the §6 evidence is already frozen
        # (the terminal events were read in drive_scenario); a kill of the remote conversation
        # never touches the local frozen dossier. Best-effort + idempotent.
        await _release_conversation(client, client.last_conversation_id)


# ---- CLI --------------------------------------------------------------------


def _exit_code(status: str) -> int:
    return {
        fc.PASS: 0,
        fc.FAIL: 1,
        fc.INVALID_RUN: 2,
        fc.INFRA_FAILURE: 3,
    }.get(status, 1)


def _policy_from_args(args: argparse.Namespace) -> AdmissionPolicy:
    return AdmissionPolicy(
        memory_reserve_bytes=int(args.memory_reserve_gib * GIB),
        memory_per_worker_bytes=int(args.memory_per_worker_gib * GIB),
        disk_reserve_bytes=int(args.disk_reserve_gib * GIB),
        disk_per_worker_bytes=int(args.disk_per_worker_gib * GIB),
        cpus_per_worker=args.cpus_per_worker,
    )


def _policy_dict(policy: AdmissionPolicy) -> dict[str, int]:
    return {
        "memory_reserve_bytes": policy.memory_reserve_bytes,
        "memory_per_worker_bytes": policy.memory_per_worker_bytes,
        "disk_reserve_bytes": policy.disk_reserve_bytes,
        "disk_per_worker_bytes": policy.disk_per_worker_bytes,
        "cpus_per_worker": policy.cpus_per_worker,
    }


def _run_batch_item(
    *, index: int, run_id: str, base: Path, classification: dict[str, Any]
) -> dict[str, Any]:
    cid = str(classification.get("conversation_id") or "")
    trace_path = base / run_id / "conversations" / cid / "inspect-trace.json"
    trace: dict[str, Any] = {}
    if cid and trace_path.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            loaded = json.loads(trace_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                trace = loaded
    thrash = next(
        (
            result
            for result in classification.get("oracle_results") or []
            if isinstance(result, dict) and result.get("oracle") == "ThrashOracle"
        ),
        None,
    )
    # Bounded aggregation scalars only (work-order §6.2): the batch summary must
    # disclose source drops, continuity, finalization, conflicts, and the
    # lossless verdict without ever copying the unbounded event list.
    aggregation_raw = trace.get("aggregation")
    aggregation = aggregation_raw if isinstance(aggregation_raw, dict) else {}
    return {
        "index": index,
        "run_id": run_id,
        "run_dir": str(base / run_id),
        "scenario_id": classification.get("scenario_id"),
        "status": classification.get("status"),
        "code": classification.get("code"),
        "severity": classification.get("severity"),
        "conversation_id": classification.get("conversation_id"),
        "seed": classification.get("seed"),
        "inspect": {
            "captured": bool(trace),
            "event_count": int(trace.get("event_count") or 0),
            "routing_decisions": len(trace.get("routing_decisions") or []),
            "spans": len(trace.get("spans") or []),
            "aggregate_dropped_event_count": trace.get("dropped_event_count"),
            "source_dropped_event_count": trace.get("source_dropped_event_count"),
            "lossless": aggregation.get("lossless"),
            "finalized": aggregation.get("finalized"),
            "continuity": aggregation.get("continuity"),
            "continuity_reason": aggregation.get("continuity_reason"),
            "sample_count": aggregation.get("sample_count"),
            "accepted_sample_count": aggregation.get("accepted_sample_count"),
            "overlap_sample_count": aggregation.get("overlap_sample_count"),
            "unique_event_count": aggregation.get("unique_event_count"),
            "first_retained_seq": aggregation.get("first_retained_seq"),
            "last_retained_seq": aggregation.get("last_retained_seq"),
            "conflict_count": aggregation.get("conflict_count"),
            "failure_reasons": aggregation.get("failure_reasons"),
        },
        "thrash_oracle": thrash,
    }


def _require_exact_provider(
    selected: dict[str, dict[str, Any]],
    *,
    expected_host: str,
    expected_model: str,
) -> dict[str, dict[str, Any]]:
    """Inject the campaign's exact wire-provider assertion into every scenario.

    Scenario fixtures remain provider-neutral for hermetic classifier tests. A
    live run, however, must not let that neutrality turn the provider oracle into
    a SKIP. Existing contradictory assertions are rejected instead of overwritten.
    """

    host = expected_host.strip()
    model = expected_model.strip()
    if not host or not model:
        raise ValueError("live provider evidence requires nonempty expected host and model")
    secured = copy.deepcopy(selected)
    for scenario_id, scenario in secured.items():
        assertions = scenario.setdefault("assertions", {})
        if not isinstance(assertions, dict):
            raise ValueError(f"{scenario_id}.assertions must be a mapping")
        existing = assertions.get("provider") or {}
        if not isinstance(existing, dict):
            raise ValueError(f"{scenario_id}.assertions.provider must be a mapping")
        for field, required in (("require_host_substr", host), ("model", model)):
            configured = existing.get(field)
            if configured is not None and str(configured) != required:
                raise ValueError(
                    f"{scenario_id}.assertions.provider.{field}={configured!r} "
                    f"conflicts with required {required!r}"
                )
        assertions["provider"] = {
            **existing,
            "require_ledger": True,
            "require_host_substr": host,
            "model": model,
        }
    return secured


async def _amain(args: argparse.Namespace) -> int:
    from .adapters.disco_api import HttpTransport  # live deps only on the CLI path

    scenarios = load_scenarios(args.scenarios)
    requested_scenarios = (
        list(scenarios)
        if args.scenario.strip().lower() == "all"
        else [item.strip() for item in args.scenario.split(",") if item.strip()]
    )
    unknown = sorted(set(requested_scenarios) - set(scenarios))
    if not requested_scenarios or unknown:
        print(
            f"unknown scenario(s) {unknown or [args.scenario]!r}; known: {sorted(scenarios)}",
            file=sys.stderr,
        )
        return 2
    selected = {scenario_id: scenarios[scenario_id] for scenario_id in requested_scenarios}
    if args.iterations <= 0:
        print("--iterations must be positive", file=sys.stderr)
        return 2
    # [Lane A A-B1] FAIL-CLOSED infra preflight: a reliability soak must be able to adjudicate
    # terminal cleanup + provider-after-terminal, which REQUIRES the relay ledger. Refuse to START a
    # positive scenario without it — else run_once's _live_measure is False, the sidecar/cleanup
    # slices are omitted, the oracles SKIP, and the run classifies SKIP-as-PASS. A scenario opts out
    # only by explicitly declaring `requires_relay_ledger: false` (a pure classifier/negative case).
    relay_required = any(
        scenario.get("requires_relay_ledger", True) for scenario in selected.values()
    )
    if relay_required and not _relay_log_path():
        print(
            "[build-soak] INFRA_FAILURE: scenario requires the relay ledger to adjudicate terminal "
            "cleanup + provider-after-terminal — set DISCO_PROVIDER_LEDGER or MINIMAX_RELAY_LOG. "
            "Refusing to run a soak that could SKIP-as-PASS.",
            file=sys.stderr,
        )
        return 3
    if any(scenario.get("requires_relay_ledger", True) for scenario in selected.values()):
        try:
            selected = _require_exact_provider(
                selected,
                expected_host=args.expected_provider_host,
                expected_model=args.expected_provider_model,
            )
        except ValueError as exc:
            print(f"[build-soak] INFRA_FAILURE: {exc}", file=sys.stderr)
            return 3
    repo_root = Path(__file__).resolve().parents[2]
    try:
        repo_revision, commit, repo_dirty = _source_revision(repo_root)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        print(
            f"[build-soak] INFRA_FAILURE: could not determine exact source identity: {exc}",
            file=sys.stderr,
        )
        return 3
    db_path = args.db or os.environ.get("DISCO_DB") or str(repo_root / "disco.db")
    model = args.model or os.environ.get("DISCO_SOAK_MODEL") or None

    # Bug 9: read the authoritative host ProjectStore SNAPSHOT for workspace collection
    # (not the fragile dev-server preview proxy). "" mirrors the agent-server's OWN root
    # resolution from the SAME env (DISCO_DATA_DIR / XDG_DATA_HOME / ~/.local/share). The
    # snapshot wait absorbs the FINISHED-before-_maybe_snapshot flush window.
    projects_root = args.projects_root or os.environ.get("DISCO_PROJECTS_ROOT") or ""

    # Refuse to spend model calls unless the app can expose its actual per-run
    # routing/model spans. Individual workers validate their own trace again.
    require_inspect_preflight = any(
        bool(scenario.get("requires_inspect_trace", True)) for scenario in selected.values()
    )
    if require_inspect_preflight:
        try:
            inspect_transport = HttpTransport(args.base_url)
            inspect_status, inspect_data = await inspect_transport.get_json("/api/debug/inspect")
        except Exception as exc:  # noqa: BLE001
            print(f"[build-soak] INFRA_FAILURE: inspect preflight failed: {exc}", file=sys.stderr)
            return 3
        if inspect_status != 200 or inspect_data.get("enabled") is not True:
            print(
                "[build-soak] INFRA_FAILURE: DISCO_INSPECT is not enabled; refusing to run "
                "without per-conversation model/routing logs.",
                file=sys.stderr,
            )
            return 3
        if model:
            try:
                models_status, models_data = await inspect_transport.get_json("/models")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[build-soak] INFRA_FAILURE: driver-model preflight failed: {exc}",
                    file=sys.stderr,
                )
                return 3
            if models_status != 200 or not _driver_catalog_contains(models_data, model):
                print(
                    "[build-soak] INFRA_FAILURE: requested driver model is not live and "
                    "driver-eligible; refusing to create any conversations. Verify its signed "
                    "origin approval, secret binding, and tool-calling capability.",
                    file=sys.stderr,
                )
                return 3

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    batch_stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    batch_label = requested_scenarios[0] if len(requested_scenarios) == 1 else "scenario_matrix"
    batch_id = f"batch_{batch_label}_{batch_stamp}"
    batch_dir = out_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC).isoformat()

    try:
        policy = _policy_from_args(args)
    except ValueError as exc:
        print(f"invalid resource policy: {exc}", file=sys.stderr)
        return 2
    initial_resources = read_host_resources(disk_path=batch_dir)
    safe_workers = safe_worker_count(initial_resources, policy)
    try:
        resolved_workers = resolve_worker_count(args.parallel, initial_resources, policy)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolved_workers = min(resolved_workers, args.iterations)
    if resolved_workers == 0:
        print(
            "[build-soak] INFRA_FAILURE: no worker fits while preserving the configured "
            f"desktop reserve ({args.memory_reserve_gib:g} GiB RAM, "
            f"{args.disk_reserve_gib:g} GiB disk).",
            file=sys.stderr,
        )
        return 3
    requested_label = str(args.parallel)
    if requested_label != "auto" and int(requested_label) > resolved_workers:
        print(
            f"[build-soak] clamped --parallel {requested_label} to {resolved_workers} "
            "for the current resource headroom."
        )
    print(
        f"[build-soak] {args.iterations} iterations, {resolved_workers} parallel workers "
        f"(safe now: {safe_workers}); reserving {args.memory_reserve_gib:g} GiB RAM "
        f"and {args.disk_reserve_gib:g} GiB disk for the desktop."
    )

    gate = ResourceGate(
        policy,
        disk_path=batch_dir,
        poll_s=args.resource_poll,
        wait_timeout_s=args.resource_wait_timeout,
    )
    pool = ResourcePool(gate, policy, max_workers=resolved_workers)
    semaphore = asyncio.Semaphore(resolved_workers)

    async def run_iteration(index: int) -> dict[str, Any]:
        scenario_id = requested_scenarios[index % len(requested_scenarios)]
        task_seed = args.seed_base + index
        scenario = _materialize_task_seed(selected[scenario_id], task_seed)
        autonomous = bool(args.autonomous or scenario.get("autonomous"))
        require_inspect = bool(scenario.get("requires_inspect_trace", True))
        run_id = f"build_soak_{scenario_id}_{batch_stamp}_{index:03d}"
        try:
            async with semaphore, pool.slot():
                transport: Transport = HttpTransport(args.base_url)
                client = DiscoApiClient(
                    transport,
                    db_path=db_path,
                    projects_root=projects_root,
                    snapshot_wait_s=args.snapshot_wait,
                    require_workspace_commit=True,
                )
                classification = await run_once(
                    client,
                    scenario,
                    run_id=run_id,
                    out_root=batch_dir,
                    model=model,
                    autonomous=autonomous,
                    commit=commit,
                    repo_revision=repo_revision,
                    repo_dirty=repo_dirty,
                    kernel=args.kernel,
                    timeout_s=args.timeout,
                    hard_cap_s=args.hard_cap,
                    require_inspect_trace=require_inspect,
                    parallel_workers=resolved_workers,
                    seed=task_seed,
                )
        except TimeoutError as exc:
            classification = _infra_failure_record(
                batch_dir,
                run_id,
                scenario,
                InfraProbeError(
                    "HOST_RESOURCE_ADMISSION_TIMEOUT",
                    {"reason": str(exc), "policy": _policy_dict(policy)},
                ),
            )
        except Exception as exc:  # noqa: BLE001 — keep the rest of the soak running
            classification = _invalid_run_record(
                batch_dir, run_id, scenario, f"parallel worker error: {type(exc).__name__}: {exc}"
            )
        status = str(classification.get("status"))
        code = classification.get("code")
        print(
            f"[{index + 1}/{args.iterations}] {run_id}: {status}"
            + (f" / {code}" if code else "")
            + f"  -> {batch_dir / run_id}"
        )
        return _run_batch_item(
            index=index,
            run_id=run_id,
            base=batch_dir,
            classification=classification,
        )

    runs = await asyncio.gather(*(run_iteration(i) for i in range(args.iterations)))
    runs = sorted(runs, key=lambda item: int(item["index"]))
    status_counts = Counter(str(item.get("status")) for item in runs)
    failure_counts = Counter(str(item.get("code")) for item in runs if item.get("code"))
    final_resources = read_host_resources(disk_path=batch_dir)
    summary = {
        "schema_version": 1,
        "batch_id": batch_id,
        "scenario_ids": requested_scenarios,
        "scenario_counts": dict(
            sorted(Counter(str(item.get("scenario_id")) for item in runs).items())
        ),
        "surfaces": sorted(
            {str(scenario.get("surface", "build")) for scenario in selected.values()}
        ),
        "commit": commit,
        "repo_revision": repo_revision,
        "repo_dirty": repo_dirty,
        "model": model,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "iterations": args.iterations,
        "seed_base": args.seed_base,
        "parallel": {
            "requested": requested_label,
            "resolved": resolved_workers,
            "safe_at_start": safe_workers,
        },
        "resource_policy": _policy_dict(policy),
        "resources": {
            "initial": initial_resources.to_dict(),
            "final": final_resources.to_dict(),
        },
        "inspect_required": require_inspect_preflight,
        "status_counts": dict(sorted(status_counts.items())),
        "failure_code_counts": dict(sorted(failure_counts.items())),
        "runs": runs,
    }
    summary_path = batch_dir / "batch-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[build-soak] batch report -> {summary_path}")
    return max((_exit_code(str(item.get("status"))) for item in runs), default=0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Headless live-API Build Soak runner (§25).")
    p.add_argument(
        "--scenario",
        required=True,
        help="one id, a comma-separated scenario matrix, or 'all' from scenarios.yaml",
    )
    p.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="total runs; a scenario matrix is assigned round-robin across them",
    )
    p.add_argument(
        "--parallel",
        default="auto",
        help="concurrent workers: 'auto' (default) or a positive integer, clamped to "
        "live RAM/CPU/disk headroom",
    )
    p.add_argument("--model", default=None, help="driver model (default $DISCO_SOAK_MODEL)")
    p.add_argument(
        "--expected-provider-host",
        default=os.environ.get("DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST", ""),
        help="required substring in every observed provider-ledger host",
    )
    p.add_argument(
        "--expected-provider-model",
        default=os.environ.get("DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL", ""),
        help="exact model id required in every observed provider-ledger record",
    )
    p.add_argument("--out", default=_DEFAULT_OUT, help="output root for run folders")
    p.add_argument("--autonomous", action="store_true", help="headless auto-approve")
    p.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    p.add_argument(
        "--kernel",
        choices=("disco",),
        default="disco",
        help="Build kernel evidence label; only disco is supported.",
    )
    p.add_argument("--db", default=None, help="disco.db path (default $DISCO_DB or repo/disco.db)")
    p.add_argument(
        "--projects-root",
        default=None,
        help="ProjectStore root for the authoritative workspace snapshot read "
        "(default $DISCO_PROJECTS_ROOT or the agent-server's own resolved default)",
    )
    p.add_argument(
        "--snapshot-wait",
        type=float,
        default=45.0,
        help="seconds to wait for a just-finished build's workspace snapshot to flush "
        "(cold-start flush lag after a fresh sandbox can exceed the old 15s → INVALID_RUN)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_INACTIVITY_S,
        help="PROGRESS-AWARE inactivity window (s): the terminal wait keeps waiting while "
        "the build emits NEW events; it stops only after THIS much no-progress silence "
        "(a genuine wedge) — NOT a blind wall-clock. A still-progressing build is never cut off.",
    )
    p.add_argument(
        "--hard-cap",
        type=float,
        default=_DEFAULT_HARD_CAP_S,
        help="generous safety ceiling (s) bounding a truly-hung run, set well above a normal "
        "build; a cutoff here WHILE STILL PROGRESSING is recorded INVALID_RUN (inconclusive), "
        "not a product BUILD_DID_NOT_FINISH (Bug 15).",
    )
    p.add_argument("--scenarios", default=str(_SCENARIOS))
    p.add_argument(
        "--seed-base",
        type=int,
        default=0,
        help="first deterministic task seed; each iteration increments it by one",
    )
    p.add_argument(
        "--memory-reserve-gib",
        type=float,
        default=32.0,
        help="RAM kept unavailable to new soak workers for the desktop (default: 32 GiB)",
    )
    p.add_argument("--memory-per-worker-gib", type=float, default=3.0)
    p.add_argument("--disk-reserve-gib", type=float, default=10.0)
    p.add_argument("--disk-per-worker-gib", type=float, default=0.5)
    p.add_argument("--cpus-per-worker", type=int, default=1)
    p.add_argument("--resource-poll", type=float, default=5.0)
    p.add_argument("--resource-wait-timeout", type=float, default=1800.0)
    args = p.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
