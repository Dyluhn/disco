"""The AppKit `records` primitive: related entities + per-entity CRUD routes.

This primitive deliberately lives beside lead-gen/directory instead of widening the
lead-gen path into a generic CRUD DSL. It reuses the generator's shared
shape-agnostic emitters and owns only the multi-table schema, Drizzle schema, and
N-entity Worker surface.
"""

from __future__ import annotations

from .primitives import RECORDS_PRIMITIVE_ID, PrimitiveDefinition, register_primitive
from .recipes import SiteRecipe
from .records_primitive_parts.auth_worker import (
    _emit_records_auth_worker_ts,
    _emit_records_auth_wrangler_toml,
)
from .records_primitive_parts.drizzle_ts import (
    _emit_records_auth_drizzle_ts,
    _emit_records_drizzle_ts,
)
from .records_primitive_parts.legacy_manifest import _emit_records_legacy_manifest_ts
from .records_primitive_parts.naming import (
    _fk_order_entities,
    _records_table_name,
)
from .records_primitive_parts.owner_guide import (
    _emit_records_auth_owner_guide_md,
    _emit_records_owner_guide_md,
    _emit_records_policy_owner_guide_md,
)
from .records_primitive_parts.policy_ui import (
    emit_records_policy_app_tsx,
    emit_records_workspace_tsx,
    records_policy_css,
)
from .records_primitive_parts.policy_worker import (
    emit_records_policy_worker_ts,
    records_policy_enabled,
)
from .records_primitive_parts.schema_sql import (
    _emit_records_auth_schema_sql,
    _emit_records_schema_sql,
)
from .records_primitive_parts.verify import records_verify
from .records_primitive_parts.worker_ts import _emit_records_worker_ts
from .spec import (
    Action,
    AppSpec,
    DesignSpec,
    Entity,
    EntityField,
    Page,
    Section,
    SectionContent,
)


def default_records_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A default two-entity records app that proves a real FK relation."""
    title = name.strip() or "Team Records"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    sections = (
        Section(
            id="hero",
            kind="hero",
            variant_id=prefs.get("hero"),
            content=SectionContent(
                heading=title,
                subheading="Track team members and the shifts assigned to them.",
            ),
        ),
        Section(
            id="records",
            kind="list",
            variant_id=prefs.get("list"),
            content=SectionContent(
                heading="Records",
                items=("Team members", "Shifts", "Member assignments"),
            ),
        ),
        Section(
            id="footer",
            kind="footer",
            variant_id=prefs.get("footer"),
            content=SectionContent(heading=title),
        ),
    )
    return AppSpec(
        schema_version=1,
        app_kind=RECORDS_PRIMITIVE_ID,
        name=title,
        pages=(Page(id="home", route="/", title="Home", sections=sections),),
        entities=(
            Entity(
                id="team_member",
                name="Team Member",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="email", required=True),
                ),
            ),
            Entity(
                id="shift",
                name="Shift",
                fields=(
                    EntityField(name="title", type="str", required=True),
                    EntityField(name="starts_at", type="datetime", required=True),
                    EntityField(
                        name="member_id",
                        type="int",
                        required=True,
                        references="team_member",
                    ),
                ),
            ),
        ),
        primary_actions=(Action(id="view_records", label="View records", type="nav", target="/"),),
    )


def default_records_auth_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A default RBAC records app: members request time off, approvers approve."""
    title = name.strip() or "Shift Calendar"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    sections = (
        Section(
            id="hero",
            kind="hero",
            variant_id=prefs.get("hero"),
            content=SectionContent(
                heading=title,
                subheading="Request time off and route approvals to authorized approvers.",
            ),
        ),
        Section(
            id="requests",
            kind="list",
            variant_id=prefs.get("list"),
            content=SectionContent(
                heading="Shift calendar records",
                items=("Team members", "Time-off requests", "Approvals"),
            ),
        ),
        Section(
            id="footer",
            kind="footer",
            variant_id=prefs.get("footer"),
            content=SectionContent(heading=title),
        ),
    )
    return AppSpec(
        schema_version=1,
        app_kind=RECORDS_PRIMITIVE_ID,
        name=title,
        roles=("approver", "member"),
        pages=(Page(id="home", route="/", title="Home", sections=sections),),
        entities=(
            Entity(
                id="team_member",
                name="Team Member",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="email", required=True),
                ),
            ),
            Entity(
                id="time_off_request",
                name="Time Off Request",
                fields=(
                    EntityField(
                        name="member_id",
                        type="int",
                        required=True,
                        references="team_member",
                    ),
                    EntityField(name="reason", type="str", required=True),
                ),
            ),
            Entity(
                id="approval",
                name="Approval",
                write_roles=("approver",),
                read_roles=("approver",),
                fields=(
                    EntityField(
                        name="request_id",
                        type="int",
                        required=True,
                        references="time_off_request",
                    ),
                    EntityField(name="decision", type="str", required=True),
                ),
            ),
        ),
        primary_actions=(
            Action(id="view_requests", label="View requests", type="nav", target="/"),
        ),
    )


