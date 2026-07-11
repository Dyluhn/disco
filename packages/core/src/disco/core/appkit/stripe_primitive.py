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

The host-side ``payments.checkout`` handler, secret custody, and the live
adversarial verifier are separate WO-F4.1 slices.  Until that verifier lands,
``tier="template_only"`` + ``verify=None`` intentionally keeps the finish gate
closed even though the generated data plane exists.

THE FAIL-CLOSED GATE IS INTENTIONAL. This primitive registers with
``tier="template_only"`` and ``verify=None``. Under WO-A3's rule
(template_only + verify=None ⇒ cannot ship unverified), any app that adds
``stripe`` FAILS the finish gate until the deferred security session lands
the REAL adversarial ``verify`` harness (forged-sig rejected, replay deduped,
leaked-key blast radius bounded). Do NOT "fix" a blocked build by setting
``verify`` to a passing stub, flipping the tier, or otherwise working around
the gate — that would ship unverified payments code. ``verify`` flips from
None only WITH the real harness from the security spec doc.
"""

from __future__ import annotations

import hashlib
import html
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .primitives import HostService, PrimitiveDefinition, register_primitive
from .recipes import SiteRecipe
from .spec import AppSpec, DesignSpec, Page, Section, SectionContent

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
    if app.app_kind == "records" and not app.roles:
        raise ValueError("Stripe requires a records app with an existing non-payment role")
    if app.stripe is not None and app.stripe.entitlement_flag != spec.entitlement_flag:
        raise ValueError("Stripe entitlement_flag cannot change after initial application")
    section: dict[str, object] = {
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
    data = app.model_dump(mode="json")
    pages = list(data.get("pages") or [])
    if pages:
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
    else:
        pages = [{"id": "home", "route": "/", "title": "Home", "sections": [section]}]
    data["pages"] = pages
    roles = list(data.get("roles") or [])
    if app.stripe is None and spec.entitlement_flag in roles:
        raise ValueError("Stripe entitlement_flag must be a new, payment-managed role")
    if spec.entitlement_flag not in roles:
        roles.append(spec.entitlement_flag)
    data["roles"] = roles
    data["stripe"] = {
        "app_binding": (
            app.stripe.app_binding if app.stripe is not None else _stripe_app_binding(app)
        ),
        # The selector is deliberately NOT a Stripe price id.  The host maps
        # this stable, validated identifier to its operator-owned price config.
        "plan_selector": spec.entitlement_flag,
        "entitlement_flag": spec.entitlement_flag,
        "success_message": spec.success_message,
    }
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
        # verify=None ON PURPOSE — the fail-closed gate. WO-A3's rule makes a
        # template_only primitive with verify=None UNSHIPPABLE, which is exactly
        # right until the real adversarial harness (forged-sig rejected, replay
        # deduped, leaked-key blast radius bounded) exists. NEVER set a passing
        # stub here; see the module docstring + docs/wo-f41-stripe-security-spec.md.
        verify=None,
        apply_spec=apply_stripe_spec,
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
]
