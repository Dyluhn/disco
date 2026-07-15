"""Host-owned live-verifier dispatch and real WO-F3.3 exploit proof."""

from __future__ import annotations

import json

import pytest
from disco.agent_server.webhook_live_verifier import make_webhook_live_verifier
from disco.core.appkit import get_recipe
from disco.core.appkit.generator import generate
from disco.core.appkit.primitives import PrimitiveVerifyResult
from disco.core.appkit.records_primitive import default_records_app_spec
from disco.core.appkit.spec import AppSpec, DesignSpec, serialize_app_spec
from disco.core.appkit.webhook_primitive import WebhookSpec, apply_webhook_spec


def _app_tree() -> tuple[AppSpec, DesignSpec, dict[str, str]]:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    data = default_records_app_spec("Webhook live proof", recipe).model_dump(mode="json")
    data["roles"] = ["member"]
    app = AppSpec.model_validate(data)
    specs = (
        WebhookSpec(
            endpoint_id="orders_in",
            direction="inbound",
            event_types=["order.created"],
        ),
        WebhookSpec(
            endpoint_id="orders_out",
            direction="outbound",
            event_types=["order.created"],
        ),
    )
    for spec in specs:
        app = apply_webhook_spec(app, spec)
    design = recipe.to_design_spec()
    tree = generate(app, design)
    tree[".disco/appspec.json"] = serialize_app_spec(app)
    tree[".disco/primitives/webhook.json"] = (
        json.dumps(
            {
                "primitive_id": "webhook",
                "tier": "template_only",
                "applied_at": "2026-07-11T00:00:00Z",
                "specs": [spec.model_dump(mode="json") for spec in specs],
            },
            indent=2,
        )
        + "\n"
    )
    return app, design, tree


@pytest.mark.asyncio
async def test_unknown_live_id_fails_closed() -> None:
    app, design, tree = _app_tree()
    result = await make_webhook_live_verifier()("unknown.security.v1", app, design, tree)
    assert not result.ok
    assert [check.name for check in result.checks] == [
        "forged_signature_rejected",
        "replay_deduped",
        "egress_blocked",
        "secret_absence",
    ]
    assert all(not check.passed for check in result.checks)


@pytest.mark.asyncio
async def test_missing_wrangler_fails_all_named_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, design, tree = _app_tree()
    monkeypatch.setattr("disco.agent_server.webhook_live_verifier.find_wrangler", lambda _cwd: None)
    result = await make_webhook_live_verifier()("webhook.security.v1", app, design, tree)
    assert not result.ok
    assert all(not check.passed for check in result.checks)
    assert all("fail-closed" in check.evidence for check in result.checks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_worker_exploit_verifier_passes() -> None:
    app, design, tree = _app_tree()
    result: PrimitiveVerifyResult = await make_webhook_live_verifier()(
        "webhook.security.v1", app, design, tree
    )
    assert result.ok, result.detail
    assert {check.name: check.passed for check in result.checks} == {
        "forged_signature_rejected": True,
        "replay_deduped": True,
        "egress_blocked": True,
        "secret_absence": True,
    }
