"""Core security contract for the completed WO-F3.3 webhook primitive."""

from __future__ import annotations

import json
import sqlite3

import pytest
from disco.core.appkit import get_recipe
from disco.core.appkit.generator import generate
from disco.core.appkit.primitives import HostService, get_primitive, primitive_ids
from disco.core.appkit.records_primitive import default_records_auth_app_spec
from disco.core.appkit.spec import AppSpec, DesignSpec, Page, serialize_app_spec
from disco.core.appkit.webhook_primitive import (
    WEBHOOK_DOCS_PAGE_ID,
    WEBHOOK_EMIT_SERVICE_NAME,
    WEBHOOK_PENDING_MARKER,
    WEBHOOK_PRIMITIVE_ID,
    WEBHOOK_SECURED_MARKER,
    WebhookSpec,
    apply_webhook_spec,
    default_webhook_app_spec,
    generate_webhook,
    webhook_verify,
)
from disco.core.host_services import get_host_service
from disco.core.webhook_host_service import WEBHOOK_EMIT_SERVICE
from pydantic import ValidationError


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


def _spec(**overrides: object) -> WebhookSpec:
    raw: dict[str, object] = {
        "endpoint_id": "order_events",
        "direction": "inbound",
        "event_types": ["order.created", "order.cancelled"],
        "description": "Order lifecycle receiver.",
    }
    raw.update(overrides)
    return WebhookSpec.model_validate(raw)


def _records_app() -> AppSpec:
    app = default_records_auth_app_spec("Acme Operations", _recipe())
    assert app.roles, "the webhook fill requires the records auth surface"
    return app


def _filled_app() -> tuple[AppSpec, tuple[WebhookSpec, WebhookSpec]]:
    inbound = _spec()
    outbound = _spec(
        endpoint_id="fulfillment_events",
        direction="outbound",
        event_types=["order.shipped", "order.delayed"],
        description="Signed fulfillment deliveries.",
    )
    app = apply_webhook_spec(_records_app(), inbound)
    app = apply_webhook_spec(app, outbound)
    return app, (inbound, outbound)


def _verified_tree() -> tuple[AppSpec, dict[str, str]]:
    app, specs = _filled_app()
    tree = generate(app, _design())
    # app_add_primitive owns these two persisted inputs.  The primitive verifier
    # deliberately requires them in addition to the generated projection.
    tree[".disco/appspec.json"] = serialize_app_spec(app)
    tree[".disco/primitives/webhook.json"] = json.dumps(
        {
            "primitive_id": WEBHOOK_PRIMITIVE_ID,
            "tier": "template_only",
            "applied_at": "2026-07-11T00:00:00+00:00",
            "specs": [spec.model_dump(mode="json") for spec in specs],
        }
    )
    return app, tree


def _assert_standalone_is_inert(tree: dict[str, str]) -> None:
    assert set(tree) == {"index.html"}
    page = tree["index.html"]
    assert WEBHOOK_PENDING_MARKER in page
    lowered = page.lower()
    for marker in (
        "crypto.subtle",
        "hmac",
        "fetch(",
        "wrangler",
        "addeventlistener",
        "<script",
        "<form",
    ):
        assert marker not in lowered


# ---- bounded declarative input -------------------------------------------------


