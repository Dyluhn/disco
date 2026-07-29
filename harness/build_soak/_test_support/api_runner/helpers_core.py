"""Shared helpers for the moved collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    _EVENTS_DDL,
    UTC,
    Any,
    CollectedRun,
    DiscoApiClient,
    action,
    clean_smoke_log,
    datetime,
    json,
    load_scenarios,
    msg,
    plan,
    sqlite3,
    status,
)


def _fake_inspect_trace() -> dict[str, Any]:
    scope = {
        "mode": "planning",
        "attempt": 1,
        "complete": True,
        "offered_tools": ["file_read", "submit_plan"],
        "allowed_tools": ["file_read", "submit_plan"],
        "offered_count": 2,
        "allowed_count": 2,
    }
    routing = {"chosen_model": "m"}
    span = {"span": "agent.step", "event": "end"}
    events = [
        {"seq": 1, "kind": "routing", **routing},
        {"seq": 2, "kind": "span", **span},
        {"seq": 3, "kind": "tool_scope", **scope},
    ]
    return {
        "conversation_id": _CID,
        "event_count": len(events),
        "dropped_event_count": 0,
        "routing_decisions": [routing],
        "spans": [span],
        "tool_scopes": [scope],
        "progress_shadows": [],
        "events": events,
    }


def _seed_db(path, cid, events):
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


def _smoke_scenario():
    # Most runner tests below isolate lifecycle, preview, provider, timeout, or
    # replay behavior with the historical event-only smoke fixture.  Keep those
    # tests scoped to their named predicate; H191's production browser contract is
    # exercised explicitly by the strict tests beside the H190 capture coverage.
    scenario = json.loads(json.dumps(load_scenarios()["static_html_minimal"]))
    scenario["assertions"].pop("browser_verification")
    return scenario


def _smoke_log_with_file_write(path, content):
    """clean_smoke_log's plan→approve→execute→finish shape, but the executed action is a
    file_write of `content` to `path` with a MATCHING observation call_id — so the snapshot
    readiness gate records a PROVEN raw_sha capture for that file (vs clean_smoke_log's generic
    shell action, which leaves the declared file unproven)."""
    return [
        msg(1, "user", "build a page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
        status(5, "RUNNING", "plan_approved"),
        status(6, "RUNNING"),
        {
            "id": "act7",
            "seq": 7,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_write",
                "arguments": {"path": path, "content": content},
                "call_id": "cw7",
            },
        },
        {
            "id": "evt_8",
            "seq": 8,
            "kind": "observation",
            "source": "environment",
            "action_id": "act7",
            "tool_result": {
                "call_id": "cw7",
                "tool_name": "file_write",
                "success": True,
                "content": "wrote",
            },
        },
        msg(9, "agent", "done", role="assistant"),
        status(10, "FINISHED"),
    ]


def _strict_live_thrash_monitor() -> dict[str, Any]:
    detected_at = datetime(2026, 7, 15, 21, 0, tzinfo=UTC).timestamp() + 9
    return {
        "enabled": True,
        "sample_count": 2,
        "minimum_confirmation_samples": 2,
        "findings": [
            {
                "detected_at_epoch": detected_at,
                "terminal_status": "RUNNING",
                "event_count": 10,
                "max_event_seq": 10,
                "confirmation_samples": 2,
                "oracle_results": [
                    {
                        "oracle": "ThrashOracle",
                        "status": "FAIL",
                        "code": "MODEL_REPAIR_THRASH",
                    }
                ],
            }
        ],
    }


def _plant_snapshot(root, cid, files):
    """Write `files` ({relpath: text}) into the host ProjectStore layout
    <root>/<cid>/workspace/<relpath> the runner reads for collect_workspace."""
    ws = root / cid / "workspace"
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def _client(transport, tmp_path, *, require_workspace_commit: bool = False):
    projects_root = tmp_path / "projects"
    if transport.workspace:
        _plant_snapshot(projects_root, transport.cid, transport.workspace)
    return DiscoApiClient(
        transport,
        db_path=str(tmp_path / "disco.db"),
        poll_interval_s=0.0,
        projects_root=str(projects_root),
        require_workspace_commit=require_workspace_commit,
    )


def _workspace_commit(seq: int, digest: str, *, version_seq: int = 1) -> dict[str, Any]:
    """Historical v1 marker, intentionally unsealed."""

    return {
        "id": f"workspace_version_{seq}",
        "seq": seq,
        "kind": "workspace_version",
        "source": "system",
        "version_seq": version_seq,
        "tree_digest": digest,
        "trigger": "finish",
    }


def _sealed_workspace_commit(
    seq: int,
    version: Any,
    *,
    terminal_seq: int = 10,
    latest_effect_seq: int | None = 8,
    digest: str | None = None,
    file_count: int | None = None,
    total_bytes: int | None = None,
    conversation_id: str = _CID,
) -> dict[str, Any]:
    """Schema-v1 final marker bound to one immutable ProjectStore version."""

    effective_digest = version.tree_digest if digest is None else digest
    marker = _workspace_commit(seq, effective_digest, version_seq=version.seq)
    marker["final_seal"] = {
        "schema_version": 1,
        "scope": {"namespace": "workspace.tree", "identifier": conversation_id},
        "terminal_seq": terminal_seq,
        "latest_effect_seq": latest_effect_seq,
        "version_seq": version.seq,
        "tree_digest": effective_digest,
        "file_count": version.file_count if file_count is None else file_count,
        "total_bytes": version.total_bytes if total_bytes is None else total_bytes,
    }
    return marker


def _browser_screenshot_observation(
    path: Any,
    *,
    seq: int = 11,
    tool_name: str = "browser",
    success: bool = True,
) -> dict[str, Any]:
    structured: dict[str, Any] = {"screenshot_path": path}
    if tool_name in {"verify_web_app", "verify_appkit_app"}:
        structured.update(
            {
                "passed": True,
                "verdict": "pass",
                "http_status": 200,
                "meaningful_content": True,
                "console_errors": [],
                "network_failures": [],
            }
        )
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "action_id": f"act_{seq - 1}",
        "tool_result": {
            "call_id": f"call_{seq - 1}",
            "tool_name": tool_name,
            "success": success,
            "content": f"screenshot: {path}",
            "structured": structured,
        },
    }


def _host_verifier_verdict(
    screenshot_path: Any,
    *,
    seq: int = 11,
    verified: Any = True,
    verdict: Any = "pass",
) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "verifier_verdict",
        "source": "system",
        "artifact_path": "index.html",
        "artifact_kind": "app",
        "verified": verified,
        "verdict": verdict,
        "screenshot_path": screenshot_path,
        "failures": [],
    }


def _workspace_restore_event(*, seq: int, restored_marker: bool = False) -> dict[str, Any]:
    if restored_marker:
        return {
            "id": f"evt_{seq}",
            "seq": seq,
            "kind": "workspace_restored",
            "source": "user",
            "version_seq": 1,
            "tree_digest": "0" * 64,
            "label": "",
        }
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "workspace_mutation",
        "source": "system",
        "operation": "version.restore",
        "paths": ["."],
    }


def _h191_strict_browser_scenario() -> dict[str, Any]:
    return {
        "id": "strict_browser_verification",
        "assertions": {"browser_verification": {"required": True}},
    }


def _h191_verifier_events(
    path: str, *, passed: bool = True, host: bool = False
) -> list[dict[str, Any]]:
    events = clean_smoke_log()
    events[-2]["seq"] = 13
    events[-2]["id"] = "evt_13"
    events[-1]["seq"] = 14
    events[-1]["id"] = "evt_14"
    if host:
        events[-2:-2] = [
            _host_verifier_verdict(
                path,
                seq=12,
                verified=passed,
                verdict="pass" if passed else "fail",
            )
        ]
    else:
        verifier = _browser_screenshot_observation(path, seq=12, tool_name="verify_web_app")
        verifier["tool_result"]["structured"]["passed"] = passed
        verifier["tool_result"]["structured"]["verdict"] = "pass" if passed else "fail"
        events[-2:-2] = [
            action(11, "verify_web_app", action_id="act_11"),
            verifier,
        ]
    return events


def _preview_start_observation(port: int) -> dict[str, Any]:
    return {
        "id": "evt_10",
        "seq": 10,
        "kind": "observation",
        "source": "environment",
        "action_id": "act_preview",
        "tool_result": {
            "call_id": "call_9",
            "tool_name": "preview_start",
            "success": True,
            "content": "Preview running",
            "structured": {
                "name": "live-server",
                "port": port,
                "status": "running",
                "url": "http://127.0.0.1:44973",
            },
        },
    }


def _strict_direct_browser_observation(
    path: str, *, url: str = "http://localhost:8000/"
) -> dict[str, Any]:
    event = _browser_screenshot_observation(path, seq=12, tool_name="browser")
    event["tool_result"]["structured"].update(
        {
            "ok": True,
            "url": url,
            "title": "Live Server",
            "text": "Live Server Up",
            "elements": [],
            "console": [],
            "network": [],
            "visible_semantic_elements": 1,
            "freshness": {
                "requested_epoch": 3,
                "synchronized_epoch": 3,
                "sync_performed": True,
                "page_kind": "local_preview",
            },
        }
    )
    return event


def _h191_direct_browser_events(
    path: str,
    *,
    selected_port: int = 8000,
    action_url: str | None = None,
    observed_url: str | None = None,
    browser_action: str = "navigate",
    omit_action_url: bool = False,
) -> list[dict[str, Any]]:
    events = clean_smoke_log()
    events[-2]["seq"] = 15
    events[-2]["id"] = "evt_15"
    events[-1]["seq"] = 16
    events[-1]["id"] = "evt_16"
    browser_url = action_url or f"http://localhost:{selected_port}/"
    browser_args = {"action": browser_action}
    if not omit_action_url:
        browser_args["url"] = browser_url
    events[-2:-2] = [
        action(
            9,
            "preview_start",
            args={"name": "live-server", "command": "python -m http.server"},
            action_id="act_preview",
        ),
        _preview_start_observation(selected_port),
        action(
            11,
            "browser",
            args=browser_args,
            action_id="act_11",
        ),
        _strict_direct_browser_observation(path, url=observed_url or browser_url),
    ]
    return events


class _FakeClock:
    """Drives `_await_ready_snapshot`'s poll loop deterministically: monotonic() returns a
    virtual clock that only advances when the loop sleeps, and each sleep can mutate the
    on-disk snapshot (the rev-1 → rev-2 flush) via `on_poll(step)`."""

    def __init__(self, on_poll=None):
        self.t = 1000.0
        self.polls = 0
        self.on_poll = on_poll

    def monotonic(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll(self.polls)


def _install_clock(monkeypatch, clock):
    from harness.build_soak.adapters import disco_api as _mod

    monkeypatch.setattr(_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_mod.asyncio, "sleep", clock.sleep)


def _file_write_log(path, content, *, call="c1", first_seq=1, structured=None):
    """A minimal user → file_write(action) → SUCCESSFUL observation → FINISHED log. The action
    and observation share `call` so the readiness gate correlates the write as successful."""
    events = [
        {
            "id": "u1",
            "seq": first_seq,
            "kind": "message",
            "source": "user",
            "message": {"role": "user", "content": "build it"},
        },
        {
            "id": f"a{first_seq + 1}",
            "seq": first_seq + 1,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_write",
                "arguments": {"path": path, "content": content},
                "call_id": call,
            },
        },
        {
            "id": f"o{first_seq + 2}",
            "seq": first_seq + 2,
            "kind": "observation",
            "source": "environment",
            "tool_result": {
                "call_id": call,
                "tool_name": "file_write",
                "success": True,
                "content": "wrote",
            },
        },
        {
            "id": f"s{first_seq + 3}",
            "seq": first_seq + 3,
            "kind": "status",
            "source": "system",
            "status": "FINISHED",
        },
    ]
    if structured is not None:
        events[2]["tool_result"]["structured"] = structured
    return events


def _seal_refusal_disclosure(seq, blocking):
    """The PRODUCT's typed content-seal-refusal disclosure event (REL-27)."""
    return {
        "id": f"m{seq}",
        "seq": seq,
        "kind": "message",
        "source": "environment",
        "message": {
            "role": "user",
            "content": (
                "<system-reminder>\nProject persistence note: snapshot failed: "
                "final workspace snapshot was incomplete: "
                + "; ".join(blocking)
                + "\n</system-reminder>"
            ),
        },
        "meta": {
            "persistence_failure": {
                "kind": "seal_incomplete_content",
                "blocking": list(blocking),
            }
        },
    }


