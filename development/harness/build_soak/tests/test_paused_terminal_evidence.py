"""F-26 desired-contract reproduction, driven through the REAL `run_once` end-to-end
(hermetic: real SQLite events DB, a real immutable `ProjectStore` version, a fake
transport — no live model spend).

THE DEFECT (proven from sealed evidence): a build-soak run whose drive ends with a
terminal PAUSED status (resume budget `_MAX_RESUMES=3` exhausted,
`development/harness/build_soak/run.py:1005-1035` returns drive status PAUSED) falls through the
evidence gate at `run.py:1615-1649` — PAUSED is not in `terminal_snapshot_states`
(`run.py:1381`) and the drive status is neither `INACTIVE_TIMEOUT` nor
`LIVE_THRASH_STOP` — so `terminal_snapshot_available=True`. Strict
`collect_workspace` (`require_workspace_commit=True`) then finds no exact
`SYSTEM FINISHED` terminal (the latest durable status is `IDLE` after the harness's
own kill) and honestly returns an EMPTY manifest — the H339 carve-out,
`adapters/disco_api.py:3349-3350`. `run.py:1711`'s `collect_browser_evidence` then
sees the referenced screenshot against that empty manifest and raises
`BrowserEvidenceCollectionError`; the handler at `run.py:3267` converts the whole run
into `INVALID_RUN` / `MISSING_REQUIRED_EVIDENCE`
(`first_broken_link="browser_observation -> durable_screenshot_evidence"`). This
destroys the designed product adjudication (`BUILD_DID_NOT_FINISH`,
`oracles/output_truth.py:218-245`) even when truthful evidence exists: the product
emitted a PAUSED-triggered `workspace_version` event and a real immutable
`ProjectStore` version containing every referenced screenshot.

SEALED CERT10 EVIDENCE for this exact defect: trial
`build_soak_p4_ff_react_steer_20260728_055429_825271_003`, conversation
`conv_4c025e93e7ad47bfb8ce665658d52ef6` — durable status `PAUSED@320`, a system
`workspace_version` marker (`v3`, tree_digest `bc7ae1f7396f...`) `@321`, then
`IDLE@322` from the harness's own kill. A later provider-free replay against that
exact immutable version at `horizon_seq=321` found all nine referenced screenshots
and succeeded (9/9) — the run genuinely had the evidence the live collector refused
to look for. (`test_missing_screenshot_names_the_state_judged.py` diagnoses the same
finding at the refusal-payload unit level; this module drives the real `run_once`
end-to-end to prove the top-level classification defect it causes.)

Both tests below express the DESIRED post-fix contract and are RED today: `run_once`
returns `INVALID_RUN` / `MISSING_REQUIRED_EVIDENCE` even though (test 1) a real,
byte-identical immutable `ProjectStore` version exists, and (test 2) even without one
the run still owes an honest `BUILD_DID_NOT_FINISH` adjudication rather than an
evidence-collection invalidation. Diagnostic-and-desired-contract only: no oracle
threshold, ordering, or acceptance-rule changes. Hermetic — builds its own workspace
under `tmp_path`, no campaign paths, no skips.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from _eventlog import action, msg, observation, plan, status
from harness.build_soak import failure_codes as fc
from harness.build_soak.adapters.disco_api import DiscoApiClient
from harness.build_soak.run import run_once

_CID = "conv_paused_terminal_f26"
_REL_SCREENSHOT = ".pmx/screenshots/0001-navigate.png"
_INDEX_HTML = "<h1>paused build</h1>"
_SCREENSHOT_BYTES = b"\x89PNG\r\n\x1a\nfake-f26-screenshot-bytes-0001-navigate"

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
"""


