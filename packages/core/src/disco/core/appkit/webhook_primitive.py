"""The AppKit `webhook` primitive — the Epic F3.3 SEAM (declaration + spec ONLY).

This module is the honest, FAIL-CLOSED half of plan §7 / §10's 3.3 "Webhooks
(in+out)" row: it lets an app DECLARE webhook endpoints (id, direction, event
types) and renders that declared contract on a docs/admin surface in a visible
"Handler pending secure setup" state. It deliberately emits NO live endpoint:

* NO Worker route, NO request handler, NO fetch listener — nothing that could
  receive or emit a delivery;
* NO signature verification, NO HMAC, NO idempotency/replay dedup, NO SSRF /
  DNS-rebind protection — that entire set is the DEFERRED SECURITY FILL
  (security-classed labor per §10.5, spec'd in `docs/wo-f33-webhook-security-spec.md`);
* NO secrets: the signing secret and any outbound target URL are OPERATOR-
  CONFIGURED host-side (S-W2 vault/approvals), never part of `WebhookSpec` and
  never in the generated tree.

FAIL-CLOSED ON PURPOSE: `tier="template_only"` + `verify=None`. Under the WO-A3
finish gate, a template_only primitive whose `verify` cannot pass BLOCKS finish —
an app that declares a webhook cannot be called done until the security fill
lands a real adversarial `verify` (forged-sig rejected, replay deduped,
outbound-to-internal-IP blocked). Do NOT stub `verify` to make the gate pass;
flipping it from None to real is the security session's job, not a feature fix.

`host_contract=(HostService("webhook.emit"),)` is a DECLARATION only — the name
the outbound leg will dispatch through the A2 bus once the egress-proxied handler
exists. No such host service is registered here.

The fold (`apply_webhook_spec`) records each declared endpoint using EXISTING
AppSpec shapes only (the WO-A1 rule — no new AppSpec field): one `custom` Section
per endpoint on a dedicated `webhooks` docs page, with the contract in the
section's bounded `SectionContent` slots (heading = id + direction, subheading =
the pending marker, items = the event types). Every base primitive's shared
emitters therefore render the declaration — as documentation, never as an
affordance.
"""

from __future__ import annotations

import html
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .primitives import HostService, PrimitiveDefinition, register_primitive
from .recipes import SiteRecipe
from .spec import AppSpec, DesignSpec, Page

WEBHOOK_PRIMITIVE_ID = "webhook"

# The name of the host service the OUTBOUND leg will call through the A2 bus once
# the security fill exists. Declared in `host_contract`; NOT registered here.
WEBHOOK_EMIT_SERVICE_NAME = "webhook.emit"

# The unmistakable declared-but-not-active marker. Rendered verbatim on every
# surface that shows a declared endpoint; the tests key on this exact string.
WEBHOOK_PENDING_MARKER = "Handler pending secure setup"

# Where declared endpoints live in the AppSpec: one section per endpoint, on this
# docs page, with this id prefix. Existing shapes only — `_SECTION_ID_PREFIX` +
# endpoint_id (<= 48) stays under spec.py's 64-char id cap.
WEBHOOK_DOCS_PAGE_ID = "webhooks"
_DOCS_PAGE_ROUTE = "/webhooks"
_DOCS_PAGE_TITLE = "Webhook endpoints"
_SECTION_ID_PREFIX = "webhook_"

# endpoint_id: a snake slug (same charset discipline as spec.py's _IDENT_RE).
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# event types: dotted snake slugs, the webhook convention ("order.created").
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_MAX_ENDPOINT_ID = 48
_MAX_EVENT_TYPES = 12
_MAX_EVENT_TYPE_LEN = 64
_MAX_DESCRIPTION = 500


