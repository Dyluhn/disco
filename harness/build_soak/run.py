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
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import failure_codes as fc
from .adapters.disco_api import (
    AWAITING_PLAN_APPROVAL,
    AWAITING_USER_QUESTION,
    PAUSED_STATE,
    PROGRESSING_TIMEOUT,
    TERMINAL_STATES,
    WAITING_FOR_CONFIRMATION,
    CollectedRun,
    DiscoApiClient,
    InconclusiveRunError,
    InfraProbeError,
    Transport,
)
from .classify import CLASSIFICATION_NAME, classify
from .evidence import EvidenceManifest, compute_evidence_hashes, write_manifest

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_OUT = "test-record/build-soak"
_SCENARIOS = Path(__file__).resolve().parent / "scenarios.yaml"
_MAX_GATES = 8  # bound the approve loop so a gate flap can't spin forever
_MAX_RESUMES = 3  # bound PAUSED-resume so an actionless-paused build can't spin forever
_MAX_CLARIFY = 3  # bound clarify/confirm answers so an endlessly-asking model is let go (Bug 17)

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
_CONFIRM_ANSWER = (
    "Yes, proceed. Use sensible, conventional defaults and continue to completion. "
    "Do not ask further clarifying questions."
)

# Progress-aware terminal-wait knobs (Bug 15). The terminal wait is NOT a blind
# wall-clock: `inactivity_s` is the NO-PROGRESS window (a build that keeps emitting
# events is never cut off — only genuine silence for this long ends the wait), and
# `hard_cap_s` is the generous safety ceiling that bounds a truly-hung run, set well
# above a normal build (~5min) so a slow-but-progressing build finishes on its real
# terminal rather than being frozen mid-flight + mislabeled BUILD_DID_NOT_FINISH.
_DEFAULT_INACTIVITY_S = 180.0
_DEFAULT_HARD_CAP_S = 1200.0


# ---- scenario loading -------------------------------------------------------


def load_scenarios(path: str | Path = _SCENARIOS) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = raw.get("scenarios") or []
    return {str(s["id"]): s for s in scenarios if s.get("id")}


def _declared_workspace_paths(scenario: dict[str, Any]) -> list[str]:
    files = ((scenario.get("assertions") or {}).get("workspace") or {}).get("files") or []
    return [str(f["path"]) for f in files if f.get("path")]


def _preview_required(scenario: dict[str, Any]) -> bool:
    return bool(((scenario.get("assertions") or {}).get("preview") or {}).get("required"))


# ---- orchestration (acts as the user) ---------------------------------------


async def _inject_when_writing(
    client: DiscoApiClient,
    cid: str,
    mid_run: list[dict[str, Any]],
    timeline: list[str],
    timeout_s: float,
) -> None:
    """Background: wait for the first MUTATING action, then inject the §15.4 steer
    follow-up (a real mid-run redirect). Runs concurrently with the terminal poll so
    it works whether or not the run paused at an approval gate first."""
    for f in mid_run:
        seq = await client.wait_for_first_file_write(cid, timeout_s=timeout_s)
        if seq is None:
            timeline.append("mid-run steer skipped: run produced no file write to steer on")
            return
        await client.send_followup(cid, str(f["text"]), kind="steer")
        timeline.append(f"steered after first file write (seq={seq}): {f['text']!r}")


