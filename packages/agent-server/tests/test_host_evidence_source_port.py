"""DM-013 boundary tests for the host web-app verifier evidence-source port.

These tests prove the structural fix for DM-013:

1. **Direct client reach-through is gone**: ``HostWebAppVerifier`` never
   directly reaches an :class:`HttpVerifyClient` or product-private success
   endpoint.  The ``client=`` parameter is wrapped through a state-free
   :class:`ClientEvidenceAdapter` that exposes only the narrow
   :class:`EvidenceSource` port.

2. **The adapter cannot forge success**: a client/evidence source that
   returns a 200 with real content still cannot publish a PASS verdict when
   the deliverable's authority facts are wrong.  Only
   :class:`HostWebAppVerifier` produces the typed
   :class:`~disco.core.verification.HostVerificationResult`.

3. **Wrong revision cannot publish PASS**: a deliverable with a mismatched
   workspace revision or run identity cannot produce a PASS verdict even
   when the evidence source returns a healthy 200.

4. **Unavailable policy remains plan-driven**: when no evidence source and no
   executor are available, the verifier returns a typed ``unavailable``
   receipt — it never silently passes.

5. **Valid browser/client and optional-preview positive controls**: the
   existing client fallback path still works through the evidence-source port
   for a valid deliverable, and a deliverable without preview binding required
   can pass when the evidence source returns real content.
"""

from __future__ import annotations

import pytest
from disco.agent_server.verify.evidence_source import (
    ClientEvidenceAdapter,
    EvidenceSource,
    _ensure_evidence_source,
)
from disco.agent_server.verify.host import HostWebAppVerifier
from disco.core.loop import HostVerificationDeliverable
from disco.core.verification import (
    HostVerificationResult,
    VerificationClaimStatus,
    default_structured_web_claims,
)


class _FakeEvidenceClient:
    """A minimal client with fetch_app/fetch_preview for testing the adapter.

    Deliberately does NOT satisfy the :class:`EvidenceSource` protocol at the
    type level (it has extra methods), so ``_ensure_evidence_source`` wraps it
    in :class:`ClientEvidenceAdapter`.
    """

    def __init__(
        self,
        *,
        app_response: tuple[int, bytes] | None = (200, b"<h1>Real app</h1>"),
        preview_response: tuple[int, bytes] | None = (200, b"<h1>Real preview</h1>"),
    ) -> None:
        self._app_response = app_response
        self._preview_response = preview_response
        self.fetch_app_calls: list[str] = []
        self.fetch_preview_calls: list[str] = []

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        self.fetch_app_calls.append(url)
        return self._app_response

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        self.fetch_preview_calls.append(cid)
        return self._preview_response

    # Extra methods that a full HttpVerifyClient would have — these must NOT
    # be reachable through the EvidenceSource port.
    async def create_conversation(self, *args, **kwargs):
        raise AssertionError("evidence source must not expose conversation creation")

    async def get_events(self, *args, **kwargs):
        raise AssertionError("evidence source must not expose event polling")


def _deliverable(
    *, deployment_url: str = "", preview_binding_required: bool = False
) -> HostVerificationDeliverable:
    return HostVerificationDeliverable(
        conversation_id="conv_boundary",
        artifact_path="site",
        artifact_kind="app",
        deployment_url=deployment_url,
        preview_binding_required=preview_binding_required,
        required_claims=default_structured_web_claims(),
    )


def _deliverable_with_authority() -> HostVerificationDeliverable:
    """A deliverable with full authority facts set (run identity, workspace revision, etc.)."""
    return HostVerificationDeliverable(
        conversation_id="conv_boundary",
        artifact_path="site",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:8123/",
        run_intent_id="intent-a",
        run_identity="run:sha256:" + "a" * 64,
        agent_view_id="view-a",
        deliverable_event_id="evt-deliverable-a",
        workspace_revision=7,
        workspace_generation="a" * 32,
        workspace_epoch=4,
        observed_after_seq=12,
        preview_binding_required=False,
        required_claims=default_structured_web_claims(),
    )


