"""Epic F4.1 SEAM — the `stripe` primitive (core surface).

Covers:
  * REGISTRATION INVARIANTS the WO-A3 gate keys on: tier == "template_only" AND
    verify is None (fail-closed — a stripe-bearing app cannot ship unverified),
    plus the declared outbound host-contract service and the force-import through
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

import hashlib
import hmac
import importlib
import json
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

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
    from disco.core.appkit.records_primitive import default_records_auth_app_spec

    return default_records_auth_app_spec("Shift Manager", _recipe())


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
    """Stripe stays template-only and requires both its pure checker and a
    host-owned live verifier id; neither is optional."""
    prim = get_primitive(STRIPE_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "template_only"
    assert prim.verify is not None
    assert prim.live_verify_id == "stripe.security.v1"
    assert prim.security_metadata_field == "stripe"
    assert prim.live_verify_checks == (
        "forged_signature_rejected",
        "replay_deduped",
        "secret_absence",
        "restricted_key_only",
        "price_injection_refused",
    )
    # Webhooks are inbound Worker routes, never a host-service capability.
    assert prim.host_contract == (
        HostService("payments.checkout"),
        HostService("payments.ready"),
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
    twice = apply_stripe_spec(once, _spec(price_display="$19/mo"))
    stripe_sections = [
        s for p in twice.pages for s in p.sections if s.id == STRIPE_PRICING_SECTION_ID
    ]
    assert len(stripe_sections) == 1
    assert stripe_sections[0].content is not None
    assert stripe_sections[0].content.subheading == "$19/mo"
    assert "pro_member" in twice.roles


def test_entitlement_role_cannot_be_reassigned_or_pregranted() -> None:
    with pytest.raises(ValueError, match="existing non-payment role"):
        apply_stripe_spec(AppSpec(schema_version=1, app_kind="records", name="Bare"), _spec())
    pregranted = _records_app().model_copy(update={"roles": ("member", "pro_member")})
    with pytest.raises(ValueError, match="must be a new"):
        apply_stripe_spec(pregranted, _spec())
    once = apply_stripe_spec(_records_app(), _spec())
    with pytest.raises(ValueError, match="cannot change"):
        apply_stripe_spec(once, _spec(entitlement_flag="pro_plus"))


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


def test_folded_records_tree_renders_runtime_gated_live_checkout() -> None:
    from disco.core.appkit.generator import generate

    records = get_primitive("records")
    assert records is not None
    folded = apply_stripe_spec(_records_app(), _spec())
    tree = generate(folded, _design())

    content_ts = tree["src/generated/content.ts"]
    assert "Pro" in content_ts
    assert "$9/mo" in content_ts
    assert "Unlimited records" in content_ts
    assert PAYMENTS_PENDING_LABEL in content_ts
    assert PAYMENTS_PENDING_HELPER in content_ts

    # The add-on now has a real Worker route.  Its CTA still fails closed until
    # the host injects the runtime-ready binding.
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
    assert 'fetch("/api/stripe/status"' in component
    assert 'fetch("/api/stripe/checkout"' in component
    assert "disabled={!ready || busy}" in component
    assert PAYMENTS_PENDING_LABEL in component
    assert PAYMENTS_PENDING_HELPER in component
    assert "worker/disco-client.ts" in tree


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


# ---- 5. WO-F4.1 Worker/data-plane security fill -------------------------------


def _filled_tree() -> tuple[AppSpec, dict[str, str]]:
    from disco.core.appkit.generator import generate

    app = apply_stripe_spec(_records_app(), _spec())
    return app, generate(app, _design())


def test_fold_persists_only_non_secret_machine_metadata() -> None:
    app = apply_stripe_spec(_records_app(), _spec())
    assert app.stripe is not None
    assert app.stripe.plan_selector == "pro_member"
    assert app.stripe.app_binding.startswith("app_")
    assert len(app.stripe.app_binding) == 36
    assert app.stripe.entitlement_flag == "pro_member"
    assert app.stripe.success_message.startswith("Welcome to Pro")
    dumped = app.model_dump(mode="json")["stripe"]
    assert set(dumped) == {
        "app_binding",
        "plan_selector",
        "entitlement_flag",
        "success_message",
    }
    assert not any(key in dumped for key in ("price_id", "amount", "api_key", "secret"))


def test_stripe_app_binding_is_strict_and_stable_across_reapply() -> None:
    once = apply_stripe_spec(_records_app(), _spec())
    again = apply_stripe_spec(once, _spec(price_display="$19/mo"))
    assert once.stripe is not None and again.stripe is not None
    assert again.stripe.app_binding == once.stripe.app_binding
    data = again.model_dump(mode="json")
    data["stripe"]["app_binding"] = "app_not-a-binding"
    with pytest.raises(ValidationError, match="app_binding"):
        AppSpec.model_validate(data)


def test_stripe_addon_fails_closed_on_non_records_generation() -> None:
    from disco.core.appkit.generator import default_lead_gen_app_spec, generate

    app = apply_stripe_spec(
        default_lead_gen_app_spec("Wrong base", _recipe()),
        _spec(),
    )
    with pytest.raises(ValueError, match="auth-capable records"):
        generate(app, _design())


def test_filled_tree_has_worker_only_capabilities_and_no_secret_values() -> None:
    _, tree = _filled_tree()
    worker = tree["worker/index.ts"]
    assert "STRIPE_WEBHOOK_SECRET?: string" in worker
    assert "STRIPE_APP_BINDING_SECRET?: string" in worker
    assert "STRIPE_RUNTIME_READY?: string" in worker
    assert 'import { svc } from "./disco-client"' in worker
    assert 'svc(env, "payments.checkout"' in worker
    assert 'svc(env, "payments.ready"' in worker
    assert "DISCO_SVC_BUS" not in tree["src/generated/content.ts"]
    assert "STRIPE_WEBHOOK_SECRET" not in "\n".join(
        value for path, value in tree.items() if path.startswith("src/")
    )
    for path, contents in tree.items():
        for marker in ("sk_live", "sk_test", "whsec_"):
            assert marker not in contents, f"secret-shaped marker in {path}"


def test_webhook_verifies_raw_bytes_and_freshness_before_json_trust() -> None:
    _, tree = _filled_tree()
    worker = tree["worker/index.ts"]
    route = worker.index("async function stripeWebhook")
    verify = worker.index("stripeSignatureValid", route)
    runtime_ready = worker.index("await stripeHostReady(env)", route)
    parse = worker.index("parseStripeEnvelope(body)", route)
    assert verify < runtime_ready < parse
    assert 'request.headers.get("Stripe-Signature")' in worker
    assert "STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300" in worker
    assert "MAX_STRIPE_V1_SIGNATURES = 8" in worker
    assert 'crypto.subtle.verify("HMAC"' in worker
    assert 'new TextDecoder("utf-8", { fatal: true })' in worker
    assert "await request.json()" not in worker[route:parse]
    assert "MAX_STRIPE_WEBHOOK_BYTES = 256 * 1024" in worker
    assert 'return stripeJson({ error: "invalid signature" }, 400)' in worker
    assert 'return stripeJson({ error: "payments unavailable" }, 503)' in worker
    assert '"Cache-Control": "no-store"' in worker
    webhook_route = worker.index('rawPath === "/api/stripe/webhook"')
    csrf_gate = worker.index('return json({ error: "bad origin" }, 403)')
    checkout_route = worker.index('rawPath === "/api/stripe/checkout"')
    assert webhook_route < csrf_gate < checkout_route
    assert "new URL(origin).origin === url.origin" in worker


def test_replay_and_grant_are_one_atomic_first_primary_batch() -> None:
    _, tree = _filled_tree()
    worker = tree["worker/index.ts"]
    apply_at = worker.index("async function applyStripeEvent")
    batch_at = worker.index("await db.batch(statements)", apply_at)
    segment = worker[apply_at:batch_at]
    assert 'env.DB.withSession("first-primary")' in segment
    assert (
        segment.index("INSERT INTO stripe_events")
        < segment.index("INSERT INTO stripe_fulfillments")
        < segment.index("INSERT INTO user_role_grants")
    )
    assert "INSERT OR IGNORE" not in worker
    assert "ON CONFLICT(event_id)" not in worker
    catch = worker[batch_at : worker.index("async function stripeWebhook", batch_at)]
    assert "stripeEventAlreadyRecorded(db, event.id)" in catch
    assert 'return stripeJson({ error: "fulfillment failed" }, 500)' in catch


def test_role_grants_are_source_aware_and_revocation_is_stripe_scoped() -> None:
    _, tree = _filled_tree()
    schema = tree["schema.sql"]
    worker = tree["worker/index.ts"]
    assert 'UNIQUE("user_id", "role", "source", "source_id")' in schema
    assert "SELECT role FROM user_role_grants WHERE user_id = ? AND active = 1" in worker
    assert "session.roles.includes(role)" in worker
    assert "WHERE source = 'stripe' AND source_id = ? AND role = ?" in worker
    assert 'event.type === "customer.subscription.deleted"' in worker
    assert 'event.type === "checkout.session.expired"' in worker
    assert 'event.type === "checkout.session.async_payment_succeeded"' in worker
    assert 'event.type === "checkout.session.async_payment_failed"' in worker
    # The emitted migration is executable SQLite, not just plausible text.
    db = sqlite3.connect(":memory:")
    db.executescript(schema)
    assert {
        "stripe_events",
        "stripe_fulfillments",
        "user_role_grants",
    } <= {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_success_ui_depends_on_server_observed_entitlement_not_redirect() -> None:
    _, tree = _filled_tree()
    component = next(
        value
        for path, value in tree.items()
        if path.startswith("src/components/") and 'data-appkit-section="stripe_pricing"' in value
    )
    assert 'fetch("/api/stripe/status"' in component
    assert "status?.entitled && status.message" in component
    assert "location.search" not in component
    assert "URLSearchParams" not in component
    worker = tree["worker/index.ts"]
    assert "message: entitled ? STRIPE_SUCCESS_MESSAGE : null" in worker
    assert 'success_path: "/"' in worker
    assert 'cancel_path: "/"' in worker


def test_anonymous_pricing_card_is_a_disabled_sign_in_state() -> None:
    _, tree = _filled_tree()
    component = next(
        value
        for path, value in tree.items()
        if path.startswith("src/components/") and "/api/stripe/checkout" in value
    )
    assert "authenticated: boolean" in component
    assert '"Sign in to choose plan"' in component
    assert "status?.ready === true && status.authenticated" in component
    assert "disabled={!ready || busy}" in component
    worker = tree["worker/index.ts"]
    anonymous = worker[worker.index("async function stripeStatus") :]
    assert "ready: false, authenticated: false" in anonymous


def test_runtime_readiness_is_bound_to_current_host_secrets() -> None:
    _, tree = _filled_tree()
    worker = tree["worker/index.ts"]
    proof = worker[worker.index("async function stripeRuntimeProof") :]
    assert "stripe-runtime\\0${STRIPE_APP_BINDING}\\0${STRIPE_PLAN_SELECTOR}" in proof
    assert '{ name: "HMAC", hash: "SHA-256" }' in proof
    assert "binding_proof: await stripeRuntimeProof(binding)" in proof
    assert "webhook_proof: await stripeRuntimeProof(webhook)" in proof
    ready = worker[worker.index("async function stripeHostReady") :]
    assert 'svc(env, "payments.ready"' in ready
    assert "...proofs" in ready
    checkout = worker[worker.index("async function stripeCheckout") :]
    assert "if (!stripeRuntimeReady(env))" in checkout
    assert 'svc(env, "payments.checkout"' in checkout
    assert "...proofs" in checkout


def test_runtime_probe_is_admin_only_and_webhooks_pause_during_rotation() -> None:
    _, tree = _filled_tree()
    worker = tree["worker/index.ts"]
    probe = worker[
        worker.index("async function stripeRuntimeProbe") : worker.index(
            "interface RouteHandlers"
        )
    ]
    assert "await isAdminAuthorized(request, env)" in probe
    assert "ready: await stripeHostReady(env)" in probe
    route = worker.index('rawPath === "/api/stripe/runtime-probe"')
    csrf_gate = worker.index('return json({ error: "bad origin" }, 403)')
    assert csrf_gate < route
    webhook = worker[
        worker.index("async function stripeWebhook") : worker.index(
            "async function stripeHostReady"
        )
    ]
    assert 'env.STRIPE_RUNTIME_READY !== "1"' in webhook
    assert 'return stripeJson({ error: "payments unavailable" }, 503)' in webhook


@pytest.mark.parametrize(
    "entity_id",
    ["user_role_grants", "stripe_events", "stripe_fulfillments"],
)
def test_stripe_internal_table_names_are_reserved(entity_id: str) -> None:
    from disco.core.appkit.generator import generate
    from disco.core.appkit.spec import Entity, EntityField

    app = apply_stripe_spec(_records_app(), _spec())
    conflicting = Entity(
        id=entity_id,
        name="Collision",
        fields=(EntityField(name="value", type="str"),),
    )
    with pytest.raises(ValueError, match="reserved for auth infrastructure"):
        generate(app.model_copy(update={"entities": (*app.entities, conflicting)}), _design())


def test_checkout_request_cannot_choose_price_or_amount() -> None:
    _, tree = _filled_tree()
    worker = tree["worker/index.ts"]
    start = worker.index("async function stripeCheckout")
    checkout = worker[start : worker.index("interface RouteHandlers", start)]
    assert "request.json()" not in checkout
    assert "STRIPE_PLAN_SELECTOR" in checkout
    assert "plan_selector: STRIPE_PLAN_SELECTOR" in checkout
    assert "user_id: session.userId" in checkout
    assert "app_origin" not in checkout
    assert "Object.keys(body).length !== 0" in checkout
    assert "const BASE_ROLES" in worker
    assert "!BASE_ROLES.includes(role)" in worker
    assert 'parsed.port !== ""' in checkout
    assert "stripeBusBindingsValid(env)" in worker
    assert "Number(part) <= 255" in worker
    assert "price" not in checkout.lower()
    assert "amount" not in checkout.lower()
    component = next(
        value
        for path, value in tree.items()
        if path.startswith("src/components/") and "/api/stripe/checkout" in value
    )
    assert 'body: "{}"' in component


def test_filled_worker_and_component_parse_as_real_typescript(tmp_path: Path) -> None:
    esbuild = Path("/var/home/dylan/projects/disclaude/frontend/node_modules/.bin/esbuild")
    if not esbuild.exists():
        pytest.skip("repository frontend esbuild is unavailable")
    _, tree = _filled_tree()
    for path, contents in tree.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    subprocess.run(
        [
            str(esbuild),
            str(tmp_path / "worker/index.ts"),
            "--bundle",
            "--platform=neutral",
            "--format=esm",
            "--external:drizzle-orm",
            "--external:drizzle-orm/*",
            f"--outfile={tmp_path / 'worker.js'}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stripe_component = next(
        path
        for path, contents in tree.items()
        if path.startswith("src/components/") and "/api/stripe/checkout" in contents
    )
    subprocess.run(
        [
            str(esbuild),
            str(tmp_path / stripe_component),
            "--bundle",
            "--platform=browser",
            "--format=esm",
            "--external:react",
            f"--outfile={tmp_path / 'component.js'}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_webhook(
    port: int,
    body: bytes,
    signature: str,
    *,
    origin: str | None = None,
) -> tuple[int, str]:
    headers = {
        "Content-Type": "application/json",
        "Stripe-Signature": signature,
    }
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/stripe/webhook",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _post_checkout(port: int, origin: str | None) -> int:
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/stripe/checkout",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def _signed_header(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _stripe_metadata(app: AppSpec, binding_secret: str, user_id: int) -> dict[str, str]:
    assert app.stripe is not None
    message = (f"{app.stripe.app_binding}\0{app.stripe.plan_selector}\0{user_id}").encode()
    return {
        "disco_app_binding": app.stripe.app_binding,
        "disco_plan_selector": app.stripe.plan_selector,
        "disco_correlation": hmac.new(binding_secret.encode(), message, hashlib.sha256).hexdigest(),
    }


@pytest.mark.integration
def test_stripe_webhook_real_workerd_requires_current_host_secret() -> None:
    """A signature alone cannot produce side effects during a stale host configuration.

    The complete grant/revoke/replay lifecycle is proven by the mandatory
    host-owned live verifier, which runs the generated bundle with its actual
    authenticated host bus. This lightweight workerd test keeps the isolated
    stale-host regression executable in the core package.
    """
    from _workerd_harness import WorkerdApp, wrangler_available
    from disco.core.appkit.generator import generate
    from disco.core.appkit.records_primitive import default_records_auth_app_spec

    if not wrangler_available():
        pytest.skip("wrangler/workerd is required for the live Stripe proof")
    app = apply_stripe_spec(
        default_records_auth_app_spec("Stripe live proof", _recipe()),
        _spec(),
    )
    tree = generate(app, _design())
    secret = "integration_webhook_secret_value"
    binding_secret = "integration-binding-secret-at-least-32-bytes"
    port = _free_port()
    with WorkerdApp(tree, admin_token="integration-admin-token") as worker:
        # Dummy integration credentials live only in the harness's temporary,
        # deleted runtime directory; the generated tree itself remains clean.
        dev_vars = worker.app_dir / ".dev.vars"
        dev_vars.write_text(
            dev_vars.read_text()
            + f"STRIPE_WEBHOOK_SECRET={secret}\n"
            + f"STRIPE_APP_BINDING_SECRET={binding_secret}\n"
            + "STRIPE_RUNTIME_READY=1\n"
            + "DISCO_SVC_BUS=https://bus.example\n"
            + "DISCO_SVC_TOKEN=malformed\n",
            encoding="utf-8",
        )
        worker.boot(port=port)
        # Exact scheme + host + port equality protects the browser checkout
        # route.  Missing Origin follows the existing non-browser policy.
        assert _post_checkout(port, f"https://127.0.0.1:{port}") == 403
        assert _post_checkout(port, "http://127.0.0.1:1") == 403
        assert _post_checkout(port, f"http://127.0.0.1:{port}") == 503
        assert _post_checkout(port, None) == 503
        status, anonymous = worker.get("/api/stripe/status")
        assert status == 200
        assert json.loads(anonymous) == {
            "ready": False,
            "authenticated": False,
            "entitled": False,
            "message": None,
        }
        status, _ = worker.post_json(
            "/api/register",
            {
                "email": "payer@example.com",
                "password": "correct horse battery",
                "role": "member",
            },
            token="integration-admin-token",
        )
        assert status == 201
        status, _ = worker.post_json(
            "/api/login",
            {"email": "payer@example.com", "password": "correct horse battery"},
        )
        assert status == 200
        status, before = worker.get("/api/stripe/status")
        assert status == 200
        assert json.loads(before) == {
            "ready": False,
            "authenticated": True,
            "entitled": False,
            "message": None,
        }

        metadata = _stripe_metadata(app, binding_secret, 1)
        unpaid = json.dumps(
            {
                "id": "evt_liveunpaid",
                "created": 100,
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_live_session",
                        "subscription": "sub_live_subscription",
                        "client_reference_id": "1",
                        "payment_status": "unpaid",
                        "metadata": metadata,
                    }
                },
            },
            separators=(",", ":"),
        ).encode()
        now = int(time.time())
        # A signature alone is insufficient during an unavailable or stale host
        # configuration: no fulfillment may happen until the Worker proves its
        # current injected secret generation through payments.ready.
        assert _post_webhook(port, unpaid, _signed_header(secret, unpaid, now))[0] == 503
        status, unchanged = worker.get("/api/stripe/status")
        assert status == 200 and json.loads(unchanged)["entitled"] is False
