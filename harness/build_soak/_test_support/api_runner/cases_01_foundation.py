"""Moved foundation collection implementations."""

from __future__ import annotations

from ._shared import (
    Any,
    _driver_catalog_contains,
    _run_mod,
    asyncio,
    cast,
    hashlib,
    io,
    pytest,
    zipfile,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _h191_strict_browser_scenario,
)


def _impl_test_driver_catalog_requires_exact_well_formed_model_key():
    payload = {
        "models": [
            {"id": "wanted", "label": "Wire label"},
            {"id": 7},
            "not-an-entry",
        ]
    }

    assert _driver_catalog_contains(payload, "wanted") is True
    assert _driver_catalog_contains(payload, "Wire label") is False
    assert _driver_catalog_contains(payload, "7") is False
    assert _driver_catalog_contains({"models": {}}, "wanted") is False


@pytest.mark.asyncio
async def _impl_test_batch_admission_stops_before_cohort_after_non_pass() -> None:
    admitted: list[int] = []

    async def run_iteration(index: int) -> dict[str, Any]:
        admitted.append(index)
        return {"index": index, "status": "FAIL" if index == 4 else "PASS"}

    runs, stopped_after = await _run_mod._run_stop_on_non_pass_cohorts(8, 3, run_iteration)

    assert admitted == [0, 1, 2, 3, 4, 5]
    assert [run["index"] for run in runs] == admitted
    assert stopped_after == 5


@pytest.mark.asyncio
async def _impl_test_batch_admission_runs_every_cohort_when_all_pass() -> None:
    admitted: list[int] = []

    async def run_iteration(index: int) -> dict[str, Any]:
        admitted.append(index)
        return {"index": index, "status": "PASS"}

    runs, stopped_after = await _run_mod._run_stop_on_non_pass_cohorts(5, 2, run_iteration)

    assert admitted == [0, 1, 2, 3, 4]
    assert [run["index"] for run in runs] == admitted
    assert stopped_after is None


def _impl_test_host_screenshot_strictness_is_scenario_scoped() -> None:
    assert _run_mod._browser_verification_required(_h191_strict_browser_scenario()) is True
    assert _run_mod._browser_verification_required({"assertions": {}}) is False
    assert (
        _run_mod._browser_verification_required(
            {"assertions": {"browser_verification": {"required": False}}}
        )
        is False
    )


@pytest.mark.asyncio
async def _impl_test_import_fixture_uses_real_import_surface_before_kick(tmp_path):
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)

    cid = await client.create_build_conversation(
        "Update the imported project.",
        import_fixture={
            "filename": "sample.zip",
            "files": {"README.md": "seed", "src/app.js": "export const seed = 1;"},
        },
    )

    assert cid == transport.cid
    assert transport.posts[0][0] == "/api/projects/import"
    assert transport.posts[1] == (
        f"/conversations/{transport.cid}/messages",
        {"content": "Update the imported project."},
    )
    assert client.scenario_evidence["import"] == {
        "accepted": True,
        "file_count": 2,
        "bytes": len(b"seed") + len(b"export const seed = 1;"),
        "filename": "sample.zip",
    }


@pytest.mark.asyncio
async def _impl_test_soak_adapter_posts_typed_verification_requirements(tmp_path):
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)
    requirements = {
        "claims": [
            {
                "claim_id": "web.visual:reference",
                "kind": "visual_semantic",
                "expected": "match the governed reference",
                "reference_image_index": None,
            }
        ],
        "reference_images": [],
        "supersedes_event_id": None,
    }

    await client.create_build_conversation(
        "Build against the governed visual requirement.",
        verification_requirements=requirements,
    )

    assert transport.posts[-1] == (
        f"/conversations/{transport.cid}/messages",
        {
            "content": "Build against the governed visual requirement.",
            "verification_requirements": requirements,
        },
    )


@pytest.mark.asyncio
async def _impl_test_import_fixture_refuses_appkit_and_autonomous_shortcuts(tmp_path):
    client = _client(FakeTransport(tmp_path / "disco.db", states=["RUNNING"]), tmp_path)
    fixture = {"filename": "sample.zip", "files": {"README.md": "seed"}}

    with pytest.raises(ValueError, match="Freeform Build"):
        await client.create_build_conversation("x", appkit=True, import_fixture=fixture)
    with pytest.raises(ValueError, match="interactive approval"):
        await client.create_build_conversation("x", autonomous=True, import_fixture=fixture)


@pytest.mark.asyncio
async def _impl_test_import_fixture_expands_bounded_line_generator(tmp_path):
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)

    await client.create_build_conversation(
        "Inspect the catalog.",
        import_fixture={
            "filename": "catalog.zip",
            "files": {
                "catalog.txt": {
                    "line_template": "seed=41 row={{row}}\n",
                    "count": 3,
                }
            },
        },
    )

    assert client.scenario_evidence["import"]["accepted"] is True
    assert client.scenario_evidence["import"]["bytes"] == len(
        b"seed=41 row=1\nseed=41 row=2\nseed=41 row=3\n"
    )


def _impl_test_task_seed_materialization_is_recursive_and_nonmutating() -> None:
    source = {
        "prompt": "build seed {{seed}}",
        "followups": [{"text": "revise {{seed}}"}],
        "import_fixture": {
            "files": {"catalog.txt": {"line_template": "{{seed}}/{{row}}\n", "count": 2}}
        },
    }

    materialized = _run_mod._materialize_task_seed(source, 701)

    assert materialized["prompt"] == "build seed 701"
    assert materialized["followups"][0]["text"] == "revise 701"
    assert materialized["import_fixture"]["files"]["catalog.txt"]["line_template"] == (
        "701/{{row}}\n"
    )
    assert source["prompt"] == "build seed {{seed}}"


