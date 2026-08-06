#!/usr/bin/env python3
"""PKG-03-EDIT-EVIDENCE — the LIVE producer of the five P8D edit-evidence slices.

The five targeted/manual-edit oracles have SKIPped in every classification this campaign
has frozen, because the producer their docstrings name ("the P1B-LIVE / soak runner") was
never built. This runner is that producer: it drives a REAL build and a REAL targeted edit
on the governed isolated stack, observes what the product actually did, and hands the
result to the existing capture→classify bridge.

Sequence — every step OBSERVES; none synthesizes:

1. **PAIR** with the agent-server through the product's own dev pairing flow
   (:class:`harness.reliability._runner.api_session.ApiSession`, reused rather than
   re-derived — deriving it again is one of the two errors the A4 boundary recorded).
2. **BUILD** (turn 1) the scenario's page — four id-carrying sections, each with a heading
   and a comment anchor — with a real autonomous build.
3. **OVERRIDE** (turn 2): the USER'S OWN change, asking for one exact verbatim line in a
   section the later edit will not touch. This goes through the product's own follow-up
   surface, because there is **no** user-facing path to hand-edit workspace source between
   turns: measured in run r3 (2026-08-05o), a host-side edit to the ProjectStore workspace is
   silently discarded when the next turn materializes from its own durable capture. Seeding
   there produced a MANUAL_EDIT_CLOBBERED that was an artifact of the seeding point rather
   than a product fact — recorded in this boundary's receipt, and the reason for this shape.
4. **SNAPSHOT `before`** by reading the file BACK from the workspace, never from what we
   believe was written, and REFUSE to continue if the user's line is not actually there:
   with no override in place there is nothing to preserve, and a later PASS would be vacuous.
5. **EDIT** (turn 3) with a targeted follow-up scoped to ONE other section — the later agent
   edit that must not take the user's line, the anchors, or the other labels with it.
6. **SNAPSHOT `after`**, and read the successful edit calls out of the run's own event log,
   counting only calls after turn 2's last seq so the measurement is of the LATER edit.
7. **ASSEMBLE** the five slices via :mod:`edit_evidence`, plus the ``lifecycle`` and
   ``cleanup`` slices this runner genuinely observes, then classify.

Vacuity guards, all fail-closed: the build must produce the file and at least two labelled
sections; the build must have planted at least two anchors; the user's line must be present
in the pre-edit snapshot. An adjudication over an empty or fabricated slice proves nothing,
so this runner fails loudly rather than emit a well-formed but meaningless dossier.

Usage (see the boundary's ``run_edit_evidence.sh`` for the governed invocation)::

    python -m harness.product_build.edit_evidence_run --out <dir> --ledger <path>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from harness.product_build.classify_captured import classify_capture
from harness.product_build.edit_evidence import (
    EditCapture,
    EditObservationError,
    build_edit_slices,
    observe_comment_anchors,
    observe_edited_files,
    observe_screen_labels,
)
from harness.product_build.scenario_runner import STATIC_SITE_EDIT_GOVERNED
from harness.reliability._runner.api_session import ApiSession, ProductError

_TERMINAL = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER", "FAILED", "CANCELLED"}
_TERMINAL_OK = {"FINISHED", "VERIFIED"}

# A section's opening tag WITH an id — the anchor insertion point and the label key.
SECTION_OPEN_RE = re.compile(
    r"""<section\b[^>]*\bid\s*=\s*["']([A-Za-z0-9_-]+)["'][^>]*>""", re.IGNORECASE
)

# The user's hand-written line. Deliberately distinctive: no model produces this by chance,
# so its presence in the post-edit file is evidence of survival rather than of coincidence.
MANUAL_OVERRIDE_SNIPPET = (
    '<p class="owner-note">Hand-written by the owner — MANUAL-OVERRIDE-7Q4X — '
    "please keep this line exactly as it is.</p>"
)

MIN_LABELLED_SECTIONS = 2
MIN_ANCHORS = 2


class LiveRunError(RuntimeError):
    """The live run could not produce honest evidence. Never downgraded to a soft pass."""


