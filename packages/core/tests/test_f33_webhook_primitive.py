"""Epic F3.3 SEAM — the `webhook` primitive (declaration + spec only, fail-closed).

Covers:
  * WebhookSpec validation — direction literal, slug/bounds on endpoint_id and
    event_types, unknown-key refusal (extra=forbid), description cap;
  * the fold (`apply_webhook_spec`) — endpoints recorded as documentation
    sections on the `webhooks` docs page (existing AppSpec shapes only),
    duplicate endpoint_id refused, docs-route collision refused, wrong spec
    type refused;
  * the emitted surface is PENDING-STATE ONLY — the "Handler pending secure
    setup" marker is present and the whole tree contains NO worker route, NO
    wrangler config, NO signature-verification / HMAC / dedup code (absence
    asserted by grep over every emitted path + byte);
  * the registration invariants the WO-A3 finish gate keys on —
    tier == "template_only", verify is None (fail-closed ON PURPOSE),
    host_contract declares webhook.emit, spec_schema/apply_spec make it addable,
    and generator.py's force-import registers it.
"""

from __future__ import annotations

import pytest
from disco.core.appkit import get_recipe
from disco.core.appkit.generator import generate
from disco.core.appkit.primitives import get_primitive, primitive_ids
from disco.core.appkit.recipes import SiteRecipe
from disco.core.appkit.spec import AppSpec, DesignSpec, Page
from disco.core.appkit.webhook_primitive import (
    WEBHOOK_DOCS_PAGE_ID,
    WEBHOOK_EMIT_SERVICE_NAME,
    WEBHOOK_PENDING_MARKER,
    WEBHOOK_PRIMITIVE_ID,
    WebhookSpec,
    apply_webhook_spec,
    default_webhook_app_spec,
    generate_webhook,
)
from pydantic import ValidationError


def _recipe() -> SiteRecipe:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


def _spec(**overrides: object) -> WebhookSpec:
    base: dict[str, object] = {
        "endpoint_id": "order_events",
        "direction": "inbound",
        "event_types": ["order.created", "order.cancelled"],
    }
    base.update(overrides)
    return WebhookSpec.model_validate(base)


# Tokens whose presence in the emitted tree would mean security code / a live
# receiver leaked into the SEAM. Checked case-insensitively over every path and
# every emitted byte.
_FORBIDDEN_TREE_TOKENS = (
    "hmac",
    "createhmac",
    "timingsafeequal",
    "crypto.subtle",
    "x-webhook-signature",
    "idempotency",
    "addeventlistener",
    "export default",  # a Worker module handler
    "wrangler",
    "fetch(",
)


def _assert_tree_is_inert(tree: dict[str, str]) -> None:
    """The SEAM guarantee: no worker route, no script, no security code."""
    assert set(tree) == {"index.html"}, f"unexpected files emitted: {sorted(tree)}"
    for path, contents in tree.items():
        haystack = (path + "\n" + contents).lower()
        for token in _FORBIDDEN_TREE_TOKENS:
            assert token not in haystack, f"forbidden token {token!r} in {path}"
        assert "<script" not in haystack, f"script tag emitted in {path}"
        assert "<form" not in haystack, f"form emitted in {path} (false affordance)"


# ---- 1. WebhookSpec validation ---------------------------------------------------


def test_valid_spec_accepts_and_defaults() -> None:
    spec = _spec()
    assert spec.endpoint_id == "order_events"
    assert spec.direction == "inbound"
    assert spec.description is None
    outbound = _spec(direction="outbound", description="Emits order lifecycle events.")
    assert outbound.direction == "outbound"


def test_direction_is_a_closed_literal() -> None:
    with pytest.raises(ValidationError):
        _spec(direction="sideways")