def _validate_records_role_tables(app: AppSpec) -> None:
    """Refuse an auth-enabled records app that shadows a reserved auth/Stripe/
    webhook infrastructure table with one of its own entities."""
    if not app.roles:
        return
    reserved_tables = {"users", "sessions"}
    if app.stripe is not None:
        reserved_tables.update({"user_role_grants", "stripe_events", "stripe_fulfillments"})
    if app.webhooks is not None:
        reserved_tables.update({"webhook_events", "webhook_effects"})
    for entity in app.entities:
        table = _records_table_name(entity)
        if table in reserved_tables:
            raise ValueError(
                f"auth-enabled records app cannot declare entity table {table!r}; "
                "it is reserved for auth infrastructure"
            )


def _validate_records_stripe(app: AppSpec) -> None:
    """Refuse a Stripe-enabled records app whose roles/pricing section don't
    match the Stripe add-on's fold contract."""
    if app.stripe is None:
        return
    if app.stripe.entitlement_flag not in app.roles:
        raise ValueError("Stripe entitlement_flag must be declared in AppSpec.roles")
    if not any(role != app.stripe.entitlement_flag for role in app.roles):
        raise ValueError("Stripe requires at least one non-payment records role")
    stripe_sections = tuple(
        section for page in app.pages for section in page.sections if section.id == "stripe_pricing"
    )
    if len(stripe_sections) != 1 or stripe_sections[0].kind != "pricing":
        raise ValueError("Stripe records app requires one stripe_pricing section")


def _validate_records_webhooks(app: AppSpec) -> None:
    """Refuse a webhook-enabled records app that lacks session auth or whose
    webhook sections don't exactly match the declared endpoints."""
    if app.webhooks is None:
        return
    if not app.roles:
        raise ValueError("Webhook records apps require session auth")
    section_ids = {
        section.id
        for page in app.pages
        for section in page.sections
        if section.id.startswith("webhook_")
    }
    expected_ids = {f"webhook_{endpoint.endpoint_id}" for endpoint in app.webhooks.endpoints}
    if section_ids != expected_ids:
        raise ValueError("Webhook metadata must exactly match the declared webhook sections")


def _validate_records_policies(app: AppSpec) -> None:
    """Keep the row-policy surface explicit and free of ambiguous legacy gates."""
    if not records_policy_enabled(app):
        return
    from .form_primitive import form_submission_entities_for

    if not app.roles:
        raise ValueError("records policies require session roles")
    if app.stripe is not None or app.webhooks is not None:
        raise ValueError("records policies cannot currently be combined with Stripe or webhooks")
    if app.blog is not None:
        raise ValueError("records policies cannot currently be combined with blog routing")
    form_ids = {entity.id for entity in form_submission_entities_for(app)}
    for entity in app.entities:
        if entity.id in form_ids:
            continue
        if entity.record_policy is None:
            raise ValueError(
                "records policy mode requires record_policy on every persistent entity; "
                f"entity {entity.id!r} is ungoverned"
            )
        if entity.record_policy is not None and entity.write_roles:
            raise ValueError(
                f"entity {entity.id!r} record_policy owns write authorization; "
                "remove write_roles and use create_roles/manage_roles"
            )