async def _drive_to_terminal(
    client: DiscoApiClient,
    cid: str,
    *,
    autonomous: bool,
    mid_run: list[dict[str, Any]],
    timeline: list[str],
    inactivity_s: float,
    hard_cap_s: float,
    clarification_answer: str | None = None,
) -> str:
    """Drive the run to a terminal state, approving each plan gate (interactive),
    answering any mid-build clarify/confirm gate (Bug 17), and injecting any mid-run
    steer follow-up concurrently. Returns the terminal status.

    The wait is PROGRESS-AWARE (Bug 15): a still-actively-progressing build is never cut
    off by a wall-clock — only a genuine terminal, genuine inactivity (INACTIVE_TIMEOUT →
    fall through to normal classification of the wedged run), or the hard cap ends it. A
    hard-cap cutoff WHILE STILL PROGRESSING (PROGRESSING_TIMEOUT) is INCONCLUSIVE, not a
    product failure → raise InconclusiveRunError so the run records INVALID_RUN (§17)."""
    injector: asyncio.Task[None] | None = None
    if mid_run:
        injector = asyncio.create_task(
            _inject_when_writing(client, cid, mid_run, timeline, hard_cap_s)
        )
    gates = 0
    resumes = 0
    clarifies = 0
    try:
        while True:
            if autonomous:
                status = await client.poll_until_terminal(
                    cid, inactivity_s=inactivity_s, hard_cap_s=hard_cap_s
                )
            else:
                status = await client.poll_until_terminal_or_gate(
                    cid, inactivity_s=inactivity_s, hard_cap_s=hard_cap_s
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
                # The runner ACTS AS THE USER: a build that asks a clarifying question
                # (or pauses for a go-ahead) mid-build is ANSWERED so it proceeds, instead
                # of false-stalling into NO_PLAN (Bug 17). Sent over the SAME send_message
                # path a real user follow-up uses (appends the user turn + kicks the loop).
                # BOUNDED (≤ _MAX_CLARIFY): a model that keeps asking past the cap is let go
                # to a real terminal/inactivity and classified HONESTLY — never an infinite
                # answer-loop, never a masked failure.
                clarifies += 1
                if status == WAITING_FOR_CONFIRMATION:
                    answer = _CONFIRM_ANSWER
                    label = "confirmed"
                else:
                    answer = clarification_answer or _GENERIC_CLARIFY_ANSWER
                    label = "answered clarifying question"
                await client.send_followup(cid, answer, kind="message")
                timeline.append(
                    f"{label} (clarify {clarifies}/{_MAX_CLARIFY}, status {status}): {answer!r}"
                )
                # Wait for the gate to clear so the same unprocessed gate isn't re-read +
                # re-answered (wastes the clarify budget), mirroring the approval gate.
                await client.wait_until_status_leaves(
                    cid, status, timeout_s=min(inactivity_s, 60.0)
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
            timeline.append(f"reached terminal/stop status: {status}")
            return status
    finally:
        if injector is not None:
            injector.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await injector


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
    prompt = str(scenario["prompt"])
    cid = await client.create_build_conversation(prompt, model=model, autonomous=autonomous)
    timeline.append(f"created build conversation {cid} (autonomous={autonomous})")
    state_initial = await client.get_state(cid)

    # Optional scenario-provided answer to a mid-build clarifying question (Bug 17). When
    # absent, the runner sends a generic safe default that instructs the model not to ask
    # further questions. Either way the build proceeds + is adjudicated on its real outcome.
    clarification_answer = scenario.get("clarification_answer")
    clarification_answer = str(clarification_answer) if clarification_answer is not None else None

    followups = scenario.get("followups") or []
    mid_run = [f for f in followups if f.get("trigger") == "after_first_file_write"]
    after_terminal = [f for f in followups if f.get("trigger") != "after_first_file_write"]

    # Phase 1: the initial build (+ any mid-run steer) to terminal.
    await _drive_to_terminal(
        client,
        cid,
        autonomous=autonomous,
        mid_run=mid_run,
        timeline=timeline,
        inactivity_s=timeout_s,
        hard_cap_s=hard_cap_s,
        clarification_answer=clarification_answer,
    )

    # Phase 2: after-terminal follow-ups — each is its own re-plan→approve→terminal
    # cycle (the user types a follow-up; the PRODUCT decides to re-plan).
    for f in after_terminal:
        await client.send_followup(cid, str(f["text"]), kind="message")
        timeline.append(f"sent after-terminal follow-up: {f['text']!r}")
        await _drive_to_terminal(
            client,
            cid,
            autonomous=autonomous,
            mid_run=[],
            timeline=timeline,
            inactivity_s=timeout_s,
            hard_cap_s=hard_cap_s,
            clarification_answer=clarification_answer,
        )

    # Collect (post-terminal, race-free DB read for events).
    events = client.collect_events(cid)
    state_final = await client.get_state(cid)
    workspace = await client.collect_workspace(cid, _declared_workspace_paths(scenario))
    preview = await client.collect_preview(cid) if _preview_required(scenario) else None
    timeline.append(f"collected {len(events)} events; workspace files={list(workspace)}")
    return CollectedRun(
        conversation_id=cid,
        events=events,
        state_initial=state_initial,
        state_final=state_final,
        workspace_manifest=workspace,
        preview=preview,
        timeline=timeline,
    )


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
    seed: int | None = None,
    mode: str = "api",
) -> Path:
    """Write the §5 dossier and freeze it under the §6 evidence lock. Returns the
    run-folder path. classification.json is written separately by classify_dossier."""
    base = Path(out_root) / run_id
    conv = base / "conversations" / run.conversation_id
    conv.mkdir(parents=True, exist_ok=True)

    (base / "prompt.txt").write_text(str(scenario.get("prompt", "")), encoding="utf-8")
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
    if run.preview is not None:
        preview_dir = conv / "preview"
        preview_dir.mkdir(exist_ok=True)
        (preview_dir / "health.json").write_text(
            json.dumps(run.preview.get("health") or {}, indent=2, sort_keys=True), encoding="utf-8"
        )
        (preview_dir / "served.html").write_text(
            str(run.preview.get("content") or ""), encoding="utf-8"
        )

    # Evidence lock: hash the durable §6 set (relative to the run folder).
    conv_rel = f"conversations/{run.conversation_id}"
    evidence_files = {
        "events.jsonl": f"{conv_rel}/events.jsonl",
        "state.final.json": f"{conv_rel}/state.final.json",
        "workspace-manifest.json": f"{conv_rel}/workspace-manifest.json",
    }
    # P1 (codex): the PREVIEW dossier is preview TRUTH the oracle adjudicates on — it
    # MUST be under the hash lock too, else a preview-health/served-html tamper would
    # not trip the §6 INVALID_RUN. Hash both preview files when a preview was captured.
    if run.preview is not None:
        evidence_files["preview/health.json"] = f"{conv_rel}/preview/health.json"
        evidence_files["preview/served.html"] = f"{conv_rel}/preview/served.html"
    manifest = EvidenceManifest(
        run_id=run_id,
        scenario_id=str(scenario.get("id")),
        seed=seed,
        repo_commit=commit,
        model=model or "",
        autonomous=autonomous,
        surface="build",
        mode=mode,
        started_at="",
        finished_at=datetime.now(UTC).isoformat(),
        evidence_files={"events": f"{conv_rel}/events.jsonl", **evidence_files},
        evidence_hashes=compute_evidence_hashes(base, evidence_files),
    )
    write_manifest(base, manifest)
    return base


def classify_dossier(
    base: Path,
    scenario: dict[str, Any],
    run: CollectedRun,
    *,
    autonomous: bool,
    commit: str = "",
    seed: int | None = None,
) -> dict[str, Any]:
    """Run the deterministic classifier on the collected run, passing workspace +
    preview + autonomy DIRECTLY (codex #2 — so OutputTruth/preview actually run),
    and write classification.json into the dossier."""
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
    )
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
        "conversation_id": None,
        "facts": record_facts,
        "required_evidence_present": False,
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
    }
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (base / "timeline.md").write_text(
        f"# INVALID_RUN ({code})\n\nreason: {reason}\n", encoding="utf-8"
    )
    return record


