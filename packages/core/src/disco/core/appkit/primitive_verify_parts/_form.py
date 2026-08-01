"""Form primitive: the `form_verify` hook and its check-cluster helpers.

Extracted from ``primitive_verify`` to keep that module's facade under the
module logical-line budget and each check under its own complexity/length
cap. Pure ports: every check name and evidence string is byte-identical to
the original ``form_verify``.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..generator import _table_name, _ts, resolve_lead_entity
from ..primitive_verify import _SCHEMA_RELPATH, _WORKER_RELPATH, _component_path, _result
from ..primitives import PrimitiveVerifyResult, VerifyCheck
from ..spec import AppSpec, DesignSpec, Entity, Section


def _form_sections(app: AppSpec) -> list[tuple[str, Section, Entity]]:
    from ..form_primitive import form_submission_entities_for

    try:
        lead = resolve_lead_entity(app)
        forms = form_submission_entities_for(app, reserved_entities=(lead,))
    except Exception:
        forms = form_submission_entities_for(app)
    by_id = {entity.id: entity for entity in forms}
    out: list[tuple[str, Section, Entity]] = []
    for page in app.pages:
        for section in page.sections:
            if section.kind != "form" or section.content_ref is None:
                continue
            entity = by_id.get(section.content_ref)
            if entity is not None:
                out.append((page.id, section, entity))
    return out


def _form_schema_table_check(
    forms: list[tuple[str, Section, Entity]], schema_sql: str | None
) -> VerifyCheck:
    """The form_schema_table check, pure port of the block of the same name in
    the original `form_verify`."""
    if schema_sql is None:
        return VerifyCheck("form_schema_table", False, "no schema.sql in the workspace.")
    missing_tables = [
        _table_name(entity)
        for _page_id, _section, entity in forms
        if f'CREATE TABLE IF NOT EXISTS "{_table_name(entity)}"' not in schema_sql
    ]
    return VerifyCheck(
        "form_schema_table",
        not missing_tables,
        (
            "schema.sql contains the folded form submissions table(s): "
            + ", ".join(_table_name(entity) for _p, _s, entity in forms)
            if not missing_tables
            else "schema.sql is missing folded form submissions table(s): "
            + ", ".join(missing_tables)
        ),
    )


def _form_worker_validate_route_check(
    app: AppSpec, forms: list[tuple[str, Section, Entity]], worker_ts: str | None
) -> VerifyCheck:
    """The form_worker_validate_route check, pure port of the block of the same
    name in the original `form_verify`."""
    if worker_ts is None:
        return VerifyCheck(
            "form_worker_validate_route", False, "no worker/index.ts in the workspace."
        )

    from ..form_primitive import form_route_for

    missing_routes = [
        form_route_for(app, entity)
        for _page_id, _section, entity in forms
        if _ts(form_route_for(app, entity)) not in worker_ts
    ]
    has_422 = "return json({ error: check.error }, 422);" in worker_ts
    has_validator = "function validateAppForm" in worker_ts and "APP_FORM_ROUTES" in worker_ts
    return VerifyCheck(
        "form_worker_validate_route",
        not missing_routes and has_422 and has_validator,
        (
            "worker/index.ts contains app-form route(s) with validateAppForm and "
            "the 422 validation path."
            if not missing_routes and has_422 and has_validator
            else "; ".join(
                [
                    *(
                        [f"missing form route(s): {', '.join(missing_routes)}"]
                        if missing_routes
                        else []
                    ),
                    *(
                        ["missing validateAppForm/APP_FORM_ROUTES"]
                        if not has_validator
                        else []
                    ),
                    *(["missing 422 validation response"] if not has_422 else []),
                ]
            )
        ),
    )


def _collect_form_section_state(
    app: AppSpec, forms: list[tuple[str, Section, Entity]], tree: Mapping[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Walk the folded form sections and collect missing/stale components plus
    missing success-copy, pure port of the render loop in the original
    `form_verify`."""
    missing_components: list[str] = []
    stale_components: list[str] = []
    missing_success: list[str] = []
    for page_id, section, _entity in forms:
        path = _component_path(app, page_id, section.id)
        src = tree.get(path)
        if src is None:
            missing_components.append(path)
            missing_success.append(section.id)
            continue
        if f'data-appkit-section="{section.id}"' not in src:
            stale_components.append(path)
        success = (
            section.content.success_message
            if section.content is not None and section.content.success_message is not None
            else "Thanks — we will be in touch."
        )
        if _ts(success) not in src and success not in src:
            missing_success.append(section.id)
    return missing_components, stale_components, missing_success


def _form_section_component_check(
    missing_components: list[str], stale_components: list[str]
) -> VerifyCheck:
    """The form_section_component check, pure port of the block of the same
    name in the original `form_verify`."""
    ok = not missing_components and not stale_components
    return VerifyCheck(
        "form_section_component",
        ok,
        (
            "folded form section component(s) exist on their target pages."
            if ok
            else "; ".join(
                [
                    *(
                        ["missing component(s): " + ", ".join(missing_components)]
                        if missing_components
                        else []
                    ),
                    *(
                        ["component(s) missing section marker: " + ", ".join(stale_components)]
                        if stale_components
                        else []
                    ),
                ]
            )
        ),
    )


def _form_success_message_check(missing_success: list[str]) -> VerifyCheck:
    """The form_success_message check, pure port of the block of the same name
    in the original `form_verify`."""
    return VerifyCheck(
        "form_success_message",
        not missing_success,
        (
            "folded form success_message literal(s) are present in the emitted component(s)."
            if not missing_success
            else "success_message literal missing for form section(s): "
            + ", ".join(missing_success)
        ),
    )


def form_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Pure checks for folded form primitive output."""
    del design
    if app is None:
        return _result(
            [
                VerifyCheck(
                    "form_schema_table",
                    False,
                    "no .disco/appspec.json — cannot verify folded form output.",
                ),
                VerifyCheck(
                    "form_worker_validate_route",
                    False,
                    "no .disco/appspec.json — cannot derive folded form routes.",
                ),
                VerifyCheck(
                    "form_section_component",
                    False,
                    "no .disco/appspec.json — cannot locate folded form sections.",
                ),
                VerifyCheck(
                    "form_success_message",
                    False,
                    "no .disco/appspec.json — cannot verify form success copy.",
                ),
            ]
        )

    forms = _form_sections(app)
    if not forms:
        return _result(
            [
                VerifyCheck(
                    "form_schema_table",
                    False,
                    "the AppSpec has no folded form sections with form content_ref entities.",
                ),
                VerifyCheck(
                    "form_worker_validate_route",
                    False,
                    "the AppSpec has no folded form routes to verify.",
                ),
                VerifyCheck(
                    "form_section_component",
                    False,
                    "the AppSpec has no folded form section components to verify.",
                ),
                VerifyCheck(
                    "form_success_message",
                    False,
                    "the AppSpec has no folded form success messages to verify.",
                ),
            ]
        )

    checks: list[VerifyCheck] = []
    schema_sql = tree.get(_SCHEMA_RELPATH)
    checks.append(_form_schema_table_check(forms, schema_sql))

    worker_ts = tree.get(_WORKER_RELPATH)
    checks.append(_form_worker_validate_route_check(app, forms, worker_ts))

    missing_components, stale_components, missing_success = _collect_form_section_state(
        app, forms, tree
    )
    checks.append(_form_section_component_check(missing_components, stale_components))
    checks.append(_form_success_message_check(missing_success))
    return _result(checks)
