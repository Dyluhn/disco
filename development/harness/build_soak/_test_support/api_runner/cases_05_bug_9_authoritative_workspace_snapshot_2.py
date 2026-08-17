"""Moved bug 9 authoritative workspace snapshot 2 collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    CollectedRun,
    DiscoApiClient,
    _successful_browser_verification_paths,
    action,
    assemble_dossier,
    classify,
    classify_dossier,
    classify_run_folder,
    clean_smoke_log,
    json,
    load_manifest,
    pytest,
    run_once,
)
from .helpers_01 import (
    FakeTransport,
    _browser_screenshot_observation,
    _client,
    _h191_direct_browser_events,
    _h191_strict_browser_scenario,
    _h191_verifier_events,
    _host_verifier_verdict,
    _plant_snapshot,
    _seed_db,
    _smoke_scenario,
)


def _impl_test_h191_direct_browser_rejects_preview_stopped_before_navigation():
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path)
    events[-4]["seq"] = 13
    events[-4]["tool_call"]["call_id"] = "call_13"
    events[-3]["seq"] = 14
    events[-3]["tool_result"]["call_id"] = "call_13"
    events[-2]["seq"] = 17
    events[-2]["id"] = "evt_17"
    events[-1]["seq"] = 18
    events[-1]["id"] = "evt_18"
    events[-4:-4] = [
        action(11, "preview_stop", args={}, action_id="act_stop"),
        {
            "id": "evt_12",
            "seq": 12,
            "kind": "observation",
            "source": "environment",
            "action_id": "act_stop",
            "tool_result": {
                "call_id": "call_11",
                "tool_name": "preview_stop",
                "success": True,
                "content": "Stopped previews",
                "structured": {"stopped": ["live-server"]},
            },
        },
    ]

    assert path not in _successful_browser_verification_paths(events)
    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


@pytest.mark.parametrize("browser_action", ["navigate", "screenshot"])
@pytest.mark.parametrize(
    "mutation_tool",
    ["file_write", "safe_write_file", "file_str_replace", "exact_replace", "write_file"],
)
def _impl_test_h191_direct_browser_rejects_proof_stale_after_deliverable_mutation(
    mutation_tool, browser_action
):
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path, browser_action=browser_action)
    events[-2:-2] = [
        action(
            13,
            mutation_tool,
            args={"path": "index.html", "content": "<script>throw Error()</script>"},
            action_id="act_after_proof",
        ),
        {
            "id": "evt_14",
            "seq": 14,
            "kind": "observation",
            "source": "environment",
            "action_id": "act_after_proof",
            "tool_result": {
                "call_id": "call_13",
                "tool_name": mutation_tool,
                "success": True,
                "content": "Wrote index.html",
            },
        },
    ]

    assert path not in _successful_browser_verification_paths(events)
    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


def _impl_test_h191_direct_browser_accepts_fresh_proof_after_preview_then_mutation():
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path)
    events[-4]["seq"] = 13
    events[-4]["tool_call"]["call_id"] = "call_13"
    events[-3]["seq"] = 14
    events[-3]["tool_result"]["call_id"] = "call_13"
    events[-4:-4] = [
        action(
            11,
            "file_write",
            args={"path": "index.html", "content": "<h1>Final content</h1>"},
            action_id="act_final_edit",
        ),
        {
            "id": "evt_12",
            "seq": 12,
            "kind": "observation",
            "source": "environment",
            "action_id": "act_final_edit",
            "tool_result": {
                "call_id": "call_11",
                "tool_name": "file_write",
                "success": True,
                "content": "Wrote index.html",
            },
        },
    ]

    assert _successful_browser_verification_paths(events) == {path}
    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "PASS", record


def _impl_test_h191_unverifiable_or_generic_available_evidence_cannot_spoof_contract():
    path = ".pmx/screenshots/verify.png"
    scenario = _h191_strict_browser_scenario()
    unverifiable = classify(
        _h191_verifier_events(path, passed=False),
        scenario=scenario,
        browser_evidence_paths={path},
        available_evidence={"browser_verification"},
    )

    assert unverifiable["status"] == "FAIL", unverifiable
    assert unverifiable["code"] == "VERIFICATION_GATE_BYPASSED"


@pytest.mark.parametrize(
    "path",
    [
        ".pmx/screenshots/verify.PNG",
        ".pmx/screenshots/verify\x00.png",
    ],
)
def _impl_test_h191_replay_path_admissibility_exactly_matches_h190_capture(path):
    record = classify(
        _h191_verifier_events(path),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "INVALID_RUN", record
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert record["facts"]["missing_or_untrusted_paths"] == [path]


def _impl_test_h191_passing_verifier_with_matching_frozen_path_satisfies_opt_in_contract():
    path = ".pmx/screenshots/verify.png"
    events = _h191_verifier_events(path)

    strict = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )
    legacy = classify(clean_smoke_log(), scenario={"id": "headless", "assertions": {}})

    assert strict["status"] == "PASS", strict
    assert legacy["status"] == "PASS", legacy


def _impl_test_h191_passing_host_verdict_with_matching_frozen_path_satisfies_contract():
    path = ".pmx/screenshots/host-verify.png"

    strict = classify(
        _h191_verifier_events(path, host=True),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert strict["status"] == "PASS", strict


@pytest.mark.parametrize(
    ("path", "verified", "verdict"),
    [
        (".pmx/screenshots/host.png", False, "pass"),
        (".pmx/screenshots/host.png", 1, "pass"),
        (".pmx/screenshots/host.png", True, "fail"),
        (".pmx/screenshots/host.png", True, "PASS"),
        ("../outside.png", True, "pass"),
        (".pmx/screenshots/host.PNG", True, "pass"),
        (None, True, "pass"),
    ],
)
def _impl_test_h191_unverified_failing_or_malformed_host_verdict_does_not_count(
    path, verified, verdict
):
    events = _h191_verifier_events(".pmx/screenshots/unused.png", host=True)
    events[-3] = _host_verifier_verdict(path, seq=12, verified=verified, verdict=verdict)

    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={".pmx/screenshots/host.png"},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED"


def _impl_test_h191_frozen_replay_requires_manifest_locked_verifier_screenshot(tmp_path):
    path = ".pmx/screenshots/verify.png"
    data = b"\x89PNG\r\n\x1a\nstrict-verifier-proof"
    events = _h191_verifier_events(path)
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
        browser_evidence={path: data},
    )
    scenario = _h191_strict_browser_scenario()
    base = assemble_dossier(
        tmp_path,
        "h191_frozen",
        scenario,
        run,
        model="m",
        autonomous=False,
    )

    assert classify_dossier(base, scenario, run, autonomous=False)["status"] == "PASS"
    assert classify_run_folder(base)["status"] == "PASS"

    # A manifest-label rewrite cannot launder some other locked file into browser
    # proof: label, hash entry, and canonical conversation-scoped path must agree.
    manifest_path = base / "manifest.json"
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    browser_label = f"browser-evidence/{path}"
    manifest_raw["evidence_files"][browser_label] = manifest_raw["evidence_files"]["events.jsonl"]
    manifest_raw["evidence_hashes"][browser_label] = manifest_raw["evidence_hashes"]["events.jsonl"]
    manifest_path.write_text(json.dumps(manifest_raw), encoding="utf-8")
    relabeled = classify_run_folder(base)
    assert relabeled["status"] == "INVALID_RUN", relabeled
    assert relabeled["code"] == "MISSING_REQUIRED_EVIDENCE"

    # An unlisted file beside the dossier is not durable proof.  Removing the
    # browser-evidence label from a newly assembled legacy dossier must fail the
    # strict contract on replay even if identical bytes are planted nearby.
    absent = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
    )
    absent_base = assemble_dossier(
        tmp_path,
        "h191_unlocked",
        scenario,
        absent,
        model="m",
        autonomous=False,
    )
    planted = absent_base / "conversations" / _CID / "browser-evidence" / path
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(data)
    replay = classify_run_folder(absent_base)
    assert replay["status"] == "INVALID_RUN", replay
    assert replay["code"] == "MISSING_REQUIRED_EVIDENCE"


@pytest.mark.asyncio
async def _impl_test_h190_collection_error_is_recorded_as_invalid_run(tmp_path):
    content = "<h1>Build Smoke OK</h1>"
    events = clean_smoke_log()
    events[-2]["seq"] = 13
    events[-2]["id"] = "evt_13"
    events[-1]["seq"] = 14
    events[-1]["id"] = "evt_14"
    events[-2:-2] = [
        action(11, "browser", action_id="act_11"),
        _browser_screenshot_observation(".pmx/screenshots/missing.png", seq=12),
    ]
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = FakeTransport(
        db,
        states=["RUNNING", "AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED"],
        workspace={"index.html": content},
        preview_html=content,
    )
    client = _client(transport, tmp_path)

    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_h190_missing_invalid",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="a" * 40,
        repo_revision="a" * 40 + "+dirty.fedcba9876543210",
        repo_dirty=True,
        timeout_s=5,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert record["first_broken_link"] == ("browser_observation -> durable_screenshot_evidence")
    # F3: the invalidation verdict is unchanged, but the durable evidence the
    # harness already held is FROZEN instead of discarded. The browser bytes
    # themselves stay honestly absent — only the missing screenshot invalidates
    # the run; the events/state/inspect slices must survive for diagnosis.
    conv = tmp_path / "out" / "run_h190_missing_invalid" / "conversations" / _CID
    assert (conv / "events.jsonl").exists()
    assert not (conv / "browser-evidence").exists()
    freeze = record["facts"]["evidence_freeze"]
    assert freeze["dossier_written"] is True
    assert "events" in freeze["present"]
    manifest = load_manifest(tmp_path / "out" / "run_h190_missing_invalid")
    assert manifest.repo_revision == "a" * 40 + "+dirty.fedcba9876543210"
    assert manifest.repo_dirty is True


@pytest.mark.asyncio
async def _impl_test_collect_workspace_reads_snapshot_when_preview_proxy_404s(tmp_path):
    # Bug 9: a build genuinely SUCCEEDED (index.html written + FINISHED) but the
    # dev-server preview proxy 404s, so the OLD collect produced an empty manifest →
    # the oracle false-FAILed (FALSE_FINISH_NO_OUTPUT). The authoritative host snapshot
    # has the file; collect_workspace must populate the manifest from it even though
    # the proxy is dead.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    # transport.get_text 404s for everything (the dead preview proxy — the root cause).
    transport = FakeTransport(db, states=["FINISHED"], workspace={})
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" in manifest, manifest
    entry = manifest["index.html"]
    assert entry["present"] is True
    assert "Build Smoke OK" in entry["content"]
    assert entry["size"] == len(b"<h1>Build Smoke OK</h1>")
    assert len(entry["sha256"]) == 64
    # came from the snapshot, NOT the (dead) preview proxy
    assert entry.get("source") != "preview_proxy"


@pytest.mark.asyncio
async def _impl_test_collect_workspace_genuinely_missing_file_is_omitted(tmp_path):
    # Do NOT mask a real missing deliverable: a declared file absent from BOTH the
    # snapshot and the preview proxy must be OMITTED (no present:false key) so the
    # unchanged oracle still emits FALSE_FINISH_NO_OUTPUT.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"other.txt": "unrelated"})  # snapshot exists, no index.html
    transport = FakeTransport(db, states=["FINISHED"], workspace={})  # proxy 404s too
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" not in manifest  # genuinely missing → omitted (FALSE_FINISH stays)
    assert manifest["other.txt"]["present"] is True  # the snapshot is faithfully reflected


@pytest.mark.asyncio
async def _impl_test_collect_workspace_without_projects_root_fails_closed_without_preview_fallback(
    tmp_path,
):
    # Generated preview bytes are not authoritative workspace source, and the
    # authenticated preview-app route is now capability-forbidden. No ProjectStore
    # therefore means no workspace truth, never a hidden served-copy substitution.
    db = tmp_path / "disco.db"

    class _ForbiddenLegacyWorkspacePreview(FakeTransport):
        async def get_text(self, path):
            if "/preview-app/" in path:
                raise AssertionError("workspace collector crossed preview capability boundary")
            return await super().get_text(path)

    transport = _ForbiddenLegacyWorkspacePreview(db, states=["FINISHED"])
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)  # projects_root=None

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest == {}


@pytest.mark.asyncio
async def _impl_test_snapshot_authoritative_does_not_proxy_mask_missing_required_file(tmp_path):
    # Anti-false-PASS hole #1: when the snapshot IS authoritative (its workspace dir exists)
    # but a DECLARED file is absent from it, the proxy must NOT be consulted — even though
    # the proxy WOULD serve a (served/stale) copy. The file stays OMITTED so a genuinely
    # missing required deliverable still trips FALSE_FINISH_NO_OUTPUT.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"other.txt": "snapshot exists, but no index.html"})
    # the proxy WOULD serve index.html (a served/stale version) — it must be ignored:
    transport = FakeTransport(
        db, states=["FINISHED"], workspace={"index.html": "<h1>STALE SERVED COPY</h1>"}
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" not in manifest  # NOT proxy-masked → FALSE_FINISH_NO_OUTPUT preserved
    assert manifest["other.txt"]["present"] is True