def ensure_paired(session: ApiSession, agent: str) -> None:
    """Pair with the agent-server using the product's own dev flow.

    Deliberately NOT ``ApiSession.pair()``: that method requires ``/api/auth/pairing-token``
    to answer 200, which is the fresh-device runner's posture. On the governed isolated stack
    that route answers **403** while ``DISCO_AUTH_DEV_AUTO_PAIR=1`` still mints a session from
    an empty body — so ``pair()`` treats a working stack as a dead one (observed live, and the
    reason this function exists). This mirrors the live spec's proven ``ensurePaired``:

    * ``/api/auth/session`` is a PUBLIC route answering 200 ``{authenticated:false}``, so the
      flag is read rather than the status — gating on ``ok()`` would never pair;
    * the pairing token is best-effort, because minting without one is the supported dev path;
    * mutations then need the session CSRF, not merely the cookie.

    It authenticates the harness; it does not weaken the product's auth.
    """
    state = session.json("GET", agent, "/api/auth/session") or {}
    if state.get("authenticated") is not True:
        token = ""
        try:
            probe = session.json("GET", agent, "/api/auth/pairing-token") or {}
            token = str(probe.get("pairing_token") or "")
        except ProductError:
            token = ""  # route locked down on this stack; auto-pair still mints
        session.json(
            "POST", agent, "/api/auth/mint", data={"pairing_token": token} if token else {}
        )
        state = session.json("GET", agent, "/api/auth/session") or {}
    csrf = str(state.get("csrf_token") or "")
    if state.get("authenticated") is not True or not csrf:
        raise LiveRunError(f"agent-server pairing failed: {state!r}")
    session.csrf = csrf


def _poll_terminal(
    session: ApiSession, agent: str, cid: str, timeout_s: int
) -> tuple[str, list[str]]:
    """Poll to a terminal status, recording the status transitions actually seen."""
    statuses: list[str] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = session.json("GET", agent, f"/conversations/{cid}/state") or {}
        status = str(state.get("execution_status") or "")
        if status and (not statuses or statuses[-1] != status):
            statuses.append(status)
        if status in _TERMINAL:
            return status, statuses
        time.sleep(5)
    return "TIMEOUT", statuses


def _run_turn(
    session: ApiSession, agent: str, cid: str, prompt: str, timeout_s: int
) -> tuple[str, list[str]]:
    """Send one follow-up turn and poll it to a terminal."""
    session.json("POST", agent, f"/conversations/{cid}/followup", data={"content": prompt})
    return _poll_terminal(session, agent, cid, timeout_s)


def _all_events(session: ApiSession, agent: str, cid: str) -> list[dict[str, Any]]:
    """The conversation's full event log, paged to exhaustion."""
    out: list[dict[str, Any]] = []
    after = 0
    for _ in range(200):
        path = f"/conversations/{cid}/events?limit=200&after_seq={after}"
        body = session.json("GET", agent, path)
        batch = (body or {}).get("events") or []
        if not batch:
            break
        out.extend(batch)
        after = int(batch[-1].get("seq") or after)
    return out


def _last_seq(events: list[dict[str, Any]]) -> int:
    return max((int(e.get("seq") or 0) for e in events), default=0)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def locate_workspace_file(projects_root: Path, conversation_id: str, relpath: str) -> Path:
    """The durable workspace copy of ``relpath`` for this conversation.

    ``<projects_root>/<cid>/workspace/<relpath>`` — the ProjectStore layout, keyed on the
    conversation this runner created, so there is nothing to disambiguate.

    Measured on this governed stack (run r2, 2026-08-05o): the process backend's per-instance
    dirs are ephemeral — they live under a ``disco-sbx-*`` root, are torn down with the
    session, and were already empty by the time the build reached FINISHED. The configured
    ``sandbox.workspace_root`` directory was never even created. The project store is
    therefore the only durable workspace a user could hand-edit between turns, which is
    exactly the state a self-hosted owner edits.
    """
    target = projects_root / conversation_id / "workspace" / relpath
    if not target.is_file():
        raise LiveRunError(
            f"no workspace copy of {relpath!r} for {conversation_id} at {target} — "
            "the build did not persist the file this scenario edits"
        )
    return target


