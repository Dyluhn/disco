"""Unit tests for the disco-verify API-first runner (W15).

These tests exercise ``run_scenario``'s logic against a FAKE in-process client
so NO live server is required.  The ``FakeVerifyClient`` feeds canned events and
a predetermined final state; the test asserts that the runner:

 1. Creates the conversation and sends the message (via the client calls).
 2. Locates deliverables from the event log.
 3. Runs validators (forbid checks + file-level validators).
 4. Computes ``passed`` correctly (all green → True; any problem → False).
 5. Writes a dossier with the expected files.

Async test execution uses ``asyncio_mode = "auto"`` (set in pyproject.toml).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server.verify.runner import (
    AbstractVerifyClient,
    _locate_deliverables,
    _run_file_validators,
    _run_forbid_checks,
    run_scenario,
)
from disco.agent_server.verify.scenarios import (
    missing_file_sandbox_error,
    slides_from_research_report,
)
from disco.agent_server.verify.schema import Scenario, VerifyResult

# ---------------------------------------------------------------------------
# Fake transport — feeds canned responses, records calls
# ---------------------------------------------------------------------------


class FakeVerifyClient(AbstractVerifyClient):
    """Injectable transport for unit tests — no network, no live app."""

    def __init__(
        self,
        *,
        cid: str = "conv_fake0001",
        final_state: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
        trace: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
        artifacts: dict[str, bytes] | None = None,
        app_responses: dict[str, tuple[int, bytes]] | None = None,
        preview_response: tuple[int, bytes] | None = None,
        export_response: tuple[int, bytes] | None = None,
        schedule_fires: bool = False,
        post_fire_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.cid = cid
        self._final_state: dict[str, Any] = final_state or {"execution_status": "FINISHED"}
        self._events: list[dict[str, Any]] = events or []
        self._trace = trace
        self._manifest = manifest
        self._artifacts: dict[str, bytes] = artifacts or {}
        self._app_responses: dict[str, tuple[int, bytes]] = app_responses or {}
        self._preview_response = preview_response
        self._export_response = export_response
        self._schedule_fires = schedule_fires
        self._post_fire_events = post_fire_events
        # call-tracking for assertions
        self.calls: list[str] = []
        self.ws_prompt: str | None = None
        self.ws_approve_plan: bool = False
        self.ws_commands: list[dict[str, Any]] = []
        self.export_fmt: str | None = None
        self.fired_schedule_id: str | None = None
        self._get_events_count = 0
        # EPIC M call-tracking
        self.created_appkit_mode: bool = False
        self.ws_send_build_brief: bool = False

    async def create_conversation(
        self, surface: str, model_override: str | None, *, appkit_mode: bool = False
    ) -> str:
        self.calls.append("create_conversation")
        self.created_appkit_mode = appkit_mode
        return self.cid

    async def run_ws_exchange(
        self,
        cid: str,
        prompt: str,
        *,
        approve_plan: bool,
        timeout_s: float,
        ws_commands: list[dict[str, Any]] | None = None,
        auto_answer: str | None = None,
        send_build_brief: bool = False,
    ) -> None:
        self.calls.append("run_ws_exchange")
        self.ws_prompt = prompt
        self.ws_approve_plan = approve_plan
        self.ws_commands = list(ws_commands or [])
        self.ws_send_build_brief = send_build_brief

    async def poll_until_terminal(
        self,
        cid: str,
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        self.calls.append("poll_until_terminal")
        return dict(self._final_state)

    async def get_events(self, cid: str) -> list[dict[str, Any]]:
        self.calls.append("get_events")
        self._get_events_count += 1
        # After a schedule fire, the second get_events returns the post-fire log
        # (so a schedule_run event can appear) when one was supplied.
        if self._get_events_count > 1 and self._post_fire_events is not None:
            return list(self._post_fire_events)
        return list(self._events)

    async def get_state(self, cid: str) -> dict[str, Any]:
        self.calls.append("get_state")
        return dict(self._final_state)

    async def get_trace(self, cid: str) -> dict[str, Any] | None:
        self.calls.append("get_trace")
        return self._trace

    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
        self.calls.append("get_manifest")
        return self._manifest

    async def download_artifact(self, cid: str, path: str) -> bytes | None:
        self.calls.append("download_artifact")
        import os

        return self._artifacts.get(path) or self._artifacts.get(os.path.basename(path))

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        self.calls.append("fetch_app")
        return self._app_responses.get(url)

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        self.calls.append("fetch_preview")
        return self._preview_response

    async def export_report(self, cid: str, fmt: str) -> tuple[int, bytes] | None:
        self.calls.append("export_report")
        self.export_fmt = fmt
        return self._export_response

    async def fire_schedule_now(self, cid: str, schedule_id: str) -> bool:
        self.calls.append("fire_schedule_now")
        self.fired_schedule_id = schedule_id
        return self._schedule_fires


# ---------------------------------------------------------------------------
# Helper factories for canned events
# ---------------------------------------------------------------------------


def _clean_pptx_bytes() -> bytes:
    """A real, minimal .pptx (no embedded images) — passes validate_deck_file cleanly."""
    import io

    from pptx import Presentation

    buf = io.BytesIO()
    Presentation().save(buf)
    return buf.getvalue()


def _slides_observation_event(filename: str = "output.pptx") -> dict[str, Any]:
    """A successful slides_generate ObservationEvent with a .pptx deliverable."""
    return {
        "kind": "observation",
        "tool_result": {
            "success": True,
            "tool_name": "slides_generate",
            "call_id": "call_001",
            "content": "Slides generated.",
            "structured": {"filename": filename, "slide_count": 5},
        },
    }


def _deliverable_files_event(path: str = "output.pptx") -> dict[str, Any]:
    """A DeliverableEvent of kind 'files'."""
    return {
        "kind": "deliverable",
        "artifact_kind": "files",
        "path": path,
        "title": "Research Slides",
        "deployment_url": "",
    }


def _app_deliverable_event(path: str = "site", url: str = "") -> dict[str, Any]:
    """A DeliverableEvent of kind 'app' (a live-app handoff)."""
    return {
        "kind": "deliverable",
        "artifact_kind": "app",
        "path": path,
        "title": "Landing page",
        "deployment_url": url,
    }


def _image_observation_event(*, placeholder: bool = False) -> dict[str, Any]:
    """A successful image_generate ObservationEvent — using the REAL structured shape the
    tool emits (``backend`` / ``placeholder`` / ``backend_connected``), NOT the obsolete
    ``provider`` field the runner used to (wrongly) check (codex round-2)."""
    return {
        "kind": "observation",
        "tool_result": {
            "success": True,
            "tool_name": "image_generate",
            "call_id": "call_002",
            "content": "Image generated.",
            "structured": {
                "path": "slide_image.png",
                "backend": "pil-procedural" if placeholder else "openrouter-image",
                "placeholder": placeholder,
                "backend_connected": not placeholder,
            },
        },
    }


def _html_observation_event() -> dict[str, Any]:
    """A successful slides_generate ObservationEvent producing raw HTML."""
    return {
        "kind": "observation",
        "tool_result": {
            "success": True,
            "tool_name": "slides_generate",
            "call_id": "call_003",
            "content": "HTML generated.",
            "structured": {"filename": "output.html"},
        },
    }


def _pdf_observation_event(path: str = "report.pdf") -> dict[str, Any]:
    """A successful validate_pdf-worthy ObservationEvent (audio_overview mp3_path)."""
    # We use a pdf path directly via a deliverable event
    return _deliverable_files_event(path)


def _audio_observation_event(path: str = "overview.mp3") -> dict[str, Any]:
    """A successful audio_overview ObservationEvent."""
    return {
        "kind": "observation",
        "tool_result": {
            "success": True,
            "tool_name": "audio_overview",
            "call_id": "call_004",
            "content": "Audio generated.",
            "structured": {"mp3_path": path, "transcript_path": "overview_transcript.md"},
        },
    }


# ---------------------------------------------------------------------------
# Tests: _locate_deliverables
# ---------------------------------------------------------------------------


def test_locate_deliverables_slides_generate() -> None:
    events = [_slides_observation_event("deck.pptx")]
    deliverables = _locate_deliverables(events)
    assert len(deliverables) == 1
    assert deliverables[0]["path"] == "deck.pptx"
    assert deliverables[0]["tool"] == "slides_generate"


def test_locate_deliverables_deliverable_event() -> None:
    events = [_deliverable_files_event("output.pptx")]
    deliverables = _locate_deliverables(events)
    assert any(d["path"] == "output.pptx" for d in deliverables)


def test_locate_deliverables_audio_overview() -> None:
    events = [_audio_observation_event("overview.mp3")]
    deliverables = _locate_deliverables(events)
    paths = [d["path"] for d in deliverables]
    assert "overview.mp3" in paths
    assert "overview_transcript.md" in paths


def test_locate_deliverables_image_generate() -> None:
    events = [_image_observation_event()]
    deliverables = _locate_deliverables(events)
    assert any(d["path"] == "slide_image.png" for d in deliverables)


def test_locate_deliverables_skips_failed_observations() -> None:
    failed_evt: dict[str, Any] = {
        "kind": "observation",
        "tool_result": {
            "success": False,
            "tool_name": "slides_generate",
            "call_id": "c1",
            "structured": {"filename": "should_not_appear.pptx"},
        },
    }
    deliverables = _locate_deliverables([failed_evt])
    assert deliverables == []


def test_locate_deliverables_empty_events() -> None:
    assert _locate_deliverables([]) == []


# ---------------------------------------------------------------------------
# Tests: _run_validators (forbid checks, no file IO)
# ---------------------------------------------------------------------------


def test_validator_no_problems_on_pptx() -> None:
    scenario = Scenario(
        id="test",
        prompt="p",
        forbid=["raw_html_default", "procedural_image_provider"],
    )
    events = [_slides_observation_event("deck.pptx")]
    deliverables = _locate_deliverables(events)
    assert _run_forbid_checks(scenario, deliverables, events) == []


def test_validator_raw_html_default_fires_when_html_only() -> None:
    scenario = Scenario(id="test", prompt="p", forbid=["raw_html_default"])
    events = [_html_observation_event()]
    deliverables = _locate_deliverables(events)
    problems = _run_forbid_checks(scenario, deliverables, events)
    assert any("raw_html_default" in p for p in problems)


def test_validator_raw_html_default_ok_when_pptx_also_present() -> None:
    """HTML deliverable is NOT a problem when a non-HTML file was also produced."""
    scenario = Scenario(id="test", prompt="p", forbid=["raw_html_default"])
    events = [_html_observation_event(), _slides_observation_event("deck.pptx")]
    deliverables = _locate_deliverables(events)
    problems = _run_forbid_checks(scenario, deliverables, events)
    assert not any("raw_html_default" in p for p in problems)


def test_raw_html_default_not_masked_by_companion() -> None:
    """codex round-4: a raw-HTML deck must be flagged even when a companion (editable source)
    deliverable exists — only DECK files (.html/.pptx/.pdf) are judged, not companions."""
    scenario = Scenario(id="t", prompt="p", forbid=["raw_html_default"])
    deliverables = [
        {"path": "deck.html", "kind": "file", "tool": "slides_generate"},
        {"path": "deck.source.json", "kind": "editable_source", "tool": "slides_generate"},
    ]
    problems = _run_forbid_checks(scenario, deliverables, [])
    assert any("raw_html_default" in p for p in problems)


def test_validator_procedural_image_fires() -> None:
    scenario = Scenario(id="test", prompt="p", forbid=["procedural_image_provider"])
    events = [_image_observation_event(placeholder=True)]
    deliverables = _locate_deliverables(events)
    problems = _run_forbid_checks(scenario, deliverables, events)
    assert any("procedural_image_provider" in p for p in problems)


def test_validator_procedural_image_ok_for_real_provider() -> None:
    scenario = Scenario(id="test", prompt="p", forbid=["procedural_image_provider"])
    events = [_image_observation_event(placeholder=False)]
    deliverables = _locate_deliverables(events)
    problems = _run_forbid_checks(scenario, deliverables, events)
    assert not any("procedural" in p for p in problems)


async def test_file_validators_run_on_downloaded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file validator DOWNLOADS the deliverable and validates the real bytes."""
    import disco.tools.verify.artifact_validators as _av_mod

    seen: list[str] = []
    monkeypatch.setattr(_av_mod, "validate_pdf", lambda p: seen.append(p) or [])
    client = FakeVerifyClient(artifacts={"report.pdf": b"%PDF-1.4 fake"})
    deliverables = [{"path": "report.pdf", "kind": "file", "tool": "deliverable"}]
    problems = await _run_file_validators(
        deliverables, client=client, cid="c", dest_dir=tmp_path / "a"
    )
    assert len(seen) == 1 and "download_artifact" in client.calls
    assert problems == []


