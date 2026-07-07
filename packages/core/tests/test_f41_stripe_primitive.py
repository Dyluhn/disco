"""Epic F4.1 SEAM — the `stripe` primitive (core surface).

Covers:
  * REGISTRATION INVARIANTS the WO-A3 gate keys on: tier == "template_only" AND
    verify is None (fail-closed — a stripe-bearing app cannot ship unverified),
    plus the two declared host-contract services and the force-import through
    generator.py;
  * spec validation: bounds, blank refusals, entitlement-flag slug rule,
    unknown key refused (extra="forbid" — no smuggled price_id/secret fields);
  * fold correctness on a records app: pricing section (house kind + catalog
    variant) with plan/price/features, entitlement flag recorded in
    AppSpec.roles, replace-not-duplicate on re-apply, every spec field visible
    (changed spec ⇒ changed app), page synthesized when the app has none;
  * the emitted UI is in the DISABLED/pending state on BOTH paths — the folded
    section lowered by the records base primitive (no CTA/button affordance at
    all) and the standalone static page (disabled button + helper text) — and
    the tree contains NO live checkout surface (no /api/checkout, no
    stripe.com URL, no secret-shaped string).
"""

from __future__ import annotations

import importlib

import pytest
from disco.core.appkit import get_recipe
from disco.core.appkit.primitives import HostService, get_primitive
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.appkit.stripe_primitive import (
    PAYMENTS_PENDING_HELPER,
    PAYMENTS_PENDING_LABEL,
    STRIPE_PRICING_SECTION_ID,
    STRIPE_PRICING_VARIANT_ID,
    STRIPE_PRIMITIVE_ID,
    StripeSpec,
    apply_stripe_spec,
    default_stripe_app_spec,
    generate_stripe,
)
from pydantic import ValidationError


def _spec(**overrides: object) -> StripeSpec:
    base: dict[str, object] = {
        "plan_name": "Pro",
        "price_display": "$9/mo",
        "entitlement_flag": "pro_member",
        "success_message": "Welcome to Pro — your workspace is unlocked.",
        "features": ["Unlimited records", "Priority support"],
    }
    base.update(overrides)
    return StripeSpec.model_validate(base)


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


def _records_app() -> AppSpec:
    records = get_primitive("records")
    assert records is not None
    return records.default_app_spec("Shift Manager", _recipe())


# Strings that would indicate a LIVE checkout surface or a secret leaked into the
# emitted tree. None of these may appear anywhere (contents), and no emitted
# PATH may be payments-named either.
_FORBIDDEN_CONTENT = (
    "/api/checkout",
    "api.stripe.com",
    "checkout.stripe.com",
    "js.stripe.com",
    "sk_live",
    "sk_test",
    "whsec_",
)


def _assert_no_live_checkout_surface(tree: dict[str, str]) -> None:
    for path, contents in tree.items():
        lowered_path = path.lower()
        assert "checkout" not in lowered_path, f"payments-named path emitted: {path}"
        assert "webhook" not in lowered_path, f"payments-named path emitted: {path}"
        if "stripe" in lowered_path:
            # the ONLY legitimate stripe-named file is the pricing-card section
            # component (named after the `stripe_pricing` section id) — never a
            # route/client/api file
            assert path.startswith("src/components/") and "pricing" in lowered_path, (
                f"unexpected payments-named path emitted: {path}"
            )
        for marker in _FORBIDDEN_CONTENT:
            assert marker not in contents, f"live-checkout/secret marker {marker!r} in {path}"


# ---- 1. registration: the exact invariants the WO-A3 gate keys on ----------------


def test_registered_through_generator_force_import() -> None:
    # the ONLY wiring is the force-import line in generator.py — importing the
    # generator must be sufficient for the registry to know stripe.
    importlib.import_module("disco.core.appkit.generator")
    prim = get_primitive(STRIPE_PRIMITIVE_ID)
    assert prim is not None
    assert prim.id == STRIPE_PRIMITIVE_ID