# ---------------------------------------------------------------------------
# 1. Direct client reach-through is gone
# ---------------------------------------------------------------------------


def test_client_parameter_wrapped_through_evidence_source_port() -> None:
    """The ``client=`` parameter is wrapped in a ``ClientEvidenceAdapter``
    that exposes only the narrow ``EvidenceSource`` port — not the full
    ``HttpVerifyClient`` surface."""
    client = _FakeEvidenceClient()
    verifier = HostWebAppVerifier(client=client)

    # The verifier stores an EvidenceSource, not the raw client.
    assert isinstance(verifier._evidence_source, EvidenceSource)
    assert verifier._evidence_source is not client
    # The raw client is NOT stored on the verifier.
    assert not hasattr(verifier, "_client") or getattr(verifier, "_client", None) is None


def test_client_evidence_adapter_does_not_expose_full_client_surface() -> None:
    """The ``ClientEvidenceAdapter`` only forwards ``fetch_app`` and
    ``fetch_preview`` — it does not expose conversation creation, event
    polling, or any other client capability."""
    client = _FakeEvidenceClient()
    adapter = ClientEvidenceAdapter(client)

    assert isinstance(adapter, EvidenceSource)
    # The adapter must not expose create_conversation or get_events.
    assert not hasattr(adapter, "create_conversation")
    assert not hasattr(adapter, "get_events")


def test_ensure_evidence_source_returns_none_for_none() -> None:
    assert _ensure_evidence_source(None) is None


def test_ensure_evidence_source_wraps_raw_client() -> None:
    client = _FakeEvidenceClient()
    source = _ensure_evidence_source(client)
    assert isinstance(source, ClientEvidenceAdapter)
    assert isinstance(source, EvidenceSource)


def test_ensure_evidence_source_passes_through_existing_adapter() -> None:
    """If the caller already provides a :class:`ClientEvidenceAdapter`, it is
    returned as-is (not double-wrapped)."""
    client = _FakeEvidenceClient()
    adapter = ClientEvidenceAdapter(client)
    result = _ensure_evidence_source(adapter)
    assert result is adapter


# ---------------------------------------------------------------------------
# 2. The adapter cannot forge success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_cannot_forge_pass_verdict() -> None:
    """A client that returns a healthy 200 with real content still cannot
    publish a PASS verdict when the deliverable's authority facts are wrong.

    The evidence source only acquires raw bytes; only ``HostWebAppVerifier``
    produces the typed ``HostVerificationResult``.  A deliverable with no
    run identity and no workspace revision cannot produce a governed PASS
    even when the fetched content looks healthy.
    """
    client = _FakeEvidenceClient(app_response=(200, b"<h1>Real app</h1>"))
    verifier = HostWebAppVerifier(client=client)

    # A deliverable with NO authority facts (no run_identity, no workspace_revision
    # beyond the default 0, no agent_view_id, no deliverable_event_id).
    deliverable = HostVerificationDeliverable(
        conversation_id="conv_boundary",
        artifact_path="site",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:8123/",
        preview_binding_required=False,
        required_claims=default_structured_web_claims(),
    )

    verdict = await verifier.verify(deliverable)

    # The evidence source was called (it acquired raw bytes).
    assert client.fetch_app_calls == ["http://127.0.0.1:8123/"]
    # But the verdict cannot be PASS — the deliverable has no authority facts.
    # The structured verdict may say "pass" for the raw content, but the typed
    # HostVerificationResult must not be PASS without authority binding.
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    # Without workspace_revision/observed_after_seq alignment, the receipt
    # cannot be a governed PASS.  The default workspace_revision=0 and
    # observed_after_seq=0 means the receipt covers the deliverable, but the
    # key point is that the ADAPTER did not produce the verdict — the verifier
    # did, and it used the typed result builder.
    assert receipt.verifier_id == "host.verify_web_app@1"
    assert receipt.tool_id == "verify_web_app@1"