# ---- runner hygiene: kill an abandoned conversation -------------------------


async def _release_conversation(
    client: DiscoApiClient, cid: str | None, timeline: list[str] | None = None
) -> None:
    """Tear down the conversation the runner is DONE with so it doesn't leak as a RUNNING
    build on the shared server. Called from run_once's `finally` AFTER evidence has been
    collected + frozen (the §6 read of the terminal events already happened in
    drive_scenario), so the kill never races the dossier.

    Kills ONLY a STILL-NON-TERMINAL conversation (RUNNING / PAUSED / AWAITING_*): one that
    already reached a genuine terminal (FINISHED / ERROR / STUCK / IDLE) needs no kill. The
    kill is BEST-EFFORT + IDEMPOTENT — any error (server gone, already terminal) is swallowed
    so teardown never turns a real verdict into a crash, and an already-terminal conv is
    never double-killed (we don't kill it at all)."""
    if not cid:
        return
    status = ""
    with contextlib.suppress(Exception):
        status = DiscoApiClient._status_of(await client.get_state(cid))
    if status in TERMINAL_STATES:
        if timeline is not None:
            timeline.append(f"released conversation {cid}: already terminal ({status}); no kill")
        return
    # Non-terminal (or undeterminable) → the runner is abandoning a live build: kill it.
    with contextlib.suppress(Exception):
        resp = await client.kill(cid)
        if timeline is not None:
            timeline.append(
                f"killed abandoned conversation {cid} "
                f"(was {status or 'unknown'}, http {resp.get('http_status')})"
            )


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
    timeout_s: float,
    hard_cap_s: float = _DEFAULT_HARD_CAP_S,
) -> dict[str, Any]:
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
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.RUN_TIMEOUT_WHILE_PROGRESSING,
                first_broken_link="terminal_wait -> no_terminal_before_hard_cap",
                facts=exc.facts,
            )
        except Exception as exc:  # noqa: BLE001 — surface the real reason as INVALID_RUN
            return _invalid_run_record(
                out_root, run_id, scenario, f"{type(exc).__name__}: {exc}"
            )

        base = assemble_dossier(
            out_root, run_id, scenario, run, model=model, autonomous=autonomous, commit=commit
        )
        return classify_dossier(base, scenario, run, autonomous=autonomous, commit=commit)
    finally:
        # RUNNER HYGIENE: kill the conversation this run created if it's still non-terminal,
        # so an abandoned RUNNING / PAUSED / AWAITING build (inconclusive cutoff, error path,
        # or simply released after evidence collection) doesn't leak and load the shared
        # server. AFTER assemble_dossier on the happy path → the §6 evidence is already frozen
        # (the terminal events were read in drive_scenario); a kill of the remote conversation
        # never touches the local frozen dossier. Best-effort + idempotent.
        await _release_conversation(client, client.last_conversation_id)


