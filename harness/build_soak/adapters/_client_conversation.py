"""Bounded client conversation lifecycle extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import asyncio
import io
import posixpath
import sqlite3
import time
import zipfile
from typing import Any

from ._api_browser import (
    _choose_alternative,
    _payload,
)
from ._api_transport import (
    _classify_conn_error,
)
from ._api_types import (
    _PRECREATE_INFRA_ERRORS,
    AWAITING_USER_DECISION,
    InfraProbeError,
)
from ._client_base import _ClientBase


def _safe_import_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("import_fixture file paths must be non-empty strings")
    normalized = posixpath.normpath(raw_path.replace("\\", "/"))
    if (
        normalized in {"", ".", ".."}
        or normalized.startswith("../")
        or posixpath.isabs(normalized)
        or normalized != raw_path.replace("\\", "/")
    ):
        raise ValueError(f"unsafe import_fixture path: {raw_path!r}")
    return normalized


def _import_fixture_text(text_spec: Any) -> str:
    if isinstance(text_spec, str):
        return text_spec
    valid_generator = (
        isinstance(text_spec, dict)
        and set(text_spec) == {"line_template", "count"}
        and isinstance(text_spec.get("line_template"), str)
        and type(text_spec.get("count")) is int
        and 1 <= int(text_spec["count"]) <= 100_000
    )
    if not valid_generator:
        raise ValueError(
            "import_fixture file contents must be UTF-8 text or a bounded "
            "line_template/count generator"
        )
    template = str(text_spec["line_template"])
    return "".join(
        template.replace("{{row}}", str(row)) for row in range(1, int(text_spec["count"]) + 1)
    )


def _import_fixture_archive(
    fixture: dict[str, Any],
) -> tuple[str, dict[Any, Any], bytes, int]:
    filename = fixture.get("filename")
    files = fixture.get("files")
    if not isinstance(filename, str) or not filename.endswith(".zip"):
        raise ValueError("import_fixture.filename must be a .zip filename")
    if not isinstance(files, dict) or not files:
        raise ValueError("import_fixture.files must be a non-empty mapping")
    archive = io.BytesIO()
    total_bytes = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for raw_path, text_spec in sorted(files.items()):
            normalized = _safe_import_path(raw_path)
            encoded = _import_fixture_text(text_spec).encode("utf-8")
            total_bytes += len(encoded)
            bundle.writestr(normalized, encoded)
    return filename, files, archive.getvalue(), total_bytes


def _selected_version(
    versions: list[Any],
    selector: str,
) -> dict[str, Any] | None:
    valid = [row for row in versions if isinstance(row, dict) and type(row.get("seq")) is int]
    if not valid:
        return None
    if selector != "previous":
        return min(valid, key=lambda row: int(row["seq"]))
    ordered = sorted(valid, key=lambda row: int(row["seq"]), reverse=True)
    return ordered[1] if len(ordered) > 1 else ordered[0]


def _workspace_restore_evidence(
    status: int,
    result: dict[str, Any],
    selected_seq: int,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "ok": 200 <= status < 300 and result.get("restored") == selected_seq,
        "http_status": status,
        "selected_seq": selected_seq,
        "restored": result.get("restored"),
        "new_version": result.get("new_version"),
        "tree_digest": result.get("tree_digest"),
    }
    if status >= 400:
        evidence["error"] = result.get("detail", result or None)
    return evidence


class _ConversationMixin(_ClientBase):
    async def pre_create_probe(self, model: str | None) -> None:
        """Probe the agent-server (and, transitively, the model provider) BEFORE
        creating a conversation. Raise InfraProbeError on a matched signature; the
        caller maps that to INFRA_FAILURE with bounded retry (per infra_signatures).

        We probe the server health endpoint — a connection error / 5xx here is a
        pre-create, runner-side infra condition. (A provider 429/5xx that only shows
        up mid-loop is POST-create and is therefore a PRODUCT outcome, never infra.)
        """
        try:
            status, _body = await self._t.health()
        except _PRECREATE_INFRA_ERRORS as exc:
            raise InfraProbeError(
                "agent_server_unreachable_before_conversation",
                {
                    "component": "agent_server",
                    "error_kind": _classify_conn_error(exc),
                    "error": str(exc),
                },
            ) from exc
        if status in (500, 502, 503, 504):
            raise InfraProbeError(
                "provider_5xx_before_conversation",
                {"component": "agent_server", "http_status": status},
            )
        if status == 429:
            raise InfraProbeError(
                "provider_429_before_conversation",
                {"component": "agent_server", "http_status": status},
            )

    async def create_build_conversation(
        self,
        prompt: str,
        *,
        model: str | None = None,
        autonomous: bool = False,
        appkit: bool = False,
        surface: str = "build",
        import_fixture: dict[str, Any] | None = None,
        verification_requirements: dict[str, Any] | None = None,
    ) -> str:
        """POST /conversations then POST the user message (which the route appends
        AND kicks). Returns the conversation_id."""
        if surface not in {"build", "agent"}:
            raise ValueError(f"unsupported soak conversation surface: {surface!r}")
        if import_fixture is not None:
            if surface != "build" or appkit:
                raise ValueError("import fixtures are supported only for Freeform Build")
            if autonomous:
                raise ValueError("import fixtures must use the interactive approval path")
            cid = await self._create_imported_conversation(import_fixture)
            self.last_conversation_id = cid
            await self.start_inspect_collection(cid)
            mstatus, _ = await self._t.post_json(
                f"/conversations/{cid}/messages",
                {
                    "content": prompt,
                    **(
                        {"verification_requirements": verification_requirements}
                        if verification_requirements is not None
                        else {}
                    ),
                },
            )
            if mstatus >= 400:
                raise RuntimeError(f"post_message failed: HTTP {mstatus}")
            return cid

        body: dict[str, Any] = {"surface": surface, "autonomous": autonomous}
        if appkit:
            # EPIC F strict AppKit mode — the phase-based allowlist build. The
            # appkit soak lane (deadlock regression cbfec1fd) sets this per
            # scenario; the route flips runtime.set_appkit_mode at create time.
            body["appkit_mode"] = True
        if model:
            body["model_override"] = model
        status, data = await self._t.post_json("/conversations", body)
        if status >= 400 or "conversation_id" not in data:
            raise RuntimeError(f"create_conversation failed: HTTP {status} {data}")
        cid = str(data["conversation_id"])
        # Record the created cid IMMEDIATELY (before the kick) so the runner can tear it
        # down even if the subsequent message POST / drive raises — a created-but-abandoned
        # conversation must never leak as a RUNNING build on the shared server.
        self.last_conversation_id = cid
        # BF2: inspect collection starts at the first instant the harness owns
        # the conversation id, before POST /messages can kick a model request.
        await self.start_inspect_collection(cid)
        # POST the user task — appends the USER message AND kicks the loop. AppKit
        # lanes mirror the production UI's initial frame: `build_brief` present makes
        # the route classify a Build Brief from the prompt (codex finding #11 — without
        # it the contract-activation path never fires for the soak).
        mbody: dict[str, Any] = {"content": prompt}
        if appkit:
            mbody["build_brief"] = {}
        if verification_requirements is not None:
            mbody["verification_requirements"] = verification_requirements
        mstatus, _ = await self._t.post_json(f"/conversations/{cid}/messages", mbody)
        if mstatus >= 400:
            raise RuntimeError(f"post_message failed: HTTP {mstatus}")
        return cid

    async def _create_imported_conversation(self, fixture: dict[str, Any]) -> str:
        """Seed a real imported project through ``/api/projects/import``.

        The fixture is deliberately small and data-only: a safe archive filename
        plus a mapping of relative POSIX paths to UTF-8 text. The product endpoint
        remains responsible for its own independent archive validation and project
        provenance; the runner merely constructs the user-supplied zip bytes.
        """
        filename, files, archive, total_bytes = _import_fixture_archive(fixture)
        status, data = await self._t.post_file(
            "/api/projects/import",
            field="file",
            filename=filename,
            content=archive,
            content_type="application/zip",
        )
        if status >= 400 or "conversation_id" not in data:
            raise RuntimeError(f"import_project failed: HTTP {status} {data}")
        cid = str(data["conversation_id"])
        accepted = data.get("files") == len(files) and data.get("bytes") == total_bytes
        self.scenario_evidence["import"] = {
            "accepted": accepted,
            "file_count": data.get("files"),
            "bytes": data.get("bytes"),
            "filename": filename,
        }
        return cid

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan via the REAL WS plan gate (routes/ws.py:95)."""
        await self._t.ws_control(conversation_id, {"type": "approve_plan"})

    async def confirm(self, conversation_id: str) -> None:
        """Confirm a pending RISKY ACTION via the REAL WS confirmation gate
        (routes/ws.py:89 -> runtime.confirm -> ControlOps.confirm -> loop.confirm() + kick).
        This is the confirmation ANALOGUE of approve_plan, NOT a free-text message: a plain
        user `send_message` does NOT clear a WAITING_FOR_CONFIRMATION gate — only the dedicated
        `confirm` control frame executes the pending action and resumes the loop past the gate."""
        await self._t.ws_control(conversation_id, {"type": "confirm"})

    async def resolve_decision(
        self, conversation_id: str, *, preferred_option_id: str | None = None
    ) -> dict[str, Any] | None:
        """Auto-resolve an AWAITING_USER_DECISION gate by picking one of the agent's proposed
        alternatives — the runner ACTS AS THE USER for a non-interactive soak. Routed through
        the REAL decision mechanism: the WS `pick_alternative` frame (routes/ws.py:108 →
        runtime.pick_alternative), NOT approve_plan (a plan gate) — the loop synthesizes the
        picked option's ToolCall as the next action and resumes.

        STATE-BIND before selecting (never auto-resolve the wrong / a stale branch): re-read
        the CURRENT conversation state and require it is STILL AWAITING_USER_DECISION with a
        LIVE `pending_alternatives_id` (the state machine recomputes this from the full event
        log every read, so it is always the live pending gate). Bind the options to THAT id by
        reading the matching AlternativesEvent from the durable log; require a non-empty option
        list with valid ids. Selection: the caller's `preferred_option_id` (scenario override)
        if it names a valid option, else a recommended option (recommendation flag), else the
        first valid option.

        Returns {alternatives_id, option_id} on a successful pick, or None when the gate is no
        longer live / the payload is invalid (no pending id, no valid options) — the caller
        treats None as a HARD signal (never a clean pass)."""
        state = await self.get_state(conversation_id)
        if self._status_of(state) != AWAITING_USER_DECISION:
            return None
        alt_id = state.get("pending_alternatives_id")
        if not alt_id:
            return None
        options = self._alternatives_options(conversation_id, str(alt_id))
        chosen = _choose_alternative(options, preferred_option_id)
        if chosen is None:
            return None
        await self._t.ws_control(conversation_id, {"type": "pick_alternative", "option_id": chosen})
        return {"alternatives_id": str(alt_id), "option_id": chosen}

    def _alternatives_options(
        self, conversation_id: str, alternatives_id: str
    ) -> list[dict[str, Any]]:
        """The option list of the AlternativesEvent whose id == `alternatives_id` (the live
        pending gate), read from the durable event log. Empty when absent / malformed."""
        try:
            events = self._read_events(conversation_id)
        except sqlite3.Error:
            return []
        for e in events:
            if e.get("kind") != "alternatives":
                continue
            p = _payload(e)
            if str(e.get("id") or p.get("id") or "") != str(alternatives_id):
                continue
            opts = p.get("options")
            if isinstance(opts, list):
                return [o for o in opts if isinstance(o, dict)]
        return []

    async def resume(self, conversation_id: str) -> dict[str, Any]:
        """Resume a PAUSED (cooperative / actionless) run — the runner ACTS AS THE
        USER who hits Resume. POST /conversations/{cid}/resume (conversations.py:284,
        the same mode-agnostic path the WS `resume` frame uses). A 409 (not resumable)
        is returned as-is so the caller can stop retrying.

        The HTTP code is returned under `http_status` (NOT `status`): the resume body
        itself carries a ``status`` field (e.g. ``{"ok": true, "status": "RUNNING"}``), so
        merging it under the same key would clobber the HTTP int with the body's STATE
        STRING — the caller's ``int(resp["status"])`` then crashed the whole drive with
        ``ValueError: invalid literal for int() ... 'RUNNING'`` the moment a build paused."""
        status, data = await self._t.post_json(f"/conversations/{conversation_id}/resume", {})
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def pause(self, conversation_id: str) -> None:
        """Request the real cooperative pause control over the product WebSocket."""
        await self._t.ws_control(conversation_id, {"type": "pause"})

    async def restore_workspace_version(
        self, conversation_id: str, *, selector: str = "oldest"
    ) -> dict[str, Any]:
        """Restore a durable project version through the public history routes.

        The product publishes its `workspace_version` events as part of the
        FINISHED finalization pipeline, shortly AFTER the terminal status flips
        (observed ~1.3s on a loaded host; the UI consumes the event stream, so a
        human cannot lose this race). Keying the drive on the terminal status
        alone therefore needs the same bounded flush wait the workspace snapshot
        read already applies (`snapshot_wait_s`): poll the public versions route
        to the deadline, then adjudicate availability truthfully. A build whose
        versions never publish still fails as `versions_unavailable`.
        """
        deadline = time.monotonic() + self._snapshot_wait_s
        while True:
            status, data = await self._t.get_json(f"/conversations/{conversation_id}/versions")
            versions = data.get("versions") if isinstance(data, dict) else None
            if status < 400 and isinstance(versions, list) and versions:
                break
            if time.monotonic() >= deadline:
                return {"ok": False, "http_status": status, "reason": "versions_unavailable"}
            await asyncio.sleep(0.5)
        selected = _selected_version(versions, selector)
        if selected is None:
            return {"ok": False, "http_status": status, "reason": "versions_malformed"}
        selected_seq = int(selected["seq"])
        restore_status, result = await self._t.post_json(
            f"/conversations/{conversation_id}/versions/{selected_seq}/restore", {}
        )
        return _workspace_restore_evidence(restore_status, result, selected_seq)

    async def download_project(self, conversation_id: str) -> tuple[int, bytes]:
        status, content, _headers = await self._t.get_bytes(
            f"/api/projects/{conversation_id}/download"
        )
        return status, content

    async def kill(self, conversation_id: str) -> dict[str, Any]:
        """KILL (force-terminate) a conversation the runner is DONE with — the hygiene
        teardown so an abandoned RUNNING / PAUSED / AWAITING build the runner stopped
        watching (inconclusive cutoff, error path, or post-evidence release) does not
        leak and load the shared server. POST /conversations/{cid}/kill
        (conversations.py:275 — halts the agent, tears down its sandbox, revokes its
        capabilities). IDEMPOTENT: a kill on an already-terminal conversation is harmless.

        The HTTP code is returned under `http_status` (mirrors :meth:`resume`); the route
        body is ``{"killed": true, "state": {...}}``. Returns ``{"http_status": int, ...}``."""
        status, data = await self._t.post_json(f"/conversations/{conversation_id}/kill", {})
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def send_followup(
        self, conversation_id: str, text: str, *, kind: str = "message"
    ) -> None:
        """Send a follow-up over the REAL WS path. `kind`:
        * "message"      -> {"type":"send_message"}  (after-terminal user follow-up:
                            appended + kick; the PRODUCT decides to re-plan)
        * "steer"        -> {"type":"steer"}         (mid-run redirect of a RUNNING
                            agent — the §15.4 after-first-file-write injection)
        * "request_plan" -> {"type":"request_plan"}  (explicit re-plan gate)
        """
        if kind == "steer":
            frame = {"type": "steer", "steer_text": text}
        elif kind == "request_plan":
            frame = {"type": "request_plan", "content": text}
        else:
            frame = {"type": "send_message", "content": text}
        await self._t.ws_control(conversation_id, frame)
