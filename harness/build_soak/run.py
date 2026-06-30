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
    AWAITING_USER_DECISION,
    AWAITING_USER_QUESTION,
    FOLLOWUP_PICKUP_TIMEOUT,
    PAUSED_STATE,
    PROGRESSING_TIMEOUT,
    TERMINAL_STATES,
    WAITING_FOR_CONFIRMATION,
    CollectedRun,
    DiscoApiClient,
    FollowupPickupError,
    InconclusiveRunError,
    InfraProbeError,
    SnapshotNotReadyError,
    Transport,
)
from .classify import CLASSIFICATION_NAME, classify
from .evidence import EvidenceManifest, compute_evidence_hashes, write_manifest
from .product_evidence import PRODUCT_EVIDENCE_NAME, write_product_evidence
from .provider_ledger import parse_relay_log

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
    declared_seqs: list[int] | None = None,
    declared_requires: list[bool] | None = None,
) -> None:
    """Background: wait for the first MUTATING action, then inject the §15.4 steer
    follow-up (a real mid-run redirect). Runs concurrently with the terminal poll so
    it works whether or not the run paused at an approval gate first."""
    for f in mid_run:
        seq = await client.wait_for_first_file_write(cid, timeout_s=timeout_s)
        if seq is None:
            timeline.append("mid-run steer skipped: run produced no file write to steer on")
            return
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
    if mid_run:
        injector = asyncio.create_task(
            _inject_when_writing(
                client, cid, mid_run, timeline, hard_cap_s,
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
                resolved = await client.resolve_decision(
                    cid, preferred_option_id=decision_answer
                )
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
        decision_answer=decision_answer,
        decisions=decisions,
        injected_user_seqs=harness_injected_user_seqs,
        declared_followup_seqs=declared_followup_seqs,
        declared_followup_requires_revision=declared_followup_requires_revision,
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
        await _drive_to_terminal(
            client,
            cid,
            autonomous=autonomous,
            mid_run=[],
            timeline=timeline,
            inactivity_s=timeout_s,
            hard_cap_s=hard_cap_s,
            clarification_answer=clarification_answer,
            decision_answer=decision_answer,
            decisions=decisions,
            injected_user_seqs=harness_injected_user_seqs,
            min_seq=baseline_seq,
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
        decision_resolutions=decisions,
        declared_followup_seqs=declared_followup_seqs,
        declared_followup_requires_revision=declared_followup_requires_revision,
        harness_injected_user_seqs=harness_injected_user_seqs,
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
    kernel: str = "disco",
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

    # [Lane A A-B4] PERSIST product_evidence so a folder reclassify/replay ADJUDICATES instead of
    # going green-by-absence (the headless soak's REL-5 cleanup evidence was in-memory only → any
    # classify_run_folder saw product_evidence=None → all browser oracles SKIP → ungated GREEN).
    # Written into the conv dir (classify_run_folder rglobs product-evidence.json) and added to the
    # §6 hash lock so a tamper trips INVALID_RUN. strict=False: persist the truthful evidence as the
    # oracles saw it (their _int coercion already fails-closed on malformed counts).
    _pe = getattr(run, "product_evidence", None)
    if _pe:
        write_product_evidence(conv, _pe, strict=False)

    # Evidence lock: hash the durable §6 set (relative to the run folder).
    conv_rel = f"conversations/{run.conversation_id}"
    evidence_files = {
        "events.jsonl": f"{conv_rel}/events.jsonl",
        "state.final.json": f"{conv_rel}/state.final.json",
        "workspace-manifest.json": f"{conv_rel}/workspace-manifest.json",
    }
    if _pe:
        evidence_files[PRODUCT_EVIDENCE_NAME] = f"{conv_rel}/{PRODUCT_EVIDENCE_NAME}"
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
        kernel=kernel,
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
    # [Lane A A-M4] Wire the parsed relay ledger so the INDEPENDENT ProviderLedgerOracle runs
    # alongside the sidecar slice (defense-in-depth: two after-terminal checks, not one). Scope to
    # THIS run's window [run_start, ...] and stamp after_terminal on records past the build-terminal
    # epoch; ran AFTER _collect's grace+release so any straggler post-terminal call is already
    # logged. Live-mode only (relay env set) so deterministic unit tests are unaffected.
    provider_ledger: list[dict[str, Any]] | None = None
    _rl = _relay_log_path()
    if _rl and os.path.exists(_rl):
        try:
            with open(_rl, encoding="utf-8") as _f:
                _recs = parse_relay_log(_f.read())
            _t = _terminal_status_epoch(run.events)
            _s = _min_event_epoch(run.events)
            if _t is not None and _s is not None:
                provider_ledger = [
                    {**r, "after_terminal": float(r["ts"]) > _t}
                    for r in _recs
                    if isinstance(r.get("ts"), (int, float)) and float(r["ts"]) >= _s
                ]
        except Exception:
            provider_ledger = None
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
        # HARN-2: browser product-harness evidence (None until HARN-1b populates it on
        # CollectedRun; the browser oracles SKIP without it, so headless runs are unaffected).
        product_evidence=getattr(run, "product_evidence", None),
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
    status = ""
    with contextlib.suppress(Exception):
        status = DiscoApiClient._status_of(await client.get_state(cid))
    # Release regardless of terminal status — the orphan-container teardown. kill is idempotent +
    # suppressed, so a non-terminal abandon and a terminal release share the one proven path.
    with contextlib.suppress(Exception):
        resp = await client.kill(cid)
        if timeline is not None:
            verb = "released terminal" if status in TERMINAL_STATES else "killed abandoned"
            timeline.append(
                f"{verb} conversation {cid} — sandbox + sidecar containers destroyed "
                f"(was {status or 'unknown'}, http {resp.get('http_status')})"
            )


def _live_disco_container_count() -> int | None:
    """[REL-5 / Lane A A-B5/M3] Count RUNNING disco sandbox + egress-sidecar containers via the
    podman CLI — the host-side orphan signal for the CleanupOracle on the LOCAL PODMAN iteration
    backend. Uses `podman ps` (RUNNING only) NOT `podman ps -a`: `-a` includes exited-but-not-yet-
    pruned containers, so a correctly-torn-down box still matched the prefix and inflated the count
    on either side of the baseline→after delta (a real leak could net to 0, or a prune could push
    after<baseline). Counting only RUNNING containers measures actual liveness. Returns None if
    podman is unavailable (count UNKNOWN, never faked 0). The gVisor FINAL REL-6 run needs the
    gVisor-equivalent probe (tracked)."""
    import subprocess

    try:
        out = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}}"],  # RUNNING only (no -a)
            capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return sum(
        1 for ln in out.stdout.splitlines()
        if ln.startswith("disco-sbx-") or ln.startswith("disco-egr-")
    )


def _relay_log_path() -> str | None:
    """[codex] ONE canonical resolver for the MiniMax relay-ledger path — the SINGLE source for
    BOTH reading the ledger AND the `_live_measure` fail-closed gate, so the rule can never key on
    a different env var than the one that actually carries the ledger. The relay WRITES
    MINIMAX_RELAY_LOG (minimax_relay.py); we also accept the legacy PMX_RELAY_LOG (Playwright live
    specs) and DISCO_RELAY_LOG so no driver path can drift into a silent SKIP-as-PASS."""
    for name in ("MINIMAX_RELAY_LOG", "PMX_RELAY_LOG", "DISCO_RELAY_LOG"):
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


def _terminal_status_epoch(events: list[dict[str, Any]]) -> float | None:
    """[codex/Lane A] Epoch of the LAST `status` event whose status is a BUILD terminal
    (_BUILD_TERMINAL_STATUSES — NOT IDLE) — the true instant the build went terminal. NOT the max
    timestamp across ALL events (a durable event appended AFTER the terminal status would push the
    anchor past a real post-terminal provider call), and NOT anchored on IDLE (the rest/kill state).
    None if no build-terminal status event is present."""
    best: float | None = None
    for e in events:
        if e.get("kind") != "status":
            continue
        # run.events are raw DB rows {seq,kind,created_at,payload(JSON string)} — the status lives
        # INSIDE `payload`, not as a top-level field. Accept both shapes (flattened + DB-row).
        status = e.get("status")
        if status is None:
            pl = e.get("payload")
            if isinstance(pl, str):
                try:
                    status = (json.loads(pl) or {}).get("status")
                except Exception:
                    status = None
            elif isinstance(pl, dict):
                status = pl.get("status")
        if str(status or "").upper() not in _BUILD_TERMINAL_STATUSES:
            continue
        ep = _event_epoch(e)
        if ep is not None and (best is None or ep > best):
            best = ep
    return best


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
    grace_s: float = 8.0,
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
        ev["lifecycle"] = {"terminal": terminal, "statuses": list(getattr(run, "timeline", []) or [])}

    # provider-after-terminal — [codex] anchor on the TERMINAL STATUS event's epoch (from the FROZEN
    # run.events), then count relay calls whose ts is strictly AFTER it. A post-hoc baseline or a
    # max-of-all-events anchor would hide a call made between terminal and a later durable event.
    # LIVENESS GUARD: trust "0 calls after terminal" ONLY if the relay actually captured THIS run —
    # i.e. it logged >=1 call inside the run window [run_start, terminal]. An empty/stale relay log
    # (relay not wired) yields a meaningless 0; we OMIT the slice instead (-> fail-closed INVALID in
    # live mode) so a false 0 can't classify green. Release happens AFTER the grace window so the
    # token is live during the audited window.
    events = getattr(run, "events", []) or []
    terminal_epoch = _terminal_status_epoch(events)
    run_start_epoch = _min_event_epoch(events)
    calls_after: int | None = None
    if relay_log and os.path.exists(relay_log) and terminal_epoch is not None and run_start_epoch is not None:
        await asyncio.sleep(grace_s)
        try:
            with open(relay_log, encoding="utf-8") as f:
                recs = parse_relay_log(f.read())
            _ts_recs = [
                (float(r["ts"]), bool(r.get("has_tools", True)))
                for r in recs
                if isinstance(r.get("ts"), (int, float))
            ]
            calls_during_run = sum(1 for t, _ in _ts_recs if run_start_epoch <= t <= terminal_epoch)
            if calls_during_run > 0:  # relay PROVEN live for this run → trust the after-count
                # [REL-5b] count only BUILD-driver (tool-bearing) calls after terminal. A tool-less
                # SUMMARIZER call — the async auto-title fired off kick() — is NOT a build runaway, so
                # it must not classify a clean FINISHED run as SIDECAR_NOT_STOPPED. A real post-
                # terminal driver runaway carries the tool catalog (has_tools=True) → still flagged.
                calls_after = sum(1 for t, ht in _ts_recs if t > terminal_epoch and ht)
            else:  # relay captured nothing for this run → 0-after is meaningless → omit (fail-closed)
                calls_after = None
                timeline.append(
                    "REL-5: relay ledger captured 0 calls in this run's window — provider-after-"
                    "terminal not adjudicable (relay not wired for this run)"
                )
        except Exception:
            calls_after = None

    # release — destroy the sandbox + sidecar containers (REL-4 path), then measure orphans.
    released_ok = False
    with contextlib.suppress(Exception):
        resp = await client.kill(cid)
        released_ok = True
        timeline.append(f"REL-5 released {cid} for cleanup adjudication (http {resp.get('http_status')})")
    await asyncio.sleep(4.0)
    after_containers = _live_disco_container_count()

    # sidecar — stopped_at_terminal iff the release tore the containers down; provider calls only if
    # we could actually measure them (else the slice is omitted → oracle skips, honest).
    if calls_after is not None:
        ev["sidecar"] = {"stopped_at_terminal": released_ok, "provider_calls_after_terminal": calls_after}

    # cleanup — orphans = containers attributable to THIS run still alive after release (the count
    # rose during the run and must return to the pre-run baseline). Only populated when BOTH the
    # baseline and the post-release count are known (else omitted → oracle skips, never faked 0).
    if baseline_containers is not None and after_containers is not None:
        orphans = max(0, after_containers - baseline_containers)
        ev["cleanup"] = {"orphans": orphans, "workspace_released": released_ok and orphans == 0}

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
    kernel: str = "disco",
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
    # [REL-5] pre-run orphan baseline (host-side container count) so the CleanupOracle can tell
    # THIS run's leftovers from pre-existing ones. Measured once, before any conversation exists.
    baseline_containers = _live_disco_container_count()
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
        except FollowupPickupError as exc:
            # H1/V2: an after-terminal follow-up was never picked up by the engine (the
            # finalization dead-window) even after a re-kick. This is a HARNESS SEQUENCING
            # failure, not a product verdict — INVALID_RUN so §17 re-runs it. We must NOT drive
            # on the stale terminal (V1's silent-proceed is what collapsed the follow-ups).
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.RUN_INTERRUPTED,
                first_broken_link="followup_send -> no_pickup_before_bound",
                facts=exc.facts,
            )
        except SnapshotNotReadyError as exc:
            # The host workspace snapshot never settled on the build's AGENT-FINAL state
            # within the snapshot-wait budget (a slow/failed flush, or a multi-revision build
            # whose later bytes hadn't flushed). FAIL-FAST → INVALID_RUN rather than launder a
            # stale capture into a false ARTIFACT_TRUTH_MISMATCH or a silent stale PASS; §17
            # re-runs it. Bounded — never hangs.
            return _invalid_run_record(
                out_root,
                run_id,
                scenario,
                exc.reason,
                code=fc.WORKSPACE_SNAPSHOT_NOT_READY,
                first_broken_link="snapshot_flush -> snapshot_behind_agent_final_state",
                facts=exc.facts,
            )
        except Exception as exc:  # noqa: BLE001 — surface the real reason as INVALID_RUN
            return _invalid_run_record(
                out_root, run_id, scenario, f"{type(exc).__name__}: {exc}"
            )

        # [REL-5] Measure terminal cleanup + release BEFORE freezing the dossier, so the
        # lifecycle / sidecar / cleanup oracles ADJUDICATE (instead of SKIP "no evidence
        # (headless run)"). The build's §6 evidence is already frozen into `run` by
        # drive_scenario, so releasing here never races it. Populates run.product_evidence.
        try:
            ev = await _collect_terminal_cleanup_evidence(
                client, run.conversation_id, run,
                baseline_containers=baseline_containers,
                relay_log=_relay_log_path(),
                timeline=getattr(run, "timeline", []),
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
        if _live_measure and _missing:
            return _invalid_run_record(
                out_root, run_id, scenario,
                f"terminal cleanup not adjudicable — missing evidence slice(s): {', '.join(_missing)} "
                "(set MINIMAX_RELAY_LOG and ensure the container probe is available so the "
                "sidecar/cleanup oracles cannot silently SKIP into a green pass)",
                code=fc.RUN_INTERRUPTED,
                first_broken_link="terminal -> cleanup_evidence_unmeasurable",
                facts={"missing_slices": _missing},
            )

        base = assemble_dossier(
            out_root, run_id, scenario, run,
            model=model, autonomous=autonomous, commit=commit, kernel=kernel,
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


_KERNEL_KIND = {"disco": "disco", "pi": "pi_experimental"}


async def _select_build_kernel(app_base_url: str, kernel: str) -> str | None:
    """EPIC K: pin the GLOBAL Build kernel before a soak run (PUT the app-server
    build-kernel config). Returns None on success, else an error string. For the
    experimental Pi kernel the server gate (DISCO_PI_KERNEL_EXPERIMENTAL) MUST be on,
    else the server refuses the selection — fail fast rather than silently soak the
    DiscoKernel under a `--kernel pi` label (which would corrupt the bake-off)."""
    import httpx

    kind = _KERNEL_KIND[kernel]
    url = f"{app_base_url.rstrip('/')}/api/build-kernel/config"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            await c.put(url, json={"kind": kind})
            cfg = (await c.get(url)).json()
    except Exception as exc:  # noqa: BLE001 — surface as INFRA, never a product verdict
        return f"could not reach build-kernel config at {url}: {exc}"
    if cfg.get("kind") != kind:
        return f"kernel select failed: requested {kind!r}, server reports {cfg.get('kind')!r}"
    if kernel == "pi" and not cfg.get("experimental_enabled"):
        return (
            "pi kernel selected but experimental_enabled=false — set "
            "DISCO_PI_KERNEL_EXPERIMENTAL=1 on the agent-server AND app-server"
        )
    return None


async def _amain(args: argparse.Namespace) -> int:
    from .adapters.disco_api import HttpTransport  # live deps only on the CLI path

    scenarios = load_scenarios(args.scenarios)
    if args.scenario not in scenarios:
        print(f"unknown scenario {args.scenario!r}; known: {sorted(scenarios)}", file=sys.stderr)
        return 2
    scenario = scenarios[args.scenario]
    # [Lane A A-B1] FAIL-CLOSED infra preflight: a reliability soak must be able to adjudicate
    # terminal cleanup + provider-after-terminal, which REQUIRES the relay ledger. Refuse to START a
    # positive scenario without it — else run_once's _live_measure is False, the sidecar/cleanup
    # slices are omitted, the oracles SKIP, and the run classifies SKIP-as-PASS. A scenario opts out
    # only by explicitly declaring `requires_relay_ledger: false` (a pure classifier/negative case).
    if scenario.get("requires_relay_ledger", True) and not _relay_log_path():
        print(
            "[build-soak] INFRA_FAILURE: scenario requires the relay ledger to adjudicate terminal "
            "cleanup + provider-after-terminal — set MINIMAX_RELAY_LOG (or PMX_/DISCO_RELAY_LOG). "
            "Refusing to run a soak that could SKIP-as-PASS.",
            file=sys.stderr,
        )
        return 3
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

    # EPIC K: pin the Build kernel for this whole run before any iteration. A failure
    # here is INFRA (the bake-off can't run), not a product verdict.
    kernel_err = await _select_build_kernel(args.app_base_url, args.kernel)
    if kernel_err:
        print(f"[build-soak] kernel-select INFRA failure: {kernel_err}", file=sys.stderr)
        return 3
    print(
        f"[build-soak] kernel={args.kernel} ({_KERNEL_KIND[args.kernel]}) pinned for this run",
        file=sys.stderr,
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
            kernel=args.kernel,
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
    p.add_argument(
        "--kernel",
        choices=("disco", "pi"),
        default="disco",
        help="EPIC K bake-off selector: which Build kernel to drive — disco (default) "
        "or pi (experimental). Sets the GLOBAL build-kernel config before the run.",
    )
    p.add_argument(
        "--app-base-url",
        default="http://127.0.0.1:8800",
        help="app-server base URL (the build-kernel config endpoint lives here)",
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