@pytest.mark.parametrize(
    "bad_id", ["", "Order-Events", "9lives", "has space", "x" * 49]
)
def test_endpoint_id_slug_bounds(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _spec(endpoint_id=bad_id)


@pytest.mark.parametrize(
    "bad_events",
    [
        [],  # min 1
        [f"ev_{i}" for i in range(13)],  # max 12
        ["Order.Created"],  # not a slug
        ["order..created"],  # malformed dotted slug
        ["x" * 65],  # per-item length cap
        ["order.created", "order.created"],  # duplicate
    ],
)
def test_event_types_bounds(bad_events: list[str]) -> None:
    with pytest.raises(ValidationError):
        _spec(event_types=bad_events)


def test_unknown_key_refused() -> None:
    # extra=forbid — and specifically, a smuggled secret is a refusal.
    with pytest.raises(ValidationError):
        WebhookSpec.model_validate(
            {
                "endpoint_id": "e",
                "direction": "inbound",
                "event_types": ["a.b"],
                "signing_secret": "shh",
            }
        )


def test_description_bounded() -> None:
    with pytest.raises(ValidationError):
        _spec(description="x" * 501)


# ---- 2. the fold ------------------------------------------------------------------


def test_fold_records_endpoint_on_docs_page() -> None:
    app = default_webhook_app_spec("Acme Studio", _recipe())
    folded = apply_webhook_spec(app, _spec())
    page = next(p for p in folded.pages if p.id == WEBHOOK_DOCS_PAGE_ID)
    section = next(s for s in page.sections if s.id == "webhook_order_events")
    assert section.kind == "custom"
    assert section.content is not None
    assert "order_events" in (section.content.heading or "")
    assert "inbound" in (section.content.heading or "")
    assert WEBHOOK_PENDING_MARKER in (section.content.subheading or "")
    assert tuple(section.content.items) == ("order.created", "order.cancelled")


def test_fold_creates_docs_page_on_foreign_base_app() -> None:
    app = AppSpec(
        schema_version=1,
        app_kind="hello",
        name="Acme",
        pages=(Page(id="home", route="/", title="Home"),),
    )
    folded = apply_webhook_spec(app, _spec(direction="outbound"))
    page = next(p for p in folded.pages if p.id == WEBHOOK_DOCS_PAGE_ID)
    assert page.route == "/webhooks"
    assert [s.id for s in page.sections] == ["webhook_order_events"]
    # a second, different endpoint appends to the SAME docs page
    folded2 = apply_webhook_spec(
        folded, _spec(endpoint_id="billing", event_types=["invoice.paid"])
    )
    page2 = next(p for p in folded2.pages if p.id == WEBHOOK_DOCS_PAGE_ID)
    assert [s.id for s in page2.sections] == [
        "webhook_order_events",
        "webhook_billing",
    ]


def test_duplicate_endpoint_id_refused() -> None:
    app = apply_webhook_spec(default_webhook_app_spec("Acme", _recipe()), _spec())
    with pytest.raises(ValueError, match="duplicate webhook endpoint_id"):
        apply_webhook_spec(app, _spec(direction="outbound"))


def test_docs_route_collision_refused() -> None:
    app = AppSpec(
        schema_version=1,
        app_kind="hello",
        name="Acme",
        pages=(Page(id="other", route="/webhooks", title="Not the docs page"),),
    )
    with pytest.raises(ValueError, match="already taken"):
        apply_webhook_spec(app, _spec())


def test_wrong_spec_type_refused() -> None:
    from disco.core.appkit.hello_primitive import HelloSpec

    app = default_webhook_app_spec("Acme", _recipe())
    with pytest.raises(TypeError):
        apply_webhook_spec(app, HelloSpec(headline="nope"))


# ---- 3. the emitted surface is pending-state, with NO security code ---------------


def test_empty_standalone_surface_is_inert_and_marked() -> None:
    app = default_webhook_app_spec("Acme Studio", _recipe())
    tree = generate_webhook(app, _design())
    _assert_tree_is_inert(tree)
    page = tree["index.html"]
    assert WEBHOOK_PENDING_MARKER in page
    assert "No webhook endpoints declared yet." in page


def test_declared_endpoint_renders_pending_not_active() -> None:
    app = apply_webhook_spec(
        default_webhook_app_spec("Acme Studio", _recipe()),
        _spec(description="Order lifecycle receiver."),
    )
    tree = generate_webhook(app, _design())
    _assert_tree_is_inert(tree)
    page = tree["index.html"]
    # the declared contract is visible…
    assert "order_events" in page
    assert "inbound" in page
    assert "order.created" in page
    assert "order.cancelled" in page
    assert "Order lifecycle receiver." in page
    # …and unmistakably NOT active
    assert WEBHOOK_PENDING_MARKER in page
    assert "declared, not active" in page
    assert "no live handler exists" in page.lower()


def test_registry_generate_dispatches_to_webhook() -> None:
    # through the public generate() (app_kind dispatch), not just the module fn
    app = apply_webhook_spec(default_webhook_app_spec("Acme", _recipe()), _spec())
    tree = generate(app, _design())
    _assert_tree_is_inert(tree)
    assert WEBHOOK_PENDING_MARKER in tree["index.html"]


# ---- 4. registration invariants (what the WO-A3 gate keys on) ---------------------


def test_registered_via_generator_force_import() -> None:
    assert WEBHOOK_PRIMITIVE_ID in primitive_ids()


def test_registration_is_fail_closed_template_only() -> None:
    prim = get_primitive(WEBHOOK_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "template_only"
    assert prim.verify is None  # fail-closed ON PURPOSE — the security fill flips it
    assert any(s.name == WEBHOOK_EMIT_SERVICE_NAME for s in prim.host_contract)
    assert prim.spec_schema is WebhookSpec
    assert prim.apply_spec is not None  # addable via app_add_primitive


def test_no_host_service_actually_registered_for_webhook_emit() -> None:
    # declaration only: the host_contract names webhook.emit but the SEAM must
    # NOT register a live handler under that name.
    from disco.core.host_services import get_host_service

    assert get_host_service(WEBHOOK_EMIT_SERVICE_NAME) is None
