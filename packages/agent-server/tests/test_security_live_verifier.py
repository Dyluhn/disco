"""Exact fail-closed dispatch across mandatory security primitive verifiers."""

from __future__ import annotations

import pytest
from disco.agent_server import security_live_verifier as dispatch
from disco.core.appkit import get_recipe
from disco.core.appkit.primitives import PrimitiveVerifyResult, VerifyCheck
from disco.core.appkit.records_primitive import default_records_app_spec


def _result(name: str) -> PrimitiveVerifyResult:
    return PrimitiveVerifyResult(
        ok=True,
        detail="ok",
        checks=(VerifyCheck(name=name, passed=True, evidence="called"),),
    )


@pytest.mark.asyncio
async def test_dispatches_only_exact_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def stripe(live_id, _app, _design, _tree):
        calls.append(live_id)
        return _result("stripe")

    async def webhook(live_id, _app, _design, _tree):
        calls.append(live_id)
        return _result("webhook")

    monkeypatch.setattr(dispatch, "make_stripe_live_verifier", lambda: stripe)
    monkeypatch.setattr(dispatch, "make_webhook_live_verifier", lambda: webhook)
    verifier = dispatch.make_security_live_verifier()
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    app = default_records_app_spec("Dispatch", recipe)
    design = recipe.to_design_spec()

    assert (await verifier("stripe.security.v1", app, design, {})).checks[0].name == "stripe"
    assert (await verifier("webhook.security.v1", app, design, {})).checks[0].name == "webhook"
    refused = await verifier("stripe.security.v2", app, design, {})
    assert not refused.ok
    assert refused.checks[0].name == "unknown_live_verify_id"
    assert calls == ["stripe.security.v1", "webhook.security.v1"]
