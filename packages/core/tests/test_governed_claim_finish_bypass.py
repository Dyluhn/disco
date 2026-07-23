"""Reproduction: governed app handoff bypasses mandatory claim enforcement.

Scenario ``p4_ff_python_cancel_recovery`` (seed 401614): after cancel/recovery the
agent rebuilt ``app.py``, launched it with raw ``shell_exec``, truthfully observed
it with the browser, then emitted an ``app`` handoff. No canonical PreviewManager
selection/generation or complete typed host receipt existed. Host verification
returned ``unavailable`` with ``requested_verification=false``, but ``FINISHED``
was still persisted.

The contradiction is deterministically closed:
- governed structured-browser target: true;
- current app handoff: present;
- canonical Preview selection: absent;
- legacy ``_is_web_deliverable``: false.

``gate_host_verify`` falls through expecting inline enforcement.
``gate_browser_verify`` only arms its inline gate when the legacy detector says
the history is a web deliverable (or strict AppKit applies). A governed ``app.py``
handoff is therefore able to bypass mandatory claim enforcement.

This module reproduces the bypass model-free through the real ``AgentLoop`` and
proves the composed controls after the correction.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from disco.core import (
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    DeliverableEvent,
    MessageEvent,
    NoOpCondenser,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
)
from disco.core.events import EventSource
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentLoop, NeverConfirm
from disco.core.loop.finish.common import (
    _is_web_deliverable,
    _latest_app_deliverable_event,
)
from disco.core.loop.finish.verify_gates import (
    _governed_structured_browser_target,
    _preview_selection_at,
)
from disco.core.verification import (
    HostVerificationClaim,
    VerificationClaimKind,
    default_structured_web_claims,
    structured_web_verification_result,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)

CID = "conv"
_DIGEST = "sha256:" + "a" * 64
_RUN_ID = "run:sha256:" + "b" * 64
_APP_SOURCE = (
    "from http.server import HTTPServer, SimpleHTTPRequestHandler\n"
    "HTTPServer(('', 8000), SimpleHTTPRequestHandler).serve_forever()"
)


def _platform_admission() -> BuildPlatformAdmissionEvent:
    return BuildPlatformAdmissionEvent(
        route="platform",
        profile_id="disco.freeform_web@1",
        run_intent_id="intent-test",
        composition_authority="build_platform_core",
        composition_digest=_DIGEST,
        run_identity=_RUN_ID,
        verification_claims=default_structured_web_claims(),
    )


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _statuses(events) -> list[tuple[str, str | None]]:
    return [(e.status.value, e.detail) for e in events if isinstance(e, StatusEvent)]


class _UnavailableHostVerifier:
    """Host verifier that returns ``unavailable`` — the exact false-finish shape.

    The typed receipt is omitted (no ``verification_result``), so
    ``_prepare_typed_host_verdict`` rewrites to generic ``unavailable``. This is
    the real production behavior when canonical Preview authority is absent.
    """

    def __init__(self) -> None:
        self.calls: list = []

    async def verify(self, deliverable) -> dict:
        self.calls.append(deliverable)
        return {
            "passed": False,
            "verdict": "unavailable",
            "url": deliverable.deployment_url or "http://127.0.0.1:8000/",
            "http_status": 0,
            "title": "",
            "meaningful_content": False,
            "visible_text_chars": 0,
            "elements_count": 0,
            "console_errors": [],
            "console_warnings": [],
            "network_failures": [],
            "screenshot_path": "",
            "failure_fingerprint": "host_verifier_unavailable",
            "summary": (
                "verification could not run: host verifier omitted the typed "
                "authority/claim receipt."
            ),
            "next_action": "",
        }


class _PassingHostVerifier:
    """Host verifier that returns a complete typed PASS receipt.

    Used for the positive control: the same recovered app WITH current Preview
    authority and every target claim satisfied may finish.
    """

    def __init__(self) -> None:
        self.calls: list = []

    async def verify(self, deliverable) -> dict:
        self.calls.append(deliverable)
        selected = deliverable.preview_selection
        observed_url = (
            selected.verification_target_url(deliverable.artifact_path)
            if selected is not None
            else deliverable.deployment_url
        )
        verdict = {
            "passed": True,
            "verdict": "pass",
            "url": observed_url or "http://127.0.0.1:8000/",
            "http_status": 200,
            "title": "app",
            "meaningful_content": True,
            "visible_text_chars": 40,
            "elements_count": 1,
            "console_errors": [],
            "console_warnings": [],
            "network_failures": [],
            "screenshot_path": "",
            "failure_fingerprint": "HOST_PASS",
            "summary": "ok",
            "next_action": "",
        }
        verdict["artifact_identity"] = {
            "conversation_id": deliverable.conversation_id,
            "artifact_path": deliverable.artifact_path,
            "artifact_kind": deliverable.artifact_kind,
            "requested_url": deliverable.deployment_url,
            "observed_url": str(verdict.get("url") or ""),
            "preview_selection": (
                deliverable.preview_selection.model_dump(mode="json")
                if deliverable.preview_selection is not None
                else None
            ),
            "preview_live_match": True,
        }
        verdict["verification_result"] = structured_web_verification_result(
            deliverable=deliverable,
            verdict=verdict,
        ).model_dump(mode="json")
        return verdict


class _RecoveryExecutor(FakeExecutor):
    """Executor that simulates the cancel/recovery chronology:

    - ``shell_exec`` launches ``app.py`` (raw shell runtime, not managed Preview)
    - ``browser`` observes ``localhost:8000`` (raw observation, diagnostic-only)
    - ``serve`` emits the ``app`` handoff
    - ``verify_web_app`` is available but the agent never calls it through the
      managed preview path
    """

    def __init__(self) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="shell", description="shell", parameters_schema={}),
                ToolSpec(name="browser", description="browse", parameters_schema={}),
                ToolSpec(name="serve", description="serve", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="verify_web_app", description="verify", parameters_schema={}),
            ]
        )
        self.verify_calls = 0

    async def execute(self, call):
        if call.tool_name == "browser":
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="browser",
                success=True,
                content=(
                    "[UNTRUSTED WEB CONTENT]\nURL: http://127.0.0.1:8000/\nTITLE: Python Recovery"
                ),
                structured={
                    "ok": True,
                    "url": "http://127.0.0.1:8000/",
                    "title": "Python Recovery 401614",
                    "console": [],
                    "elements": ["1[:] <h1>Python Recovery 401614</h1>"],
                    "text": "Python Recovery 401614",
                    "screenshot_path": "",
                },
            )
        if call.tool_name == "verify_web_app":
            self.verify_calls += 1
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_web_app",
                success=True,
                content="verified",
                structured={
                    "passed": True,
                    "verdict": "pass",
                    "url": "http://127.0.0.1:8000/",
                    "http_status": 200,
                    "title": "Python Recovery",
                    "meaningful_content": True,
                    "visible_text_chars": 50,
                    "elements_count": 3,
                    "console_errors": [],
                    "console_warnings": [],
                    "network_failures": [],
                    "checks": [],
                    "summary": "App verified",
                },
            )
        return await super().execute(call)


def _recovery_loop(agent, executor, *, host_verifier=None):
    store = SqliteEventStore(":memory:")
    loop = AgentLoop(
        CID,
        store,
        agent,
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        planning_tools=frozenset({"submit_plan"}),
        host_verifier=host_verifier,
        host_verify_authoritative=True,
    )
    return loop, store


# ---- model-free contradiction proof (pure event helpers) ---------------------


async def test_contradiction_state_is_deterministically_closed():
    """The exact contradictory state from the defect map, proven model-free.

    A governed admission with structured-browser claims + an app handoff for
    ``app.py`` + no preview_start + no index.html write yields:
    - ``_governed_structured_browser_target`` == True
    - ``_latest_app_deliverable_event`` == the app handoff
    - ``_preview_selection_at`` == None
    - ``_is_web_deliverable`` == False
    """
    store = SqliteEventStore(":memory:")
    await store.append(CID, _platform_admission())
    await store.append(
        CID,
        DeliverableEvent(
            title="Recovery app",
            path="app.py",
            artifact_kind="app",
        ),
    )
    events = await store.get_events(CID)

    assert _governed_structured_browser_target(events) is True
    assert _latest_app_deliverable_event(events) is not None
    assert _latest_app_deliverable_event(events).path == "app.py"
    assert _preview_selection_at(events, through_seq=100) is None
    assert _is_web_deliverable(events) is False


@pytest.mark.asyncio
async def test_recovered_app_handoff_with_host_unavailable_no_preview_must_not_finish():
    """NEGATIVE CONTROL 1: cancel/recover → raw shell runtime → browser
    observation → app handoff → host unavailable (no Preview, no typed receipt)
    must NOT persist FINISHED. The governed target has mandatory structured-
    browser claims but no canonical Preview selection and no complete typed
    receipt. The host gate must refuse with an actionable recovery route and
    an unchanged repeat must HALT (STUCK) — never FINISHED.
    """
    host = _UnavailableHostVerifier()
    execu = _RecoveryExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            action_step(
                tool="shell",
                args={"command": "python3 app.py &"},
            ),
            action_step(
                tool="browser",
                args={"action": "navigate", "url": "http://127.0.0.1:8000/"},
            ),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live and verified."),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=host)
    await loop._emit(_platform_admission())

    # Capture the governed state at the host-verifier point (before any terminal
    # status releases the admission).
    governed_at_finish: list[bool] = []

    async def _capture_hook(event) -> None:
        if hasattr(event, "verdict") and event.verdict == "unavailable":
            evs = await store.get_events(CID)
            governed_at_finish.append(_governed_structured_browser_target(evs))

    loop._host_verifier_verdict_hook = _capture_hook

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    statuses = _statuses(events)
    env = _env_messages(events)

    # The governed target was active when the host verifier returned unavailable
    assert governed_at_finish, "host verifier verdict hook should have captured the state"
    assert governed_at_finish[0] is True, "governed structured-browser target was active"
    # No preview selection existed
    assert _preview_selection_at(events, through_seq=max(e.seq or 0 for e in events)) is None
    # Host verifier ran and returned unavailable
    assert len(host.calls) >= 1
    # The refusal must point toward establishing current canonical Preview/typed
    # verification rather than repeat an impossible request.
    assert any("preview_start" in m or "typed receipt" in m for m in env), (
        f"refusal must point toward canonical Preview/typed receipt: {env}"
    )
    # SEVERE 1: The run must NOT persist FINISHED — it must HALT (STUCK) or
    # remain non-terminal. Never FINISHED, never with unverified_release.
    assert state.execution_status != ConversationStatus.FINISHED, (
        f"Governed target with no typed receipt must NOT persist FINISHED. "
        f"Got {state.execution_status}. Statuses: {statuses}"
    )
    assert ("RUNNING", "unverified_release") not in statuses, (
        f"Governed target must fail closed (STUCK), not release unverified. Statuses: {statuses}"
    )
    assert any(status == "STUCK" for status, _ in statuses)
    assert len(host.calls) == 2


@pytest.mark.asyncio
async def test_current_preview_but_missing_typed_receipt_plus_inline_pass_must_not_finish():
    """NEGATIVE CONTROL 2: current Preview exists but the host omits the typed
    receipt (verification_result). The inline gate must NOT accept an untyped
    verify_web_app PASS as a substitute. The governed target must refuse and
    an unchanged repeat must HALT (STUCK) — never FINISHED.

    This is the SEVERE 2 finding: Preview selection is non-null, so the old
    `preview_selection is None` check was skipped. The inline gate then accepted
    an untyped PASS and persisted clean FINISHED. The correction checks
    `typed_result is None` instead, catching this case regardless of Preview
    selection.
    """
    from test_host_verify_authoritative import _HostVerifier, _verdict

    class _OmitReceiptHost(_HostVerifier):
        """Host verifier that returns PASS but omits verification_result."""

        async def verify(self, deliverable) -> dict:
            verdict = await super().verify(deliverable)
            # Deliberately remove verification_result — the typed receipt is missing
            verdict.pop("verification_result", None)
            return verdict

    class _PreviewWithInlinePassExecutor(_RecoveryExecutor):
        def __init__(self):
            super().__init__()
            self._tools.append(
                ToolSpec(name="preview_start", description="preview", parameters_schema={})
            )

        async def execute(self, call):
            if call.tool_name != "preview_start":
                return await super().execute(call)
            self.calls.append(call)
            intent = {"launch_kind": "custom"}
            identity = {
                "command": "python3 app.py",
                "exec_dir": ".",
                "intent": intent,
                "name": "web",
                "port": 8000,
            }
            digest = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content="preview running",
                structured={
                    **identity,
                    "status": "running",
                    "url": "http://127.0.0.1:8000/",
                    "projection_id": "pv_" + "c" * 32,
                    "sandbox_instance_id": "sandbox-test",
                    "sandbox_generation": 1,
                    "launch_kind": "custom",
                    "intent_digest": digest,
                },
            )

    host = _OmitReceiptHost(_verdict(passed=True, fp="HOST"))
    execu = _PreviewWithInlinePassExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            action_step(tool="preview_start", args={}),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live and verified."),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=host)
    await loop._emit(_platform_admission())

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    statuses = _statuses(events)

    # Preview selection existed
    assert _preview_selection_at(events, through_seq=max(e.seq or 0 for e in events)) is not None
    # Host verifier ran
    assert len(host.calls) >= 1
    # SEVERE 2: The run must NOT persist FINISHED — the inline untyped PASS
    # cannot substitute for the missing typed receipt.
    assert state.execution_status != ConversationStatus.FINISHED, (
        f"Governed target with missing typed receipt must NOT persist FINISHED "
        f"even with Preview selection and inline PASS. "
        f"Got {state.execution_status}. Statuses: {statuses}"
    )
    assert ("RUNNING", "unverified_release") not in statuses, (
        f"Governed target must fail closed (STUCK), not release unverified. Statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_governed_browser_unavailable_without_receipt_must_not_finish():
    """NEGATIVE CONTROL 3: browser_verification_unavailable=true without a typed
    receipt. The old code called _emit_browser_unavailable_release → FALLTHROUGH
    → FINISHED. The correction routes ALL governed non-PASS outcomes to
    fail-closed handling. Must NOT persist FINISHED.
    """
    from test_host_verify_authoritative import _HostVerifier, _verdict

    class _BrowserUnavailableHost(_HostVerifier):
        """Host verifier that returns browser_unavailable with no typed receipt."""

        async def verify(self, deliverable) -> dict:
            self.calls.append(deliverable)
            verdict = dict(self._verdict)
            verdict["passed"] = False
            verdict["verdict"] = "unavailable"
            verdict["url"] = deliverable.deployment_url or "http://127.0.0.1:8000/"
            verdict["http_status"] = 0
            verdict["failure_fingerprint"] = "browser_unavailable"
            verdict["browser_unavailable"] = True
            verdict["summary"] = "browser infrastructure could not run"
            verdict["next_action"] = ""
            # No verification_result — typed receipt is missing
            return verdict

    host = _BrowserUnavailableHost(_verdict(passed=False, fp="browser_unavailable"))
    execu = _RecoveryExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live."),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=host)
    await loop._emit(_platform_admission())

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    statuses = _statuses(events)

    assert len(host.calls) >= 1
    # SEVERE 1: browser_unavailable must NOT release governed FINISHED
    assert state.execution_status != ConversationStatus.FINISHED, (
        f"Governed target with browser_unavailable must NOT persist FINISHED. "
        f"Got {state.execution_status}. Statuses: {statuses}"
    )
    assert ("RUNNING", "unverified_release") not in statuses, (
        f"Governed target must fail closed (STUCK), not release unverified. Statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_governed_target_without_configured_host_verifier_must_not_finish():
    """A missing host collaborator cannot silently delegate governed authority
    to an untyped inline PASS. It produces one recovery reminder, then an
    unchanged repeat lands STUCK.
    """

    execu = _RecoveryExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live."),
            finish_step(),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=None)
    await loop._emit(_platform_admission())

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    statuses = _statuses(events)
    assert state.execution_status != ConversationStatus.FINISHED
    assert any(status == "STUCK" for status, _ in statuses)
    assert ("RUNNING", "unverified_release") not in statuses
    assert any("no current, complete typed receipt" in message for message in _env_messages(events))
    assert execu.verify_calls == 0


@pytest.mark.asyncio
async def test_governed_current_typed_fail_receipt_must_not_finish():
    """NEGATIVE CONTROL 4: current typed FAIL receipt. The old code used
    _host_verify_failure_disposition whose cap emitted unverified_release →
    FALLTHROUGH → FINISHED. The correction routes ALL governed non-PASS
    outcomes to fail-closed handling. Must NOT persist FINISHED.
    """
    from test_host_verify_authoritative import _HostVerifier, _verdict

    class _TypedFailHost(_HostVerifier):
        """Host verifier that returns a current typed FAIL receipt."""

        async def verify(self, deliverable) -> dict:
            verdict = await super().verify(deliverable)
            # Override to FAIL with a complete typed receipt
            verdict["passed"] = False
            verdict["verdict"] = "fail"
            verdict["console_errors"] = [{"text": "Uncaught Error: boom", "source": "app.js:1"}]
            verdict["failure_fingerprint"] = "HOST_FAIL"
            verdict["summary"] = "broken by host verifier"
            verdict["next_action"] = "fix the console error"
            verdict["verification_result"] = structured_web_verification_result(
                deliverable=deliverable,
                verdict=verdict,
            ).model_dump(mode="json")
            return verdict

    host = _TypedFailHost(_verdict(passed=False, fp="HOST_FAIL"))
    execu = _RecoveryExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live."),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=host)
    await loop._emit(_platform_admission())

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    statuses = _statuses(events)

    assert len(host.calls) >= 1
    # SEVERE 2: typed FAIL must NOT release governed FINISHED
    assert state.execution_status != ConversationStatus.FINISHED, (
        f"Governed target with typed FAIL receipt must NOT persist FINISHED. "
        f"Got {state.execution_status}. Statuses: {statuses}"
    )
    assert ("RUNNING", "unverified_release") not in statuses, (
        f"Governed target must fail closed (STUCK), not release unverified. Statuses: {statuses}"
    )


class _PreviewPlatformExecutor(_RecoveryExecutor):
    """Executor that also supports ``preview_start`` with a canonical Preview
    selection identity — the managed runtime authority the host verifier binds to."""

    def __init__(self) -> None:
        super().__init__()
        self._tools.append(
            ToolSpec(name="preview_start", description="preview", parameters_schema={})
        )

    async def execute(self, call):
        if call.tool_name != "preview_start":
            return await super().execute(call)
        self.calls.append(call)
        intent = {"launch_kind": "static", "serve_dir": "."}
        identity = {
            "command": "python3 app.py",
            "exec_dir": ".",
            "intent": intent,
            "name": "web",
            "port": 8000,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="preview running",
            structured={
                **identity,
                "status": "running",
                "url": "http://127.0.0.1:8000/",
                "projection_id": "pv_" + "c" * 32,
                "sandbox_instance_id": "sandbox-test",
                "sandbox_generation": 1,
                "launch_kind": "static",
                "intent_digest": digest,
            },
        )


@pytest.mark.asyncio
async def test_new_preview_authority_clears_unchanged_failure_marker_and_may_pass():
    """A refusal is not a retry cap: establishing a managed Preview advances
    verification authority and allows the next current typed PASS to finish.
    """

    class _AdaptiveHost:
        def __init__(self) -> None:
            self.calls: list = []

        async def verify(self, deliverable) -> dict:
            self.calls.append(deliverable)
            if deliverable.preview_selection is None:
                return await _UnavailableHostVerifier().verify(deliverable)
            return await _PassingHostVerifier().verify(deliverable)

    host = _AdaptiveHost()
    execu = _PreviewPlatformExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live."),
            action_step(tool="preview_start", args={}),
            finish_step("Recovery app is live through managed Preview."),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=host)
    await loop._emit(_platform_admission())

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    assert state.execution_status == ConversationStatus.FINISHED
    assert len(host.calls) == 2
    assert host.calls[0].preview_selection is None
    assert host.calls[1].preview_selection is not None
    assert ("RUNNING", "unverified_release") not in _statuses(events)


@pytest.mark.asyncio
async def test_recovered_app_with_preview_authority_and_passing_host_may_finish():
    """POSITIVE CONTROL: the same recovered app WITH current Preview authority
    and every target claim satisfied may finish. The host verifier returns a
    complete typed PASS receipt bound to the canonical Preview selection.

    This mirrors the proven pattern from
    ``test_governed_missing_or_files_handoff_must_be_replaced_by_app``: an
    ``_AcceptingPlatformHost`` that sets ``preview_live_match=True`` and
    produces a ``structured_web_verification_result`` receipt, combined with a
    ``preview_start`` that establishes canonical Preview selection authority.
    The first ``finish_step()`` triggers the "no app handoff" refusal; then
    ``preview_start`` + ``serve`` + ``finish_step()`` completes cleanly.
    """
    from test_host_verify_authoritative import _HostVerifier, _verdict, _VerifyExecutor

    class _AcceptingPlatformHost(_HostVerifier):
        async def verify(self, deliverable) -> dict:
            verdict = await super().verify(deliverable)
            verdict["artifact_identity"]["preview_live_match"] = True
            verdict["verification_result"] = structured_web_verification_result(
                deliverable=deliverable,
                verdict=verdict,
            ).model_dump(mode="json")
            return verdict

    class _AppPlatformExecutor(_VerifyExecutor):
        def __init__(self):
            super().__init__(_verdict(passed=True, fp="INLINE"))
            self._tools.append(
                ToolSpec(name="preview_start", description="preview", parameters_schema={})
            )

        async def execute(self, call):
            if call.tool_name != "preview_start":
                return await super().execute(call)
            self.calls.append(call)
            intent = {"launch_kind": "custom"}
            identity = {
                "command": "python3 app.py",
                "exec_dir": ".",
                "intent": intent,
                "name": "web",
                "port": 8000,
            }
            digest = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content="preview running",
                structured={
                    **identity,
                    "status": "running",
                    "url": "http://127.0.0.1:8000/",
                    "projection_id": "pv_" + "c" * 32,
                    "sandbox_instance_id": "sandbox-test",
                    "sandbox_generation": 1,
                    "launch_kind": "custom",
                    "intent_digest": digest,
                },
            )

    host = _AcceptingPlatformHost(_verdict(passed=True, fp="HOST"))
    execu = _AppPlatformExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "app.py", "content": _APP_SOURCE},
            ),
            finish_step(),
            action_step(tool="preview_start", args={}),
            action_step(
                tool="serve",
                args={"title": "Recovery app", "path": "app.py", "kind": "app"},
            ),
            finish_step("Recovery app is live and verified via managed preview."),
        ]
    )
    loop, store = _recovery_loop(agent, execu, host_verifier=host)
    await loop._emit(_platform_admission())

    await loop.send_message("build a python web app")
    state = await loop.run()

    events = await store.get_events(CID)
    statuses = _statuses(events)

    # The positive path: with current Preview authority and a passing typed host
    # receipt, the governed target MAY finish cleanly.
    assert state.execution_status == ConversationStatus.FINISHED, (
        f"Expected clean FINISHED with Preview authority + passing host. "
        f"Got {state.execution_status}. Statuses: {statuses}"
    )
    assert len(host.calls) == 1, (
        f"Host verifier should be called exactly once (PASS delegates). "
        f"Got {len(host.calls)} calls. Statuses: {statuses}"
    )
    assert host.calls[0].artifact_kind == "app"
    # Preview selection existed
    assert _preview_selection_at(events, through_seq=max(e.seq or 0 for e in events)) is not None


# ---- positive control: non-governed target without browser claims passes -------


def _non_browser_claims() -> tuple[HostVerificationClaim, ...]:
    """Target-specific claims that do NOT require a structured browser runtime."""
    return (
        HostVerificationClaim(
            claim_id="target.artifact_identity",
            kind=VerificationClaimKind.ARTIFACT_IDENTITY,
            source_authority="target.specific@1",
        ),
    )


@pytest.mark.asyncio
async def test_non_browser_target_claims_remain_target_owned_and_pass():
    """POSITIVE CONTROL: non-browser target contracts remain target-owned and
    pass. A governed admission with only artifact-identity claims (no structured-
    browser claims) does not arm the browser gate and finishes normally.
    """
    host = _PassingHostVerifier()
    execu = _RecoveryExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "report.txt", "content": "A complete report."},
            ),
            action_step(
                tool="serve",
                args={"title": "Report", "path": "report.txt", "kind": "files"},
            ),
            finish_step("Report delivered."),
        ]
    )
    store = SqliteEventStore(":memory:")
    loop = AgentLoop(
        CID,
        store,
        agent,
        execu,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        planning_tools=frozenset({"submit_plan"}),
        host_verifier=host,
        host_verify_authoritative=True,
    )
    await loop._emit(
        BuildPlatformAdmissionEvent(
            route="platform",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent-test",
            composition_authority="build_platform_core",
            composition_digest=_DIGEST,
            run_identity=_RUN_ID,
            verification_claims=_non_browser_claims(),
        )
    )

    await loop.send_message("write a report")
    state = await loop.run()

    events = await store.get_events(CID)
    # Non-browser target: no structured-browser claims → gate does not arm →
    # finishes normally without browser verification.
    assert state.execution_status == ConversationStatus.FINISHED
    assert not _governed_structured_browser_target(events)


# ---- positive control: legacy non-governed web deliverable still works ---------


@pytest.mark.asyncio
async def test_legacy_non_governed_web_deliverable_finishes_with_inline_verify():
    """POSITIVE CONTROL: a legacy (non-governed) web deliverable (index.html
    write, no platform admission) still finishes through the inline browser gate.
    The correction only arms the governed disjunct; the legacy path is unchanged.
    """
    from loop_fakes import build_loop

    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            action_step(
                tool="browser",
                args={"action": "navigate", "url": "http://127.0.0.1:8000/"},
            ),
            finish_step("Page is live."),
        ]
    )
    execu = _RecoveryExecutor()
    loop, store = build_loop(
        agent,
        executor=execu,
        mode=OperatingMode.LONG_HORIZON,
        planning_tools=frozenset({"submit_plan"}),
    )

    await loop.send_message("build a page")
    state = await loop.run()

    events = await store.get_events(CID)
    # Legacy path: no platform admission → not governed → inline gate works as
    # before.
    assert state.execution_status == ConversationStatus.FINISHED
    assert not _governed_structured_browser_target(events)
    assert _is_web_deliverable(events)