def _seed_db(path: Path, cid: str, events: list[dict[str, Any]]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_EVENTS_DDL)
        for e in events:
            conn.execute(
                "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    cid,
                    e["seq"],
                    e.get("id", f"evt_{e['seq']}"),
                    e["kind"],
                    e["source"],
                    e.get("timestamp", ""),
                    json.dumps(e),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _plant_workspace(root: Path, cid: str) -> Path:
    """Write the head workspace `<root>/<cid>/workspace/…` — the SAME layout both
    `collect_workspace` and `ProjectStore.cut_version` read (`store.path_for`)."""
    ws = root / cid / "workspace"
    (ws / ".pmx" / "screenshots").mkdir(parents=True, exist_ok=True)
    (ws / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (ws / ".pmx" / "screenshots" / "0001-navigate.png").write_bytes(_SCREENSHOT_BYTES)
    return ws


def _browser_observation(seq: int, action_id: str) -> dict[str, Any]:
    """A successful browser observation carrying a `screenshot_path` under
    `tool_result.structured` — the shape `_referenced_screenshot_paths`
    (`adapters/disco_api.py:3772`) walks."""
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "action_id": action_id,
        "tool_result": {
            "call_id": f"call_{seq}",
            "tool_name": "browser",
            "success": True,
            "content": f"navigated and captured {_REL_SCREENSHOT}",
            "structured": {"screenshot_path": _REL_SCREENSHOT},
        },
    }


def _workspace_version_event(seq: int, *, version_seq: int, tree_digest: str) -> dict[str, Any]:
    """The PAUSED-triggered `workspace_version` marker cert10 shows landing durably
    right after a PAUSED status — the marker the live collector never consults."""
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "workspace_version",
        "source": "system",
        "version_seq": version_seq,
        "tree_digest": tree_digest,
        "trigger": "PAUSED",
    }


