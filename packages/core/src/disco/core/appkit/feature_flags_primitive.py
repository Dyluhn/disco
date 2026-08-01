"""The AppKit `feature_flags` primitive (Epic 6.2): D1-backed flags for lead-gen apps.

This is an ADD-ON primitive, not a base scaffold. `app_add_primitive` validates a
`FeatureFlagsSpec`, `apply_feature_flags_spec` folds it into the AppSpec as a
stable admin section marker, and the lead-gen generator lowers that marker into:

* a `_flags` D1 table seeded from the declared keys;
* public `GET /api/_flags`, returning only enabled flags;
* owner-gated `POST /api/_flags/toggle`, reusing the lead-gen `ADMIN_TOKEN`
  bearer-token convention and fail-closed `isAuthorized` helper;
* a small React admin section plus a bundled `useFlag(key)` / `flags.isEnabled(key)`
  helper for app code.

Scope: v1 folds only into lead_gen-shaped apps. Directory is static and records owns
its own data/auth surface, so both are refused with guidance rather than silently
emitting a partial flag plane.

This module is the public compatibility/export facade. Cohesive private
implementation (schema/Worker/CSS lowering, the client hook + admin component
emitters, and the verify hook) lives under
:mod:`disco.core.appkit.feature_flags_primitive_parts` and is re-imported here
so every public symbol keeps its original import path.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .primitives import (
    FEATURE_FLAGS_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
    resolve_primitive,
)
from .spec import AppSpec, DesignSpec, Section, SectionContent

_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_MAX_FLAGS = 20
_MAX_KEY = 48
_MAX_DESCRIPTION = 160
_FLAGS_SECTION_ID = "feature_flags"
_FLAGS_CONTENT_REF = "feature_flags"
_FLAGS_TABLE = "_flags"
_FLAGS_GET_ROUTE = "/api/_flags"
_FLAGS_TOGGLE_ROUTE = "/api/_flags/toggle"
_RESERVED_KEYS = frozenset(
    {
        "constructor",
        "prototype",
        "hasownproperty",
        "isprototypeof",
        "propertyisenumerable",
        "tostring",
        "tolocalestring",
        "valueof",
        "proto",
    }
)


class FeatureFlag(BaseModel):
    """One declared feature flag. The key is a bounded lowercase slug that is safe
    as JSON object data, a D1 row value, and a generated TS string literal."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=_MAX_KEY,
        description=(
            "Lowercase slug key (starts with a letter; letters, digits and hyphens only)."
        ),
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_DESCRIPTION,
        description="Optional owner-facing description for the flag.",
    )
    enabled: bool = Field(description="Whether the flag is initially enabled.")

    @field_validator("key")
    @classmethod
    def _key_is_slug(cls, value: str) -> str:
        if not _KEY_RE.match(value):
            raise ValueError(
                "feature flag key must be a lowercase slug "
                f"(pattern {_KEY_RE.pattern!r}), got {value!r}"
            )
        if value in _RESERVED_KEYS:
            raise ValueError(
                f"feature flag key {value!r} is reserved for JS object safety; choose another key"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("feature flag description must be non-empty when given")
        return value


class FeatureFlagsSpec(BaseModel):
    """The `feature_flags` primitive's declarative spec. Unknown keys are refused so
    model recovery gets a precise schema error instead of a silent drop."""

    model_config = ConfigDict(extra="forbid")

    flags: list[FeatureFlag] = Field(
        min_length=1,
        max_length=_MAX_FLAGS,
        description=f"1..{_MAX_FLAGS} feature flags; keys must be unique.",
    )

    @model_validator(mode="after")
    def _keys_unique(self) -> FeatureFlagsSpec:
        seen: set[str] = set()
        for flag in self.flags:
            if flag.key in seen:
                raise ValueError(f"duplicate feature flag key: {flag.key!r}")
            seen.add(flag.key)
        return self


def _flag_item(flag: FeatureFlag) -> str:
    return json.dumps(
        {
            "description": flag.description,
            "enabled": flag.enabled,
            "key": flag.key,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def is_feature_flags_admin_section(section: Section) -> bool:
    """The folded-section marker the generator lowers to the admin component."""
    return section.id == _FLAGS_SECTION_ID and section.content_ref == _FLAGS_CONTENT_REF


def feature_flags_for(app: AppSpec) -> tuple[FeatureFlag, ...]:
    """Read the folded flags back from the AppSpec marker section. Empty tuple means
    the primitive is not applied. Malformed marker content fails hard because the
    generated D1/Worker plane would otherwise drift from the persisted spec."""
    matches = [
        section
        for page in app.pages
        for section in page.sections
        if is_feature_flags_admin_section(section)
    ]
    if not matches:
        return ()
    if len(matches) > 1:
        raise ValueError("feature_flags primitive marker appears more than once")
    content = matches[0].content
    if content is None:
        raise ValueError("feature_flags primitive marker has no content")
    flags: list[FeatureFlag] = []
    for raw in content.items:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("feature_flags marker contains invalid JSON") from exc
        flags.append(FeatureFlag.model_validate(payload))
    FeatureFlagsSpec(flags=flags)
    return tuple(flags)


def apply_feature_flags_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated FeatureFlagsSpec into the AppSpec as one stable admin
    section. Re-applying updates that section in place, so app_add_primitive's
    no-op check naturally catches identical specs."""
    if not isinstance(spec, FeatureFlagsSpec):
        raise TypeError(f"apply_spec for {FEATURE_FLAGS_PRIMITIVE_ID!r} needs a FeatureFlagsSpec")

    base = resolve_primitive(app.app_kind)
    if base.id != LEAD_GEN_PRIMITIVE_ID:
        raise ValueError(
            "the feature_flags primitive currently folds only into lead_gen-shaped "
            f"apps; this app's app_kind {app.app_kind!r} lowers through {base.id!r}. "
            "Use a lead_gen app for v1 flags."
        )
    if not app.pages:
        raise ValueError("the app has no pages to place the feature flags admin section on")

    section = Section(
        id=_FLAGS_SECTION_ID,
        kind="custom",
        content_ref=_FLAGS_CONTENT_REF,
        content=SectionContent(
            heading="Feature flags",
            items=tuple(_flag_item(flag) for flag in spec.flags),
        ),
    )
    section_json = section.model_dump(mode="json")
    data = app.model_dump(mode="json")
    found: list[tuple[int, int, dict[str, object]]] = []
    for page_index, page in enumerate(data["pages"]):
        for section_index, current in enumerate(page["sections"]):
            if current["id"] == _FLAGS_SECTION_ID:
                found.append((page_index, section_index, current))

    if len(found) > 1:
        raise ValueError(
            f"section id {_FLAGS_SECTION_ID!r} appears more than once; cannot fold flags"
        )
    if found:
        page_index, section_index, current = found[0]
        if current.get("kind") != "custom" or current.get("content_ref") != _FLAGS_CONTENT_REF:
            raise ValueError(
                f"section id {_FLAGS_SECTION_ID!r} is already taken by a non-flags "
                "section; choose or move that section before adding feature_flags"
            )
        data["pages"][page_index]["sections"][section_index] = {
            **current,
            "content": section_json["content"],
        }
    else:
        sections = list(data["pages"][0]["sections"])
        insert_at = len(sections)
        if sections and sections[-1].get("kind") == "footer":
            insert_at -= 1
        sections.insert(insert_at, section_json)
        data["pages"][0]["sections"] = sections

    return AppSpec.model_validate(data)


def default_feature_flags_app_spec(name: str, recipe: object) -> AppSpec:
    del name, recipe
    raise ValueError(
        "the 'feature_flags' primitive is an ADD-ON, not a base scaffold: "
        "app_create a lead_gen app first, then "
        "app_add_primitive(primitive_id='feature_flags', spec={...})."
    )


def prepare_feature_flags_app_spec(app: AppSpec) -> AppSpec:
    return app


def generate_feature_flags(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    del app, design
    raise ValueError(
        "the 'feature_flags' primitive does not generate a tree of its own; the "
        "host app's base primitive regenerates from the folded AppSpec."
    )


# ---- private implementation re-imports ---------------------------------------
# The cohesive private helpers live under ``feature_flags_primitive_parts`` and
# are imported here AFTER all public models/constants are defined, so the
# private modules can import those public names back without a circular-
# dependency error. These names are the runtime implementation behind the
# public facade; external callers must continue to import them from
# ``disco.core.appkit.feature_flags_primitive``.

from .feature_flags_primitive_parts._emit import (  # noqa: E402
    emit_feature_flags_admin_component,
    emit_feature_flags_hook_ts,
)
from .feature_flags_primitive_parts._lowering import (  # noqa: E402
    lower_feature_flags_drizzle_ts,
    lower_feature_flags_schema_sql,
    lower_feature_flags_styles_css,
    lower_feature_flags_worker_ts,
)
from .feature_flags_primitive_parts._verify import feature_flags_verify  # noqa: E402

register_primitive(
    PrimitiveDefinition(
        id=FEATURE_FLAGS_PRIMITIVE_ID,
        default_app_spec=default_feature_flags_app_spec,
        prepare_app_spec=prepare_feature_flags_app_spec,
        generate=generate_feature_flags,
        tier="fillable",
        host_contract=(),
        spec_schema=FeatureFlagsSpec,
        verify=feature_flags_verify,
        apply_spec=apply_feature_flags_spec,
    )
)


__all__ = [
    "FEATURE_FLAGS_PRIMITIVE_ID",
    "FeatureFlag",
    "FeatureFlagsSpec",
    "apply_feature_flags_spec",
    "emit_feature_flags_admin_component",
    "emit_feature_flags_hook_ts",
    "feature_flags_for",
    "feature_flags_verify",
    "generate_feature_flags",
    "is_feature_flags_admin_section",
    "lower_feature_flags_drizzle_ts",
    "lower_feature_flags_schema_sql",
    "lower_feature_flags_styles_css",
    "lower_feature_flags_worker_ts",
    "prepare_feature_flags_app_spec",
]
