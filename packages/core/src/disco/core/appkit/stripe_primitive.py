"""The AppKit ``stripe`` primitive — WO-F4.1's non-secret app contract.

⚠⚠ READ THIS BEFORE TOUCHING ANYTHING IN THIS MODULE ⚠⚠

This module owns the model-fillable, non-secret surface.  A records app carrying
the folded ``StripeMeta`` is lowered by ``stripe_worker`` into Disco-owned
checkout/webhook/entitlement code; operator prices and credentials never enter
these models.

* ``StripeSpec`` — the model-fillable declarative spec: DISPLAY strings (plan
  name, price display, features), the entitlement flag, the success message.
  Real Stripe price IDs and API keys are NOT spec fields ON PURPOSE — they are
  operator-configured HOST-SIDE later (see the security spec doc).
* ``apply_stripe_spec`` — folds the spec into an AppSpec: one ``pricing``
  section (catalog variant ``pricing.single-plan-emphasis``) plus the
  entitlement flag recorded in ``AppSpec.roles`` (the existing RBAC surface —
  the records vertical already lowers roles into per-entity read/write gates,
  so a role IS the clean existing place for an entitlement flag).
* a standalone Stripe primitive remains an explicit disabled/pending page.  The
  only supported live composition is an auth-capable records app; every other
  composition fails closed in the top-level generator.

The host-side ``payments.checkout`` handler and secret custody remain separate
WO-F4.1 slices.  Verification is deliberately two-stage: ``stripe_verify`` is a
pure, secret-free provenance/trusted-output check, while ``live_verify_id``
requires a host-injected adversarial runner (forged signature, replay, secret
absence, restricted key, and price injection).  Neither stage can ship alone.

THE FAIL-CLOSED GATE IS INTENTIONAL. Do not replace the host live callback with
a passing stub, flip the tier, or derive membership only from the deletable
provenance file. AppSpec.stripe itself makes both verification stages mandatory.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .primitives import (
    HostService,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
)
from .recipes import SiteRecipe
from .spec import (
    AppSpec,
    DesignSpec,
    Page,
    Section,
    SectionContent,
    StripeMeta,
    serialize_app_spec,
)

STRIPE_PRIMITIVE_ID = "stripe"

# The one section this primitive contributes, and the catalog variant it renders
# with: a single confident plan card with an itemized what's-included list
# (`pricing.single-plan-emphasis` — see section_catalog.py). Reusing the existing
# `pricing` SectionKind means the app's own base primitive lowers the folded
# section through the SHARED house emitter — no stripe-specific generator code.
STRIPE_PRICING_SECTION_ID = "stripe_pricing"
STRIPE_PRICING_VARIANT_ID = "pricing.single-plan-emphasis"

# The visible pending-state vocabulary. Both emission paths (the folded section's
# content and the standalone static page) use these EXACT strings so a viewer —
# and a test — can always tell checkout is not live.
PAYMENTS_PENDING_LABEL = "Payments setup pending"
PAYMENTS_PENDING_HELPER = "Checkout activates after payment setup."

# Entitlement flags land in `AppSpec.roles`, so they must satisfy the same
# snake_case identifier rule the spec enforces on roles (`_IDENT_RE` in spec.py).
_FLAG_PATTERN = r"^[a-z][a-z0-9_]*$"

_MAX_FEATURES = 8
_MAX_FEATURE_LEN = 120

_STRIPE_PROVENANCE_RELPATH = ".disco/primitives/stripe.json"
_STRIPE_LIVE_VERIFY_ID = "stripe.security.v1"
_SECRET_VALUE_RE = re.compile(r"(?i)(?:sk_(?:live|test)|rk_(?:live|test)|whsec_)[A-Za-z0-9_-]{4,}")

_FlagStr = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=_FLAG_PATTERN)]


def _stripe_app_binding(app: AppSpec) -> str:
    """Stable, public app identity used to bind Stripe webhook metadata.

    It deliberately excludes mutable content and contains no credential.  The
    per-deployment correlation secret supplies authenticity at runtime.
    """
    canonical = json.dumps(
        {"app_kind": app.app_kind, "name": app.name},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"app_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


class StripeSpec(BaseModel):
    """NON-SECRET pricing-card spec. Real Stripe price IDs / API keys / webhook
    secrets are NOT spec fields — operator-configured host-side later."""

    # This docstring doubles as the JSON-schema `description` carried in
    # app_add_primitive's self-recovering refusals (truncated at 600 chars), so it
    # stays SHORT enough that the field names survive the truncation. The full
    # seam story lives in the module docstring + the field descriptions below.
    # `extra="forbid"`: an unknown key (e.g. a smuggled `price_id`) is a precise
    # refusal, not a silent drop.
    model_config = ConfigDict(extra="forbid")

    plan_name: str = Field(
        min_length=1,
        max_length=80,
        description="The plan's display name (1-80 chars), e.g. 'Pro'. Rendered as "
        "the pricing card heading.",
    )
    price_display: str = Field(
        min_length=1,
        max_length=40,
        description="DISPLAY-ONLY price string (1-40 chars), e.g. '$9/mo'. This is "
        "card copy, NOT a Stripe price object — the real price ID is operator-"
        "configured host-side later, never in this spec.",
    )
    entitlement_flag: _FlagStr = Field(
        description="The RBAC flag (snake_case slug, <=64 chars) a successful "
        "payment would grant, recorded in AppSpec.roles so entity read/write "
        "rules can gate on it. The GRANT itself is deferred security fill — "
        "nothing grants this flag until verified fulfillment exists.",
    )
    success_message: str = Field(
        min_length=1,
        max_length=200,
        description="What a member sees after a VERIFIED successful payment "
        "(1-200 chars). Surfaces only as described-future copy in the pending "
        "card until the security fill lands.",
    )
    features: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=_MAX_FEATURES,
        description=f"Up to {_MAX_FEATURES} short feature bullets "
        f"(each 1-{_MAX_FEATURE_LEN} chars) for the pricing card.",
    )

    @field_validator("plan_name", "price_display", "success_message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank/whitespace-only")
        return value

    @field_validator("features")
    @classmethod
    def _features_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item.strip():
                raise ValueError("a feature bullet must not be blank")
            if len(item) > _MAX_FEATURE_LEN:
                raise ValueError(
                    f"feature bullet too long ({len(item)} > {_MAX_FEATURE_LEN} chars): "
                    f"{item[:40]!r}…"
                )
        return value


def _pending_body(spec: StripeSpec) -> str:
    """The pricing card's body copy: states the pending reality FIRST, then
    describes (future tense, honestly) what the deferred fill will do with the
    entitlement flag and success message. Every spec field is thereby visible in
    the folded AppSpec, so changing ANY field re-applies as a changed app."""
    return (
        f"{PAYMENTS_PENDING_LABEL} — {PAYMENTS_PENDING_HELPER} "
        f"Once live, a verified payment grants the '{spec.entitlement_flag}' "
        f'entitlement and shows: "{spec.success_message}"'
    )


def _validate_stripe_apply(app: AppSpec, spec: StripeSpec) -> None:
    """The two apply-time invariants: a records app needs a pre-existing
    non-payment role, and the entitlement flag is immutable once applied."""
    if app.app_kind == "records" and not app.roles:
        raise ValueError("Stripe requires a records app with an existing non-payment role")
    if app.stripe is not None and app.stripe.entitlement_flag != spec.entitlement_flag:
        raise ValueError("Stripe entitlement_flag cannot change after initial application")


def _stripe_pricing_section_payload(spec: StripeSpec) -> dict[str, object]:
    """The folded ``stripe_pricing`` section dict: plan copy plus pending body."""
    return {
        "id": STRIPE_PRICING_SECTION_ID,
        "kind": "pricing",
        "variant_id": STRIPE_PRICING_VARIANT_ID,
        "content": {
            "heading": spec.plan_name,
            "subheading": spec.price_display,
            "body": _pending_body(spec),
            "items": list(spec.features),
        },
    }


def _merge_stripe_pricing_pages(pages: list[Any], section: dict[str, object]) -> list[Any]:
    """Replace the ``stripe_pricing`` section in place on the first page (or
    append it there), creating a single-page app when there are no pages yet."""
    if not pages:
        return [{"id": "home", "route": "/", "title": "Home", "sections": [section]}]
    first = dict(pages[0])
    sections = list(first.get("sections") or [])
    for i, existing in enumerate(sections):
        if isinstance(existing, dict) and existing.get("id") == STRIPE_PRICING_SECTION_ID:
            sections[i] = section
            break
    else:
        sections.append(section)
    first["sections"] = sections
    pages[0] = first
    return pages


def _merge_stripe_role(app: AppSpec, roles: list[Any], spec: StripeSpec) -> list[Any]:
    """Append the entitlement flag as a role, refusing a name collision with an
    existing role on first application."""
    if app.stripe is None and spec.entitlement_flag in roles:
        raise ValueError("Stripe entitlement_flag must be a new, payment-managed role")
    if spec.entitlement_flag not in roles:
        roles.append(spec.entitlement_flag)
    return roles


def _stripe_metadata_dict(app: AppSpec, spec: StripeSpec) -> dict[str, object]:
    """The strict ``AppSpec.stripe`` metadata folded from this apply."""
    return {
        "app_binding": (
            app.stripe.app_binding if app.stripe is not None else _stripe_app_binding(app)
        ),
        # The selector is deliberately NOT a Stripe price id.  The host maps
        # this stable, validated identifier to its operator-owned price config.
        "plan_selector": spec.entitlement_flag,
        "entitlement_flag": spec.entitlement_flag,
        "success_message": spec.success_message,
    }


def apply_stripe_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated StripeSpec into the AppSpec (house dance: dump → mutate →
    re-validate):

    * ONE `pricing` section (id ``stripe_pricing``, variant
      ``pricing.single-plan-emphasis``) on the first page — replaced in place on
      re-apply, never duplicated; a page is created if the app has none. The
      section content carries plan_name/price_display/features plus the
    pending-state body. No ``cta_label`` is model-controlled: the records
      emitter replaces this section with the Disco-owned runtime-gated CTA.
    * the entitlement flag appended to ``AppSpec.roles`` plus strict
      ``AppSpec.stripe`` metadata consumed by the generated Worker."""
    if not isinstance(spec, StripeSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {STRIPE_PRIMITIVE_ID!r} needs a StripeSpec")
    _validate_stripe_apply(app, spec)
    section = _stripe_pricing_section_payload(spec)
    data = app.model_dump(mode="json")
    data["pages"] = _merge_stripe_pricing_pages(list(data.get("pages") or []), section)
    data["roles"] = _merge_stripe_role(app, list(data.get("roles") or []), spec)
    data["stripe"] = _stripe_metadata_dict(app, spec)
    return AppSpec.model_validate(data)


def default_stripe_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The minimal standalone stripe AppSpec: one page, one placeholder pricing
    section already in the pending state.

    This primitive is MEANT TO BE ADDED to a real app (records/lead_gen) via
    `app_add_primitive`; the standalone shape exists to satisfy the
    `PrimitiveDefinition` contract and as the smallest visible proof of the
    pending-state UI. `recipe` is deliberately unused — a static pending card
    has no design-preference surface."""
    del recipe
    title = name.strip() or "Pricing"
    return AppSpec(
        schema_version=1,
        app_kind=STRIPE_PRIMITIVE_ID,
        name=title,
        pages=(
            Page(
                id="home",
                route="/",
                title="Home",
                sections=(
                    Section(
                        id=STRIPE_PRICING_SECTION_ID,
                        kind="pricing",
                        variant_id=STRIPE_PRICING_VARIANT_ID,
                        content=SectionContent(
                            heading="Your plan",
                            subheading="Price to be configured",
                            body=f"{PAYMENTS_PENDING_LABEL} — {PAYMENTS_PENDING_HELPER}",
                        ),
                    ),
                ),
            ),
        ),
    )


def prepare_stripe_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the standalone stripe shape needs no normalization."""
    return app


def _stripe_section(app: AppSpec) -> Section | None:
    """The section the standalone page renders: the canonical ``stripe_pricing``
    section if present, else the first `pricing` section, else None."""
    fallback: Section | None = None
    for page in app.pages:
        for section in page.sections:
            if section.id == STRIPE_PRICING_SECTION_ID:
                return section
            if fallback is None and section.kind == "pricing":
                fallback = section
    return fallback


# The ONLY payment-adjacent thing the emitted tree may say about secrets: the env
# NAMES the deferred host-side fill will use, in a comment, for the operator's
# orientation. No value, no template, no route. (Task rule: "NO secret template
# beyond documenting the env NAME in a comment".)
_ENV_NAME_DOC_COMMENT = (
    "  <!--\n"
    "    Payments are NOT wired in this tree ON PURPOSE (fail-closed seam; see\n"
    "    docs/wo-f41-stripe-security-spec.md). The deferred host-side fill keeps\n"
    "    the operator-configured secrets HOST-SIDE ONLY - env NAMES for\n"
    "    reference: STRIPE_RESTRICTED_KEY and STRIPE_WEBHOOK_SECRET. No secret\n"
    "    value, no checkout route, no webhook route belongs in this tree.\n"
    "  -->\n"
)


def generate_stripe(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a standalone stripe AppSpec into a single static page: the pricing
    card in its explicit "Payments setup pending" state.

    The card renders the folded plan/price/features and a DISABLED button —
    `disabled` + `aria-disabled` + the pending label — with helper text under
    it, so a viewer can never mistake it for live checkout (no false
    affordance). There is deliberately NO form, NO fetch, NO /api/checkout —
    those are the deferred security fill. `design` is deliberately unused —
    see `default_stripe_app_spec`."""
    del design
    title = html.escape(app.name)
    section = _stripe_section(app)
    content = section.content if section is not None else None
    heading = html.escape(content.heading) if content and content.heading else "Your plan"
    price = (
        html.escape(content.subheading)
        if content and content.subheading
        else "Price to be configured"
    )
    note = (
        html.escape(content.body)
        if content and content.body
        else html.escape(f"{PAYMENTS_PENDING_LABEL} — {PAYMENTS_PENDING_HELPER}")
    )
    items = content.items if content else ()
    features_html = "".join(f"        <li>{html.escape(item)}</li>\n" for item in items)
    features_block = f"      <ul>\n{features_html}      </ul>\n" if features_html else ""
    return {
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{title}</title>\n"
            "  </head>\n"
            "  <body>\n" + _ENV_NAME_DOC_COMMENT + f"    <h1>{title}</h1>\n"
            '    <section class="pricing-card" data-payments-state="setup-pending">\n'
            f"      <h2>{heading}</h2>\n"
            f'      <p class="price">{price}</p>\n'
            + features_block
            + f'      <p class="pending-note">{note}</p>\n'
            '      <button type="button" disabled aria-disabled="true">\n'
            f"        {html.escape(PAYMENTS_PENDING_LABEL)}\n"
            "      </button>\n"
            f'      <p class="payments-helper">{html.escape(PAYMENTS_PENDING_HELPER)}</p>\n'
            "    </section>\n"
            "  </body>\n"
            "</html>\n"
        )
    }


def _stripe_result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    failed = sum(not check.passed for check in checks)
    return PrimitiveVerifyResult(
        ok=failed == 0,
        detail=f"{len(checks) - failed} passed / {failed} failed",
        checks=tuple(checks),
    )


def _stripe_metadata_check(app: AppSpec | None) -> VerifyCheck:
    if app is None or app.stripe is None:
        return VerifyCheck(
            "stripe_metadata_binding",
            False,
            "Stripe provenance requires strict AppSpec.stripe metadata (fail-closed).",
        )
    meta = app.stripe
    stripe_sections = [
        section
        for page in app.pages
        for section in page.sections
        if section.id == STRIPE_PRICING_SECTION_ID
    ]
    reasons: list[str] = []
    if app.app_kind != "records":
        reasons.append("app_kind is not records")
    if meta.app_binding != _stripe_app_binding(app):
        reasons.append("app_binding does not match the canonical app identity")
    if meta.plan_selector != meta.entitlement_flag:
        reasons.append("plan_selector does not match entitlement_flag")
    if app.roles.count(meta.entitlement_flag) != 1:
        reasons.append("entitlement_flag is not declared exactly once in AppSpec.roles")
    if not any(role != meta.entitlement_flag for role in app.roles):
        reasons.append("records app has no non-payment base role")
    if len(stripe_sections) != 1:
        reasons.append("AppSpec does not contain exactly one stripe_pricing section")
    return VerifyCheck(
        "stripe_metadata_binding",
        not reasons,
        (
            "StripeMeta is bound to the records app, canonical app identity, plan selector, "
            "entitlement role, and unique pricing section."
            if not reasons
            else "; ".join(reasons)
        ),
    )


def _parse_stripe_provenance_record(tree: Mapping[str, str]) -> dict[str, Any] | VerifyCheck:
    """Load, JSON-decode, and shape-check the on-disk provenance record."""
    raw = tree.get(_STRIPE_PROVENANCE_RELPATH)
    if raw is None:
        return VerifyCheck(
            "stripe_provenance_binding",
            False,
            f"missing {_STRIPE_PROVENANCE_RELPATH}; AppSpec.stripe cannot ship "
            "without exact provenance.",
        )
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return VerifyCheck(
            "stripe_provenance_binding", False, f"invalid Stripe provenance JSON: {exc}"
        )
    if not isinstance(record, dict) or set(record) != {
        "primitive_id",
        "tier",
        "applied_at",
        "spec",
    }:
        return VerifyCheck(
            "stripe_provenance_binding",
            False,
            "Stripe provenance must contain exactly primitive_id, tier, applied_at, and spec.",
        )
    if record.get("primitive_id") != STRIPE_PRIMITIVE_ID or record.get("tier") != "template_only":
        return VerifyCheck(
            "stripe_provenance_binding",
            False,
            "Stripe provenance primitive_id/tier does not match the registered security primitive.",
        )
    if not isinstance(record.get("applied_at"), str) or not record["applied_at"].strip():
        return VerifyCheck(
            "stripe_provenance_binding", False, "Stripe provenance applied_at is empty."
        )
    return record


def _stripe_provenance_spec(record: Mapping[str, Any]) -> StripeSpec | VerifyCheck:
    """Validate the embedded StripeSpec; the broad except is deliberate — the
    validation detail is safe declarative metadata, never a secret."""
    try:
        return StripeSpec.model_validate(record.get("spec"))
    except Exception as exc:  # noqa: BLE001 - validation detail is safe declarative metadata
        return VerifyCheck(
            "stripe_provenance_binding", False, f"invalid Stripe provenance spec: {exc}"
        )


def _stripe_provenance_pricing_section(app: AppSpec) -> Section | None:
    """The exact ``stripe_pricing`` section id — no kind-based fallback (unlike
    ``_stripe_section``, which is used for the standalone-page render path)."""
    return next(
        (
            section
            for page in app.pages
            for section in page.sections
            if section.id == STRIPE_PRICING_SECTION_ID
        ),
        None,
    )


def _stripe_provenance_reasons(
    meta: StripeMeta, spec: StripeSpec, section: Section | None
) -> list[str]:
    """Compare the validated provenance spec against the live AppSpec.stripe
    metadata, entitlement role, and folded pricing section content."""
    expected_content = {
        "heading": spec.plan_name,
        "subheading": spec.price_display,
        "body": _pending_body(spec),
        "cta_label": None,
        "items": tuple(spec.features),
    }
    reasons: list[str] = []
    if meta.plan_selector != spec.entitlement_flag:
        reasons.append("plan_selector differs from provenance entitlement_flag")
    if meta.entitlement_flag != spec.entitlement_flag:
        reasons.append("StripeMeta entitlement_flag differs from provenance")
    if meta.success_message != spec.success_message:
        reasons.append("StripeMeta success_message differs from provenance")
    if section is None:
        reasons.append("stripe_pricing section is missing")
    else:
        if section.kind != "pricing" or section.variant_id != STRIPE_PRICING_VARIANT_ID:
            reasons.append("stripe_pricing kind/variant differs from the trusted primitive")
        actual_content = (
            section.content.model_dump(mode="python") if section.content is not None else None
        )
        if actual_content != expected_content:
            reasons.append("stripe_pricing content differs from the validated provenance spec")
    return reasons


def _stripe_provenance_check(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    record_or_failure = _parse_stripe_provenance_record(tree)
    if isinstance(record_or_failure, VerifyCheck):
        return record_or_failure
    spec_or_failure = _stripe_provenance_spec(record_or_failure)
    if isinstance(spec_or_failure, VerifyCheck):
        return spec_or_failure
    spec = spec_or_failure
    if app is None or app.stripe is None:
        return VerifyCheck(
            "stripe_provenance_binding",
            False,
            "Stripe provenance exists without AppSpec.stripe metadata (fail-closed).",
        )
    meta = app.stripe
    section = _stripe_provenance_pricing_section(app)
    reasons = _stripe_provenance_reasons(meta, spec, section)
    return VerifyCheck(
        "stripe_provenance_binding",
        not reasons,
        (
            "Validated StripeSpec provenance exactly matches StripeMeta, entitlement "
            "role, and pricing section."
            if not reasons
            else "; ".join(reasons)
        ),
    )


def _stripe_trusted_tree_check(
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: Mapping[str, str],
) -> VerifyCheck:
    if app is None or app.stripe is None or design is None:
        return VerifyCheck(
            "stripe_trusted_tree",
            False,
            "cannot reconstruct trusted Stripe output without valid AppSpec.stripe and DesignSpec.",
        )
    # Lazy import avoids the generator -> stripe_primitive registration cycle.
    from .generator import generate

    try:
        expected = generate(app, design)
    except Exception as exc:  # noqa: BLE001 - projection errors become a closed gate
        return VerifyCheck("stripe_trusted_tree", False, f"trusted Stripe projection failed: {exc}")
    stripe_components = [
        path
        for path, contents in expected.items()
        if path.startswith("src/components/") and 'data-appkit-section="stripe_pricing"' in contents
    ]
    if len(stripe_components) != 1:
        return VerifyCheck(
            "stripe_trusted_tree",
            False,
            "trusted projection did not emit exactly one Stripe pricing component.",
        )
    sensitive_paths = (
        "worker/index.ts",
        "worker/disco-client.ts",
        "wrangler.toml",
        "package.json",
        "package-lock.json",
        "schema.sql",
        "migrations/0001_init.sql",
        "src/db/schema.ts",
        stripe_components[0],
    )
    mismatched = [path for path in sensitive_paths if tree.get(path) != expected.get(path)]
    if tree.get(".disco/appspec.json") != serialize_app_spec(app):
        mismatched.append(".disco/appspec.json")
    return VerifyCheck(
        "stripe_trusted_tree",
        not mismatched,
        (
            "Stripe Worker, host client, Wrangler entrypoint, dependency lock, SQL schemas, "
            "pricing component, and canonical AppSpec are byte-identical to Disco's trusted "
            "projection."
            if not mismatched
            else "security-sensitive Stripe file mismatch: " + ", ".join(mismatched)
        ),
    )


def _stripe_secret_absence_check(tree: Mapping[str, str]) -> VerifyCheck:
    hits = sorted(path for path, contents in tree.items() if _SECRET_VALUE_RE.search(contents))
    return VerifyCheck(
        "stripe_static_secret_absence",
        not hits,
        (
            "No Stripe secret-value patterns occur in the trusted emitted tree."
            if not hits
            else "Stripe secret-value pattern found in: " + ", ".join(hits)
        ),
    )


def stripe_verify(
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: Mapping[str, str],
) -> PrimitiveVerifyResult:
    """Secret-free half of the mandatory Stripe security gate.

    The tools layer separately dispatches ``_STRIPE_LIVE_VERIFY_ID`` through a
    host-owned ToolContext callback.  Passing this deterministic half alone is
    intentionally insufficient to ship.
    """
    return _stripe_result(
        [
            _stripe_metadata_check(app),
            _stripe_provenance_check(app, tree),
            _stripe_trusted_tree_check(app, design, tree),
            _stripe_secret_absence_check(tree),
        ]
    )


register_primitive(
    PrimitiveDefinition(
        id=STRIPE_PRIMITIVE_ID,
        default_app_spec=default_stripe_app_spec,
        prepare_app_spec=prepare_stripe_app_spec,
        generate=generate_stripe,
        # template_only: the model's role is spec-only; every emitted byte is
        # Disco-owned. Webhook delivery is inbound and therefore is NOT a host
        # capability; only outbound Checkout Session creation is declared.
        tier="template_only",
        host_contract=(
            HostService("payments.checkout"),
            HostService("payments.ready"),
        ),
        spec_schema=StripeSpec,
        verify=stripe_verify,
        apply_spec=apply_stripe_spec,
        live_verify_id=_STRIPE_LIVE_VERIFY_ID,
        live_verify_checks=(
            "forged_signature_rejected",
            "replay_deduped",
            "secret_absence",
            "restricted_key_only",
            "price_injection_refused",
        ),
        security_metadata_field="stripe",
    )
)


__all__ = [
    "PAYMENTS_PENDING_HELPER",
    "PAYMENTS_PENDING_LABEL",
    "STRIPE_PRICING_SECTION_ID",
    "STRIPE_PRICING_VARIANT_ID",
    "STRIPE_PRIMITIVE_ID",
    "StripeSpec",
    "apply_stripe_spec",
    "default_stripe_app_spec",
    "generate_stripe",
    "prepare_stripe_app_spec",
    "stripe_verify",
]