def _base_events() -> list[dict[str, Any]]:
    """user -> plan -> approve -> file_write(index.html) -> browser observation
    (screenshot referenced). Callers append the terminal PAUSED status (+ optional
    workspace_version marker)."""
    return [
        msg(1, "user", "build a page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
        status(5, "RUNNING", "plan_approved"),
        action(
            6, "file_write", args={"path": "index.html", "content": _INDEX_HTML}, action_id="act6"
        ),
        observation(7, "act6", tool="file_write"),
        action(8, "browser", args={"url": "http://preview/"}, action_id="act8"),
        _browser_observation(9, "act8"),
    ]


def _scenario() -> dict[str, Any]:
    return {
        "id": "paused_terminal_evidence_f26",
        "prompt": "build a page",
        "assertions": {
            "workspace": {"files": [{"path": "index.html"}]},
            "browser_verification": {"required": True},
        },
    }


class FakeTransport:
    """Scripts the live surfaces the drive touches. Self-contained copy of the
    established `test_api_runner.FakeTransport` shape (not imported — see the task
    note on not importing test-module internals across files), trimmed to what this
    module's PAUSED-terminal drive actually exercises."""

    def __init__(self, db_path: Path, *, states: list[str], cid: str = _CID) -> None:
        self.db_path = db_path
        self._states = list(states)
        self._idx = 0
        self.cid = cid
        self.ws_frames: list[dict[str, Any]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._killed = False

    async def health(self):
        return 200, {"ok": True}

    async def post_json(self, path: str, body: dict[str, Any]):
        self.posts.append((path, body))
        if path == "/conversations":
            return 200, {"conversation_id": self.cid, "surface": "build"}
        if path.endswith("/kill"):
            self._killed = True
            try:
                with closing(sqlite3.connect(str(self.db_path))) as conn, conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE conversation_id = ?",
                        (self.cid,),
                    ).fetchone()
                seq = int(row[0] if row else 0) + 1
                killed = status(seq, "IDLE", "killed")
                conn = sqlite3.connect(str(self.db_path))
                try:
                    conn.execute(
                        "INSERT INTO events (conversation_id, seq, id, kind, source, "
                        "created_at, payload) VALUES (?,?,?,?,?,?,?)",
                        (
                            self.cid,
                            killed["seq"],
                            killed["id"],
                            killed["kind"],
                            killed["source"],
                            "",
                            json.dumps(killed),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass
            return 200, {
                "killed": True,
                "state": {"execution_status": "IDLE"},
                "sandbox_instance_ids": [],
            }
        if path.endswith("/resume"):
            return 200, {"ok": True, "status": "RUNNING"}
        return 200, {"event_id": "e", "seq": 1}

    async def get_json(self, path: str):
        if path == f"/api/debug/trace/{self.cid}":
            return 200, {
                "conversation_id": self.cid,
                "event_count": 0,
                "dropped_event_count": 0,
                "routing_decisions": [],
                "spans": [],
                "tool_scopes": [],
                "progress_shadows": [],
                "events": [],
            }
        if path.endswith("/state"):
            if self._killed:
                return 200, {"execution_status": "IDLE"}
            st = self._states[min(self._idx, len(self._states) - 1)]
            self._idx += 1
            return 200, {"execution_status": st}
        if path.endswith("/preview"):
            return 200, {"available": True}
        return 200, {}

    async def get_text(self, path: str):
        return 404, "", {}

    async def post_file(self, path: str, *, field, filename, content, content_type):
        return 200, {"conversation_id": self.cid, "files": 0, "bytes": 0, "title": filename}

    async def get_bytes(self, path: str):
        return 200, b"", {"content-type": "application/zip"}

    async def fetch_isolated_preview(self, conversation_id: str):
        return 200, "", {"cache-control": "private, no-store"}

    async def ws_control(self, conversation_id: str, frame: dict[str, Any]) -> None:
        self.ws_frames.append(frame)


def _client(db: Path, projects_root: Path, states: list[str]) -> DiscoApiClient:
    return DiscoApiClient(
        FakeTransport(db, states=states),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects_root),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )


# The drive-status shape cert10 traced: RUNNING -> AWAITING_PLAN_APPROVAL (approved) ->
# RUNNING -> PAUSED, clamped — so the harness resumes 3x (its bounded valve) and the
# 4th PAUSED falls through as the terminal drive status (`run.py:1005-1035`).
_PAUSED_DRIVE_STATES = ["RUNNING", "AWAITING_PLAN_APPROVAL", "RUNNING", "PAUSED"]


@pytest.mark.asyncio
async def test_a_paused_terminal_run_is_adjudicated_not_invalidated(tmp_path: Path) -> None:
    """Desired F-26 contract, RED today.

    cert10: trial `build_soak_p4_ff_react_steer_20260728_055429_825271_003`, conv
    `conv_4c025e93e7ad47bfb8ce665658d52ef6` — durable `PAUSED@320`, system
    `workspace_version` marker `v3` (tree_digest `bc7ae1f7396f...`) `@321`, `IDLE@322`
    (the harness's kill). A provider-free replay against that exact immutable version
    at `horizon_seq=321` found all nine referenced screenshots and succeeded (9/9).

    Here: a REAL immutable `ProjectStore` version is cut from a workspace holding
    `index.html` and `.pmx/screenshots/0001-navigate.png`, and a PAUSED-triggered
    `workspace_version` event durably references it right after the terminal PAUSED
    status — exactly cert10's shape. The desired fix must adjudicate the run
    (FAIL / BUILD_DID_NOT_FINISH) using that real evidence instead of invalidating it
    for evidence the product actually recorded.
    """
    from disco.tools.projects.store import ProjectStore

    db = tmp_path / "disco.db"
    projects_root = tmp_path / "projects"

    _plant_workspace(projects_root, _CID)
    version = ProjectStore(str(projects_root)).cut_version(_CID, trigger="PAUSED")
    assert version is not None, "setup: the immutable version must actually cut"

    events = [
        *_base_events(),
        status(10, "PAUSED"),
        _workspace_version_event(11, version_seq=version.seq, tree_digest=version.tree_digest),
    ]
    _seed_db(db, _CID, events)

    client = _client(db, projects_root, _PAUSED_DRIVE_STATES)
    record = await run_once(
        client,
        _scenario(),
        run_id="run_paused_f26_with_version",
        out_root=str(tmp_path / "out"),
        model="fake-model",
        autonomous=False,
        commit="a" * 40,
        timeout_s=0.2,
        hard_cap_s=5.0,
    )

    assert record["status"] == fc.FAIL and record["code"] == fc.BUILD_DID_NOT_FINISH, (
        "F-26 desired contract: a PAUSED terminal backed by a real immutable "
        "ProjectStore version must be honestly adjudicated (FAIL / "
        f"BUILD_DID_NOT_FINISH), never invalidated for missing evidence. Got: {record}"
    )
    # `run_once` returns the CLASSIFICATION dict (classify_dossier), which carries no
    # evidence payloads — the collected evidence lives in the dossier the run wrote
    # (run.py `_write_dossier`: conversations/<cid>/workspace-manifest.json + exact
    # screenshot bytes under browser-evidence/<rel>). Assert against those bytes.
    conv_dir = tmp_path / "out" / "run_paused_f26_with_version" / "conversations" / _CID
    manifest = json.loads((conv_dir / "workspace-manifest.json").read_text(encoding="utf-8"))
    assert _REL_SCREENSHOT in manifest, (
        "F-26 desired contract: the paused-terminal immutable version's referenced "
        f"screenshot must be in the collected workspace manifest. Got: {sorted(manifest)}"
    )
    shot = conv_dir / "browser-evidence" / _REL_SCREENSHOT
    assert shot.read_bytes() == _SCREENSHOT_BYTES, (
        "F-26 desired contract: the referenced screenshot's exact bytes must be "
        "captured from the verified paused version"
    )


@pytest.mark.asyncio
async def test_a_paused_terminal_without_a_version_event_stays_fail_closed(
    tmp_path: Path,
) -> None:
    """Desired F-26 contract, RED today.

    cert10: trial `build_soak_p4_ff_react_steer_20260728_055429_825271_003`, conv
    `conv_4c025e93e7ad47bfb8ce665658d52ef6` — durable `PAUSED@320`, system
    `workspace_version` marker `v3` (tree_digest `bc7ae1f7396f...`) `@321`, `IDLE@322`.
    That trial DID carry the version marker; this test is its negative twin — the
    same PAUSED-terminal shape but with NO `workspace_version` marker and no cut
    immutable version at all (e.g. the product crashed before it could snapshot).

    Desired: the run must still be honestly adjudicated (FAIL / BUILD_DID_NOT_FINISH)
    rather than invalidated — with the workspace evidence honestly recorded as NOT
    collected (no fabricated files), since no real version exists to certify it.
    """
    db = tmp_path / "disco.db"
    projects_root = tmp_path / "projects"

    _plant_workspace(projects_root, _CID)
    # Deliberately NO ProjectStore.cut_version call and NO workspace_version event.

    events = [
        *_base_events(),
        status(10, "PAUSED"),
    ]
    _seed_db(db, _CID, events)

    client = _client(db, projects_root, _PAUSED_DRIVE_STATES)
    record = await run_once(
        client,
        _scenario(),
        run_id="run_paused_f26_without_version",
        out_root=str(tmp_path / "out"),
        model="fake-model",
        autonomous=False,
        commit="a" * 40,
        timeout_s=0.2,
        hard_cap_s=5.0,
    )

    assert record["status"] == fc.FAIL and record["code"] == fc.BUILD_DID_NOT_FINISH, (
        "F-26 desired contract: a PAUSED terminal with NO version evidence at all "
        "must still be honestly adjudicated (FAIL / BUILD_DID_NOT_FINISH), never "
        f"invalidated as a missing-evidence harness defect. Got: {record}"
    )
    # Honesty: with no real cut version, nothing may be fabricated — neither into the
    # dossier's workspace manifest (flat or `files`-wrapped not-collected shape) nor
    # as screenshot bytes under browser-evidence/.
    conv_dir = tmp_path / "out" / "run_paused_f26_without_version" / "conversations" / _CID
    manifest = json.loads((conv_dir / "workspace-manifest.json").read_text(encoding="utf-8"))
    flat_and_wrapped = {**manifest, **(manifest.get("files") or {})}
    assert _REL_SCREENSHOT not in flat_and_wrapped, (
        "honesty check: with no real cut version, the screenshot must never be "
        f"fabricated into the collected manifest. Got manifest={sorted(flat_and_wrapped)}"
    )
    assert not (conv_dir / "browser-evidence" / _REL_SCREENSHOT).exists(), (
        "honesty check: no screenshot bytes may be captured without a verified version"
    )
