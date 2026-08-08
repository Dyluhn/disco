"""BF1 host-verifier ownership gates.

Host verification gets one fixed serialized page lane and always attempts to
release it. It never changes the model-facing verifier schema or the executor's
ordinary agent context.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server.verify.host import HostWebAppVerifier
from disco.core.loop import HostVerificationDeliverable
from disco.core.loop.finish.verify_gates import _GAME_INTERACTION_EXPECTED
from disco.core.verification import (
    HostVerificationClaim,
    HostVerificationResult,
    PreviewSelectionIdentity,
    VerificationClaimKind,
    VerificationClaimStatus,
    default_structured_web_claims,
)
from disco.tools import ToolContext, ToolOutcome
from disco.tools.builtin.browser import BrowserTool
from disco.tools.builtin.preview import PreviewStatusTool
from disco.tools.builtin.verify_app import VerifyWebAppTool


class _Executor:
    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []

    async def _build_context(self, _definition) -> ToolContext:
        ctx = ToolContext(
            sandbox=object(),
            workspace_path=".",
            timeout_s=5,
            capabilities=None,
            owner_id="owner",
            conversation_id="conv",
            browser_workspace_epoch=4,
            browser_generation="a" * 32,
            browser_lane="agent",
        )
        self.contexts.append(ctx)
        return ctx


def _deliverable() -> HostVerificationDeliverable:
    return HostVerificationDeliverable(
        conversation_id="conv",
        artifact_path="site",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:8123/",
    )


def _preview_selection() -> PreviewSelectionIdentity:
    return PreviewSelectionIdentity(
        projection_id="pv_" + "1" * 32,
        session_name="static-site",
        port=8123,
        url="http://127.0.0.1:8123/",
        launch_kind="static",
        intent_digest="2" * 64,
        sandbox_instance_id="sandbox-a",
        sandbox_generation=3,
        static_serve_dir=".",
        source_action_id="evt-preview-action",
        source_action_seq=2,
        source_observation_id="evt-preview-observation",
        source_observation_seq=3,
    )


def _preview_status(selection: PreviewSelectionIdentity) -> dict[str, object]:
    return {
        "projection_id": selection.projection_id,
        "name": selection.session_name,
        "port": selection.port,
        "url": selection.url,
        "launch_kind": selection.launch_kind,
        "intent_digest": selection.intent_digest,
        "sandbox_instance_id": selection.sandbox_instance_id,
        "sandbox_generation": selection.sandbox_generation,
        "status": "running",
    }


@pytest.mark.asyncio
async def test_game_host_lowers_exact_interaction_probe_without_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _preview_selection()
    interaction = HostVerificationClaim(
        claim_id="web.interaction:canvas-keyboard-smoke",
        kind=VerificationClaimKind.INTERACTION,
        # DERIVED from the production symbol that owns this sentence, not retyped
        # (F62 / the Attestation-Binding Invariant, F58): the point of the test is
        # that the host lowers the EXACT probe production declares, so a reword of
        # the product must move this fixture rather than leave it asserting on a
        # string nothing emits.
        expected=_GAME_INTERACTION_EXPECTED,
        source_authority="target.game.functional_interaction@1",
    )
    deliverable = _deliverable().model_copy(
        update={
            "verification_medium": "game",
            "preview_selection": selection,
            "preview_binding_required": True,
            "required_claims": (*default_structured_web_claims(), interaction),
        }
    )
    observed_medium: list[str] = []

    async def status_run(self, args, ctx):  # noqa: ANN001
        del self, args, ctx
        return ToolOutcome(
            success=True,
            content="running",
            structured={"previews": [_preview_status(selection)]},
        )

    async def verify_run(self, args, ctx):  # noqa: ANN001
        del self
        observed_medium.append(args.medium)
        assert args.url == "http://127.0.0.1:8123/site"
        assert ctx.browser_capture_screenshot_b64 is False
        steps = [
            {"action": "click", "success": True},
            {"action": "press", "key": "Space", "success": True},
            {"action": "press", "key": "ArrowRight", "success": True},
            {"action": "screenshot", "success": True},
        ]
        return ToolOutcome(
            success=True,
            content="pass",
            structured={
                "passed": True,
                "verdict": "pass",
                "url": args.url,
                "http_status": 200,
                "meaningful_content": True,
                "rendered_text": "Canvas game",
                "console_errors": [],
                "network_failures": [],
                "game_interaction": {
                    "before_screenshot_path": ".pmx/screenshots/before.png",
                    "after_screenshot_path": ".pmx/screenshots/after.png",
                    "steps": steps,
                },
            },
        )

    async def close_lane(self, ctx):  # noqa: ANN001
        del self, ctx
        return True

    monkeypatch.setattr(PreviewStatusTool, "run", status_run)
    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    verdict = await HostWebAppVerifier(_Executor()).verify(deliverable)

    assert observed_medium == ["game"]
    result = verdict["verification_result"]
    assert result["status"] == "pass"
    interaction_result = next(
        claim for claim in result["claim_results"] if claim["claim_id"] == interaction.claim_id
    )
    assert interaction_result["status"] == "pass"


@pytest.mark.asyncio
async def test_changed_live_preview_projection_fails_before_browser_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _preview_selection()
    deliverable = _deliverable().model_copy(
        update={
            "run_intent_id": "intent-a",
            "run_identity": "run:sha256:" + "a" * 64,
            "agent_view_id": "view-a",
            "deliverable_event_id": "evt-deliverable-a",
            "workspace_revision": 7,
            "workspace_generation": "a" * 32,
            "workspace_epoch": 4,
            "observed_after_seq": 12,
            "preview_selection": selection,
            "preview_binding_required": True,
            "required_claims": default_structured_web_claims(),
        }
    )
    browser_calls = 0

    async def status_run(self, args, ctx):  # noqa: ANN001
        del self, args, ctx
        changed = _preview_status(selection)
        changed["projection_id"] = "pv_" + "9" * 32
        return ToolOutcome(
            success=True,
            content="running",
            structured={"previews": [changed]},
        )

    async def verify_run(self, args, ctx):  # noqa: ANN001
        nonlocal browser_calls
        del self, args, ctx
        browser_calls += 1
        raise AssertionError("foreign preview must not be probed")

    async def close_lane(self, ctx):  # noqa: ANN001
        del self, ctx
        return True

    monkeypatch.setattr(PreviewStatusTool, "run", status_run)
    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    verdict = await HostWebAppVerifier(_Executor()).verify(deliverable)

    assert browser_calls == 0
    assert verdict["verdict"] == "fail"
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    assert receipt.is_current_for(deliverable, observed_url=str(verdict["url"]))
    assert receipt.tool_id == "preview_status@1"
    artifact = next(
        claim
        for claim in receipt.claim_results
        if claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY
    )
    assert artifact.status is VerificationClaimStatus.FAIL
    assert all(
        claim.status is VerificationClaimStatus.UNAVAILABLE
        for claim in receipt.claim_results
        if claim.kind is not VerificationClaimKind.ARTIFACT_IDENTITY
    )


@pytest.mark.asyncio
async def test_missing_required_preview_produces_current_typed_binding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deliverable = _deliverable().model_copy(
        update={
            "run_intent_id": "intent-a",
            "run_identity": "run:sha256:" + "a" * 64,
            "agent_view_id": "view-a",
            "deliverable_event_id": "evt-deliverable-a",
            "workspace_revision": 7,
            "workspace_generation": "a" * 32,
            "workspace_epoch": 4,
            "observed_after_seq": 12,
            "preview_binding_required": True,
            "required_claims": default_structured_web_claims(),
        }
    )
    browser_calls = 0

    async def verify_run(self, args, ctx):  # noqa: ANN001
        nonlocal browser_calls
        del self, args, ctx
        browser_calls += 1
        raise AssertionError("missing Preview authority must fail before browser probe")

    async def close_lane(self, ctx):  # noqa: ANN001
        del self, ctx
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    verdict = await HostWebAppVerifier(_Executor()).verify(deliverable)

    assert browser_calls == 0
    assert verdict["failure_fingerprint"] == "preview_selection_mismatch"
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    assert receipt.is_current_for(deliverable, observed_url=str(verdict["url"]))
    assert receipt.tool_id == "host.preview_binding_preflight@1"
    assert receipt.status is VerificationClaimStatus.FAIL
    assert all(
        claim.status is VerificationClaimStatus.UNAVAILABLE
        for claim in receipt.claim_results
        if claim.kind is not VerificationClaimKind.ARTIFACT_IDENTITY
    )


@pytest.mark.asyncio
async def test_host_verifier_uses_only_fixed_serial_lane_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _Executor()
    verifier = HostWebAppVerifier(executor)
    active = 0
    maximum_active = 0
    lanes: list[tuple[str, int | None, str, bool]] = []
    closed: list[str] = []

    async def verify_run(self, args, ctx):  # noqa: ANN001
        nonlocal active, maximum_active
        del self, args
        lanes.append(
            (
                ctx.browser_lane,
                ctx.browser_workspace_epoch,
                ctx.browser_generation,
                ctx.browser_capture_screenshot_b64,
            )
        )
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ToolOutcome(
            success=True,
            content="pass",
            structured={"passed": True, "verdict": "pass"},
        )

    async def close_lane(self, ctx):  # noqa: ANN001
        del self
        closed.append(ctx.browser_lane)
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    first, second = await asyncio.gather(
        verifier.verify(_deliverable()), verifier.verify(_deliverable())
    )

    assert first["passed"] is True and second["passed"] is True
    assert maximum_active == 1
    assert lanes == [
        ("host_verifier", 4, "a" * 32, False),
        ("host_verifier", 4, "a" * 32, False),
    ]
    assert closed == ["host_verifier", "host_verifier"]
    # model_copy derives the host lane; the executor's canonical contexts stay agent-owned.
    assert [ctx.browser_lane for ctx in executor.contexts] == ["agent", "agent"]


@pytest.mark.asyncio
async def test_visual_claim_requests_pixels_from_host_lane_not_driver_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = HostWebAppVerifier(_Executor())
    observed: list[bool] = []
    visual = HostVerificationClaim(
        claim_id="web.visual:reference",
        kind=VerificationClaimKind.VISUAL_SEMANTIC,
        expected="reference_image_sha256:" + "a" * 64,
        source_authority="user_event:evt-reference:image:0",
    )

    async def verify_run(self, args, ctx):  # noqa: ANN001
        del self, args
        observed.append(ctx.browser_capture_screenshot_b64)
        return ToolOutcome(
            success=True,
            content="unavailable until independent judge",
            structured={"passed": True, "verdict": "pass"},
        )

    async def close_lane(self, ctx):  # noqa: ANN001
        del self, ctx
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    await verifier.verify(_deliverable().model_copy(update={"required_claims": (visual,)}))

    assert observed == [True]


@pytest.mark.asyncio
async def test_host_verifier_closes_lane_after_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _Executor()
    verifier = HostWebAppVerifier(executor)
    closed: list[str] = []

    async def failing_run(self, args, ctx):  # noqa: ANN001
        del self, args
        assert ctx.browser_lane == "host_verifier"
        raise RuntimeError("probe failed")

    async def close_lane(self, ctx):  # noqa: ANN001
        del self
        closed.append(ctx.browser_lane)
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", failing_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    verdict = await verifier.verify(_deliverable())

    assert verdict["verdict"] == "unavailable"
    assert closed == ["host_verifier"]


@pytest.mark.asyncio
async def test_lane_cleanup_failure_does_not_replace_host_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = HostWebAppVerifier(_Executor())

    async def verify_run(self, args, ctx):  # noqa: ANN001
        del self, args
        assert ctx.browser_lane == "host_verifier"
        return ToolOutcome(
            success=True,
            content="pass",
            structured={"passed": True, "verdict": "pass"},
        )

    async def broken_close(self, ctx):  # noqa: ANN001
        del self, ctx
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", broken_close)

    verdict = await verifier.verify(_deliverable())
    assert verdict["passed"] is True
    assert verdict["verdict"] == "pass"
    assert verdict["verification_result"]["verifier_id"] == "host.verify_web_app@1"


@pytest.mark.asyncio
async def test_host_verifier_cancellation_propagates_after_lane_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = HostWebAppVerifier(_Executor())
    entered = asyncio.Event()
    closed: list[str] = []

    async def wait_forever(self, args, ctx):  # noqa: ANN001
        del self, args, ctx
        entered.set()
        await asyncio.Future()

    async def close_lane(self, ctx):  # noqa: ANN001
        del self
        closed.append(ctx.browser_lane)
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", wait_forever)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    task = asyncio.create_task(verifier.verify(_deliverable()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == ["host_verifier"]
