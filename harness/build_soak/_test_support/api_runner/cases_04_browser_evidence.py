"""Moved H190/H191 browser-evidence collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    _disco_mod,
    _successful_browser_verification_paths,
    action,
    assemble_dossier,
    classify,
    classify_run_folder,
    clean_smoke_log,
    load_manifest,
    pytest,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _browser_screenshot_observation,
    _fake_inspect_trace,
    _h191_direct_browser_events,
    _h191_strict_browser_scenario,
    _h191_verifier_events,
    _host_verifier_verdict,
    _plant_snapshot,
    _seed_db,
    _smoke_scenario,
    _workspace_restore_event,
)


def _impl_test_h190_referenced_browser_screenshot_is_retained_byte_identical_and_locked(tmp_path):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>Build Smoke OK</h1>"
    ws = _plant_snapshot(proj, _CID, {"index.html": content})
    screenshot_rel = ".pmx/screenshots/0001-navigate.png"
    screenshot = b"\x89PNG\r\n\x1a\n\x00visual-proof\xff\x00"
    screenshot_path = ws / screenshot_rel
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(screenshot)
    verifier_rel = ".pmx/screenshots/0002-verify.png"
    verifier_screenshot = b"\x89PNG\r\n\x1a\nverifier-proof"
    (ws / verifier_rel).write_bytes(verifier_screenshot)
    events = clean_smoke_log()
    events[-2]["seq"] = 15
    events[-2]["id"] = "evt_15"
    events[-1]["seq"] = 16
    events[-1]["id"] = "evt_16"
    events[-2:-2] = [
        action(11, "browser", action_id="act_11"),
        _browser_screenshot_observation(screenshot_rel, seq=12),
        action(13, "verify_web_app", action_id="act_13"),
        _browser_screenshot_observation(verifier_rel, seq=14, tool_name="verify_web_app"),
    ]
    _seed_db(db, _CID, events)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
    )
    workspace = client._read_snapshot_manifest(_CID, ["index.html"], ws)

    captured = client.collect_browser_evidence(_CID, events, workspace)

    assert captured == {
        screenshot_rel: screenshot,
        verifier_rel: verifier_screenshot,
    }
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest=workspace,
        preview={
            "health": {"status": 200},
            "content": content,
            "available": True,
            "source": "isolated_path_capability",
        },
        browser_evidence=captured,
        inspect_trace=_fake_inspect_trace(),
    )
    base = assemble_dossier(
        tmp_path / "out",
        "run_h190_browser_evidence",
        _smoke_scenario(),
        run,
        model="m",
        autonomous=False,
    )
    frozen = base / "conversations" / _CID / "browser-evidence" / screenshot_rel
    manifest = load_manifest(base)
    label = f"browser-evidence/{screenshot_rel}"

    assert frozen.read_bytes() == screenshot
    assert manifest.evidence_files[label] == (
        f"conversations/{_CID}/browser-evidence/{screenshot_rel}"
    )
    assert manifest.evidence_hashes[label].startswith("sha256:")
    verifier_label = f"browser-evidence/{verifier_rel}"
    assert manifest.evidence_hashes[verifier_label].startswith("sha256:")
    assert (
        base / "conversations" / _CID / "browser-evidence" / verifier_rel
    ).read_bytes() == verifier_screenshot
    assert verify_evidence_unchanged(base, manifest).intact
    assert classify_run_folder(base)["status"] == "PASS"

    frozen.write_bytes(screenshot + b"tampered")
    replayed = classify_run_folder(base)
    assert replayed["status"] == "INVALID_RUN"
    assert replayed["code"] == "EVIDENCE_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("path", "plant_rel"),
    [
        (".pmx/screenshots/missing.png", None),
        ("../outside.png", "../outside.png"),
        ("secrets.bin", "secrets.bin"),
    ],
)
def _impl_test_h190_missing_or_escaping_screenshot_fails_closed(tmp_path, path, plant_rel):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"index.html": "ok"})
    if plant_rel is not None:
        planted = ws / plant_rel
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(b"must-not-be-copied")
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        projects_root=str(proj),
    )
    manifest = client._read_snapshot_manifest(_CID, ["index.html"], ws)

    with pytest.raises(BrowserEvidenceCollectionError):
        client.collect_browser_evidence(_CID, [_browser_screenshot_observation(path)], manifest)


@pytest.mark.parametrize(
    ("constant", "limit", "paths"),
    [
        (
            "_BROWSER_EVIDENCE_MAX_FILES",
            1,
            [".pmx/screenshots/a.png", ".pmx/screenshots/b.png"],
        ),
        ("_BROWSER_EVIDENCE_MAX_FILE_BYTES", 2, [".pmx/screenshots/a.png"]),
        (
            "_BROWSER_EVIDENCE_MAX_TOTAL_BYTES",
            5,
            [".pmx/screenshots/a.png", ".pmx/screenshots/b.png"],
        ),
    ],
)
def _impl_test_h190_screenshot_bounds_fail_closed_without_truncation(
    tmp_path, monkeypatch, constant, limit, paths
):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {path: "abc" for path in paths})
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        projects_root=str(proj),
    )
    manifest = client._read_snapshot_manifest(_CID, [], ws)
    events = [_browser_screenshot_observation(path, seq=11 + i) for i, path in enumerate(paths)]
    monkeypatch.setattr(_disco_mod, constant, limit)

    with pytest.raises(BrowserEvidenceCollectionError):
        client.collect_browser_evidence(_CID, events, manifest)


def _impl_test_h190_screenshot_manifest_hash_mismatch_fails_closed(tmp_path):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    rel = ".pmx/screenshots/0001.png"
    ws = _plant_snapshot(proj, _CID, {rel: "original"})
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        projects_root=str(proj),
    )
    manifest = client._read_snapshot_manifest(_CID, [], ws)
    (ws / rel).write_bytes(b"changed-after-manifest")

    with pytest.raises(BrowserEvidenceCollectionError, match="does not match"):
        client.collect_browser_evidence(_CID, [_browser_screenshot_observation(rel)], manifest)


def _impl_test_h190_passing_host_verdict_screenshot_is_retained_and_hash_locked(tmp_path):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    rel = ".pmx/screenshots/host-verify.png"
    data = b"\x89PNG\r\n\x1a\nhost-verifier-proof"
    ws = _plant_snapshot(proj, _CID, {"index.html": "ok"})
    screenshot = ws / rel
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(data)
    events = _h191_verifier_events(rel, host=True)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        projects_root=str(proj),
    )
    manifest = client._read_snapshot_manifest(_CID, ["index.html"], ws)

    captured = client.collect_browser_evidence(
        _CID, events, manifest, require_verified_host_screenshot=True
    )

    assert captured == {rel: data}
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest=manifest,
        preview=None,
        browser_evidence=captured,
    )
    base = assemble_dossier(
        tmp_path / "out",
        "h305_host_verifier",
        _h191_strict_browser_scenario(),
        run,
        model="m",
        autonomous=False,
    )
    frozen = base / "conversations" / _CID / "browser-evidence" / rel
    assert frozen.read_bytes() == data
    assert classify_run_folder(base)["status"] == "PASS"

    frozen.write_bytes(data + b"tampered")
    replayed = classify_run_folder(base)
    assert replayed["status"] == "INVALID_RUN"
    assert replayed["code"] == "EVIDENCE_HASH_MISMATCH"


def _impl_test_h190_restore_collects_only_current_workspace_generation(tmp_path):
    projects = tmp_path / "projects"
    current_rel = ".pmx/screenshots/current.png"
    current_bytes = b"\x89PNG\r\n\x1a\ncurrent-generation"
    workspace_dir = _plant_snapshot(
        projects,
        _CID,
        {"index.html": "<h1>current</h1>"},
    )
    current_path = workspace_dir / current_rel
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_bytes(current_bytes)
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=str(projects),
    )
    workspace = client._read_snapshot_manifest(_CID, ["index.html"], workspace_dir)
    events = [
        _browser_screenshot_observation(".pmx/screenshots/historical.png", seq=10),
        _workspace_restore_event(seq=11),
        _workspace_restore_event(seq=12, restored_marker=True),
        _browser_screenshot_observation(current_rel, seq=13),
    ]

    assert client.collect_browser_evidence(_CID, events, workspace) == {current_rel: current_bytes}


def _impl_test_h190_restore_does_not_hide_missing_current_screenshot(tmp_path):
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=None,
    )
    events = [
        _browser_screenshot_observation(".pmx/screenshots/historical.png", seq=10),
        _workspace_restore_event(seq=11),
        _browser_screenshot_observation(".pmx/screenshots/missing-current.png", seq=12),
    ]

    with pytest.raises(BrowserEvidenceCollectionError, match="no authoritative workspace snapshot"):
        client.collect_browser_evidence(_CID, events, {})


def _impl_test_h191_restore_invalidates_old_proof_and_admits_only_fresh_host_verdict():
    old_path = ".pmx/screenshots/old.png"
    new_path = ".pmx/screenshots/new.png"
    restored_without_new_proof = [
        _host_verifier_verdict(old_path, seq=10),
        _workspace_restore_event(seq=11),
    ]
    restored_with_new_proof = [
        *restored_without_new_proof,
        _host_verifier_verdict(new_path, seq=12),
    ]

    assert _successful_browser_verification_paths(restored_without_new_proof) == set()
    assert _successful_browser_verification_paths(restored_with_new_proof) == {new_path}


def _impl_test_h191_latest_of_multiple_restores_is_the_only_evidence_generation():
    first_path = ".pmx/screenshots/first.png"
    middle_path = ".pmx/screenshots/middle.png"
    current_path = ".pmx/screenshots/current.png"
    events = [
        _host_verifier_verdict(first_path, seq=10),
        _workspace_restore_event(seq=11),
        _host_verifier_verdict(middle_path, seq=12),
        _workspace_restore_event(seq=13),
        _host_verifier_verdict(current_path, seq=14),
    ]

    assert _successful_browser_verification_paths(events) == {current_path}


@pytest.mark.parametrize("path", [None, 7, "", "../outside.png", ".pmx/screenshots/x.PNG"])
def _impl_test_h190_passing_host_verdict_with_missing_or_malformed_path_fails_closed(
    tmp_path, path
):
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=None,
    )

    with pytest.raises(BrowserEvidenceCollectionError):
        client.collect_browser_evidence(
            _CID,
            [_host_verifier_verdict(path)],
            {},
            require_verified_host_screenshot=True,
        )


def _impl_test_h190_passing_host_verdict_with_absent_path_fails_closed(tmp_path):
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=None,
    )
    verdict = _host_verifier_verdict(".pmx/screenshots/host.png")
    verdict.pop("screenshot_path")

    with pytest.raises(BrowserEvidenceCollectionError):
        client.collect_browser_evidence(
            _CID,
            [verdict],
            {},
            require_verified_host_screenshot=True,
        )


def _impl_test_h190_non_strict_scenario_ignores_host_pass_without_screenshot(tmp_path):
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=None,
    )

    assert client.collect_browser_evidence(_CID, [_host_verifier_verdict(None)], {}) == {}


@pytest.mark.parametrize(
    ("verified", "verdict"),
    [(False, "pass"), (1, "pass"), (True, "fail"), (True, "PASS")],
)
def _impl_test_h190_failing_or_unverified_host_verdict_does_not_claim_screenshot(
    tmp_path, verified, verdict
):
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=None,
    )

    assert (
        client.collect_browser_evidence(
            _CID,
            [_host_verifier_verdict("../must-not-be-read.png", verified=verified, verdict=verdict)],
            {},
        )
        == {}
    )


def _impl_test_h190_legacy_run_without_screenshot_reference_needs_no_snapshot(tmp_path):
    client = DiscoApiClient(
        FakeTransport(tmp_path / "disco.db", states=["FINISHED"], workspace={}),
        db_path=str(tmp_path / "disco.db"),
        projects_root=None,
    )
    run = CollectedRun(
        conversation_id=_CID,
        events=[],
        state_initial={},
        state_final={},
        workspace_manifest={},
        preview=None,
    )

    assert client.collect_browser_evidence(_CID, [], {}) == {}
    assert (
        client.collect_browser_evidence(
            _CID,
            [_browser_screenshot_observation("../ignored-failed.png", success=False)],
            {},
        )
        == {}
    )
    assert run.browser_evidence == {}
    base = assemble_dossier(
        tmp_path / "out", "legacy_no_browser", {"id": "legacy"}, run, model="m", autonomous=False
    )
    assert not (base / "conversations" / _CID / "browser-evidence").exists()


def _impl_test_h191_required_browser_verification_fails_closed_when_observation_or_bytes_absent():
    scenario = _h191_strict_browser_scenario()
    no_observation = classify(clean_smoke_log(), scenario=scenario)
    verifier_without_bytes = classify(
        _h191_verifier_events(".pmx/screenshots/verify.png"),
        scenario=scenario,
    )
    generic_events = clean_smoke_log()
    generic_events[-2]["seq"] = 13
    generic_events[-2]["id"] = "evt_13"
    generic_events[-1]["seq"] = 14
    generic_events[-1]["id"] = "evt_14"
    generic_events[-2:-2] = [
        action(11, "browser", action_id="act_11"),
        _browser_screenshot_observation(".pmx/screenshots/browser.png", seq=12),
    ]
    generic_browser_only = classify(
        generic_events,
        scenario=scenario,
        browser_evidence_paths={".pmx/screenshots/browser.png"},
    )

    for record in (no_observation, generic_browser_only):
        assert record["status"] == "FAIL", record
        assert record["code"] == "VERIFICATION_GATE_BYPASSED", record
        assert record["first_broken_link"] == "finish -> browser_verification"
        assert record["facts"]["passing_verifier_observations"] == 0

    assert verifier_without_bytes["status"] == "INVALID_RUN", verifier_without_bytes
    assert verifier_without_bytes["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert verifier_without_bytes["first_broken_link"] == (
        "browser_verification -> durable_screenshot_evidence"
    )


def _impl_test_h191_strict_direct_browser_with_locked_screenshot_satisfies_contract():
    path = ".pmx/screenshots/browser.png"
    record = classify(
        _h191_direct_browser_events(path),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "PASS", record


def _impl_test_h191_strict_direct_browser_accepts_dynamic_selected_preview_port():
    path = ".pmx/screenshots/browser.png"
    record = classify(
        _h191_direct_browser_events(path, selected_port=5173),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "PASS", record


@pytest.mark.parametrize("omit_action_url", [False, True])
def _impl_test_h191_synchronized_screenshot_with_locked_bytes_satisfies_contract(
    omit_action_url,
):
    path = ".pmx/screenshots/browser.png"
    record = classify(
        _h191_direct_browser_events(
            path,
            browser_action="screenshot",
            omit_action_url=omit_action_url,
        ),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "PASS", record


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_epoch", 2),
        ("synchronized_epoch", True),
        ("sync_performed", "yes"),
        ("page_kind", "external"),
    ],
)
def _impl_test_h191_screenshot_rejects_missing_or_stale_freshness(field, value):
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path, browser_action="screenshot")
    events[-3]["tool_result"]["structured"]["freshness"][field] = value

    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


def _impl_test_h191_screenshot_rejects_absent_freshness_receipt():
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path, browser_action="screenshot")
    del events[-3]["tool_result"]["structured"]["freshness"]

    assert path not in _successful_browser_verification_paths(events)
    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )
    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ok", False),
        ("url", "https://example.com:443/"),
        ("console", [{"level": "error", "text": "boom"}]),
        ("network", [{"url": "http://localhost:8000/api", "status": 500}]),
        ("visible_semantic_elements", 0),
        ("visible_semantic_elements", True),
        ("screenshot_path", ""),
    ],
)
def _impl_test_h191_direct_browser_fails_closed_without_strict_pass_facts(field, value):
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path)
    structured = events[-3]["tool_result"]["structured"]
    structured[field] = value
    if field == "visible_semantic_elements":
        structured["title"] = ""
        structured["text"] = "Live Server Up"
        structured["elements"] = []

    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL"
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


@pytest.mark.parametrize(
    "mutate",
    [
        lambda events: events.__setitem__(
            -4, action(11, "shell", args={"cmd": "true"}, action_id="act_11")
        ),
        lambda events: events.pop(-4),
        lambda events: events[-3]["tool_result"].__setitem__("call_id", "call_forged"),
        lambda events: events[-3]["tool_result"]["structured"].__setitem__("console", [{}]),
        lambda events: events[-3]["tool_result"]["structured"].update(
            {
                "title": "",
                "text": "short",
                "visible_semantic_elements": 0,
                "elements": [{}],
            }
        ),
    ],
)
def _impl_test_h191_direct_browser_rejects_unbound_or_malformed_evidence(mutate):
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path)
    mutate(events)

    assert path not in _successful_browser_verification_paths(events)

    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record


@pytest.mark.parametrize(
    ("selected_port", "action_url", "observed_url"),
    [
        (5173, "http://localhost:8000/", "http://localhost:8000/"),
        (8000, "http://localhost:7777/", "http://localhost:8000/"),
        (8000, "http://localhost:8000/", "http://localhost:9999/"),
    ],
)
def _impl_test_h191_direct_browser_rejects_wrong_or_mismatched_preview_target(
    selected_port, action_url, observed_url
):
    path = ".pmx/screenshots/browser.png"
    record = classify(
        _h191_direct_browser_events(
            path,
            selected_port=selected_port,
            action_url=action_url,
            observed_url=observed_url,
        ),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


@pytest.mark.parametrize(
    ("action_url", "observed_url"),
    [
        ("http://localhost:7777/", "http://localhost:8000/"),
        ("http://localhost:8000/", "http://localhost:9999/"),
    ],
)
def _impl_test_h191_screenshot_rejects_wrong_or_mismatched_preview_target(action_url, observed_url):
    path = ".pmx/screenshots/browser.png"
    record = classify(
        _h191_direct_browser_events(
            path,
            browser_action="screenshot",
            action_url=action_url,
            observed_url=observed_url,
        ),
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record


def _impl_test_h191_direct_browser_rejects_missing_preview_start_evidence():
    path = ".pmx/screenshots/browser.png"
    events = _h191_direct_browser_events(path)
    del events[-6:-4]

    record = classify(
        events,
        scenario=_h191_strict_browser_scenario(),
        browser_evidence_paths={path},
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "VERIFICATION_GATE_BYPASSED", record
