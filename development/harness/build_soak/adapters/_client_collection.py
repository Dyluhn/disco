"""Bounded client evidence collection extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ._api_browser import (
    _jailed_browser_evidence_path,
    _last_terminal,
    _payload,
    _referenced_screenshot_paths,
    _successful_observation_refs,
)
from ._api_types import (
    _FILE_MUTATION_TOOLS,
    _RAW_SHA256_RE,
    FOLLOWUP_PICKED_UP,
    FOLLOWUP_PICKUP_TIMEOUT,
    FOLLOWUP_REPLANNED,
    BrowserEvidenceCollectionError,
)
from ._client_base import _ClientBase


def _events_through_horizon(
    events: list[dict[str, Any]],
    horizon_seq: int | None,
) -> list[dict[str, Any]]:
    if horizon_seq is None:
        return events
    eligible: list[dict[str, Any]] = []
    for event in events:
        try:
            seq = int(event.get("seq", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= seq <= horizon_seq:
            eligible.append(event)
    return eligible


def _browser_manifest_entry(
    workspace_manifest: dict[str, Any],
    *,
    workspace: Path,
    conversation_id: str,
    path: str,
    references: list[str],
) -> dict[str, Any]:
    entry = workspace_manifest.get(path)
    if isinstance(entry, dict) and entry.get("present") is True:
        return entry
    raise BrowserEvidenceCollectionError(
        "referenced browser screenshot is absent from the workspace manifest",
        {
            "conversation_id": conversation_id,
            "path": path,
            "workspace_dir": str(workspace),
            "manifest_entry_count": len(workspace_manifest),
            "manifest_has_pmx_entries": sum(
                1 for key in workspace_manifest if str(key).startswith(".pmx/")
            ),
            "referenced_paths": list(references),
        },
    )


def _read_browser_screenshot(
    source: Path,
    *,
    conversation_id: str,
    path: str,
) -> bytes:
    try:
        return source.read_bytes()
    except OSError as exc:
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot could not be read",
            {
                "conversation_id": conversation_id,
                "path": path,
                "error": type(exc).__name__,
            },
        ) from exc


def _verify_browser_screenshot(
    data: bytes,
    entry: dict[str, Any],
    *,
    conversation_id: str,
    path: str,
    max_file_bytes: int,
) -> None:
    if len(data) > max_file_bytes:
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot exceeds the per-file evidence bound",
            {
                "conversation_id": conversation_id,
                "path": path,
                "size": len(data),
                "max_file_bytes": max_file_bytes,
            },
        )
    actual_sha = hashlib.sha256(data).hexdigest()
    expected_size = entry.get("size")
    expected_sha = entry.get("sha256")
    identity_matches = (
        isinstance(expected_size, int)
        and not isinstance(expected_size, bool)
        and expected_size == len(data)
        and isinstance(expected_sha, str)
        and _RAW_SHA256_RE.fullmatch(expected_sha)
        and expected_sha.lower() == actual_sha
    )
    if identity_matches:
        return
    raise BrowserEvidenceCollectionError(
        "referenced browser screenshot does not match the workspace manifest",
        {
            "conversation_id": conversation_id,
            "path": path,
            "expected_size": expected_size,
            "actual_size": len(data),
            "expected_sha256": expected_sha,
            "actual_sha256": actual_sha,
        },
    )


def _raise_if_browser_total_exceeded(
    total: int,
    *,
    conversation_id: str,
    path: str,
    max_total_bytes: int,
) -> None:
    if total <= max_total_bytes:
        return
    raise BrowserEvidenceCollectionError(
        "referenced browser screenshots exceed the total evidence bound",
        {
            "conversation_id": conversation_id,
            "path": path,
            "total_bytes": total,
            "max_total_bytes": max_total_bytes,
        },
    )


class _CollectionMixin(_ClientBase):
    async def capture_followup_baseline(self, conversation_id: str) -> dict[str, Any]:
        """Snapshot the state needed to detect REAL pickup of the NEXT follow-up (H1),
        captured BEFORE send_followup. `max_seq` is the PRIMARY anchor (V2): pickup and the
        follow-up's own new terminal are both measured as events at seq > max_seq.
          * `max_seq` — the latest durable event seq; the baseline for BOTH the event-sequenced
            pickup detector (a non-user progress event past it) AND the min_terminal_seq drive
            guard (the follow-up's OWN new terminal must have seq > it, not the stale one);
          * `plan_revision` — the latest plan revision so a re-plan bump is detectable;
          * `status` — the build's current (prior-terminal) status, e.g. FINISHED (a
            belt-and-suspenders signal only; a follow-up appended during finalization leaves
            the status UNCHANGED, so the seq — not the status — is what V2 keys on).
        Deliberately does NOT use a bare event-count bump as pickup — the user-message append
        alone bumps the seq, and the append is NOT processing (that red herring is the bug)."""
        status = self._status_of(await self.get_state(conversation_id))
        return {
            "status": status,
            "plan_revision": self._latest_plan_revision(conversation_id),
            "max_seq": self._progress_marker(conversation_id)[1],
        }

    async def wait_for_followup_pickup(
        self,
        conversation_id: str,
        baseline: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> str:
        """SERIALIZE after-terminal follow-ups (H1): after send_followup, BLOCK until the
        engine actually PICKS UP this follow-up — measured against `baseline` (captured
        BEFORE the send) — so the NEXT follow-up is not piled on top of an unprocessed one
        and collapsed into a single plan revision.

        EVENT-SEQUENCED (V2 — V1 was status-only and timed out): pickup is detected from the
        durable EVENT LOG relative to `baseline.max_seq`, NOT from a status change. A follow-up
        appended while the prior run is FINALIZING causes NO status change (status stays
        FINISHED) — V1 watched the status, saw nothing, timed out, and PROCEEDED, letting
        follow-up 2 collapse into follow-up 1. V2 returns as soon as there is REAL pickup
        evidence relative to the baseline:
          * FOLLOWUP_REPLANNED — a re-plan: the latest plan revision bumped above baseline, OR
            a `planning` re-entry status appeared past the baseline seq; OR
          * FOLLOWUP_PICKED_UP — the FIRST non-user PROGRESS event (action / observation / plan
            / agent message / RUNNING-or-gate status) appeared at seq > the baseline seq — the
            engine began processing, EVEN IF the status is still FINISHED. (Belt-and-suspenders:
            a genuine status change off the prior terminal also counts.)

        A new event seq caused MERELY by the user-message append is NOT treated as pickup —
        the append itself is not processing (that is precisely the stale-terminal red herring
        this guards against), so the raw event marker is never a pickup signal.

        BOUNDED by `timeout_s` (default = the policy-driven `_followup_pickup_timeout_s()`):
        on timeout it RETURNS FOLLOWUP_PICKUP_TIMEOUT (never hangs). The CALLER must treat that
        as a SEQUENCING FAILURE — HARD-FAIL the run (do NOT re-send, do NOT drive on the stale
        terminal): re-sending duplicates the user turn (no legal kick-without-append on a
        FINISHED run) and driving on the stale terminal collapses the next follow-up (V1's bug)."""
        if timeout_s is None:
            timeout_s = self._followup_pickup_timeout_s()
        base_status = str(baseline.get("status") or "")
        base_rev = int(baseline.get("plan_revision") or 0)
        base_seq = int(baseline.get("max_seq", -1))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._status_of(await self.get_state(conversation_id))
            # (1) a re-plan signal: a revision bump OR a `planning` re-entry past the baseline.
            if self._latest_plan_revision(conversation_id) > base_rev:
                return FOLLOWUP_REPLANNED
            if self._planning_reentry_since(conversation_id, base_seq):
                return FOLLOWUP_REPLANNED
            # (2) EVENT-SEQUENCED pickup: a non-user progress event past the baseline seq means
            #     the engine started processing — detectable even when the status stays FINISHED.
            if self._progress_event_since(conversation_id, base_seq):
                return FOLLOWUP_PICKED_UP
            # (3) belt-and-suspenders: the build left its prior terminal/resting status.
            if base_status and status and status != base_status:
                return FOLLOWUP_PICKED_UP
            await asyncio.sleep(self._poll)
        return FOLLOWUP_PICKUP_TIMEOUT

    async def wait_for_first_file_write(
        self, conversation_id: str, *, timeout_s: float
    ) -> int | None:
        """Poll the DB for the first executed file-mutation action (the §15.4 trigger).
        Returns its seq, or None on timeout / terminal-without-write.

        A trigger is valid only after a write-family action has a SUCCESSFUL observation.
        Preview/verify/shell actions and failed or still-pending file writes are not a
        disconnect/cancel boundary.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            events = self._read_events(conversation_id)
            success_call_ids, success_action_ids = _successful_observation_refs(events)
            for e in events:
                if e.get("kind") != "action":
                    continue
                tc = _payload(e).get("tool_call") or {}
                name = tc.get("tool_name")
                if name not in _FILE_MUTATION_TOOLS:
                    continue
                call_id = tc.get("call_id")
                action_id = e.get("id") or _payload(e).get("id")
                if (call_id is not None and str(call_id) in success_call_ids) or (
                    action_id is not None and str(action_id) in success_action_ids
                ):
                    return int(e.get("seq", -1))
            # bail early if the run already finished without a write
            if events and _last_terminal(events) is not None:
                return None
            await asyncio.sleep(self._poll)
        return None

    def latest_user_message_seq(self, conversation_id: str) -> int:
        """The highest seq of a USER message in the durable log, or -1 when none yet. Used to
        snapshot the user-turn watermark BEFORE the harness sends a turn (declared follow-up or
        injected auto-answer) so the NEW user-message seq it produces can be attributed to the
        harness (revision-anchor metadata)."""
        best = -1
        try:
            for e in self._read_events(conversation_id):
                if e.get("kind") == "message" and e.get("source") == "user":
                    best = max(best, int(e.get("seq", -1)))
        except sqlite3.Error:
            return -1
        return best

    async def wait_for_new_user_message_seq(
        self, conversation_id: str, *, after_seq: int, timeout_s: float
    ) -> int | None:
        """After the harness sends a user turn, poll the durable log until a USER message with
        seq > `after_seq` appears; return ITS seq (the turn the harness just sent), or None on
        timeout / no new user message (e.g. a `confirm` or `pick_alternative` control frame
        appends NO user message — nothing to attribute). Bounded — never hangs."""
        deadline = time.monotonic() + timeout_s
        while True:
            seq = self.latest_user_message_seq(conversation_id)
            if seq > after_seq:
                return seq
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll)

    def collect_events(self, conversation_id: str) -> list[dict[str, Any]]:
        """Read the FULL event log from disco.db AFTER the run reached terminal —
        the durable, race-free source (the trace_conversation.py pattern). Returns
        DB-row dicts {seq,kind,source,id,created_at,payload(JSON string)} which the
        classifier's normalizer flattens identically to events.jsonl."""
        return self._read_events(conversation_id)

    async def collect_state(self, conversation_id: str) -> dict[str, Any]:
        return await self.get_state(conversation_id)

    async def collect_inspect_trace(self, conversation_id: str) -> dict[str, Any] | None:
        """Fetch the redacted per-conversation routing/model span trace.

        Inspect is intentionally optional at the adapter layer so deterministic
        fake-transport tests and non-campaign callers remain usable. The live CLI
        campaign enforces its presence before counting a run.
        """
        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is not None:
            if not aggregation.finalized:
                await self._sample_inspect_trace(conversation_id)
            return aggregation.render()

        # Compatibility for adapter-only callers that did not create the
        # conversation through this client. Governed runs always use the
        # aggregate path above and the repository gate requires its receipt.
        disposition, data = await self._fetch_inspect_snapshot(conversation_id)
        if disposition != "snapshot":
            return None
        if not isinstance(data, dict):
            return None
        if data.get("conversation_id") != conversation_id or not isinstance(
            data.get("events"), list
        ):
            return None
        return data

    async def collect_workspace(
        self, conversation_id: str, file_paths: list[str]
    ) -> dict[str, Any]:
        """Collect the conversation's workspace files AUTHORITATIVELY (Bug 9 fix).

        ROOT CAUSE this replaces: the old path fetched each declared file via the
        single-origin preview proxy (``GET …/preview-app/<path>``). That proxies the
        agent's DEV SERVER, so it returns the file ONLY when the served preview is up,
        registered, and serving that exact route — fragile. A build that genuinely
        SUCCEEDED (index.html written, served, FINISHED) produced an EMPTY manifest
        because the proxy 404'd → the OutputTruthOracle false-FAILed it
        (FALSE_FINISH_NO_OUTPUT). Verified live: ``…/preview-app/index.html``,
        ``…/workspace/index.html`` (image-only allowlist), and ``…/artifacts/index.html``
        (declared-"files" only; an app deliverable is artifact_kind="app") ALL 404 for a
        finished static build — there is no HTTP route that serves arbitrary workspace
        source. The durable, dev-server-independent truth is the host ProjectStore
        SNAPSHOT (``<projects_root>/<cid>/workspace/…``), the SAME source the product's
        own preview-edit / artifact-download routes fall back to once a run is terminal.

        Returns a manifest keyed by workspace-relative path:
            {path: {"present": True, "size": int, "sha256": hex, "content": str}}
        for EVERY file in the snapshot (a faithful workspace reflection, not just the
        declared paths), plus each declared path under its EXACT scenario-declared key
        so the OutputTruthOracle's ``path in files`` presence check matches. A declared
        file that is genuinely absent is OMITTED (never a present:false key — that would
        mask FALSE_FINISH_NO_OUTPUT into ARTIFACT_TRUTH_MISMATCH), so a real
        missing-deliverable build STILL FAILs correctly. ``content`` is the decoded UTF-8
        text (so must_contain substring checks run on the real file); a binary / oversized
        file keeps present+size+sha256 with empty content.

        There is deliberately NO generated-preview fallback. The capability-isolated
        preview serves an app, not an authoritative arbitrary-source filesystem, and the
        authenticated legacy preview-app route is security-forbidden. With no ProjectStore
        snapshot, the honest manifest is empty and the evidence contract fails closed.
        """
        declared = list(file_paths)
        manifest: dict[str, Any] = {}
        snapshot_dir: Path | None = None

        if self._projects_root is not None:
            manifest, snapshot_dir = await self._await_ready_snapshot(conversation_id, declared)

        # The snapshot is AUTHORITATIVE: return it as-is. A declared file absent from the
        # snapshot stays OMITTED — never proxy-substituted (hole #1).
        if snapshot_dir is not None:
            return manifest

        # No authoritative snapshot means no workspace truth. Never substitute
        # generated/served bytes for source evidence or cross the preview auth boundary.
        return manifest

    async def workspace_snapshot_digest(
        self, conversation_id: str, file_paths: list[str]
    ) -> str | None:
        """Return a deterministic digest of the authoritative committed snapshot.

        Used by restart scenarios to prove that a full App/Agent process restart
        reconstructed the same project authority before any new user turn.
        """
        manifest = await self.collect_workspace(conversation_id, file_paths)
        entries: list[tuple[str, str, int]] = []
        for path, item in sorted(manifest.items()):
            if not isinstance(item, dict) or item.get("present") is not True:
                continue
            sha = item.get("sha256")
            size = item.get("size")
            if not isinstance(sha, str) or type(size) is not int:
                return None
            entries.append((str(path), sha, size))
        if not entries:
            return None
        raw = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def collect_browser_evidence(
        self,
        conversation_id: str,
        events: list[dict[str, Any]],
        workspace_manifest: dict[str, Any],
        *,
        require_verified_host_screenshot: bool = False,
        horizon_seq: int | None = None,
    ) -> dict[str, bytes]:
        """Capture referenced screenshots from the authoritative ProjectStore snapshot.

        Only paths carried by successful ``browser`` / structured verifier observations
        are eligible.  Each path is jailed beneath this conversation's workspace, read in
        full, and cross-checked against the already-collected workspace manifest size and
        SHA256.  Missing, changing, escaping, or over-limit bytes raise
        :class:`BrowserEvidenceCollectionError`; silently dropping visual evidence would
        make a frozen replay look stronger than the live run.

        ``horizon_seq`` binds collection to an accepted event horizon: only
        observations at or before that seq are eligible.  A workspace frozen at a
        ``WorkspaceVersionEvent`` cannot contain a screenshot that was referenced
        *after* that event, so certifying one would assert evidence the frozen bytes
        do not carry.  Events without a usable ``seq`` are dropped rather than
        assumed in-horizon: an unplaceable observation cannot be proven to precede
        the freeze.
        """
        references = _referenced_screenshot_paths(
            _events_through_horizon(events, horizon_seq),
            require_verified_host_screenshot=require_verified_host_screenshot,
        )
        if not references:
            return {}
        if len(references) > self._browser_evidence_max_files():
            raise BrowserEvidenceCollectionError(
                "referenced browser screenshot count exceeds the durable evidence bound",
                {
                    "conversation_id": conversation_id,
                    "referenced_count": len(references),
                    "max_files": self._browser_evidence_max_files(),
                },
            )

        ws = self._collected_workspace_dirs.get(conversation_id) or self._snapshot_workspace_dir(
            conversation_id
        )
        if ws is None:
            raise BrowserEvidenceCollectionError(
                "referenced browser screenshot has no authoritative workspace snapshot",
                {"conversation_id": conversation_id, "paths": references},
            )

        captured: dict[str, bytes] = {}
        total = 0
        for rel in references:
            source = _jailed_browser_evidence_path(ws, rel, conversation_id=conversation_id)
            entry = _browser_manifest_entry(
                workspace_manifest,
                workspace=ws,
                conversation_id=conversation_id,
                path=rel,
                references=references,
            )
            data = _read_browser_screenshot(
                source,
                conversation_id=conversation_id,
                path=rel,
            )
            _verify_browser_screenshot(
                data,
                entry,
                conversation_id=conversation_id,
                path=rel,
                max_file_bytes=self._browser_evidence_max_file_bytes(),
            )
            total += len(data)
            _raise_if_browser_total_exceeded(
                total,
                conversation_id=conversation_id,
                path=rel,
                max_total_bytes=self._browser_evidence_max_total_bytes(),
            )
            captured[rel] = data
        return captured