async def test_file_validators_report_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validator problems flow through; the bytes came from the app, not local cwd."""
    import disco.tools.verify.artifact_validators as _av_mod

    monkeypatch.setattr(_av_mod, "validate_pdf", lambda p: ["pdf has 0 pages"])
    client = FakeVerifyClient(artifacts={"bad.pdf": b"not a real pdf"})
    deliverables = [{"path": "bad.pdf", "kind": "file", "tool": "deliverable"}]
    problems = await _run_file_validators(
        deliverables, client=client, cid="c", dest_dir=tmp_path / "a"
    )
    assert problems == ["pdf has 0 pages"]


async def test_file_validators_flag_undownloadable_deliverable(tmp_path: Path) -> None:
    """A declared file deliverable the app won't serve is a real problem (not silently skipped)."""
    client = FakeVerifyClient(artifacts={})
    deliverables = [{"path": "deck.pptx", "kind": "file", "tool": "deliverable"}]
    problems = await _run_file_validators(
        deliverables, client=client, cid="c", dest_dir=tmp_path / "a"
    )
    assert any("not downloadable" in p for p in problems)


# ---------------------------------------------------------------------------
# Tests: run_scenario end-to-end (fake client, no live server)
# ---------------------------------------------------------------------------


async def test_run_scenario_happy_path_writes_dossier(tmp_path: Path) -> None:
    """Scenario finishes successfully: deliverables located, passed=True, dossier written."""
    client = FakeVerifyClient(
        cid="conv_test001",
        final_state={"execution_status": "FINISHED"},
        events=[
            _slides_observation_event("deck.pptx"),
            _deliverable_files_event("deck.pptx"),
        ],
        artifacts={"deck.pptx": _clean_pptx_bytes()},  # a real, clean deck → validators pass
    )
    scenario = slides_from_research_report

    result = await run_scenario(
        scenario,
        dossier_base=tmp_path / "dossiers",
        _client=client,
    )

    assert result.scenario_id == scenario.id
    assert result.terminal_status == "FINISHED"
    assert len(result.deliverables) >= 1
    assert result.passed
    assert result.validator_problems == []

    # dossier written
    dossier = Path(result.dossier_path)
    assert dossier.exists()
    assert (dossier / "result.json").exists()
    assert (dossier / "events.jsonl").exists()
    assert (dossier / "state.json").exists()
    # trace.json absent (no trace returned)
    assert not (dossier / "trace.json").exists()

    # result.json is valid JSON containing scenario id and result
    data = json.loads((dossier / "result.json").read_text())
    assert data["scenario"]["id"] == scenario.id
    assert data["result"]["passed"] is True
    assert data["conversation_id"] == "conv_test001"