def _file_read_full_content(text):
    """The EXACT rendered content a FULL file_read returns for `text`: a `[lines 1-N of N]`
    header then file_read's `<line-no>\\t<line>` body (mirrors FileReadTool's full-read path)."""
    lines = text.splitlines()
    total = len(lines)
    width = len(str(total)) or 1
    body = "\n".join(f"{i + 1:>{width}}\t{lines[i]}" for i in range(total))
    return f"[lines 1-{total} of {total}]\n" + body


def _edit_then_read_log(path, final_text, *, read_content=None, include_read=True):
    """user → file_edit(action+obs) → [optional file_read(action+obs)] → FINISHED. The file_edit
    is a partial mutator (no reconstructable content); the file_read (when present) is the FULL
    readback carrying `final_text` unless `read_content` overrides it (paged/synthetic cases)."""
    log = [
        {
            "id": "u1",
            "seq": 1,
            "kind": "message",
            "source": "user",
            "message": {"role": "user", "content": "revise it"},
        },
        {
            "id": "a2",
            "seq": 2,
            "kind": "action",
            "source": "agent",
            "tool_call": {"tool_name": "file_edit", "arguments": {"path": path}, "call_id": "m1"},
        },
        {
            "id": "o3",
            "seq": 3,
            "kind": "observation",
            "source": "environment",
            "tool_result": {
                "call_id": "m1",
                "tool_name": "file_edit",
                "success": True,
                "content": "edited",
            },
        },
    ]
    if include_read:
        rc = read_content if read_content is not None else _file_read_full_content(final_text)
        log += [
            {
                "id": "a4",
                "seq": 4,
                "kind": "action",
                "source": "agent",
                "tool_call": {
                    "tool_name": "file_read",
                    "arguments": {"path": path},
                    "call_id": "r1",
                },
            },
            {
                "id": "o5",
                "seq": 5,
                "kind": "observation",
                "source": "environment",
                "tool_result": {
                    "call_id": "r1",
                    "tool_name": "file_read",
                    "success": True,
                    "content": rc,
                },
            },
        ]
    log.append({"id": "s9", "seq": 9, "kind": "status", "source": "system", "status": "FINISHED"})
    return log