def require_sections(html: str) -> list[str]:
    """The id-carrying sections the build produced, or a loud failure.

    A label oracle over fewer than two sections cannot distinguish "the unedited sections
    held" from "there were no unedited sections", so too few is an unusable observation
    rather than a lenient one.
    """
    sections = [m.group(1) for m in SECTION_OPEN_RE.finditer(html)]
    if len(sections) < MIN_LABELLED_SECTIONS:
        raise LiveRunError(
            f"the build produced {len(sections)} id-carrying <section> elements; "
            f"at least {MIN_LABELLED_SECTIONS} are needed for a non-vacuous label observation"
        )
    return sections


def _provider_records(ledger_path: Path) -> list[dict[str, Any]]:
    """The run's own provider calls, from the stack's append-only ledger."""
    if not ledger_path.is_file():
        raise LiveRunError(f"provider ledger absent: {ledger_path}")
    records = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            records.append({"host": row.get("host"), "model": row.get("model")})
    if not records:
        raise LiveRunError(f"provider ledger empty: {ledger_path} — the run made no real calls")
    return records


def _observe_cleanup(session: ApiSession, agent: str, cid: str) -> dict[str, Any]:
    """Release the conversation and observe cleanup from the PRODUCT'S OWN kill response.

    Mirrors the governed runner's adjudication (2xx + ``killed`` + IDLE) rather than
    inferring release from the absence of containers; ``orphans`` is scoped to this
    conversation. Under the process backend no container is created, so none can survive.
    """
    released = False
    try:
        body = session.json("POST", agent, f"/conversations/{cid}/kill") or {}
        released = bool(body.get("killed")) and str(
            (body.get("state") or {}).get("execution_status") or ""
        ) == "IDLE"
    except Exception as exc:  # noqa: BLE001 — a failed kill is an observation, not a crash
        print(f"[edit-evidence] kill failed: {exc}", file=sys.stderr)
    return {"orphans": 0, "workspace_released": released, "scope": "conversation"}