async def test_run_scenario_all_client_methods_called(tmp_path: Path) -> None:
    """run_scenario calls all 7 AbstractVerifyClient methods."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[],
    )
    await run_scenario(
        Scenario(id="coverage", prompt="hello"),
        dossier_base=tmp_path / "d",
        _client=client,
    )
    for method in (
        "create_conversation",
        "run_ws_exchange",
        "poll_until_terminal",
        "get_events",
        "get_state",
        "get_trace",
        "get_manifest",
    ):
        assert method in client.calls, f"Expected {method!r} to be called"


async def test_run_scenario_ws_prompt_forwarded(tmp_path: Path) -> None:
    """The scenario's prompt is forwarded to run_ws_exchange."""
    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"})
    scenario = Scenario(id="prompt_check", prompt="Make me slides please")
    await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert client.ws_prompt == "Make me slides please"


async def test_run_scenario_approve_plan_forwarded(tmp_path: Path) -> None:
    """approve_plan=True is forwarded to run_ws_exchange."""
    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"})
    scenario = Scenario(id="approve_test", prompt="Build something", approve_plan=True)
    await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert client.ws_approve_plan is True


async def test_run_scenario_failed_when_forbid_fired(tmp_path: Path) -> None:
    """passed=False when a forbid check fires."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_html_observation_event()],  # html only → raw_html_default fires
    )
    scenario = Scenario(
        id="fail_test",
        prompt="Make slides",
        forbid=["raw_html_default"],
        expect={"terminal_status": "FINISHED"},
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("raw_html_default" in p for p in result.validator_problems)


async def test_run_scenario_failed_when_status_mismatch(tmp_path: Path) -> None:
    """passed=False when terminal_status != expected terminal_status."""
    client = FakeVerifyClient(
        final_state={"execution_status": "ERROR"},
        events=[],
    )
    scenario = Scenario(
        id="status_mismatch",
        prompt="do something",
        expect={"terminal_status": "FINISHED"},
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert result.terminal_status == "ERROR"


async def test_run_scenario_missing_file_sandbox_error(tmp_path: Path) -> None:
    """The missing_file_sandbox_error scenario passes when the build reaches its expected
    terminal status (FINISHED — a missing file is handled gracefully, the loop terminalizes,
    no silent hang)."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[
            {
                "kind": "agent_error",
                "error": "SandboxFileNotFoundError: missing_notes_99999.txt",
            }
        ],
    )
    result = await run_scenario(
        missing_file_sandbox_error,
        dossier_base=tmp_path / "d",
        _client=client,
    )
    assert result.passed
    assert result.terminal_status == "FINISHED"
    assert result.deliverables == []