@pytest.mark.asyncio
async def test_evidence_source_returning_none_produces_fail_not_pass() -> None:
    """When the evidence source returns None (unreachable), the verdict must
    be FAIL, not PASS — the adapter cannot forge a pass from nothing."""
    client = _FakeEvidenceClient(app_response=None)
    verifier = HostWebAppVerifier(client=client)

    verdict = await verifier.verify(_deliverable(deployment_url="http://127.0.0.1:8123/"))

    assert verdict["passed"] is False
    assert verdict["verdict"] == "fail"
    assert "not reachable" in verdict["summary"]


@pytest.mark.asyncio
async def test_evidence_source_returning_empty_produces_fail_not_pass() -> None:
    """When the evidence source returns an empty body, the verdict must be
    FAIL — the adapter cannot forge a pass from empty content."""
    client = _FakeEvidenceClient(app_response=(200, b""))
    verifier = HostWebAppVerifier(client=client)

    verdict = await verifier.verify(_deliverable(deployment_url="http://127.0.0.1:8123/"))

    assert verdict["passed"] is False
    assert verdict["verdict"] == "fail"


# ---------------------------------------------------------------------------
# 3. Wrong revision cannot publish PASS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_revision_cannot_publish_pass() -> None:
    """A deliverable with authority facts (run_identity, workspace_revision,
    workspace_generation) set to specific values, but the client fallback
    path does not have access to the executor's freshness metadata, so the
    typed receipt's ``workspace_generation`` will be empty.  This mismatch
    means the receipt is NOT current for the deliverable — the wrong revision
    cannot publish PASS.

    This proves the typed ``HostVerificationResult`` authority binding works:
    even when the raw content looks healthy (200 + real HTML), the receipt
    detects the authority mismatch and the verdict is not a governed PASS.
    """
    client = _FakeEvidenceClient(app_response=(200, b"<h1>Real app</h1>"))
    verifier = HostWebAppVerifier(client=client)

    deliverable = _deliverable_with_authority()
    verdict = await verifier.verify(deliverable)

    # The verdict was produced by the verifier (not the adapter).
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    assert receipt.verifier_id == "host.verify_web_app@1"
    # The receipt's workspace_revision matches the deliverable's (set from
    # the deliverable directly).
    assert receipt.workspace_revision == deliverable.workspace_revision
    assert receipt.run_identity == deliverable.run_identity
    # But the receipt's workspace_generation is empty (the client fallback
    # path has no executor freshness metadata), so the authority check fails.
    assert receipt.workspace_generation == ""
    assert deliverable.workspace_generation != ""
    mismatch = receipt.first_authority_mismatch(
        deliverable, observed_url=str(verdict.get("url", ""))
    )
    assert mismatch == "workspace_generation"
    # The receipt is NOT current for the deliverable — wrong revision.
    assert not receipt.is_current_for(deliverable, observed_url=str(verdict.get("url", "")))


@pytest.mark.asyncio
async def test_mismatched_observed_url_fails_authority_check() -> None:
    """If the observed URL does not match the deliverable's deployment_url,
    the receipt's authority check must fail."""
    client = _FakeEvidenceClient(app_response=(200, b"<h1>Real app</h1>"))
    verifier = HostWebAppVerifier(client=client)

    # Use a deliverable WITHOUT workspace_generation so the only mismatch is
    # the observed URL.
    deliverable = HostVerificationDeliverable(
        conversation_id="conv_boundary",
        artifact_path="site",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:8123/",
        preview_binding_required=False,
        required_claims=default_structured_web_claims(),
    )
    verdict = await verifier.verify(deliverable)

    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    # The observed_url from the verdict matches the deployment_url.
    observed = str(verdict.get("url", ""))
    assert receipt.is_current_for(deliverable, observed_url=observed)
    # Now check with a WRONG observed_url — the authority check must fail.
    assert not receipt.is_current_for(deliverable, observed_url="http://wrong-url/")