def prepare_records_app_spec(app: AppSpec) -> AppSpec:
    """Validate a records app and persist it verbatim when it is already complete."""
    if app.app_kind != RECORDS_PRIMITIVE_ID:
        raise ValueError(
            f"records primitive requires app_kind={RECORDS_PRIMITIVE_ID!r}, got {app.app_kind!r}"
        )
    if not app.entities:
        raise ValueError("records primitive requires at least one entity")
    _validate_records_role_tables(app)
    _validate_records_stripe(app)
    _validate_records_webhooks(app)
    _validate_records_policies(app)
    return AppSpec.model_validate(app.model_dump(mode="json"))


def _records_split_form_entities(
    app: AppSpec,
) -> tuple[tuple[Entity, ...], tuple[Entity, ...], AppSpec, dict[str, str]]:
    """Partition `app.entities` into form-submission entities (folded by the form
    add-on) and record entities, and compute each form's public POST route.
    Returns (form_entities, record_entities, records_app, form_routes)."""
    from .form_primitive import form_route_for, form_submission_entities_for

    form_entities = form_submission_entities_for(app)
    form_entity_ids = {entity.id for entity in form_entities}
    record_entities = tuple(entity for entity in app.entities if entity.id not in form_entity_ids)
    if not record_entities:
        raise ValueError("records primitive requires at least one non-form entity")
    if form_entities:
        data = app.model_dump(mode="json")
        data["entities"] = [
            entity for entity in data["entities"] if entity["id"] not in form_entity_ids
        ]
        records_app = AppSpec.model_validate(data)
    else:
        records_app = app
    form_routes = {entity.id: form_route_for(app, entity) for entity in form_entities}
    return form_entities, record_entities, records_app, form_routes


def _records_schema_sql(
    record_entities: tuple[Entity, ...],
    form_entities: tuple[Entity, ...],
    *,
    auth_enabled: bool,
    stripe_enabled: bool,
    webhook_enabled: bool,
) -> str:
    """`schema.sql`: the records tables (auth tables prepended when session auth
    is enabled) plus any folded form submissions/Stripe/webhook tables."""
    from .form_primitive import emit_form_schema_sql

    schema_sql = (
        _emit_records_auth_schema_sql(record_entities)
        if auth_enabled
        else _emit_records_schema_sql(record_entities)
    )
    form_schema = emit_form_schema_sql(form_entities)
    if form_schema:
        schema_sql = "\n".join([schema_sql, form_schema])
    if stripe_enabled:
        from .stripe_worker import emit_stripe_schema_sql

        schema_sql = "\n\n".join([schema_sql, emit_stripe_schema_sql()]) + "\n"
    if webhook_enabled:
        from .webhook_worker import emit_webhook_schema_sql

        schema_sql = "\n\n".join([schema_sql, emit_webhook_schema_sql()]) + "\n"
    return schema_sql


def _records_worker_ts(
    app: AppSpec,
    records_app: AppSpec,
    record_entities: tuple[Entity, ...],
    form_entities: tuple[Entity, ...],
    form_routes: dict[str, str],
    *,
    auth_enabled: bool,
) -> str:
    """`worker/index.ts`: the base or session-auth Worker, with the folded form
    submission plane spliced in."""
    from .form_primitive import lower_form_records_worker_ts

    worker_ts = (
        emit_records_policy_worker_ts(records_app, form_entities)
        if auth_enabled and records_policy_enabled(records_app)
        else _emit_records_auth_worker_ts(records_app, form_entities)
        if auth_enabled
        else _emit_records_worker_ts(record_entities, form_entities)
    )
    return lower_form_records_worker_ts(
        worker_ts, form_entities, form_routes, auth_enabled=auth_enabled
    )


