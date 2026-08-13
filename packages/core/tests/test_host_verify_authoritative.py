"""REL-1e host verifier authoritative gate."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from disco.core import (
    ActionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    DeliverableEvent,
    LLMMessage,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    VerifierShadowEvent,
    VerifierVerdictEvent,
)
from disco.core.events import EventSource
from disco.core.llm import ModelRole, OperatingMode, ToolSpec
from disco.core.llm.prompts import DriverPrompts
from disco.core.loop import (
    AgentLoop,
    NeverConfirm,
    TypedVerifierVerdict,
    VerifierContextSeed,
    host_verify_authoritative_enabled,
)
from disco.core.loop.finish.verify_gate_parts.host_claims import (
    _UNCLASSIFIED_MODEL_VERIFIER_CAUSE,
)
from disco.core.loop.finish.verify_gates import _bounded_model_verifier_cause
from disco.core.verification import (
    AdmittedVerificationContract,
    VerificationClaimKind,
    VerificationDeliveryContract,
    VerificationRequestedClaim,
    VerificationRequirementsDirective,
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


def _verdict(*, passed: bool, fp: str = "HOST", verdict: str | None = None) -> dict:
    label = verdict or ("pass" if passed else "fail")
    return {
        "passed": passed,
        "verdict": label,
        "url": "http://127.0.0.1:8000/",
        "http_status": 200,
        "title": "app",
        "meaningful_content": passed,
        "visible_text_chars": 40 if passed else 0,
        "elements_count": 1 if passed else 0,
        "console_errors": [] if passed else [{"text": "Boom", "source": "app.js:1"}],
        "console_warnings": [],
        "network_failures": [],
        "screenshot_path": "",
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": fp,
        "summary": "ok" if passed else "broken by host verifier",
        "next_action": "" if passed else "fix the console error",
    }


class _VerifyExecutor(FakeExecutor):
    def __init__(self, verdict: dict | None = None):
        super().__init__(
            tools=[
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="serve", description="serve", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="verify_web_app", description="verify", parameters_schema={}),
            ]
        )
        self._verdict = verdict or _verdict(passed=True, fp="INLINE")
        self.verify_calls = 0

    async def execute(self, call):
        if call.tool_name == "verify_web_app":
            self.calls.append(call)
            self.verify_calls += 1
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_web_app",
                success=True,
                content=f"VERIFY {self._verdict['verdict']}",
                structured=self._verdict,
            )
        return await super().execute(call)


class _HostVerifier:
    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict
        self.calls = []

    async def verify(self, deliverable):
        self.calls.append(deliverable)
        verdict = dict(self._verdict)
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
            "preview_live_match": not deliverable.preview_binding_required,
        }
        verdict["verification_result"] = structured_web_verification_result(
            deliverable=deliverable,
            verdict=verdict,
        ).model_dump(mode="json")
        return verdict


class _VerifierJudge:
    def __init__(self, verdict: TypedVerifierVerdict) -> None:
        self._verdict = verdict
        self.seeds: list[VerifierContextSeed] = []
        self.transcript = "SECRET VERIFY TRANSCRIPT"

    async def judge(self, seed: VerifierContextSeed) -> TypedVerifierVerdict:
        self.seeds.append(seed)
        return self._verdict


def _loop(
    agent,
    executor,
    *,
    host_verifier=None,
    hook=None,
    verifier_judge=None,
    finish_alias: str | None = None,
):
    store = SqliteEventStore(":memory:")
    loop = AgentLoop(
        "conv",
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
        finish_alias=finish_alias,
        host_verifier=host_verifier,
        host_verifier_verdict_hook=hook,
        host_verify_authoritative=True,
        verifier_judge=verifier_judge,
    )
    return loop, store


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _agent_messages(events) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT and e.message is not None
    ]


def _statuses(events) -> list[tuple[str, str | None]]:
    return [(e.status.value, e.detail) for e in events if isinstance(e, StatusEvent)]


def test_model_verifier_event_cause_rejects_untrusted_free_form_text() -> None:
    # The fallback is DERIVED from the production constant that owns it, not retyped
    # (F62 / F58). Deriving it by CALLING the same function on a second unclassifiable
    # input was rejected deliberately: that would make the assertion `f(a) == f(b)`,
    # which survives a mutation collapsing the classifier to one constant — a mutation
    # the literal catches today. Binding must not weaken the oracle.
    assert (
        _bounded_model_verifier_cause("provider echoed TOP_SECRET_RESPONSE_CONTENT")
        == _UNCLASSIFIED_MODEL_VERIFIER_CAUSE
    )


@pytest.mark.asyncio
async def test_host_pass_finishes_verified_and_skips_inline_verify() -> None:
    host_verdict = _verdict(passed=True, fp="HOST")
    host_verdict["screenshot_path"] = ".pmx/screenshots/0002-navigate.png"
    host = _HostVerifier(host_verdict)
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step("Verified cleanly by the host browser."),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    assert len(host.calls) == 1
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].verified is True
    assert verdicts[0].verdict == "pass"
    assert verdicts[0].screenshot_path == ".pmx/screenshots/0002-navigate.png"
    assert not any(
        isinstance(e, ActionEvent) and e.tool_call.tool_name == "verify_web_app" for e in events
    )
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "verify_web_app"
        for e in events
    )
    final = _agent_messages(events)[-1]
    assert final.message.content == "Verified cleanly by the host browser."
    assert final.meta.get("host_owned_terminal_warning") is not True
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_empty_artifact_contract_ignores_stray_web_preview_evidence() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="SHOULD_NOT_RUN"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="SHOULD_NOT_RUN"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "primes.py", "content": "print([2, 3, 5, 7])"},
            ),
            action_step(
                tool="serve",
                args={"title": "Primes", "path": "primes.py", "kind": "files"},
            ),
            finish_step("Script complete."),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)
    contract = AdmittedVerificationContract(
        target_id="disco.legacy_artifact@1",
        verifier_id="disco.host_web_verifier@1",
        delivery=VerificationDeliveryContract(
            shape="artifact.legacy_deliverable",
            mode="artifact",
            entry_kind="deliverable_manifest",
            entry_reference="active-deliverable",
        ),
        preview_modality="none",
        checks=(),
        required=False,
        unavailable="degrade",
        unverified_finish="allow_without_verified_label",
    )

    await loop.send_message("write and run a Python script")
    await loop._emit(
        BuildPlatformAdmissionEvent(
            route="platform",
            profile_id="disco.freeform_artifact@1",
            run_intent_id="intent-files",
            composition_authority="build_platform_core",
            composition_digest="sha256:" + "c" * 64,
            run_identity="run:sha256:" + "d" * 64,
            verification_contract=contract,
        )
    )
    preview = await loop._emit(
        ActionEvent(
            thought="old preview residue",
            tool_call=ToolCall(tool_name="preview_start", arguments={"serve_dir": "."}),
        )
    )
    await loop._emit(
        ObservationEvent(
            action_id=preview.id,
            tool_result=ToolResult(
                call_id=preview.tool_call.call_id,
                tool_name="preview_start",
                success=True,
                content="running",
            ),
        )
    )

    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 0
    assert not any(
        isinstance(event, VerifierVerdictEvent) for event in await store.get_events("conv")
    )


@pytest.mark.asyncio
async def test_host_screenshot_provenance_never_creates_a_false_verified_claim() -> None:
    host_verdict = _verdict(passed=False, fp="HOST_FAIL")
    host_verdict["screenshot_path"] = ".pmx/screenshots/0003-failed.png"
    host = _HostVerifier(host_verdict)
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
    )

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts
    assert all(event.screenshot_path == ".pmx/screenshots/0003-failed.png" for event in verdicts)
    assert all(event.verified is False for event in verdicts)
    assert ("RUNNING", "unverified_release") in _statuses(events)


@pytest.mark.parametrize("malformed_passed", ["false", 1])
@pytest.mark.asyncio
async def test_truthy_non_boolean_host_pass_cannot_verify_or_release(malformed_passed) -> None:
    host_verdict = _verdict(passed=True, fp="MALFORMED_HOST")
    host_verdict["passed"] = malformed_passed
    host_verdict["screenshot_path"] = ".pmx/screenshots/malformed.png"
    host = _HostVerifier(host_verdict)
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
    )

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]
    assert verdicts
    assert all(event.verified is False for event in verdicts)
    assert ("RUNNING", "unverified_release") in _statuses(events)
    assert any(
        "WITHOUT a passing host verifier verdict" in message for message in _env_messages(events)
    )


@pytest.mark.asyncio
async def test_host_screenshot_provenance_rejects_unbounded_path() -> None:
    host_verdict = _verdict(passed=True, fp="HOST")
    host_verdict["screenshot_path"] = "x" * 513
    host = _HostVerifier(host_verdict)
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
    )

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdict = next(e for e in events if isinstance(e, VerifierVerdictEvent))
    assert verdict.verified is True
    assert verdict.screenshot_path is None


@pytest.mark.asyncio
async def test_later_host_pass_supersedes_stale_unverified_release_marker() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step("Verified after a fresh passing host verdict."),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    async def _seed_stale_marker() -> None:
        await store.append(
            "conv",
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            ),
        )

    agent._before[0] = _seed_stale_marker
    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts[-1].verified is True
    final = _agent_messages(events)[-1]
    assert final.message.content == "Verified after a fresh passing host verdict."
    assert final.meta.get("host_owned_terminal_warning") is not True
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_host_fail_refuses_then_releases_loudly_without_inline_verify() -> None:
    host = _HostVerifier(_verdict(passed=False, fp="HOST_FAIL"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    assert len(host.calls) == 1
    events = await store.get_events("conv")
    env = _env_messages(events)
    assert any("Boom @ app.js:1" in m for m in env)
    assert any("WITHOUT a passing host verifier verdict" in m for m in env)
    assert ("RUNNING", "unverified_release") in _statuses(events)
    assert loop._browser_verify_refusals == 0


@pytest.mark.asyncio
async def test_model_verifier_gets_bounded_seed_and_builder_gets_summary_only() -> None:
    raw = _verdict(passed=True, fp="HOST")
    raw["summary"] = "RAW_CHECK_SECRET"
    raw["screenshot_path"] = ".pmx/screenshots/0001-navigate.png"
    host = _HostVerifier(raw)
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=False,
            verdict="fail",
            detail="Typed verifier summary only.",
            failures=[{"kind": "copy_quality", "message": "Typed failure only."}],
            next_action="Revise the hero copy.",
            failure_fingerprint="typed-copy-quality",
        )
    )
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        execu,
        host_verifier=host,
        verifier_judge=judge,
        finish_alias="ready_for_static_site_verification",
    )

    await loop.send_message("build a page")
    await loop.run()

    assert judge.seeds
    seed = judge.seeds[0]
    assert set(seed.model_dump(mode="json")) == {
        "contract",
        "deliverable_paths",
        "check_results",
        "screenshot",
        "reference_images",
        "medium",
        "claims",
    }
    assert seed.contract["verify"]["finalizer"] == "ready_for_static_site_verification"
    assert seed.deliverable_paths == ["index.html"]
    assert seed.check_results["summary"] == "RAW_CHECK_SECRET"
    assert seed.screenshot.path == ".pmx/screenshots/0001-navigate.png"
    assert seed.medium is None

    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts
    assert verdicts[0].verdict == "fail"
    assert verdicts[0].detail == "Typed verifier summary only."
    assert verdicts[0].failures[0]["message"] == "Typed failure only."

    builder_context = "\n".join(msg.content for view in agent.seen_views for msg in view.messages)
    assert "Typed verifier summary only." not in builder_context
    assert "Typed failure only." not in builder_context
    assert "RAW_CHECK_SECRET" not in builder_context
    assert judge.transcript not in builder_context


@pytest.mark.asyncio
async def test_contractless_freeform_skips_model_judge_and_keeps_host_pass() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=False,
            verdict="unverifiable",
            detail="Contract is empty; cannot verify deliverable requirements.",
            failures=[{"kind": "contract", "message": "missing contract"}],
            failure_fingerprint="contract_missing",
        )
    )
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step("Verified by the host browser."),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
        verifier_judge=judge,
    )

    await loop.send_message("build a flexible page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert judge.seeds == []
    events = await store.get_events("conv")
    verdict = next(event for event in events if isinstance(event, VerifierVerdictEvent))
    assert verdict.verified is True and verdict.verdict == "pass"
    assert verdict.meta.get("model_verifier_applied") is not True
    assert ("RUNNING", "unverified_release") not in _statuses(events)
    assert not any("WITHOUT browser-render verification" in m for m in _env_messages(events))


@pytest.mark.asyncio
async def test_user_visual_reference_is_judged_with_output_and_reference_pixels() -> None:
    output_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    reference_png = output_png
    host_verdict = _verdict(passed=True, fp="HOST_VISUAL")
    host_verdict["screenshot_b64"] = base64.b64encode(output_png).decode("ascii")
    host_verdict["screenshot_path"] = ".pmx/screenshots/visual-reference.png"
    host = _HostVerifier(host_verdict)
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=True,
            verdict="pass",
            detail="Rendered output satisfies the supplied visual reference.",
            failure_fingerprint="visual-reference-pass",
        )
    )
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>reference build</h1>"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
        verifier_judge=judge,
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(
                role="user",
                content="Match this supplied visual reference.",
                images=["data:image/png;base64," + base64.b64encode(reference_png).decode("ascii")],
            ),
            verification_requirements=VerificationRequirementsDirective(
                claims=(
                    VerificationRequestedClaim(
                        claim_id="web.visual:reference",
                        kind=VerificationClaimKind.VISUAL_SEMANTIC,
                        expected="match the supplied visual reference",
                        reference_image_index=0,
                    ),
                ),
                reference_images=(
                    "data:image/png;base64," + base64.b64encode(reference_png).decode("ascii"),
                ),
            ),
        )
    )

    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(judge.seeds) == 1
    seed = judge.seeds[0]
    assert seed.screenshot.image_data_url
    assert len(seed.reference_images) == 1
    assert seed.reference_images[0].image_data_url
    assert len(seed.claims) == 1
    assert seed.claims[0].kind.value == "visual_semantic"
    events = await store.get_events("conv")
    verdict = next(event for event in events if isinstance(event, VerifierVerdictEvent))
    assert verdict.verified is True and verdict.verdict == "pass"
    assert verdict.verification_result is not None
    visual_result = next(
        result
        for result in verdict.verification_result.claim_results
        if result.kind.value == "visual_semantic"
    )
    assert visual_result.capability_basis == "configured independent vision-capable verifier"


@pytest.mark.asyncio
async def test_model_verifier_unavailable_fallback_is_visible_in_persisted_events() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=False,
            verdict="unavailable",
            detail=(
                "model verifier unavailable (JSONDecodeError: Expecting value at line 1 column 1)"
            ),
            failures=[
                {
                    "kind": "verifier_unavailable",
                    "message": "JSONDecodeError: line 1 column 1",
                }
            ],
            failure_fingerprint="model_verifier_unavailable",
        )
    )
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
        verifier_judge=judge,
        finish_alias="ready_for_static_site_verification",
    )

    await loop.send_message("build a page")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    verdict = next(event for event in events if isinstance(event, VerifierVerdictEvent))
    shadow = next(event for event in events if isinstance(event, VerifierShadowEvent))
    # Generic runtime evidence cannot waive a registered semantic contract.
    assert verdict.verified is False and verdict.verdict == "unavailable"
    for event in (verdict, shadow):
        assert event.meta["model_verifier_status"] == "unavailable"
        assert event.meta["model_verifier_applied"] is True
        assert event.meta["model_verifier_cause"] == (
            "model verifier unavailable (JSONDecodeError: Expecting value at line 1 column 1)"
        )


@pytest.mark.asyncio
async def test_contract_judge_unverifiable_is_not_browser_unavailable() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    judge = _VerifierJudge(
        TypedVerifierVerdict(
            verified=False,
            verdict="unverifiable",
            detail="Required contract evidence is missing.",
            failures=[{"kind": "contract", "message": "missing evidence"}],
            next_action="Provide evidence for the registered requirements.",
            failure_fingerprint="contract_evidence_missing",
        )
    )
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    loop, store = _loop(
        agent,
        _VerifyExecutor(_verdict(passed=True, fp="INLINE")),
        host_verifier=host,
        verifier_judge=judge,
        finish_alias="ready_for_static_site_verification",
    )

    await loop.send_message("build a contract-bound page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(judge.seeds) == 1
    events = await store.get_events("conv")
    verdicts = [event for event in events if isinstance(event, VerifierVerdictEvent)]
    assert verdicts and all(event.verdict == "unavailable" for event in verdicts)
    assert all(
        event.verification_result is not None
        and any(
            result.claim_id == "web.contract_semantic" and result.status.value == "unavailable"
            for result in event.verification_result.claim_results
        )
        for event in verdicts
    )
    env = _env_messages(events)
    assert any("WITHOUT a passing host verifier verdict" in message for message in env)
    assert not any("browser infrastructure could not run" in message for message in env)


@pytest.mark.asyncio
async def test_host_unavailable_degrades_to_inline_gate_not_refusal() -> None:
    """Unavailable host infrastructure produces one terminal unverified result;
    it never becomes a pass or another agent-facing verifier cycle."""
    host = _HostVerifier(
        _verdict(passed=False, fp="host_verifier_unavailable", verdict="unavailable")
    )
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            action_step(tool="verify_web_app", args={}),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    events = await store.get_events("conv")
    env = _env_messages(events)
    # No host-refusal cycle was burned; the unavailable result is released loudly.
    assert not any("Host verification did not pass" in m for m in env)
    assert any("WITHOUT browser-render verification" in m for m in env)
    assert ("RUNNING", "unverified_release") in _statuses(events)
    # The honest ``unavailable`` verdict is still on the audit trail.
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert verdicts and all(v.verdict == "unavailable" for v in verdicts)
    assert all(v.verified is False for v in verdicts)


@pytest.mark.asyncio
async def test_host_unverifiable_finishes_with_explicit_unverified_marker() -> None:
    """Browser infrastructure absence is terminal but can never become a pass."""
    host_verdict = _verdict(passed=False, fp="browser_unavailable", verdict="unverifiable")
    host_verdict["startup_diagnostic"] = "exit=1; API_KEY=do-not-retain chromium launch failed"
    host = _HostVerifier(host_verdict)
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(
                'Created index.html. Verified: HTTP 200 and the heading "hello" is present.'
            ),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert execu.verify_calls == 0
    assert len(host.calls) == 1
    events = await store.get_events("conv")
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].verified is False
    assert verdicts[0].verdict == "unavailable"
    assert verdicts[0].verification_result is not None
    assert all(
        result.status.value == "unavailable"
        for result in verdicts[0].verification_result.claim_results
        if result.required
    )
    assert verdicts[0].detail is not None
    assert "Browser startup diagnostic: exit=1; <redacted> chromium launch failed" in (
        verdicts[0].detail
    )
    assert "do-not-retain" not in verdicts[0].detail
    assert ("RUNNING", "unverified_release") in _statuses(events)
    env = _env_messages(events)
    assert any("WITHOUT browser-render verification" in m for m in env)
    assert any("UNVERIFIED" in m and "INCOMPLETE" in m for m in env)
    assert not any("Host verification did not pass" in m for m in env)
    final = _agent_messages(events)[-1]
    assert final.message.content.startswith("⚠ UNVERIFIED FINAL RESULT:")
    assert "Browser rendering remains UNVERIFIED" in final.message.content
    assert "Verified: HTTP 200" not in final.message.content
    assert final.meta.get("host_owned_terminal_warning") is True
    assert final.meta.get("unverified_release") is True
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_authoritative_non_web_without_handoff_stays_unchanged() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "main.py", "content": "print(1)"}),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("write a script")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 0
    events = await store.get_events("conv")
    assert not any(isinstance(e, VerifierVerdictEvent) for e in events)


@pytest.mark.asyncio
async def test_files_handoff_without_validator_records_unverifiable() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    verdict_events: list[VerifierVerdictEvent] = []

    async def hook(event: VerifierVerdictEvent) -> None:
        verdict_events.append(event)

    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "report.txt", "content": "done"},
            ),
            action_step(
                tool="serve",
                args={"title": "Report", "path": "report.txt", "kind": "files"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host, hook=hook)

    await loop.send_message("write a report")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 0
    events = await store.get_events("conv")
    assert any(
        isinstance(e, DeliverableEvent) and e.artifact_kind == "files" and e.path == "report.txt"
        for e in events
    )
    verdicts = [e for e in events if isinstance(e, VerifierVerdictEvent)]
    assert len(verdicts) == 1
    assert verdicts[0].artifact_kind == "files"
    assert verdicts[0].verified is False
    assert verdicts[0].verdict == "unverifiable"
    assert verdict_events == verdicts


@pytest.mark.asyncio
async def test_files_unverifiable_verdict_cannot_delegate_reconstructed_web_target() -> None:
    host = _HostVerifier(_verdict(passed=True, fp="HOST"))
    execu = _VerifyExecutor(_verdict(passed=True, fp="INLINE"))
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            action_step(
                tool="serve",
                args={"title": "Site", "path": "index.html", "kind": "files"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)

    await loop.send_message("build a page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert host.calls == []
    assert execu.verify_calls == 1
    verdicts = [
        event for event in await store.get_events("conv") if isinstance(event, VerifierVerdictEvent)
    ]
    assert len(verdicts) == 1
    assert verdicts[0].artifact_kind == "files"
    assert verdicts[0].verdict == "unverifiable"
    assert verdicts[0].verification_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_handoff", [None, "files"])
async def test_governed_missing_or_files_handoff_must_be_replaced_by_app(
    initial_handoff: str | None,
) -> None:
    class _AcceptingPlatformHost(_HostVerifier):
        async def verify(self, deliverable):
            verdict = await super().verify(deliverable)
            verdict["artifact_identity"]["preview_live_match"] = True
            verdict["verification_result"] = structured_web_verification_result(
                deliverable=deliverable,
                verdict=verdict,
            ).model_dump(mode="json")
            return verdict

    class _PlatformExecutor(_VerifyExecutor):
        def __init__(self):
            super().__init__(_verdict(passed=True, fp="INLINE"))
            self._tools.append(
                ToolSpec(name="preview_start", description="preview", parameters_schema={})
            )

            class _HTMLSandbox:
                async def file_exists(self, path: str) -> bool:
                    return path == "index.html"

                async def read_file(self, path: str) -> bytes:
                    assert path == "index.html"
                    return b"<!doctype html><html><body><h1>hello</h1></body></html>"

            self.sandbox = _HTMLSandbox()

        async def execute(self, call):
            if call.tool_name != "preview_start":
                return await super().execute(call)
            self.calls.append(call)
            intent = {"launch_kind": "static", "serve_dir": "."}
            identity = {
                "command": "python -m http.server 8000",
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

    host = _AcceptingPlatformHost(_verdict(passed=True, fp="HOST"))
    execu = _PlatformExecutor()
    agent = ScriptedAgent(
        [
            action_step(
                tool="file_write",
                args={"path": "index.html", "content": "<h1>hello</h1>"},
            ),
            finish_step(),
            action_step(tool="preview_start", args={}),
            action_step(
                tool="serve",
                args={"title": "Site", "path": "index.html", "kind": "app"},
            ),
            finish_step(),
        ]
    )
    loop, store = _loop(agent, execu, host_verifier=host)
    await loop._emit(
        BuildPlatformAdmissionEvent(
            route="platform",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent-test",
            composition_authority="build_platform_core",
            composition_digest="sha256:" + "a" * 64,
            run_identity="run:sha256:" + "b" * 64,
            verification_claims=default_structured_web_claims(),
        )
    )
    if initial_handoff is not None:
        await loop._emit(
            DeliverableEvent(
                title="stale files handoff",
                path="index.html",
                artifact_kind="files",
            )
        )

    await loop.send_message("finish the governed web target")
    state = await loop.run()
    await store.append(
        "conv",
        DeliverableEvent(
            title="Source attachment",
            path="report.txt",
            artifact_kind="files",
        ),
    )

    assert state.execution_status == ConversationStatus.FINISHED
    recorded = await store.get_events("conv")
    assert len(host.calls) == 1, [
        (event.verdict, event.detail, event.verification_result)
        for event in recorded
        if isinstance(event, VerifierVerdictEvent)
    ]
    assert host.calls[0].artifact_kind == "app"
    assert execu.verify_calls == 0
    events = recorded
    assert (
        sum(
            isinstance(event, MessageEvent)
            and event.meta.get("diagnostic") == "finish_target_shape_refused"
            for event in events
        )
        == 1
    )
    latest_handoff = next(
        event for event in reversed(events) if isinstance(event, DeliverableEvent)
    )
    latest_app_handoff = next(
        event
        for event in reversed(events)
        if isinstance(event, DeliverableEvent) and event.artifact_kind == "app"
    )
    assert latest_handoff.artifact_kind == "files"
    assert latest_app_handoff.path == "index.html"


def test_authoritative_flag_default_on_and_explicit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """REL-1e flip (2026-07-03): authoritative is the DEFAULT; only an explicit
    falsy value restores the REL-1c shadow posture."""
    monkeypatch.delenv("DISCO_HOST_VERIFY_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_AUTHORITATIVE", raising=False)
    assert host_verify_authoritative_enabled() is True

    monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", "on")
    assert host_verify_authoritative_enabled() is True

    for falsy in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", falsy)
        assert host_verify_authoritative_enabled() is False, falsy


def test_prompt_self_verify_mandate_softens_only_when_authoritative() -> None:
    off = DriverPrompts().system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "Structured verification tools are OPTIONAL DEBUGGING aids" in off
    assert "share a budget of TWO calls for the current artifact bytes" in off
    assert "platform's finish gate independently runs applicable acceptance checks" in off
    assert "host verifier independently runs applicable acceptance checks" not in off
    assert "Treat that single pass/fail verdict as the finish check" not in off

    on = DriverPrompts(host_verify_authoritative=True).system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "Structured verification tools are OPTIONAL DEBUGGING aids" in on
    assert "share a budget of TWO calls for the current artifact bytes" in on
    assert "host verifier independently runs applicable acceptance checks" in on
    assert "names any governed defect" in on
    assert "Treat that single pass/fail verdict as the finish check" not in on

    small = DriverPrompts(host_verify_authoritative=True).system_prompt(
        model_family="gpt",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "DEBUGGING — `verify_web_app`, `verify_appkit_app`, and `design_lint`" in small
    assert "They share TWO calls for the current artifact bytes" in small
    assert "host verifier checks acceptance and names any governed defect" in small
    assert "Treat that single pass/fail verdict as the finish check" not in small