def test_spec_accepts_bounded_non_secret_contract() -> None:
    spec = _spec()
    assert spec.direction == "inbound"
    assert spec.event_types == ["order.created", "order.cancelled"]
    dumped = spec.model_dump(mode="json")
    assert set(dumped) == {"endpoint_id", "direction", "event_types", "description"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", "sideways"),
        ("endpoint_id", "Order-Events"),
        ("endpoint_id", "9events"),
        ("event_types", []),
        ("event_types", ["Order.Created"]),
        ("event_types", ["order.created", "order.created"]),
        ("description", "x" * 501),
    ],
)
def test_spec_refuses_invalid_contract(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _spec(**{field: value})


def test_spec_refuses_smuggled_target_or_secret() -> None:
    for field in ("target_url", "signing_secret", "secret_ref"):
        with pytest.raises(ValidationError):
            _spec(**{field: "attacker-controlled"})


# ---- honest standalone and supported fold -------------------------------------


def test_standalone_remains_inert_and_visibly_pending() -> None:
    app = default_webhook_app_spec("Acme", _recipe())
    tree = generate_webhook(app, _design())
    _assert_standalone_is_inert(tree)
    assert "No webhook endpoints declared yet." in tree["index.html"]

    declared = apply_webhook_spec(app, _spec())
    assert declared.webhooks is None
    declared_tree = generate_webhook(declared, _design())
    _assert_standalone_is_inert(declared_tree)
    assert "order_events" in declared_tree["index.html"]
    assert "Declared contract only" in declared_tree["index.html"]
    assert "no live handler exists" in declared_tree["index.html"]


def test_unsupported_or_unauthenticated_base_app_is_refused() -> None:
    hello = AppSpec(
        schema_version=1,
        app_kind="hello",
        name="Unsupported",
        pages=(Page(id="home", route="/", title="Home"),),
    )
    with pytest.raises(ValueError, match="D1-backed records"):
        apply_webhook_spec(hello, _spec())

    records_without_roles = _records_app().model_copy(update={"roles": ()})
    with pytest.raises(ValueError, match="session-authenticated"):
        apply_webhook_spec(records_without_roles, _spec())


def test_records_fold_persists_non_secret_metadata_and_secured_docs() -> None:
    app, _specs = _filled_app()
    assert app.webhooks is not None
    assert app.webhooks.app_binding.startswith("app_")
    assert len(app.webhooks.app_binding) == 36
    assert [endpoint.endpoint_id for endpoint in app.webhooks.endpoints] == [
        "order_events",
        "fulfillment_events",
    ]
    assert [endpoint.direction for endpoint in app.webhooks.endpoints] == [
        "inbound",
        "outbound",
    ]
    dumped = app.model_dump(mode="json")["webhooks"]
    assert set(dumped) == {"app_binding", "endpoints"}
    assert "secret" not in json.dumps(dumped).lower()
    assert "target" not in json.dumps(dumped).lower()

    docs = next(page for page in app.pages if page.id == WEBHOOK_DOCS_PAGE_ID)
    assert [section.id for section in docs.sections] == [
        "webhook_order_events",
        "webhook_fulfillment_events",
    ]
    assert all(
        section.content is not None
        and section.content.subheading == WEBHOOK_SECURED_MARKER
        for section in docs.sections
    )


def test_records_generation_emits_trusted_inbound_outbound_schema_and_bus_shim() -> None:
    app, _specs = _filled_app()
    tree = generate(app, _design())
    worker = tree["worker/index.ts"]
    assert 'rawPath === "/api/webhooks/order_events"' in worker
    assert 'rawPath === "/api/webhooks/fulfillment_events/emit"' in worker
    assert 'request.headers.get("Disco-Webhook-Signature")' in worker
    assert 'crypto.subtle.verify("HMAC"' in worker
    assert "JSON.parse" in worker
    assert worker.index('crypto.subtle.verify("HMAC"') < worker.index(
        "parseWebhookEnvelope(body)"
    )
    assert 'env.DB.withSession("first-primary")' in worker
    assert "await db.batch([" in worker
    assert worker.index("INSERT INTO webhook_events") < worker.index(
        "INSERT INTO webhook_effects"
    )
    assert 'svc(env, "webhook.emit"' in worker
    assert 'import { svc } from "./disco-client"' in worker
    assert "WEBHOOK_SIGNING_SECRET?: string" in worker
    assert "WEBHOOK_RUNTIME_READY?: string" in worker
    assert worker.count('env.WEBHOOK_RUNTIME_READY !== "1"') == 2
    assert "export async function svc" in tree["worker/disco-client.ts"]

    schema = tree["schema.sql"]
    assert "webhook_events" in schema and "webhook_effects" in schema
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(schema)
        assert {"webhook_events", "webhook_effects"} <= {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        db.close()
    emitted = "\n".join(tree.values()).lower()
    assert "webhook_secret_" not in emitted
    assert "whsec_" not in emitted


# ---- fail-closed registration and static verifier -----------------------------


def test_registration_requires_real_static_and_live_verification() -> None:
    assert WEBHOOK_PRIMITIVE_ID in primitive_ids()
    primitive = get_primitive(WEBHOOK_PRIMITIVE_ID)
    assert primitive is not None
    assert primitive.tier == "template_only"
    assert primitive.verify is webhook_verify
    assert primitive.host_contract == (HostService(WEBHOOK_EMIT_SERVICE_NAME),)
    assert primitive.spec_schema is WebhookSpec
    assert primitive.apply_spec is apply_webhook_spec
    assert primitive.live_verify_id == "webhook.security.v1"
    assert primitive.live_verify_checks == (
        "forged_signature_rejected",
        "replay_deduped",
        "egress_blocked",
        "secret_absence",
    )
    assert primitive.security_metadata_field == "webhooks"
    assert get_host_service(WEBHOOK_EMIT_SERVICE_NAME) is WEBHOOK_EMIT_SERVICE


def test_static_verifier_accepts_only_canonical_provenanced_tree() -> None:
    app, tree = _verified_tree()
    result = webhook_verify(app, _design(), tree)
    assert result.ok, result.detail
    assert all(check.passed for check in result.checks)


def test_static_verifier_fails_missing_provenance() -> None:
    app, tree = _verified_tree()
    del tree[".disco/primitives/webhook.json"]
    result = webhook_verify(app, _design(), tree)
    assert not result.ok
    check = next(c for c in result.checks if c.name == "webhook_provenance_binding")
    assert not check.passed and "missing" in check.evidence


def test_static_verifier_fails_tampered_worker() -> None:
    app, tree = _verified_tree()
    tree["worker/index.ts"] = tree["worker/index.ts"].replace(
        'crypto.subtle.verify("HMAC"', "Promise.resolve(true) // tampered"
    )
    result = webhook_verify(app, _design(), tree)
    assert not result.ok
    check = next(c for c in result.checks if c.name == "webhook_trusted_tree")
    assert not check.passed and "worker/index.ts" in check.evidence


@pytest.mark.parametrize("secret", ["whsec_do_not_ship_1234", "webhook_secret_do_not_ship"])
def test_static_verifier_fails_secret_shaped_material_anywhere(secret: str) -> None:
    app, tree = _verified_tree()
    tree["README-secret.txt"] = f"credential={secret}\n"
    result = webhook_verify(app, _design(), tree)
    assert not result.ok
    check = next(c for c in result.checks if c.name == "webhook_static_secret_absence")
    assert not check.passed and "README-secret.txt" in check.evidence