def _records_drizzle_ts(
    record_entities: tuple[Entity, ...],
    form_entities: tuple[Entity, ...],
    *,
    auth_enabled: bool,
    stripe_enabled: bool,
    webhook_enabled: bool,
) -> str:
    """`src/db/schema.ts`: the records Drizzle tables (auth tables prepended when
    session auth is enabled) plus any folded form/Stripe/webhook tables."""
    from .form_primitive import emit_form_drizzle_ts

    drizzle_ts = (
        _emit_records_auth_drizzle_ts(record_entities)
        if auth_enabled
        else _emit_records_drizzle_ts(record_entities)
    )
    form_drizzle = emit_form_drizzle_ts(form_entities)
    if form_drizzle:
        drizzle_ts = "\n".join([drizzle_ts, form_drizzle])
    if stripe_enabled:
        from .stripe_worker import emit_stripe_drizzle_ts

        drizzle_ts = "\n\n".join([drizzle_ts, emit_stripe_drizzle_ts()]) + "\n"
    if webhook_enabled:
        from .webhook_worker import emit_webhook_drizzle_ts

        drizzle_ts = "\n\n".join([drizzle_ts, emit_webhook_drizzle_ts()]) + "\n"
    return drizzle_ts


def _records_component_files(
    app: AppSpec,
    names: dict[tuple[str, str], str],
    db_entity: Entity,
    form_entities: tuple[Entity, ...],
    form_routes: dict[str, str],
    *,
    stripe_enabled: bool,
) -> dict[str, str]:
    """One `src/components/<Comp>.tsx` per section: a folded form component, the
    Stripe pricing component, or the base per-section component."""
    from .generator import _comp_name, _emit_component, _iter_sections

    files: dict[str, str] = {}
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        form_entity = (
            next((entity for entity in form_entities if entity.id == section.content_ref), None)
            if section.kind == "form" and section.content_ref is not None
            else None
        )
        if form_entity is not None:
            from .form_primitive import emit_app_form_component

            files[f"src/components/{comp}.tsx"] = emit_app_form_component(
                comp, section, form_entity, post_path=form_routes[form_entity.id]
            )
        elif stripe_enabled and section.id == "stripe_pricing":
            from .stripe_worker import emit_stripe_pricing_component

            files[f"src/components/{comp}.tsx"] = emit_stripe_pricing_component(comp, section)
        else:
            files[f"src/components/{comp}.tsx"] = _emit_component(
                comp, page, section, db_entity, f"/api/{_records_table_name(db_entity)}"
            )
    return files


def _records_base_files(
    app: AppSpec,
    design: DesignSpec,
    db_name: str,
    db_entity: Entity,
    names: dict[tuple[str, str], str],
    schema_sql: str,
    worker_ts: str,
    drizzle_ts: str,
    owner_guide: str,
    manifest_ts: str,
    *,
    auth_enabled: bool,
) -> dict[str, str]:
    """The base file tree shared by every records app, before the per-section
    component files and any blog/SEO additions are merged in."""
    from .blog_primitive import emit_app_tsx_with_blog_routes, has_blog
    from .generator import (
        _emit_api_client_ts,
        _emit_content_ts,
        _emit_dev_vars_example,
        _emit_drizzle_config_ts,
        _emit_gitignore,
        _emit_index_html,
        _emit_main_tsx,
        _emit_package_json,
        _emit_package_lock_json,
        _emit_styles_css,
        _emit_submit_hook_ts,
        _emit_tsconfig,
        _emit_vite_config,
        _emit_wrangler_toml,
    )

    policy_enabled = records_policy_enabled(app)
    files = {
        "index.html": _emit_index_html(app, design),
        "package.json": _emit_package_json(app, db_name),
        "package-lock.json": _emit_package_lock_json(app),
        "drizzle.config.ts": _emit_drizzle_config_ts(),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "wrangler.toml": (
            _emit_records_auth_wrangler_toml(app, db_entity)
            if auth_enabled
            else _emit_wrangler_toml(app, db_entity)
        ),
        "schema.sql": schema_sql,
        "migrations/0001_init.sql": schema_sql,
        "worker/index.ts": worker_ts,
        "src/main.tsx": _emit_main_tsx(),
        "src/App.tsx": (
            emit_records_policy_app_tsx(app, names)
            if policy_enabled
            else emit_app_tsx_with_blog_routes(app, names)
            if has_blog(app)
            else _emit_index_app_tsx(app, names)
        ),
        "src/api/client.ts": _emit_api_client_ts(),
        "src/hooks/useSubmit.ts": _emit_submit_hook_ts(),
        "src/styles.css": _emit_styles_css(design)
        + (records_policy_css() if policy_enabled else ""),
        "src/db/schema.ts": drizzle_ts,
        "src/generated/content.ts": _emit_content_ts(app, names),
        "src/generated/manifest.ts": manifest_ts,
        "OWNER_GUIDE.md": owner_guide,
        ".dev.vars.example": _emit_dev_vars_example(),
        ".gitignore": _emit_gitignore(),
    }
    if policy_enabled:
        files["src/components/RecordsWorkspace.tsx"] = emit_records_workspace_tsx(app)
    return files