# ---- CLI --------------------------------------------------------------------


def _git_commit(repo_root: Path) -> str:
    with contextlib.suppress(Exception):
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
        ).strip()
    return ""


def _exit_code(status: str) -> int:
    return {
        fc.PASS: 0,
        fc.FAIL: 1,
        fc.INVALID_RUN: 2,
        fc.INFRA_FAILURE: 3,
    }.get(status, 1)


async def _amain(args: argparse.Namespace) -> int:
    from .adapters.disco_api import HttpTransport  # live deps only on the CLI path

    scenarios = load_scenarios(args.scenarios)
    if args.scenario not in scenarios:
        print(f"unknown scenario {args.scenario!r}; known: {sorted(scenarios)}", file=sys.stderr)
        return 2
    scenario = scenarios[args.scenario]
    repo_root = Path(__file__).resolve().parents[2]
    commit = _git_commit(repo_root)
    db_path = args.db or os.environ.get("DISCO_DB") or str(repo_root / "disco.db")
    model = args.model or os.environ.get("DISCO_SOAK_MODEL") or None
    autonomous = bool(args.autonomous or scenario.get("autonomous"))

    # Bug 9: read the authoritative host ProjectStore SNAPSHOT for workspace collection
    # (not the fragile dev-server preview proxy). "" mirrors the agent-server's OWN root
    # resolution from the SAME env (DISCO_DATA_DIR / XDG_DATA_HOME / ~/.local/share). The
    # snapshot wait absorbs the FINISHED-before-_maybe_snapshot flush window.
    projects_root = args.projects_root or os.environ.get("DISCO_PROJECTS_ROOT") or ""
    transport: Transport = HttpTransport(args.base_url)
    client = DiscoApiClient(
        transport,
        db_path=db_path,
        projects_root=projects_root,
        snapshot_wait_s=args.snapshot_wait,
    )

    worst = 0
    for i in range(args.iterations):
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        run_id = f"build_soak_{args.scenario}_{ts}_{i:03d}"
        classification = await run_once(
            client,
            scenario,
            run_id=run_id,
            out_root=args.out,
            model=model,
            autonomous=autonomous,
            commit=commit,
            timeout_s=args.timeout,
            hard_cap_s=args.hard_cap,
        )
        status = str(classification.get("status"))
        code = classification.get("code")
        print(
            f"[{i + 1}/{args.iterations}] {run_id}: {status}"
            + (f" / {code}" if code else "")
            + f"  -> {Path(args.out) / run_id}"
        )
        worst = max(worst, _exit_code(status))
        if i + 1 < args.iterations:
            time.sleep(1.0)
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Headless live-API Build Soak runner (§25).")
    p.add_argument("--scenario", required=True, help="scenario id from scenarios.yaml")
    p.add_argument("--iterations", type=int, default=1)
    p.add_argument("--model", default=None, help="driver model (default $DISCO_SOAK_MODEL)")
    p.add_argument("--out", default=_DEFAULT_OUT, help="output root for run folders")
    p.add_argument("--autonomous", action="store_true", help="headless auto-approve")
    p.add_argument("--base-url", default=_DEFAULT_BASE_URL)
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
        default=15.0,
        help="seconds to wait for a just-finished build's workspace snapshot to flush",
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
    args = p.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
