"""The security-filled AppKit ``webhook`` primitive (WO-F3.3).

The model supplies only a bounded endpoint declaration. A records-app fold adds
strict non-secret ``AppSpec.webhooks`` metadata and documentation sections; the
records generator lowers that metadata through Disco-owned Worker/schema code.
Inbound routes authenticate endpoint-bound, timestamped raw-body HMACs before
JSON parsing and apply the dedup row plus durable inbox effect atomically.
Outbound routes require a user session and dispatch only through the authenticated
A2 bus to the host-owned ``webhook.emit`` adapter, which composes the existing
secret resolver, signed origin approval, and guarded egress chokepoint.

Secrets and target URLs remain operator-owned. The primitive stays
``template_only`` and declares both deterministic trusted-tree verification and
the mandatory ``webhook.security.v1`` live exploit runner. A standalone webhook
scaffold intentionally remains inert and visibly pending; only a session-authenticated
records fold receives live routes.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .primitives import (
    HostService,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
)
from .recipes import SiteRecipe
from .spec import AppSpec, DesignSpec, Page, Section, serialize_app_spec

WEBHOOK_PRIMITIVE_ID = "webhook"

# The name of the host service the OUTBOUND leg will call through the A2 bus once
# the security fill exists. Declared in `host_contract`; NOT registered here.
WEBHOOK_EMIT_SERVICE_NAME = "webhook.emit"

# The unmistakable declared-but-not-active marker. Rendered verbatim on every
# surface that shows a declared endpoint; the tests key on this exact string.
WEBHOOK_PENDING_MARKER = "Handler pending secure setup"
WEBHOOK_SECURED_MARKER = "Disco-secured handler; operator runtime configuration required"

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
_WEBHOOK_PROVENANCE_RELPATH = ".disco/primitives/webhook.json"
_WEBHOOK_LIVE_VERIFY_ID = "webhook.security.v1"
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk_(?:live|test)|rk_(?:live|test)|whsec_|webhook_secret_)[A-Za-z0-9_-]{4,}"
)


def _webhook_app_binding(app: AppSpec) -> str:
    canonical = json.dumps(
        {"app_kind": app.app_kind, "name": app.name},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"app_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


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


def _endpoint_section_data(spec: WebhookSpec, *, secured: bool) -> dict[str, object]:
    """The Section dict (existing AppSpec shape) recording one declared endpoint.

    kind='custom' → the generator's generic documentation block: heading/
    subheading/body render everywhere, and `items` keeps the event types as
    structured content. Nothing here is interactive — no form, no cta, no route."""
    if secured:
        direction_note = (
            "Inbound delivery verifies the timestamped raw-body signature before parsing "
            "and applies deduplication with its effect in one D1 transaction."
            if spec.direction == "inbound"
            else "Outbound emission uses the authenticated host-service bus and the "
            "host egress chokepoint; targets and signing keys remain operator-owned."
        )
    else:
        direction_note = (
            "Inbound delivery stays inactive until this declaration is added to a "
            "session-authenticated records app."
            if spec.direction == "inbound"
            else "Outbound emission stays inactive until this declaration is added to a "
            "session-authenticated records app."
        )
    body_parts: list[str] = []
    if spec.description:
        body_parts.append(spec.description.strip())
    body_parts.append(f"Event types: {', '.join(spec.event_types)}.")
    body_parts.append(direction_note)
    if secured:
        body_parts.append(
            "The route fails closed until its host-managed runtime configuration exists."
        )
    else:
        body_parts.append("Declared contract only — no live handler exists.")
    return {
        "id": f"{_SECTION_ID_PREFIX}{spec.endpoint_id}",
        "kind": "custom",
        "content": {
            "heading": f"{spec.endpoint_id} — {spec.direction} webhook",
            "subheading": (
                WEBHOOK_SECURED_MARKER
                if secured
                else f"{WEBHOOK_PENDING_MARKER} — declared, not active."
            ),
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
    standalone_pending = app.app_kind == WEBHOOK_PRIMITIVE_ID
    if app.app_kind not in {"records", WEBHOOK_PRIMITIVE_ID}:
        raise ValueError("Webhook security fill requires the D1-backed records primitive")
    if app.app_kind == "records" and not app.roles:
        raise ValueError("Webhook security fill requires a session-authenticated records app")

    section_id = f"{_SECTION_ID_PREFIX}{spec.endpoint_id}"
    for page in app.pages:
        for section in page.sections:
            if section.id == section_id:
                raise ValueError(
                    f"duplicate webhook endpoint_id {spec.endpoint_id!r}: already "
                    f"declared on page {page.id!r}"
                )

    data = app.model_dump(mode="json")
    section_data = _endpoint_section_data(spec, secured=not standalone_pending)
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
    if standalone_pending:
        return AppSpec.model_validate(data)
    existing_meta = app.webhooks
    if existing_meta is None:
        app_binding = _webhook_app_binding(app)
        endpoints: list[dict[str, object]] = []
    else:
        app_binding = existing_meta.app_binding
        endpoints = [endpoint.model_dump(mode="json") for endpoint in existing_meta.endpoints]
    endpoints.append(
        {
            "endpoint_id": spec.endpoint_id,
            "direction": spec.direction,
            "event_types": list(spec.event_types),
        }
    )
    data["webhooks"] = {"app_binding": app_binding, "endpoints": endpoints}
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
            "    The deferred security fill (see development/notes/wo-f33-webhook-security-spec.md)\n"
            "    activates them.</p>\n"
            f"{listing}"
            "  </body>\n"
            "</html>\n"
        )
    }


def _verify_result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    failed = sum(not check.passed for check in checks)
    return PrimitiveVerifyResult(
        ok=failed == 0,
        detail=f"{len(checks) - failed} passed / {failed} failed",
        checks=tuple(checks),
    )


def _metadata_check(app: AppSpec | None) -> VerifyCheck:
    if app is None or app.webhooks is None:
        return VerifyCheck(
            "webhook_metadata_binding",
            False,
            "Webhook provenance requires strict AppSpec.webhooks metadata (fail-closed).",
        )
    meta = app.webhooks
    endpoint_ids = {endpoint.endpoint_id for endpoint in meta.endpoints}
    section_ids = {
        section.id.removeprefix(_SECTION_ID_PREFIX)
        for page in app.pages
        for section in page.sections
        if section.id.startswith(_SECTION_ID_PREFIX)
    }
    reasons: list[str] = []
    if app.app_kind != "records" or not app.roles:
        reasons.append("webhooks require a session-authenticated records app")
    if meta.app_binding != _webhook_app_binding(app):
        reasons.append("app_binding does not match the canonical app identity")
    if endpoint_ids != section_ids:
        reasons.append("webhook metadata does not exactly match declared sections")
    return VerifyCheck(
        "webhook_metadata_binding",
        not reasons,
        "Webhook metadata is bound to the records app and exact endpoint sections."
        if not reasons
        else "; ".join(reasons),
    )


def _parse_webhook_provenance_record(tree: Mapping[str, str]) -> dict[str, object] | VerifyCheck:
    """Load and shape-check the raw provenance JSON; VerifyCheck means "stop here"."""
    raw = tree.get(_WEBHOOK_PROVENANCE_RELPATH)
    if raw is None:
        return VerifyCheck(
            "webhook_provenance_binding",
            False,
            f"missing {_WEBHOOK_PROVENANCE_RELPATH}; webhook metadata cannot ship "
            "without provenance.",
        )
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return VerifyCheck(
            "webhook_provenance_binding", False, f"invalid webhook provenance: {exc}"
        )
    if not isinstance(record, dict) or set(record) != {
        "primitive_id",
        "tier",
        "applied_at",
        "specs",
    }:
        return VerifyCheck(
            "webhook_provenance_binding",
            False,
            "Webhook provenance must contain exactly primitive_id, tier, applied_at, and specs.",
        )
    if record.get("primitive_id") != WEBHOOK_PRIMITIVE_ID or record.get("tier") != "template_only":
        return VerifyCheck(
            "webhook_provenance_binding", False, "Webhook provenance identity is invalid."
        )
    if not isinstance(record.get("applied_at"), str) or not record["applied_at"].strip():
        return VerifyCheck(
            "webhook_provenance_binding", False, "Webhook provenance timestamp is empty."
        )
    return record


def _parse_webhook_provenance_specs(
    record: dict[str, object],
) -> tuple[WebhookSpec, ...] | VerifyCheck:
    """Validate the embedded WebhookSpec list; the broad except is deliberate — the
    validation detail is safe declarative metadata, never a secret."""
    raw_specs = record.get("specs")
    if not isinstance(raw_specs, list) or not raw_specs:
        return VerifyCheck(
            "webhook_provenance_binding", False, "Webhook provenance specs are empty."
        )
    try:
        return tuple(WebhookSpec.model_validate(value) for value in raw_specs)
    except Exception as exc:  # noqa: BLE001 - bounded declarative validation detail
        return VerifyCheck(
            "webhook_provenance_binding", False, f"invalid webhook provenance spec: {exc}"
        )


def _webhook_provenance_endpoint_reasons(spec: WebhookSpec, app: AppSpec) -> list[str]:
    """Compare one validated provenance spec against its live metadata endpoint
    and folded docs section."""
    if app.webhooks is None:  # caller guards this; fail closed if that ever changes
        return ["Webhook provenance exists without AppSpec.webhooks metadata (fail-closed)."]
    by_id = {endpoint.endpoint_id: endpoint for endpoint in app.webhooks.endpoints}
    endpoint = by_id.get(spec.endpoint_id)
    if endpoint is None:
        return [f"unknown provenance endpoint {spec.endpoint_id}"]
    reasons: list[str] = []
    if endpoint.direction != spec.direction or endpoint.event_types != tuple(spec.event_types):
        reasons.append(f"metadata differs for endpoint {spec.endpoint_id}")
    section = next(
        (
            section
            for page in app.pages
            for section in page.sections
            if section.id == f"{_SECTION_ID_PREFIX}{spec.endpoint_id}"
        ),
        None,
    )
    expected = Section.model_validate(_endpoint_section_data(spec, secured=True))
    if section is None or section.model_dump(mode="json") != expected.model_dump(mode="json"):
        reasons.append(f"section differs for endpoint {spec.endpoint_id}")
    return reasons


def _webhook_provenance_reasons(specs: tuple[WebhookSpec, ...], app: AppSpec) -> list[str]:
    if app.webhooks is None:  # caller guards this; fail closed if that ever changes
        return ["Webhook provenance exists without AppSpec.webhooks metadata (fail-closed)."]
    by_id = {endpoint.endpoint_id: endpoint for endpoint in app.webhooks.endpoints}
    reasons: list[str] = []
    if len(specs) != len(by_id) or len({spec.endpoint_id for spec in specs}) != len(specs):
        reasons.append("provenance does not contain exactly one spec per endpoint")
    for spec in specs:
        reasons.extend(_webhook_provenance_endpoint_reasons(spec, app))
    return reasons


def _provenance_check(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    record_or_failure = _parse_webhook_provenance_record(tree)
    if isinstance(record_or_failure, VerifyCheck):
        return record_or_failure
    specs_or_failure = _parse_webhook_provenance_specs(record_or_failure)
    if isinstance(specs_or_failure, VerifyCheck):
        return specs_or_failure
    specs = specs_or_failure
    if app is None or app.webhooks is None:
        return VerifyCheck(
            "webhook_provenance_binding",
            False,
            "Webhook provenance exists without AppSpec.webhooks metadata (fail-closed).",
        )
    reasons = _webhook_provenance_reasons(specs, app)
    return VerifyCheck(
        "webhook_provenance_binding",
        not reasons,
        "Validated webhook provenance exactly matches all metadata and docs sections."
        if not reasons
        else "; ".join(reasons),
    )


def _trusted_tree_check(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> VerifyCheck:
    if app is None or app.webhooks is None or design is None:
        return VerifyCheck(
            "webhook_trusted_tree",
            False,
            "cannot reconstruct trusted webhook output without AppSpec.webhooks and DesignSpec.",
        )
    from .generator import generate

    try:
        expected = generate(app, design)
    except Exception as exc:  # noqa: BLE001 - projection errors close the gate
        return VerifyCheck(
            "webhook_trusted_tree", False, f"trusted webhook projection failed: {exc}"
        )
    webhook_components = tuple(
        sorted(
            path
            for path, contents in expected.items()
            if path.startswith("src/components/") and 'data-appkit-section="webhook_' in contents
        )
    )
    if len(webhook_components) != len(app.webhooks.endpoints):
        return VerifyCheck(
            "webhook_trusted_tree",
            False,
            "trusted projection did not emit exactly one docs component per webhook endpoint.",
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
        *webhook_components,
    )
    mismatched = [path for path in sensitive_paths if tree.get(path) != expected.get(path)]
    if tree.get(".disco/appspec.json") != serialize_app_spec(app):
        mismatched.append(".disco/appspec.json")
    return VerifyCheck(
        "webhook_trusted_tree",
        not mismatched,
        "Webhook Worker, host client, lockfile, schemas, and AppSpec match the trusted projection."
        if not mismatched
        else "security-sensitive webhook file mismatch: " + ", ".join(mismatched),
    )


def _secret_absence_check(tree: Mapping[str, str]) -> VerifyCheck:
    hits = sorted(path for path, contents in tree.items() if _SECRET_VALUE_RE.search(contents))
    return VerifyCheck(
        "webhook_static_secret_absence",
        not hits,
        "No webhook secret-value patterns occur in the emitted tree."
        if not hits
        else "Webhook secret-value pattern found in: " + ", ".join(hits),
    )


def webhook_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    return _verify_result(
        [
            _metadata_check(app),
            _provenance_check(app, tree),
            _trusted_tree_check(app, design, tree),
            _secret_absence_check(tree),
        ]
    )


register_primitive(
    PrimitiveDefinition(
        id=WEBHOOK_PRIMITIVE_ID,
        default_app_spec=default_webhook_app_spec,
        prepare_app_spec=prepare_webhook_app_spec,
        generate=generate_webhook,
        tier="template_only",
        host_contract=(HostService(WEBHOOK_EMIT_SERVICE_NAME),),
        spec_schema=WebhookSpec,
        verify=webhook_verify,
        apply_spec=apply_webhook_spec,
        live_verify_id=_WEBHOOK_LIVE_VERIFY_ID,
        live_verify_checks=(
            "forged_signature_rejected",
            "replay_deduped",
            "egress_blocked",
            "secret_absence",
        ),
        security_metadata_field="webhooks",
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
    "webhook_verify",
]