def generate_records(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a records AppSpec into a Cloudflare app tree."""
    app = prepare_records_app_spec(app)
    form_entities, record_entities, records_app, form_routes = _records_split_form_entities(app)
    ordered = _fk_order_entities(record_entities)
    db_entity = ordered[0]
    from .blog_primitive import emit_blog_files
    from .generator import _component_names, _db_name, _emit_manifest_ts, _seo_files

    db_name = _db_name(app, db_entity)
    names = _component_names(app)
    auth_enabled = bool(app.roles)
    stripe_enabled = app.stripe is not None
    webhook_enabled = app.webhooks is not None
    if stripe_enabled and not auth_enabled:
        raise ValueError("Stripe records apps require session auth")
    schema_sql = _records_schema_sql(
        record_entities,
        form_entities,
        auth_enabled=auth_enabled,
        stripe_enabled=stripe_enabled,
        webhook_enabled=webhook_enabled,
    )
    worker_ts = _records_worker_ts(
        app, records_app, record_entities, form_entities, form_routes, auth_enabled=auth_enabled
    )
    drizzle_ts = _records_drizzle_ts(
        record_entities,
        form_entities,
        auth_enabled=auth_enabled,
        stripe_enabled=stripe_enabled,
        webhook_enabled=webhook_enabled,
    )
    owner_guide = (
        _emit_records_policy_owner_guide_md(app, db_name)
        if records_policy_enabled(app)
        else _emit_records_auth_owner_guide_md(app, db_name)
        if auth_enabled
        else _emit_records_owner_guide_md(app, db_name)
    )
    manifest_ts = (
        _emit_manifest_ts(app, design, names)
        if auth_enabled
        else _emit_records_legacy_manifest_ts(app, design, names)
    )
    files = _records_base_files(
        app,
        design,
        db_name,
        db_entity,
        names,
        schema_sql,
        worker_ts,
        drizzle_ts,
        owner_guide,
        manifest_ts,
        auth_enabled=auth_enabled,
    )
    files.update(
        _records_component_files(
            app, names, db_entity, form_entities, form_routes, stripe_enabled=stripe_enabled
        )
    )
    # F5.3: {} when app.seo is None — the no-seo tree is byte-identical.
    files.update(emit_blog_files(app))
    files.update(_seo_files(app))
    return dict(sorted(files.items()))


def _emit_index_app_tsx(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    from .generator import _emit_app_tsx

    return _emit_app_tsx(app, names)


register_primitive(
    PrimitiveDefinition(
        id=RECORDS_PRIMITIVE_ID,
        default_app_spec=default_records_app_spec,
        prepare_app_spec=prepare_records_app_spec,
        generate=generate_records,
        verify=records_verify,
    )
)


__all__ = [
    "RECORDS_PRIMITIVE_ID",
    "default_records_auth_app_spec",
    "default_records_app_spec",
    "generate_records",
    "prepare_records_app_spec",
    "records_verify",
]