def _unverifiable_browser_observation(seq):
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "tool_result": {
            "tool_name": "verify_web_app",
            "success": True,
            "structured": {
                "passed": False,
                "verdict": "unverifiable",
                "browser_unavailable": True,
                "http_status": 200,
            },
        },
    }


def _alternatives_event(seq, alt_id, options):
    """An AlternativesEvent (the AWAITING_USER_DECISION gate) carrying choosable options."""
    return {
        "id": alt_id,
        "seq": seq,
        "kind": "alternatives",
        "source": "agent",
        "failed_action_id": "act_fail",
        "summary": "pick a recovery path",
        "options": options,
    }


def _sandbox_preflight_helper_fixture(detail: str) -> tuple[CollectedRun, dict, dict]:
    events = [msg(1, "user", "build"), status(2, "ERROR", detail=detail)]
    decision = {
        "role": "agent_driver",
        "chosen_model": "configured-model",
        "provider": "configured-provider",
        "path": "manual",
        "reason": "shared driver preflight success",
        "attempt": 1,
        "overflow_triggers": ["driver_preflight"],
    }
    trace = {
        "conversation_id": _CID,
        "event_count": 1,
        "dropped_event_count": 0,
        "routing_decisions": [decision],
        "spans": [],
        "tool_scopes": [],
        "events": [{"seq": 1, "kind": "routing", **decision}],
    }
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "ERROR"},
        workspace_manifest={},
        preview=None,
        inspect_trace=trace,
    )
    scenario = {"assertions": {"provider": {"model": "configured-model"}}}
    return run, trace, scenario


def _append_event(db_path, cid, seq, *, kind="action", source="agent"):
    """Append ONE new durable event — the runner's progress signal (advancing max seq)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cid,
                seq,
                f"evt_{seq}",
                kind,
                source,
                "",
                json.dumps({"seq": seq, "kind": kind, "source": source}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
