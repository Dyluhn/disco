"""REL-1c host-owned verifier.

Runs the web-app diagnostics from the host/runtime path without emitting an
agent ``ActionEvent``. The returned payload is intentionally shaped like the
existing ``verify_web_app`` structured verdict so core can compare host-vs-inline
results without depending on agent-server internals.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, cast

from disco.core.auth import ISOLATED_PATH_PREVIEW_PREFIX
from disco.core.loop import HostVerificationDeliverable
from disco.core.verification import (
    VerificationClaimKind,
    preview_binding_failure_result,
    structured_web_verification_result,
)
from disco.tools.builtin.browser import BrowserTool
from disco.tools.builtin.preview import PreviewStatusArgs, PreviewStatusTool
from disco.tools.builtin.verify_app import (
    VerifyWebAppArgs,
    VerifyWebAppTool,
)
from disco.tools.builtin.verify_app import (
    compute_verdict as verify_app_compute_verdict,
)

from .evidence_source import EvidenceSource, _ensure_evidence_source
from .probe import app_body_problem, collect_web_app_probe, compute_web_app_verdict

_LOG = logging.getLogger(__name__)
_GAME_INTERACTION_CLAIM_ID = "web.interaction:canvas-keyboard-smoke"
_GAME_INTERACTION_EXPECTED = (
    "host browser completed canvas click, Space, ArrowRight, and post-interaction capture"
)


def _verification_medium(
    deliverable: HostVerificationDeliverable,
) -> Literal["web", "deck", "mobile", "game"]:
    """Return the declared verification medium, defaulting to web."""
    medium = deliverable.verification_medium
    if medium in {"web", "deck", "mobile", "game"}:
        return cast(Literal["web", "deck", "mobile", "game"], medium)
    return "web"


def _verification_target_url(deliverable: HostVerificationDeliverable) -> str | None:
    """Resolve the URL to verify, or ``None`` when the binding cannot be honoured."""
    selected = deliverable.preview_selection
    if selected is None:
        return deliverable.deployment_url or ""
    return selected.verification_target_url(deliverable.artifact_path)


def _game_steps_exact(
    steps: Any, expected_profile: tuple[tuple[str, str | None], ...]
) -> bool:
    """Return whether observed steps match the declared profile exactly."""
    if not isinstance(steps, list) or len(steps) != len(expected_profile):
        return False
    return all(
        isinstance(step, dict)
        and step.get("action") == action
        and (key is None or step.get("key") == key)
        and step.get("success") is True
        and not step.get("error")
        for step, (action, key) in zip(steps, expected_profile, strict=True)
    )

def _game_interaction_claims(
    deliverable: HostVerificationDeliverable,
    game_interaction: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the interaction profile the game medium declares.

    This profile belongs to the game target alone. It is deliberately not
    part of the shared typed-result assembler, so a future non-web target
    supplies its own verifier and claim profile instead of inheriting web or
    game assumptions.
    """
    steps = game_interaction.get("steps")
    expected_profile: tuple[tuple[str, str | None], ...] = (
        ("click", None),
        ("press", "Space"),
        ("press", "ArrowRight"),
        ("screenshot", None),
    )
    exact_steps = bool(
        _game_steps_exact(steps, expected_profile)
        and game_interaction.get("before_screenshot_path")
        and game_interaction.get("after_screenshot_path")
    )
    interaction_claims: dict[str, Any] = {}
    for claim in deliverable.required_claims:
        if (
            claim.kind is VerificationClaimKind.INTERACTION
            and claim.claim_id == _GAME_INTERACTION_CLAIM_ID
            and claim.expected == _GAME_INTERACTION_EXPECTED
        ):
            interaction_claims[claim.claim_id] = {
                "expected": claim.expected,
                "passed": exact_steps,
                "steps": steps if isinstance(steps, list) else [],
                "before_screenshot_path": game_interaction.get("before_screenshot_path"),
                "after_screenshot_path": game_interaction.get("after_screenshot_path"),
            }
    return interaction_claims