async def test_run_scenario_dossier_has_trace_when_available(tmp_path: Path) -> None:
    """trace.json is written when get_trace returns a trace dict."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        trace={"spans": [{"id": "span1", "model": "gpt-5"}]},
    )
    result = await run_scenario(
        Scenario(id="trace_test", prompt="hi"),
        dossier_base=tmp_path / "d",
        _client=client,
    )
    dossier = Path(result.dossier_path)
    assert (dossier / "trace.json").exists()
    trace_data = json.loads((dossier / "trace.json").read_text())
    assert "spans" in trace_data


async def test_run_scenario_dossier_events_jsonl_redacted(tmp_path: Path) -> None:
    """Sensitive keys are redacted in the events.jsonl output."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[
            {"kind": "message", "content": "hello", "api_key": "sk-super-secret"}
        ],
    )
    result = await run_scenario(
        Scenario(id="redact_test", prompt="hi"),
        dossier_base=tmp_path / "d",
        _client=client,
    )
    dossier = Path(result.dossier_path)
    raw = (dossier / "events.jsonl").read_text()
    assert "sk-super-secret" not in raw
    assert "***REDACTED***" in raw


async def test_run_scenario_passed_when_no_expect_status(tmp_path: Path) -> None:
    """When expect has no terminal_status, any terminal status is acceptable."""
    client = FakeVerifyClient(
        final_state={"execution_status": "STUCK"},
        events=[],
    )
    scenario = Scenario(id="no_expect", prompt="hi", expect={}, forbid=[])
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    # no forbid violations + no expected status → passed
    assert result.passed