def _impl_test_export_archive_must_match_declared_workspace_bytes() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("index.html", b"truth")
    workspace = {
        "index.html": {
            "present": True,
            "size": 5,
            "sha256": hashlib.sha256(b"truth").hexdigest(),
        }
    }

    ok, facts = _run_mod._export_matches_workspace(archive.getvalue(), workspace, ["index.html"])
    assert ok is True
    assert facts["archive_file_count"] == 1

    with zipfile.ZipFile(archive := io.BytesIO(), "w") as bundle:
        bundle.writestr("index.html", b"stale")
    ok, facts = _run_mod._export_matches_workspace(archive.getvalue(), workspace, ["index.html"])
    assert ok is False
    assert facts["mismatched_paths"] == ["index.html"]


def _impl_test_export_archive_uses_governed_nested_target_root() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("react-continue/index.html", b"truth")
        bundle.writestr("react-continue/package.json", b'{"dependencies":{"react":"latest"}}')
    workspace = {
        "react-continue/index.html": {
            "present": True,
            "sha256": hashlib.sha256(b"truth").hexdigest(),
        },
        "react-continue/package.json": {
            "present": True,
            "sha256": hashlib.sha256(b'{"dependencies":{"react":"latest"}}').hexdigest(),
        },
    }

    ok, facts = _run_mod._export_matches_workspace(
        archive.getvalue(),
        workspace,
        ["package.json", "index.html"],
        verified_artifact_paths=["react-continue/index.html"],
    )

    assert ok is True
    assert facts["assertion_root"] == "react-continue"
    assert facts["resolved_required_paths"] == {
        "index.html": "react-continue/index.html",
        "package.json": "react-continue/package.json",
    }


def _impl_test_export_archive_never_guesses_between_two_governed_roots() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one/index.html", b"one")
        bundle.writestr("two/index.html", b"two")
    workspace = {
        "one/index.html": {"present": True, "sha256": hashlib.sha256(b"one").hexdigest()},
        "two/index.html": {"present": True, "sha256": hashlib.sha256(b"two").hexdigest()},
    }

    ok, facts = _run_mod._export_matches_workspace(
        archive.getvalue(),
        workspace,
        ["index.html"],
        verified_artifact_paths=["one/index.html", "two/index.html"],
    )

    assert ok is False
    assert (
        facts["reason"] == "multiple governed artifact roots satisfy the declared output contract"
    )


@pytest.mark.asyncio
async def _impl_test_pause_resume_trigger_records_durable_control_result(tmp_path):
    transport = FakeTransport(tmp_path / "disco.db", states=["PAUSED", "RUNNING"])
    client = _client(transport, tmp_path)
    timeline: list[str] = []

    await _run_mod._pause_resume_at_trigger(
        client, transport.cid, timeline, trigger_seq=7, timeout_s=2
    )

    assert transport.ws_frames == [{"type": "pause"}]
    assert client.scenario_evidence["pause_resume"]["ok"] is True
    assert "settled PAUSED then resumed" in timeline[-1]


@pytest.mark.asyncio
async def _impl_test_scenario_pause_owns_resume_during_driver_poll_race():
    class RacingPauseClient:
        def __init__(self) -> None:
            self._poll = 0.0
            self.scenario_evidence = {}
            self.pause_sent = asyncio.Event()
            self.driver_observed = asyncio.Event()
            self.current = "RUNNING"
            self.resume_calls = 0
            self.poll_calls = 0

        async def wait_for_first_file_write(self, _cid, *, timeout_s):
            return 7

        async def pause(self, _cid):
            self.current = "PAUSED"
            self.pause_sent.set()
            await self.driver_observed.wait()

        async def get_state(self, _cid):
            return {"execution_status": self.current}

        async def poll_until_terminal_or_gate(self, _cid, **_kwargs):
            self.poll_calls += 1
            if self.poll_calls == 1:
                await self.pause_sent.wait()
                self.driver_observed.set()
                return "PAUSED"
            return "FINISHED"

        async def resume(self, _cid):
            self.resume_calls += 1
            self.current = "RUNNING"
            return {"http_status": 200}

        async def wait_until_status_leaves(self, _cid, expected, *, timeout_s):
            assert expected == "PAUSED"
            return self.current

    client = RacingPauseClient()
    timeline: list[str] = []

    result = await _run_mod._drive_to_terminal(
        cast(Any, client),
        "conv-race",
        autonomous=False,
        mid_run=[],
        pause_at={"trigger": "after_first_file_write"},
        timeline=timeline,
        inactivity_s=2,
        hard_cap_s=2,
    )

    assert result == "FINISHED"
    assert client.resume_calls == 1
    assert client.scenario_evidence["pause_resume"]["ok"] is True
    assert not any(line.startswith("resumed PAUSED run") for line in timeline)


@pytest.mark.asyncio
async def _impl_test_restart_requires_private_isolated_stack_control(monkeypatch):
    monkeypatch.delenv("DISCO_RELIABILITY_STACK_CONTROL_URL", raising=False)
    monkeypatch.delenv("DISCO_RELIABILITY_STACK_CONTROL_TOKEN", raising=False)
    assert await _run_mod._restart_isolated_stack() == {
        "ok": False,
        "reason": "isolated_stack_control_unavailable",
    }