def _guard_observations(before: str, after: str) -> None:
    """Refuse a run whose observations would be well-formed but empty."""
    anchors = observe_comment_anchors(before)
    if len(anchors) < MIN_ANCHORS:
        raise LiveRunError(f"only {len(anchors)} anchors observed pre-edit; need {MIN_ANCHORS}")
    labels_before = observe_screen_labels(before)
    if len(labels_before) < MIN_LABELLED_SECTIONS:
        raise LiveRunError(
            f"only {len(labels_before)} labelled sections observed pre-edit; "
            f"need {MIN_LABELLED_SECTIONS} — a label oracle over one section proves nothing"
        )
    observe_screen_labels(after)  # a post-edit page that no longer parses is a failed run


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Drive the live run and return its classification."""
    scenario = STATIC_SITE_EDIT_GOVERNED
    contract = scenario.edit_contract
    assert contract is not None, "the edit scenario must declare an edit contract"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    agent = args.agent_url.rstrip("/")
    if not args.projects_root:
        raise LiveRunError(
            "--projects-root is required (the isolated stack exports DISCO_PROJECTS_ROOT "
            "into its child's environment; outside one, pass it explicitly)"
        )

    session = ApiSession(app_base=agent, agent_base=agent, origin=agent)
    ensure_paired(session, agent)

    created = session.json(
        "POST", agent, "/conversations", data={"surface": "build", "autonomous": True}
    )
    cid = str((created or {}).get("conversation_id") or "")
    if not cid:
        raise LiveRunError(f"no conversation id in {created!r}")
    print(f"[edit-evidence] conversation {cid}")

    session.json(
        "POST", agent, f"/conversations/{cid}/messages", data={"content": scenario.build_prompt}
    )
    build_terminal, build_statuses = _poll_terminal(session, agent, cid, args.build_timeout)
    print(f"[edit-evidence] build terminal={build_terminal}")
    if build_terminal not in _TERMINAL_OK:
        raise LiveRunError(
            f"build did not reach a clean terminal: {build_terminal} {build_statuses}"
        )

    target = locate_workspace_file(Path(args.projects_root), cid, contract.target_path)
    built = target.read_text(encoding="utf-8")
    (out / "built.html").write_text(built, encoding="utf-8")

    print(f"[edit-evidence] built sections: {require_sections(built)}")

    # TURN 2 — the USER'S OWN change, through the product's own follow-up surface.
    override_terminal, override_statuses = _run_turn(
        session, agent, cid, scenario.override_prompt, args.edit_timeout
    )
    print(f"[edit-evidence] override turn terminal={override_terminal}")
    # Read BACK from the workspace: `before` is the state the LATER edit was handed, and it
    # must genuinely contain the user's line. If the product did not apply it, there is no
    # manual override to preserve and any later PASS would be vacuous.
    before = target.read_text(encoding="utf-8")
    (out / "before.html").write_text(before, encoding="utf-8")
    if MANUAL_OVERRIDE_SNIPPET not in before:
        raise LiveRunError(
            "the user's verbatim override line is absent from the workspace after the "
            f"override turn (terminal={override_terminal}); there is no manual edit to preserve"
        )

    seq_before_edit = _last_seq(_all_events(session, agent, cid))

    # TURN 3 — the LATER AGENT EDIT that must not take the user's line with it.
    edit_terminal, edit_statuses = _run_turn(
        session, agent, cid, scenario.edit_prompt, args.edit_timeout
    )
    print(f"[edit-evidence] edit terminal={edit_terminal}")

    after = target.read_text(encoding="utf-8")
    (out / "after.html").write_text(after, encoding="utf-8")

    events = _all_events(session, agent, cid)
    edited_files = observe_edited_files(events, after_seq=seq_before_edit)
    print(f"[edit-evidence] edited_files={edited_files}")

    _guard_observations(before, after)

    capture_obj = EditCapture(
        path=contract.target_path,
        before=before,
        after=after,
        final_files={contract.target_path: after},
        overrides={contract.target_path: MANUAL_OVERRIDE_SNIPPET},
        edited_files=tuple(edited_files),
        expected_files=contract.expected_files,
        edited_sections=contract.edited_sections,
        edit_scope=contract.edit_scope,
        max_churn_ratio=contract.max_churn_ratio,
    )
    product_evidence = dict(build_edit_slices(capture_obj))
    statuses = [
        str(e.get("status")) for e in events if e.get("kind") == "status" and e.get("status")
    ]
    product_evidence["lifecycle"] = {
        "terminal": edit_terminal,
        "statuses": statuses or build_statuses + override_statuses + edit_statuses,
    }
    product_evidence["cleanup"] = _observe_cleanup(session, agent, cid)

    capture = {
        "product_evidence": product_evidence,
        "provider_records": _provider_records(Path(args.ledger)),
        "events": events,
        "autonomous": True,
        "run_id": cid,
        "scenario_id": scenario.id,
        "notes": {
            "build_terminal": build_terminal,
            "override_turn_terminal": override_terminal,
            "edit_turn_terminal": edit_terminal,
            "seq_before_edit": seq_before_edit,
        },
    }
    _write_json(out / "captured.json", capture)
    result = classify_capture(out / "captured.json", out / "dossier")
    _write_json(out / "classification.json", result)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="evidence directory for this run")
    parser.add_argument(
        "--projects-root",
        default=os.environ.get("DISCO_PROJECTS_ROOT", ""),
        help="the stack's ProjectStore root (defaults to the stack's own DISCO_PROJECTS_ROOT)",
    )
    parser.add_argument("--ledger", required=True, help="the stack's provider-call ledger")
    # The isolated stack exports DISCO_RELIABILITY_AGENT_URL into its child's env; the
    # historical :8000 default keeps a non-isolated invocation working unchanged.
    parser.add_argument(
        "--agent-url",
        default=os.environ.get("DISCO_RELIABILITY_AGENT_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--build-timeout", type=int, default=600)
    parser.add_argument("--edit-timeout", type=int, default=600)
    args = parser.parse_args(argv[1:])
    try:
        result = run(args)
    except (LiveRunError, EditObservationError) as exc:
        # A failed OBSERVATION is a first-class outcome: it says the product did not exhibit
        # something the producer must see. It is reported, never smoothed into a pass.
        print(json.dumps({"status": "OBSERVATION_FAILED", "reason": str(exc)}, indent=2))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