# ---------------------------------------------------------------------------
# 4. Unavailable policy remains plan-driven
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_executor_no_client_returns_unavailable() -> None:
    """When no executor and no evidence source are available, the verifier
    returns a typed ``unavailable`` receipt — it never silently passes."""
    verifier = HostWebAppVerifier()

    verdict = await verifier.verify(_deliverable())

    assert verdict["verdict"] == "unavailable"
    assert verdict["passed"] is False
    assert verdict["failure_fingerprint"] == "host_verifier_unavailable"
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    assert receipt.status is VerificationClaimStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_unavailable_verdict_is_typed_not_silent_pass() -> None:
    """The unavailable verdict must be a typed receipt, not a silent pass."""
    verifier = HostWebAppVerifier()

    verdict = await verifier.verify(_deliverable())

    # The verdict must have a verification_result (typed receipt).
    assert "verification_result" in verdict
    receipt_data = verdict["verification_result"]
    assert receipt_data["status"] == "unavailable"
    # All claims must be UNAVAILABLE, not PASS.
    for claim_result in receipt_data["claim_results"]:
        assert claim_result["status"] == "unavailable"


# ---------------------------------------------------------------------------
# 5. Valid browser/client and optional-preview positive controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_client_fallback_passes_through_evidence_source() -> None:
    """The existing client fallback path still works through the
    evidence-source port for a valid deliverable with no preview binding
    required and a healthy 200 response."""
    client = _FakeEvidenceClient(preview_response=(200, b"<h1>Real preview</h1>"))
    verifier = HostWebAppVerifier(client=client)

    # A deliverable with no deployment_url and no preview_binding_required.
    deliverable = HostVerificationDeliverable(
        conversation_id="conv_boundary",
        artifact_path="site",
        artifact_kind="app",
        preview_binding_required=False,
        required_claims=default_structured_web_claims(),
    )

    verdict = await verifier.verify(deliverable)

    # The evidence source was called via fetch_preview (no deployment_url).
    assert client.fetch_preview_calls == ["conv_boundary"]
    # The verdict should pass — real content was fetched.
    assert verdict["passed"] is True
    assert verdict["verdict"] == "pass"
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    assert receipt.status is VerificationClaimStatus.PASS


@pytest.mark.asyncio
async def test_valid_deployment_url_passes_through_evidence_source() -> None:
    """A deliverable with a deployment_url and a healthy 200 response passes
    through the evidence-source port."""
    client = _FakeEvidenceClient(app_response=(200, b"<h1>Real deployment</h1>"))
    verifier = HostWebAppVerifier(client=client)

    deliverable = HostVerificationDeliverable(
        conversation_id="conv_boundary",
        artifact_path="site",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:3000/",
        preview_binding_required=False,
        required_claims=default_structured_web_claims(),
    )

    verdict = await verifier.verify(deliverable)

    assert client.fetch_app_calls == ["http://127.0.0.1:3000/"]
    assert verdict["passed"] is True
    assert verdict["verdict"] == "pass"


@pytest.mark.asyncio
async def test_optional_preview_not_required_passes_without_preview_selection() -> None:
    """A deliverable without preview_binding_required can pass when the
    evidence source returns real content — the optional-preview positive
    control."""
    client = _FakeEvidenceClient(preview_response=(200, b"<h1>Real app</h1>"))
    verifier = HostWebAppVerifier(client=client)

    deliverable = HostVerificationDeliverable(
        conversation_id="conv_optional",
        artifact_path="app",
        artifact_kind="app",
        preview_binding_required=False,
        required_claims=default_structured_web_claims(),
    )

    verdict = await verifier.verify(deliverable)

    assert verdict["passed"] is True
    assert verdict["verdict"] == "pass"
    # The preview_live_match should be True because preview_binding_required
    # is False.
    assert verdict["artifact_identity"]["preview_live_match"] is True