def test_fail_closed_registration_invariants() -> None:
    """WO-A3's rule keys on tier=='template_only' AND verify is None ⇒ a
    stripe-bearing app CANNOT pass the finish gate. These two facts are the
    seam's whole safety story — if either assertion ever fails, someone tried
    to work around the gate (see the module docstring: forbidden)."""
    prim = get_primitive(STRIPE_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "template_only"
    assert prim.verify is None  # ON PURPOSE — never a passing stub
    assert prim.host_contract == (
        HostService("payments.checkout"),
        HostService("payments.webhook"),
    )
    # addable: both halves of the WO-A1 contract are set
    assert prim.spec_schema is StripeSpec
    assert prim.apply_spec is apply_stripe_spec


# ---- 2. spec validation -----------------------------------------------------------


def test_valid_spec_roundtrips() -> None:
    spec = _spec()
    assert spec.plan_name == "Pro"
    assert spec.features == ("Unlimited records", "Priority support")


def test_unknown_key_refused() -> None:
    # extra="forbid": a smuggled machine-price/secret field is a refusal
    for extra in ({"price_id": "price_123"}, {"api_key": "x"}, {"plan": "Pro"}):
        with pytest.raises(ValidationError):
            _spec(**extra)


@pytest.mark.parametrize(
    "overrides",
    [
        {"plan_name": ""},
        {"plan_name": "   "},
        {"plan_name": "x" * 81},
        {"price_display": ""},
        {"price_display": "x" * 41},
        {"success_message": ""},
        {"success_message": "x" * 201},
        {"features": ["ok"] * 9},  # > 8 bullets
        {"features": ["x" * 121]},  # bullet too long
        {"features": ["  "]},  # blank bullet
    ],
)
def test_bounds_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _spec(**overrides)


@pytest.mark.parametrize("flag", ["Pro", "9pro", "pro-member", "pro member", "", "_pro"])
def test_entitlement_flag_must_be_role_slug(flag: str) -> None:
    # the flag lands in AppSpec.roles, so it must satisfy the same snake_case
    # identifier rule spec.py enforces on roles
    with pytest.raises(ValidationError):
        _spec(entitlement_flag=flag)


def test_missing_required_fields_refused() -> None:
    with pytest.raises(ValidationError):
        StripeSpec.model_validate({"plan_name": "Pro"})


# ---- 3. fold correctness ----------------------------------------------------------


def test_fold_adds_pricing_section_and_entitlement_role() -> None:
    app = _records_app()
    folded = apply_stripe_spec(app, _spec())

    sections = [s for s in folded.pages[0].sections if s.id == STRIPE_PRICING_SECTION_ID]
    assert len(sections) == 1
    section = sections[0]
    assert section.kind == "pricing"  # house SectionKind, not an invented one
    assert section.variant_id == STRIPE_PRICING_VARIANT_ID
    assert section.content is not None
    assert section.content.heading == "Pro"
    assert section.content.subheading == "$9/mo"
    assert section.content.items == ("Unlimited records", "Priority support")
    # NO cta_label: the shared pricing emitter renders no button, so the folded
    # card can never show a live-looking checkout affordance
    assert section.content.cta_label is None
    body = section.content.body
    assert body is not None
    assert PAYMENTS_PENDING_LABEL in body
    assert PAYMENTS_PENDING_HELPER in body
    assert "Welcome to Pro" in body  # success_message is described, not enacted

    # the entitlement flag is recorded in the EXISTING RBAC surface: AppSpec.roles
    assert "pro_member" in folded.roles
    # ... without disturbing the app's own roles
    assert set(app.roles) <= set(folded.roles)
    # existing sections survive the fold
    assert len(folded.pages[0].sections) == len(app.pages[0].sections) + 1


def test_fold_synthesizes_home_page_when_app_has_none() -> None:
    bare = AppSpec(schema_version=1, app_kind="web_app", name="Bare")
    folded = apply_stripe_spec(bare, _spec())
    assert len(folded.pages) == 1
    assert folded.pages[0].route == "/"
    assert folded.pages[0].sections[0].id == STRIPE_PRICING_SECTION_ID
    assert folded.roles == ("pro_member",)


def test_reapply_replaces_never_duplicates() -> None:
    app = _records_app()
    once = apply_stripe_spec(app, _spec())
    twice = apply_stripe_spec(once, _spec(price_display="$19/mo", entitlement_flag="pro_plus"))
    stripe_sections = [
        s for p in twice.pages for s in p.sections if s.id == STRIPE_PRICING_SECTION_ID
    ]
    assert len(stripe_sections) == 1
    assert stripe_sections[0].content is not None
    assert stripe_sections[0].content.subheading == "$19/mo"
    # both flags are roles now (recording is additive; revocation semantics are
    # deferred security fill, not a spec fold concern)
    assert {"pro_member", "pro_plus"} <= set(twice.roles)


def test_every_spec_field_is_visible_in_the_fold() -> None:
    """Changed spec ⇒ changed app, for EACH field — otherwise app_add_primitive's
    no-op detection would refuse a genuinely different re-apply."""
    app = _records_app()
    base = apply_stripe_spec(app, _spec()).model_dump(mode="json")
    for overrides in (
        {"plan_name": "Team"},
        {"price_display": "$29/mo"},
        {"entitlement_flag": "team_member"},
        {"success_message": "You are in."},
        {"features": ["One", "Two", "Three"]},
    ):
        changed = apply_stripe_spec(app, _spec(**overrides)).model_dump(mode="json")
        assert changed != base, f"fold not visible for {sorted(overrides)}"


def test_identical_fold_is_idempotent() -> None:
    # the tool layer detects no-ops by dump equality — an identical re-apply must
    # produce an identical AppSpec
    app = _records_app()
    once = apply_stripe_spec(app, _spec())
    again = apply_stripe_spec(once, _spec())
    assert once.model_dump(mode="json") == again.model_dump(mode="json")


def test_fold_rejects_wrong_spec_type() -> None:
    from disco.core.appkit.hello_primitive import HelloSpec

    with pytest.raises(TypeError):
        apply_stripe_spec(_records_app(), HelloSpec(headline="nope"))


# ---- 4. the emitted UI: pending state, no live affordance -------------------------


def test_folded_tree_renders_pending_card_with_no_live_checkout() -> None:
    records = get_primitive("records")
    assert records is not None
    folded = apply_stripe_spec(_records_app(), _spec())
    tree = records.generate(folded, _design())

    content_ts = tree["src/generated/content.ts"]
    assert "Pro" in content_ts
    assert "$9/mo" in content_ts
    assert "Unlimited records" in content_ts
    assert PAYMENTS_PENDING_LABEL in content_ts
    assert PAYMENTS_PENDING_HELPER in content_ts

    # the stripe pricing component exists and carries NO checkout affordance:
    # the shared pricing emitter renders heading/price/items only — no ctaLabel,
    # no submit hook, no POST
    stripe_components = [
        (path, contents)
        for path, contents in tree.items()
        if path.startswith("src/components/") and 'data-appkit-section="stripe_pricing"' in contents
    ]
    assert len(stripe_components) == 1
    _, component = stripe_components[0]
    assert "ctaLabel" not in component
    assert "postJson" not in component
    assert "useSubmit" not in component
    assert "<form" not in component
    assert "fetch(" not in component

    _assert_no_live_checkout_surface(tree)


def test_standalone_generate_is_disabled_pending_card() -> None:
    prim = get_primitive(STRIPE_PRIMITIVE_ID)
    assert prim is not None
    app = prim.default_app_spec("Acme Studio", _recipe())
    assert app.app_kind == STRIPE_PRIMITIVE_ID
    tree = prim.generate(app, _design())
    page = tree["index.html"]

    # the DISABLED marker + helper text: a viewer can never mistake this for live
    assert 'data-payments-state="setup-pending"' in page
    assert "<button" in page and "disabled" in page and 'aria-disabled="true"' in page
    assert PAYMENTS_PENDING_LABEL in page
    assert PAYMENTS_PENDING_HELPER in page

    # nothing live-looking or wired: no form, no script, no fetch, no API path
    assert "<form" not in page
    assert "<script" not in page
    assert "fetch(" not in page
    assert "/api/" not in page
    assert "href=" not in page
    _assert_no_live_checkout_surface(tree)


def test_standalone_generate_renders_folded_spec_and_escapes_html() -> None:
    folded = apply_stripe_spec(
        default_stripe_app_spec("Acme <Studio>", _recipe()),
        _spec(plan_name="Pro <b>Plan</b>", features=["A & B"]),
    )
    page = generate_stripe(folded, _design())["index.html"]
    assert "Acme &lt;Studio&gt;" in page
    assert "Pro &lt;b&gt;Plan&lt;/b&gt;" in page
    assert "A &amp; B" in page
    assert "$9/mo" in page
    assert "<b>" not in page  # spec strings land as inert text, never markup