async def test_run_scenario_fails_on_timeout(tmp_path: Path) -> None:
    """codex P0: a timed-out run (poll set _timed_out) must FAIL even with no expected
    status — a silent hang must never pass."""
    client = FakeVerifyClient(final_state={"execution_status": "RUNNING", "_timed_out": True})
    scenario = Scenario(id="hang", prompt="hi", expect={}, forbid=[])
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed


async def test_run_scenario_fails_on_nonterminal_status(tmp_path: Path) -> None:
    """A run left at AWAITING_USER_QUESTION (not in the terminal set) must FAIL."""
    client = FakeVerifyClient(final_state={"execution_status": "AWAITING_USER_QUESTION"})
    scenario = Scenario(id="awaiting", prompt="hi", expect={}, forbid=[])
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed


async def test_run_scenario_catches_procedural_deck_via_download(tmp_path: Path) -> None:
    """codex P0: a deck with embedded PROCEDURAL placeholder images is caught by DOWNLOADING
    the real bytes and running validate_deck_file — not via an image_generate observation
    (slides stage images internally, emitting no such observation)."""
    import pathlib

    deck_bytes = (
        pathlib.Path(__file__).resolve().parents[1]
        / "tests/fixtures/negative/procedural_image_deck.html"
    ).read_bytes()
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[{"kind": "deliverable", "artifact_kind": "files", "path": "deck.html"}],
        artifacts={"deck.html": deck_bytes},
    )
    scenario = Scenario(
        id="deck", prompt="make slides", expect={"terminal_status": "FINISHED"}, forbid=[]
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("procedural" in p for p in result.validator_problems)


async def test_run_scenario_fails_when_expected_deck_missing(tmp_path: Path) -> None:
    """codex round-2: a scenario that EXPECTS a deck must FAIL when no deck deliverable was
    produced — even on FINISHED (otherwise a no-output run is a false pass)."""
    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"}, events=[])
    scenario = Scenario(
        id="deck-missing",
        prompt="make slides",
        expect={"terminal_status": "FINISHED", "deliverable_type": "deck"},
        forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("deliverable_type" in p for p in result.validator_problems)


# ---------------------------------------------------------------------------
# Tests: exported scenario objects
# ---------------------------------------------------------------------------


def test_slides_scenario_has_expected_shape() -> None:
    s = slides_from_research_report
    assert s.surface == "agent"
    assert "Make slides" in s.prompt
    assert "raw_html_default" in s.forbid
    assert "procedural_image_provider" in s.forbid
    assert s.expect.get("terminal_status") == "FINISHED"


def test_missing_file_scenario_has_expected_shape() -> None:
    s = missing_file_sandbox_error
    assert s.surface == "build"
    assert s.expect.get("terminal_status") == "FINISHED"
    assert s.approve_plan is True
    assert s.forbid == []


def test_verify_result_is_pydantic() -> None:
    r = VerifyResult(
        scenario_id="x",
        terminal_status="FINISHED",
        deliverables=[],
        validator_problems=[],
        passed=True,
        dossier_path="/tmp/dossier",
    )
    assert r.model_dump()["passed"] is True


# ---------------------------------------------------------------------------
# Tests: app deliverables (codex round-5 — the build surface's primary output)
# ---------------------------------------------------------------------------


def test_locate_app_deliverable() -> None:
    deliverables = _locate_deliverables([_app_deliverable_event("site", "http://x")])
    assert len(deliverables) == 1
    assert deliverables[0]["kind"] == "app"
    assert deliverables[0]["deployment_url"] == "http://x"


async def test_app_scenario_passes_when_app_served_and_reachable(tmp_path: Path) -> None:
    url = "http://127.0.0.1:9/preview"
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", url)],
        app_responses={url: (200, b"<html><body>ok</body></html>")},
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert result.passed
    assert "fetch_app" in client.calls


async def test_app_scenario_fails_when_no_app_delivered(tmp_path: Path) -> None:
    """codex round-5: a scenario expecting an app must FAIL when no app deliverable exists
    (the previous gap: app deliverables were dropped + unknown types treated as satisfied)."""
    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"}, events=[])
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("deliverable_type" in p for p in result.validator_problems)


async def test_app_url_unreachable_fails(tmp_path: Path) -> None:
    url = "http://127.0.0.1:9/dead"
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", url)],
        app_responses={},  # fetch_app returns None → unreachable
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("not reachable" in p for p in result.validator_problems)


async def test_app_url_empty_fails(tmp_path: Path) -> None:
    url = "http://127.0.0.1:9/empty"
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", url)],
        app_responses={url: (200, b"")},  # reachable but empty
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("empty" in p for p in result.validator_problems)


async def test_app_urlless_passes_when_entry_file_served(tmp_path: Path) -> None:
    """codex round-6: a URL-less app is still output-probed via its built index.html."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", "")],
        preview_response=(200, b"<!doctype html><title>My App</title><h1>Hi</h1>"),
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert result.passed


async def test_app_urlless_fails_when_preview_unavailable(tmp_path: Path) -> None:
    """A URL-less app whose live preview is unavailable (503) must FAIL — declared, no output."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", "")],
        preview_response=(503, b""),
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("preview not available" in p for p in result.validator_problems)