class HostWebAppVerifier:
    """Host-side web verifier for REL-1 shadow mode.

    The preferred path reuses ``VerifyWebAppTool`` directly with a host-built
    tool context from the runtime's executor. This drives the same browser/probe
    logic as the inline tool call but does not append an ``ActionEvent``.

    A small client fallback exists for verifier tests and executor-less hosts; it
    uses the REL-1a app-response helpers plus the shared verdict builder.
    """

    def __init__(self, executor: Any | None = None, *, client: Any | None = None) -> None:
        self._executor = executor
        # DM-013: the verifier never directly reaches a full HttpVerifyClient
        # or product-private success endpoint.  The ``client=`` parameter is
        # wrapped through a state-free :class:`ClientEvidenceAdapter` that
        # exposes only the narrow :class:`EvidenceSource` port.  Only this
        # verifier produces the typed ``HostVerificationResult``; the adapter
        # cannot publish or upgrade a verdict.
        self._evidence_source: EvidenceSource | None = _ensure_evidence_source(client)
        # BF1: one fixed host-verifier lane per verifier. Serializing it keeps
        # concurrent finish probes from sharing state while the daemon's agent
        # lane remains entirely independent.
        self._browser_lane_lock = asyncio.Lock()

    async def verify(self, deliverable: HostVerificationDeliverable) -> dict[str, Any]:
        """Publish the one host verification verdict for this deliverable.

        This method is the sole verdict publisher. It admits the check, selects
        an evidence acquisition path, and returns exactly one typed result; no
        adapter or evidence source below it may publish or upgrade a verdict.
        """
        refusal = self._admission_refusal(deliverable)
        if refusal is not None:
            return refusal
        ctx_builder: Any = getattr(self._executor, "_build_context", None)
        if ctx_builder is not None:
            return await self._verify_via_browser_lane(deliverable, ctx_builder)
        if self._evidence_source is not None:
            return await self._verify_via_evidence(deliverable)
        return self._unavailable_verdict(deliverable, "no host verifier context available")

    def _admission_refusal(
        self, deliverable: HostVerificationDeliverable
    ) -> dict[str, Any] | None:
        """Refuse any admitted check no registered host verifier adapter owns.

        Returns the refusal verdict, or ``None`` when this verifier owns the
        check and verification may proceed.
        """
        check = deliverable.verification_check
        if check is None:
            return None
        if (
            check.issuer_id == "disco.host_web_verifier@1"
            and check.receipt_kind == "disco.web_functional@1"
            and check.operation == "host.verify_deliverable"
        ):
            return None
        return self._unavailable_verdict(
            deliverable,
            "no registered host verifier adapter owns the admitted "
            f"{check.receipt_kind} check from {check.issuer_id}",
        )

    async def _verify_via_browser_lane(
        self, deliverable: HostVerificationDeliverable, ctx_builder: Any
    ) -> dict[str, Any]:
        """Acquire evidence on the fixed host-verifier browser lane.

        The lane is host-selected and serialized per verifier; it is never
        present in any model-facing tool schema.
        """
        async with self._browser_lane_lock:
            ctx = None
            try:
                agent_ctx = await ctx_builder(VerifyWebAppTool.definition)
                # The lane is host-selected and fixed; it is not present in
                # VerifyWebAppArgs or any model-facing tool schema.
                capture_pixels = any(
                    claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
                    for claim in deliverable.required_claims
                )
                ctx = agent_ctx.model_copy(
                    update={
                        "browser_lane": "host_verifier",
                        "browser_capture_screenshot_b64": capture_pixels,
                    }
                )
                live_match = await self._preview_live_match(deliverable, ctx)
                if not live_match:
                    return self._artifact_binding_failure(deliverable)
                target_url = _verification_target_url(deliverable)
                if target_url is None:
                    return self._artifact_binding_failure(deliverable)
                outcome = await VerifyWebAppTool().run(
                    VerifyWebAppArgs(
                        url=target_url,
                        medium=_verification_medium(deliverable),
                    ),
                    ctx,
                )
                if outcome.success and outcome.structured:
                    return self._with_typed_result(
                        deliverable,
                        dict(outcome.structured),
                        preview_live_match=live_match,
                    )
                return self._unavailable_verdict(
                    deliverable,
                    outcome.error or outcome.content or "host verifier produced no verdict",
                )
            except Exception as exc:  # noqa: BLE001 — shadow verifier degrades to telemetry
                _LOG.warning(
                    "host web verifier failed for %s:%s",
                    deliverable.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
                return self._unavailable_verdict(deliverable, str(exc))
            finally:
                if ctx is not None:
                    try:
                        await BrowserTool().close_host_verifier_lane(ctx)
                    except Exception:  # noqa: BLE001 — cleanup is best-effort telemetry
                        _LOG.warning(
                            "host verifier lane cleanup failed for %s",
                            deliverable.conversation_id,
                            exc_info=True,
                        )

    @staticmethod
    async def _preview_live_match(deliverable: HostVerificationDeliverable, ctx: Any) -> bool:
        selected = deliverable.preview_selection
        if selected is None:
            return not deliverable.preview_binding_required
        outcome = await PreviewStatusTool().run(
            PreviewStatusArgs(name=selected.session_name),
            ctx,
        )
        previews = (
            outcome.structured.get("previews")
            if outcome.success and isinstance(outcome.structured, dict)
            else None
        )
        if not isinstance(previews, list) or len(previews) != 1:
            return False
        live = previews[0]
        if not isinstance(live, dict):
            return False
        exact = {
            "projection_id": selected.projection_id,
            "name": selected.session_name,
            "port": selected.port,
            "url": selected.url or None,
            "launch_kind": selected.launch_kind,
            "intent_digest": selected.intent_digest,
            "sandbox_instance_id": selected.sandbox_instance_id,
            "sandbox_generation": selected.sandbox_generation,
        }
        return bool(
            live.get("status") in {"running", "unavailable"}
            and all(live.get(key) == value for key, value in exact.items())
            and (
                not deliverable.deployment_url
                or selected.url.rstrip("/") == deliverable.deployment_url.rstrip("/")
            )
            and selected.contains_artifact(deliverable.artifact_path)
        )

    @classmethod
    def _artifact_binding_failure(
        cls,
        deliverable: HostVerificationDeliverable,
    ) -> dict[str, Any]:
        check = deliverable.verification_check
        verdict = dict(
            verify_app_compute_verdict(
                url=deliverable.deployment_url or "",
                reachable=False,
                http_status=0,
                structured=None,
                meaningful=False,
            )
        )
        verdict["verdict"] = "fail"
        verdict["passed"] = False
        verdict["summary"] = "selected preview generation is absent, changed, or foreign"
        verdict["failure_fingerprint"] = "preview_selection_mismatch"
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
            "preview_live_match": False,
        }
        verdict["verification_result"] = preview_binding_failure_result(
            deliverable=deliverable,
            observed_url=str(verdict.get("url") or ""),
            reason=str(verdict["summary"]),
            verifier_id=(
                check.issuer_id if check is not None else "host.preview_binding@1"
            ),
            tool_id=(
                check.operation
                if check is not None
                else (
                    "preview_status@1"
                    if deliverable.preview_selection is not None
                    else "host.preview_binding_preflight@1"
                )
            ),
        ).model_dump(mode="json")
        return verdict

    async def _verify_via_evidence(
        self, deliverable: HostVerificationDeliverable
    ) -> dict[str, Any]:
        """Executor-less fallback that acquires immutable evidence through the
        narrow :class:`EvidenceSource` port.

        DM-013: this path never directly reaches an :class:`HttpVerifyClient`
        or product-private success endpoint.  The evidence source is a
        state-free adapter that can only fetch raw bytes; it cannot publish or
        upgrade a verdict.  Only this verifier produces the typed
        :class:`~disco.core.verification.HostVerificationResult`.
        """
        source = self._evidence_source
        if source is None:  # caller guards; keep the method total for the checker
            return self._unavailable_verdict(deliverable, "no evidence source")
        url = deliverable.deployment_url or (
            f"{ISOLATED_PATH_PREVIEW_PREFIX}/{deliverable.conversation_id}/"
        )
        if deliverable.deployment_url:
            fetched = await source.fetch_app(deliverable.deployment_url)
            label = deliverable.deployment_url
        else:
            fetched = await source.fetch_preview(deliverable.conversation_id)
            label = deliverable.artifact_path or "preview"

        reachable = fetched is not None
        status, body = fetched if fetched is not None else (0, b"")
        problem = app_body_problem(fetched, label=label)
        structured = {
            "title": "",
            "text": body[:4096].decode("utf-8", "replace"),
            "elements": [],
            "console": [],
            "network": [],
            "screenshot_path": "",
        }
        # Touch the extracted collector explicitly so this fallback composes the
        # same diagnostics normalizer the browser path uses.
        collect_web_app_probe(structured)
        verdict = compute_web_app_verdict(
            url=url,
            reachable=reachable,
            http_status=int(status or 0),
            structured=structured,
            meaningful=problem is None,
        )
        if problem:
            verdict = dict(verdict)
            verdict["passed"] = False
            verdict["verdict"] = "fail"
            verdict["summary"] = problem
            verdict["next_action"] = "Serve a reachable, non-empty web app preview."
        return self._with_typed_result(
            deliverable,
            verdict,
            preview_live_match=not deliverable.preview_binding_required,
        )

    @staticmethod
    def _with_typed_result(
        deliverable: HostVerificationDeliverable,
        verdict: dict[str, Any],
        *,
        preview_live_match: bool | None = None,
    ) -> dict[str, Any]:
        out = dict(verdict)
        if preview_live_match is None:
            preview_live_match = not deliverable.preview_binding_required
        game_interaction = out.get("game_interaction")
        if deliverable.verification_medium == "game" and isinstance(game_interaction, dict):
            interaction_claims = _game_interaction_claims(
                deliverable, game_interaction
            )
            if interaction_claims:
                out["interaction_claims"] = interaction_claims
        out["artifact_identity"] = {
            "conversation_id": deliverable.conversation_id,
            "artifact_path": deliverable.artifact_path,
            "artifact_kind": deliverable.artifact_kind,
            "requested_url": deliverable.deployment_url,
            "observed_url": str(out.get("url") or ""),
            "preview_selection": (
                deliverable.preview_selection.model_dump(mode="json")
                if deliverable.preview_selection is not None
                else None
            ),
            "preview_live_match": preview_live_match,
        }
        out["verification_result"] = structured_web_verification_result(
            deliverable=deliverable,
            verdict=out,
        ).model_dump(mode="json")
        return out

    @staticmethod
    def _unavailable_verdict(
        deliverable: HostVerificationDeliverable, detail: str
    ) -> dict[str, Any]:
        verdict = verify_app_compute_verdict(
            url=deliverable.deployment_url or "",
            reachable=False,
            http_status=0,
            structured=None,
            meaningful=False,
        )
        verdict = dict(verdict)
        verdict["verdict"] = "unavailable"
        verdict["summary"] = f"host verifier unavailable: {detail}"
        verdict["next_action"] = ""
        verdict["failure_fingerprint"] = "host_verifier_unavailable"
        return HostWebAppVerifier._with_typed_result(
            deliverable,
            verdict,
            preview_live_match=False if deliverable.preview_binding_required else True,
        )


__all__ = ["HostWebAppVerifier"]