class WebhookSpec(BaseModel):
    """One DECLARED webhook endpoint: id, direction, event types, optional prose.

    Deliberately absent: the signing secret and the outbound target URL. Both are
    operator-configured HOST-SIDE (S-W2 vault + origin approvals) — they never
    belong in a model-authored spec and never reach the generated tree.
    `extra="forbid"` so an unknown key (e.g. a smuggled `secret`) is a precise
    refusal, not a silent drop."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str = Field(
        min_length=1,
        max_length=_MAX_ENDPOINT_ID,
        description="Slug identifying the endpoint (snake_case, <= 48 chars).",
    )
    direction: Literal["inbound", "outbound"] = Field(
        description="'inbound' = deliveries arrive here (once secured); "
        "'outbound' = the app emits events via the host's webhook.emit service."
    )
    event_types: list[str] = Field(
        min_length=1,
        max_length=_MAX_EVENT_TYPES,
        description="1-12 event-type slugs this endpoint handles/emits "
        "(dotted snake_case, e.g. 'order.created').",
    )
    description: str | None = Field(
        default=None,
        max_length=_MAX_DESCRIPTION,
        description="Optional human-facing note rendered on the docs surface.",
    )

    @field_validator("endpoint_id")
    @classmethod
    def _endpoint_id_is_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                "endpoint_id must be a snake_case slug (lowercase, starts with a "
                f"letter, only letters/digits/underscore), got {value!r}"
            )
        return value

    @field_validator("event_types")
    @classmethod
    def _event_types_are_bounded_slugs(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for event in value:
            if len(event) > _MAX_EVENT_TYPE_LEN:
                raise ValueError(
                    f"event type too long ({len(event)} > {_MAX_EVENT_TYPE_LEN} "
                    f"chars): {event[:40]!r}…"
                )
            if not _EVENT_TYPE_RE.match(event):
                raise ValueError(
                    "event types must be dotted snake_case slugs like "
                    f"'order.created', got {event!r}"
                )
            if event in seen:
                raise ValueError(f"duplicate event type: {event!r}")
            seen.add(event)
        return value


def _endpoint_section_data(spec: WebhookSpec) -> dict[str, object]:
    """The Section dict (existing AppSpec shape) recording one declared endpoint.

    kind='custom' → the generator's generic documentation block: heading/
    subheading/body render everywhere, and `items` keeps the event types as
    structured content. Nothing here is interactive — no form, no cta, no route."""
    direction_note = (
        "Inbound delivery stays inactive until the deferred security fill lands "
        "(signature verification and replay dedup)."
        if spec.direction == "inbound"
        else "Outbound emission stays inactive until the deferred security fill "
        "lands (host-mediated egress with SSRF protection)."
    )
    body_parts: list[str] = []
    if spec.description:
        body_parts.append(spec.description.strip())
    body_parts.append(f"Event types: {', '.join(spec.event_types)}.")
    body_parts.append(f"Declared contract only — no live handler exists. {direction_note}")
    return {
        "id": f"{_SECTION_ID_PREFIX}{spec.endpoint_id}",
        "kind": "custom",
        "content": {
            "heading": f"{spec.endpoint_id} — {spec.direction} webhook",
            "subheading": f"{WEBHOOK_PENDING_MARKER} — declared, not active.",
            "body": " ".join(body_parts),
            "items": list(spec.event_types),
        },
    }


def apply_webhook_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated WebhookSpec into the AppSpec: record the declared endpoint
    as a documentation Section on the `webhooks` docs page (created on first use).
    Same house dance as the sibling primitives: dump → mutate → re-validate.

    A duplicate endpoint_id (already declared on ANY page) is a ValueError — a
    re-declaration is a bug or a retry, never a silent overwrite."""
    if not isinstance(spec, WebhookSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {WEBHOOK_PRIMITIVE_ID!r} needs a WebhookSpec")

    section_id = f"{_SECTION_ID_PREFIX}{spec.endpoint_id}"
    for page in app.pages:
        for section in page.sections:
            if section.id == section_id:
                raise ValueError(
                    f"duplicate webhook endpoint_id {spec.endpoint_id!r}: already "
                    f"declared on page {page.id!r}"
                )

    data = app.model_dump(mode="json")
    section_data = _endpoint_section_data(spec)
    for page_data in data["pages"]:
        if page_data["id"] == WEBHOOK_DOCS_PAGE_ID:
            page_data["sections"] = [*page_data["sections"], section_data]
            break
    else:
        for page in app.pages:
            if page.route == _DOCS_PAGE_ROUTE:
                raise ValueError(
                    f"cannot create the {WEBHOOK_DOCS_PAGE_ID!r} docs page: route "
                    f"{_DOCS_PAGE_ROUTE!r} is already taken by page {page.id!r}"
                )
        data["pages"] = [
            *data["pages"],
            {
                "id": WEBHOOK_DOCS_PAGE_ID,
                "route": _DOCS_PAGE_ROUTE,
                "title": _DOCS_PAGE_TITLE,
                "sections": [section_data],
            },
        ]
    return AppSpec.model_validate(data)


def default_webhook_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The minimal standalone webhook app: one docs page, zero endpoints declared.

    `recipe` is deliberately unused — a static contract page has no design-
    preference surface (the parameter satisfies the PrimitiveDefinition contract).
    """
    del recipe
    title = name.strip() or "Webhooks"
    return AppSpec(
        schema_version=1,
        app_kind=WEBHOOK_PRIMITIVE_ID,
        name=title,
        pages=(Page(id=WEBHOOK_DOCS_PAGE_ID, route="/", title=_DOCS_PAGE_TITLE),),
    )


def prepare_webhook_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the webhook primitive needs no normalization."""
    return app


def generate_webhook(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower into ONE static documentation page. Deliberately NOT a receiver:
    no Worker route, no wrangler config, no script, no form — a viewer sees the
    declared contract and, unmistakably, that no endpoint is active yet.
    `design` is deliberately unused — see `default_webhook_app_spec`."""
    del design
    title = html.escape(app.name)
    blocks: list[str] = []
    for page in app.pages:
        for section in page.sections:
            if not section.id.startswith(_SECTION_ID_PREFIX) or section.content is None:
                continue
            content = section.content
            item_lines = "\n".join(
                f"          <li>{html.escape(item)}</li>" for item in content.items
            )
            blocks.append(
                f'    <section id="{html.escape(section.id)}">\n'
                f"      <h2>{html.escape(content.heading or section.id)}</h2>\n"
                f"      <p><strong>{html.escape(content.subheading or WEBHOOK_PENDING_MARKER)}"
                "</strong></p>\n"
                f"      <p>{html.escape(content.body or '')}</p>\n"
                f"      <ul>\n{item_lines}\n      </ul>\n"
                "    </section>\n"
            )
    listing = "".join(blocks) if blocks else "    <p>No webhook endpoints declared yet.</p>\n"
    return {
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{title} — webhook endpoints</title>\n"
            "  </head>\n"
            "  <body>\n"
            f"    <h1>Webhook endpoints — {title}</h1>\n"
            f"    <p><strong>{WEBHOOK_PENDING_MARKER}.</strong> Every endpoint on this\n"
            "    page is DECLARED ONLY — no live receiver or emitter route is generated.\n"
            "    The deferred security fill (see docs/wo-f33-webhook-security-spec.md)\n"
            "    activates them.</p>\n"
            f"{listing}"
            "  </body>\n"
            "</html>\n"
        )
    }


register_primitive(
    PrimitiveDefinition(
        id=WEBHOOK_PRIMITIVE_ID,
        default_app_spec=default_webhook_app_spec,
        prepare_app_spec=prepare_webhook_app_spec,
        generate=generate_webhook,
        tier="template_only",
        host_contract=(HostService(WEBHOOK_EMIT_SERVICE_NAME),),
        spec_schema=WebhookSpec,
        # FAIL-CLOSED ON PURPOSE (WO-A3): a template_only primitive with verify=None
        # cannot pass the finish gate. The security session flips this to a real
        # adversarial verify (forged-sig rejected / replay deduped / SSRF blocked)
        # per docs/wo-f33-webhook-security-spec.md. Do NOT stub it to pass.
        verify=None,
        apply_spec=apply_webhook_spec,
    )
)


__all__ = [
    "WEBHOOK_DOCS_PAGE_ID",
    "WEBHOOK_EMIT_SERVICE_NAME",
    "WEBHOOK_PENDING_MARKER",
    "WEBHOOK_PRIMITIVE_ID",
    "WebhookSpec",
    "apply_webhook_spec",
    "default_webhook_app_spec",
    "generate_webhook",
    "prepare_webhook_app_spec",
]