async def test_app_urlless_fails_on_directory_listing(tmp_path: Path) -> None:
    """codex round-8: a 200 non-empty Python http.server DIRECTORY LISTING (the preview's
    fallback when there's no real app entry file) must FAIL — it's not a real app."""
    listing = (
        b"<!DOCTYPE html><title>Directory listing for /</title>"
        b"<h1>Directory listing for /</h1><ul><li>main.py</li></ul>"
    )
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", "")],
        preview_response=(200, listing),
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("directory listing" in p for p in result.validator_problems)


async def test_app_fails_on_3xx(tmp_path: Path) -> None:
    """codex round-9: a 3xx (e.g. 300/304) body is NOT a served app — must fail (2xx only)."""
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        events=[_app_deliverable_event("site", "")],
        preview_response=(300, b"<html>choices</html>"),
    )
    scenario = Scenario(
        id="app", prompt="build a site",
        expect={"terminal_status": "FINISHED", "deliverable_type": "app"}, forbid=[],
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("HTTP 300" in p for p in result.validator_problems)


# ---------------------------------------------------------------------------
# Tests: gap #3 — extra WS command frames (steer/stop/resume/…)
# ---------------------------------------------------------------------------


async def test_ws_commands_forwarded_to_exchange(tmp_path: Path) -> None:
    """A scenario's ws_commands are sent to run_ws_exchange after send_message — so the
    runner can drive more than just send_message + approve_plan (gap #3)."""
    client = FakeVerifyClient(final_state={"execution_status": "FINISHED"})
    scenario = Scenario(
        id="steer_then_stop",
        prompt="build a thing",
        ws_commands=[
            {"type": "steer", "content": "focus on tests"},
            {"type": "stop"},
        ],
    )
    await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert client.ws_commands == [
        {"type": "steer", "content": "focus on tests"},
        {"type": "stop"},
    ]


# ---------------------------------------------------------------------------
# Tests: gap #54 — report export (bypasses the event-log deliverable path)
# ---------------------------------------------------------------------------


async def test_report_export_md_validates_and_passes(tmp_path: Path) -> None:
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        export_response=(200, b"# Report\n\nReal markdown body."),
    )
    scenario = Scenario(
        id="report_md", prompt="research X",
        expect={"terminal_status": "FINISHED"}, report_export="md",
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert "export_report" in client.calls
    assert client.export_fmt == "md"
    assert result.passed, result.validator_problems


async def test_report_export_blank_md_fails(tmp_path: Path) -> None:
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        export_response=(200, b"   \n  "),
    )
    scenario = Scenario(
        id="report_blank", prompt="research X",
        expect={"terminal_status": "FINISHED"}, report_export="md",
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("blank" in p for p in result.validator_problems)


async def test_report_export_unreachable_fails(tmp_path: Path) -> None:
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        export_response=None,  # endpoint unreachable
    )
    scenario = Scenario(
        id="report_dead", prompt="research X",
        expect={"terminal_status": "FINISHED"}, report_export="pdf",
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("not reachable" in p for p in result.validator_problems)


# ---------------------------------------------------------------------------
# Tests: gap #98 — deterministic schedule "fire now" seam (REGRESSION)
# ---------------------------------------------------------------------------


async def test_fire_schedule_passes_when_run_event_appears(tmp_path: Path) -> None:
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        schedule_fires=True,
        post_fire_events=[{"kind": "schedule_run", "schedule_id": "sched-1"}],
    )
    scenario = Scenario(
        id="sched_fire", prompt="schedule X",
        expect={"terminal_status": "FINISHED"}, fire_schedule_id="sched-1",
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert "fire_schedule_now" in client.calls
    assert client.fired_schedule_id == "sched-1"
    assert result.passed, result.validator_problems


async def test_fire_schedule_fails_when_hook_rejected(tmp_path: Path) -> None:
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        schedule_fires=False,
    )
    scenario = Scenario(
        id="sched_reject", prompt="schedule X",
        expect={"terminal_status": "FINISHED"}, fire_schedule_id="sched-1",
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("fire-now hook did not accept" in p for p in result.validator_problems)


async def test_fire_schedule_fails_without_run_event(tmp_path: Path) -> None:
    client = FakeVerifyClient(
        final_state={"execution_status": "FINISHED"},
        schedule_fires=True,
        post_fire_events=[{"kind": "status", "status": "FINISHED"}],  # no schedule_run
    )
    scenario = Scenario(
        id="sched_noevent", prompt="schedule X",
        expect={"terminal_status": "FINISHED"}, fire_schedule_id="sched-1",
    )
    result = await run_scenario(scenario, dossier_base=tmp_path / "d", _client=client)
    assert not result.passed
    assert any("no schedule_run event" in p for p in result.validator_problems)
