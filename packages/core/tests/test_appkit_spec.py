"""AppKit EPIC C — AppSpec/DesignSpec models + `.disco/` JSON IO.

Proves: round-trip to a temp workspace `.disco/`, the structural invariants
(schema_version, absolute routes, unique page/section/entity/action ids), the
DesignSpec invariants (hex colors, real fonts, substantive justification reasons),
`extra="forbid"`, and that loading a missing spec raises a clear error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from disco.core.appkit import (
    Action,
    DesignSpec,
    Entity,
    EntityField,
    Justification,
    appspec_path,
    designspec_path,
    load_app_spec,
    load_design_spec,
    load_specs,
    save_app_spec,
    save_design_spec,
)
from disco.core.appkit.spec import AppSpec
from pydantic import ValidationError


def _valid_app_spec() -> AppSpec:
    return AppSpec.model_validate(
        {
            "schema_version": 1,
            "app_kind": "web_app",
            "name": "Task Tracker",
            "pages": [
                {
                    "id": "home",
                    "route": "/",
                    "title": "Home",
                    "sections": [
                        {"id": "hero", "kind": "hero", "content_ref": None},
                        {"id": "feat", "kind": "features"},
                    ],
                },
                {
                    "id": "tasks",
                    "route": "/tasks",
                    "title": "Tasks",
                    "sections": [{"id": "list", "kind": "table", "content_ref": "task"}],
                },
            ],
            "entities": [
                {
                    "id": "task",
                    "name": "Task",
                    "fields": [
                        {"name": "title", "type": "str", "required": True},
                        {"name": "done", "type": "bool", "required": False},
                        {"name": "due", "type": "datetime", "required": False},
                    ],
                }
            ],
            "primary_actions": [
                {"id": "create_task", "label": "New task", "type": "nav", "target": "/tasks"},
                {"id": "complete_task", "label": "Complete", "type": "submit", "target": "task"},
            ],
        }
    )


def _valid_design_spec() -> DesignSpec:
    return DesignSpec.model_validate(
        {
            "schema_version": 1,
            "typography": {"heading_font": "Inter", "body_font": "Inter"},
            "palette": {
                "primary": "#2563eb",
                "surface": "#ffffff",
                "text": "#0f172a",
                "accent": "#f59e0b",
            },
            "layout_family": "sidebar",
            "component_style": "soft",
            "density": "comfortable",
            "justifications": [
                {
                    "choice": "sidebar layout",
                    "reason": "the app has many top-level sections that need persistent nav",
                }
            ],
        }
    )


# ---- round trips --------------------------------------------------------------


def test_app_spec_round_trip(tmp_path: Path) -> None:
    spec = _valid_app_spec()
    path = save_app_spec(tmp_path, spec)
    assert path == appspec_path(tmp_path)
    assert path == tmp_path / ".disco" / "appspec.json"
    assert path.exists()
    assert load_app_spec(tmp_path) == spec


def test_design_spec_round_trip(tmp_path: Path) -> None:
    spec = _valid_design_spec()
    path = save_design_spec(tmp_path, spec)
    assert path == designspec_path(tmp_path)
    assert path == tmp_path / ".disco" / "designspec.json"
    assert load_design_spec(tmp_path) == spec


def test_load_specs_returns_both(tmp_path: Path) -> None:
    app = _valid_app_spec()
    design = _valid_design_spec()
    _ = save_app_spec(tmp_path, app)
    _ = save_design_spec(tmp_path, design)
    loaded_app, loaded_design = load_specs(tmp_path)
    assert loaded_app == app
    assert loaded_design == design


def test_save_creates_disco_dir(tmp_path: Path) -> None:
    assert not (tmp_path / ".disco").exists()
    _ = save_app_spec(tmp_path, _valid_app_spec())
    assert (tmp_path / ".disco").is_dir()


def test_design_justifications_round_trip(tmp_path: Path) -> None:
    base = _valid_design_spec()
    # Models are frozen, so build a NEW spec with an extra justification rather
    # than mutate in place.
    # Collection fields are tuples; model_copy(update=) skips validation, so pass a
    # tuple here (a list would round-trip back as a tuple and break the equality).
    spec = base.model_copy(
        update={
            "justifications": (
                *base.justifications,
                Justification(choice="amber accent", reason="brand color matches the client logo"),
            )
        }
    )
    _ = save_design_spec(tmp_path, spec)
    assert load_design_spec(tmp_path).justifications == spec.justifications


# ---- AppSpec validation -------------------------------------------------------


def test_rejects_bad_schema_version() -> None:
    data = _valid_app_spec().model_dump()
    data["schema_version"] = 0
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_rejects_non_absolute_route() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][0]["route"] = "tasks"
    with pytest.raises(ValidationError, match="route must start with"):
        AppSpec.model_validate(data)


def test_rejects_duplicate_page_ids() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][1]["id"] = data["pages"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate page id"):
        AppSpec.model_validate(data)


def test_rejects_duplicate_section_ids() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][0]["sections"][1]["id"] = data["pages"][0]["sections"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate section id"):
        AppSpec.model_validate(data)


def test_rejects_duplicate_entity_ids() -> None:
    data = _valid_app_spec().model_dump()
    entity_copy = dict(data["entities"][0])
    data["entities"] = [*data["entities"], entity_copy]
    with pytest.raises(ValidationError, match="duplicate entity id"):
        AppSpec.model_validate(data)


def test_rejects_duplicate_primary_actions() -> None:
    data = _valid_app_spec().model_dump()
    data["primary_actions"][1]["id"] = data["primary_actions"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate primary action"):
        AppSpec.model_validate(data)


def test_app_spec_forbids_unknown_fields() -> None:
    data = _valid_app_spec().model_dump()
    data["surprise"] = "nope"
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_page_forbids_unknown_fields() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][0]["layout"] = "grid"
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


# ---- DesignSpec validation ----------------------------------------------------


def test_rejects_bad_hex_color() -> None:
    data = _valid_design_spec().model_dump()
    data["palette"]["primary"] = "blue"
    with pytest.raises(ValidationError, match="hex color"):
        DesignSpec.model_validate(data)


def test_accepts_short_hex() -> None:
    data = _valid_design_spec().model_dump()
    data["palette"]["primary"] = "#abc"
    assert DesignSpec.model_validate(data).palette.primary == "#abc"


def test_optional_accent_may_be_null() -> None:
    data = _valid_design_spec().model_dump()
    data["palette"]["accent"] = None
    assert DesignSpec.model_validate(data).palette.accent is None


def test_rejects_bad_accent_hex() -> None:
    data = _valid_design_spec().model_dump()
    data["palette"]["accent"] = "#xyz"
    with pytest.raises(ValidationError, match="hex"):
        DesignSpec.model_validate(data)


def test_rejects_default_font() -> None:
    data = _valid_design_spec().model_dump()
    data["typography"]["heading_font"] = "default"
    with pytest.raises(ValidationError, match="real declared family"):
        DesignSpec.model_validate(data)


def test_rejects_empty_justification_reason() -> None:
    data = _valid_design_spec().model_dump()
    data["justifications"][0]["reason"] = ""
    with pytest.raises(ValidationError, match="substantive"):
        DesignSpec.model_validate(data)


def test_rejects_too_short_justification_reason() -> None:
    data = _valid_design_spec().model_dump()
    data["justifications"][0]["reason"] = "tiny"
    with pytest.raises(ValidationError, match="substantive"):
        DesignSpec.model_validate(data)


def test_design_spec_forbids_unknown_fields() -> None:
    data = _valid_design_spec().model_dump()
    data["extra"] = "x"
    with pytest.raises(ValidationError):
        DesignSpec.model_validate(data)


# ---- load errors --------------------------------------------------------------


def test_load_missing_app_spec_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no AppSpec"):
        load_app_spec(tmp_path)


def test_load_missing_design_spec_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no DesignSpec"):
        load_design_spec(tmp_path)


def test_load_malformed_json_raises_value_error(tmp_path: Path) -> None:
    path = appspec_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_app_spec(tmp_path)


# ---- P1-1: bounded loaders (size cap + list max_length) -----------------------


def test_oversized_app_spec_rejected_before_parse(tmp_path: Path) -> None:
    path = appspec_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 300 KiB > 256 KiB cap; refused on stat() before json.loads even runs.
    _ = path.write_text("x" * (300 * 1024), encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        load_app_spec(tmp_path)


def test_oversized_design_spec_rejected_before_parse(tmp_path: Path) -> None:
    path = designspec_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("x" * (300 * 1024), encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        load_design_spec(tmp_path)


def test_rejects_too_many_pages() -> None:
    data = _valid_app_spec().model_dump()
    template = data["pages"][0]
    data["pages"] = [{**template, "id": f"p{i}", "route": f"/p{i}"} for i in range(51)]
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_rejects_too_many_sections() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][0]["sections"] = [{"id": f"s{i}", "kind": "custom"} for i in range(31)]
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_rejects_too_many_entity_fields() -> None:
    data = _valid_app_spec().model_dump()
    data["entities"][0]["fields"] = [{"name": f"f{i}", "type": "str"} for i in range(61)]
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


# ---- P1: entity field names are SAFE identifiers ------------------------------


def test_entity_field_name_must_be_snake_case_identifier() -> None:
    # a field name is emitted as a SQL column + TSX/JS key, so it must be a safe
    # snake_case identifier; anything else is rejected at the spec boundary.
    for bad in ("first-name", "First", "1name", "name!", "drop table", "na me", ""):
        with pytest.raises(ValidationError):
            EntityField(name=bad, type="str")


def test_entity_field_name_accepts_valid_snake_case() -> None:
    for ok in ("first_name", "email", "headcount2", "a"):
        field = EntityField(name=ok, type="str")
        assert field.name == ok
    # and a valid field round-trips inside an Entity
    entity = Entity(
        id="lead",
        name="Lead",
        fields=(EntityField(name="first_name", type="str", required=True),),
    )
    assert entity.fields[0].name == "first_name"


def test_rejects_too_many_actions() -> None:
    data = _valid_app_spec().model_dump()
    data["primary_actions"] = [
        {"id": f"a{i}", "label": "x", "type": "submit", "target": "task"} for i in range(31)
    ]
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_rejects_too_many_justifications() -> None:
    data = _valid_design_spec().model_dump()
    data["justifications"] = [
        {"choice": f"c{i}", "reason": "a substantive reason here"} for i in range(51)
    ]
    with pytest.raises(ValidationError):
        DesignSpec.model_validate(data)


# ---- P1-2: frozen models (no post-validation mutation) ------------------------


def test_app_spec_is_frozen() -> None:
    spec = _valid_app_spec()
    with pytest.raises(ValidationError):
        spec.name = "tampered"  # type: ignore[misc]


def test_nested_model_is_frozen() -> None:
    spec = _valid_app_spec()
    with pytest.raises(ValidationError):
        spec.pages[0].route = "/hacked"  # type: ignore[misc]


def test_design_spec_is_frozen() -> None:
    spec = _valid_design_spec()
    with pytest.raises(ValidationError):
        spec.palette.primary = "not-a-hex"  # type: ignore[misc]


# ---- P1-2b: collections are immutable (tuples, not lists) ---------------------
#
# `frozen=True` only blocks attribute reassignment, NOT in-place value mutation.
# The collection fields are `tuple[...]`, so a validated spec can't be mutated in
# place to smuggle a duplicate id/route past the cross-field validators and onto
# disk via `save_*`.


def test_collection_fields_are_tuples() -> None:
    spec = _valid_app_spec()
    assert isinstance(spec.pages, tuple)
    assert isinstance(spec.entities, tuple)
    assert isinstance(spec.primary_actions, tuple)
    assert isinstance(spec.pages[0].sections, tuple)
    assert isinstance(spec.entities[0].fields, tuple)
    assert isinstance(_valid_design_spec().justifications, tuple)


def test_pages_cannot_be_appended() -> None:
    spec = _valid_app_spec()
    with pytest.raises(AttributeError):
        spec.pages.append(spec.pages[0])  # type: ignore[attr-defined]


def test_pages_cannot_be_item_assigned() -> None:
    spec = _valid_app_spec()
    # A duplicate-route page would pass per-Page validation; in-place item
    # assignment would bypass the cross-field unique-route validator. Tuples make
    # it impossible at the language level.
    dupe = spec.pages[1].model_copy(update={"route": spec.pages[0].route})
    with pytest.raises(TypeError):
        spec.pages[0] = dupe  # type: ignore[index]


def test_sections_cannot_be_cleared() -> None:
    spec = _valid_app_spec()
    with pytest.raises(AttributeError):
        spec.pages[0].sections.clear()  # type: ignore[attr-defined]


def test_entities_and_actions_are_immutable() -> None:
    spec = _valid_app_spec()
    with pytest.raises(AttributeError):
        spec.entities.append(spec.entities[0])  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        spec.entities[0].fields.append(spec.entities[0].fields[0])  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        spec.primary_actions[0] = spec.primary_actions[1]  # type: ignore[index]


def test_justifications_are_immutable() -> None:
    spec = _valid_design_spec()
    with pytest.raises(AttributeError):
        spec.justifications.append(spec.justifications[0])  # type: ignore[attr-defined]


def test_tampering_cannot_smuggle_duplicate_route_to_disk(tmp_path: Path) -> None:
    # Full attack: validate a clean spec, try to mutate in a duplicate route, save,
    # and reload. The mutation must fail outright (tuples are immutable), so the
    # persisted spec stays valid and reloads with its original distinct routes.
    spec = _valid_app_spec()
    dupe = spec.pages[1].model_copy(update={"route": spec.pages[0].route})
    with pytest.raises((TypeError, AttributeError)):
        spec.pages[0] = dupe  # type: ignore[index]
    _ = save_app_spec(tmp_path, spec)
    reloaded = load_app_spec(tmp_path)
    routes = [p.route for p in reloaded.pages]
    assert len(routes) == len(set(routes))  # still unique
    assert reloaded == spec


# ---- P1-3: strict + unique routes ---------------------------------------------


def test_rejects_duplicate_routes() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][1]["route"] = data["pages"][0]["route"]
    with pytest.raises(ValidationError, match="duplicate route"):
        AppSpec.model_validate(data)


def test_rejects_route_with_spaces() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][1]["route"] = "/my tasks"
    with pytest.raises(ValidationError, match="only letters"):
        AppSpec.model_validate(data)


def test_rejects_route_with_dotdot() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][1]["route"] = "/../etc"
    with pytest.raises(ValidationError, match="'\\.\\.'"):
        AppSpec.model_validate(data)


def test_rejects_route_with_double_slash() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][1]["route"] = "/tasks//active"
    with pytest.raises(ValidationError, match="'//'"):
        AppSpec.model_validate(data)


def test_accepts_valid_nested_route() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][1]["route"] = "/tasks/active_items-2"
    assert AppSpec.model_validate(data).pages[1].route == "/tasks/active_items-2"


# ---- P1-4: structured Actions + constrained Section.kind ----------------------


def test_structured_action_round_trips(tmp_path: Path) -> None:
    spec = _valid_app_spec()
    _ = save_app_spec(tmp_path, spec)
    loaded = load_app_spec(tmp_path)
    assert loaded == spec
    assert loaded.primary_actions[0] == Action(
        id="create_task", label="New task", type="nav", target="/tasks"
    )


def test_rejects_bad_action_type() -> None:
    data = _valid_app_spec().model_dump()
    data["primary_actions"][0]["type"] = "teleport"
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_rejects_nav_action_with_non_route_target() -> None:
    data = _valid_app_spec().model_dump()
    data["primary_actions"] = list(data["primary_actions"])
    data["primary_actions"][0] = {
        "id": "go",
        "label": "Go",
        "type": "nav",
        "target": "tasks",  # missing leading '/'
    }
    with pytest.raises(ValidationError, match="route starting with"):
        AppSpec.model_validate(data)


def test_rejects_external_action_with_non_url_target() -> None:
    data = _valid_app_spec().model_dump()
    data["primary_actions"] = list(data["primary_actions"])
    data["primary_actions"][0] = {
        "id": "docs",
        "label": "Docs",
        "type": "external",
        "target": "/local",  # not an http(s) URL
    }
    with pytest.raises(ValidationError, match="http"):
        AppSpec.model_validate(data)


def test_accepts_external_action_with_url() -> None:
    data = _valid_app_spec().model_dump()
    data["primary_actions"] = list(data["primary_actions"])
    data["primary_actions"][0] = {
        "id": "docs",
        "label": "Docs",
        "type": "external",
        "target": "https://example.com/docs",
    }
    assert AppSpec.model_validate(data).primary_actions[0].target == ("https://example.com/docs")


# ---- EPIC E schema additions: Section.variant_id + Section.content ------------


def test_section_variant_id_and_content_round_trip(tmp_path: Path) -> None:
    from disco.core.appkit import Section, SectionContent

    section = Section.model_validate(
        {
            "id": "hero",
            "kind": "hero",
            "variant_id": "hero.split-media-right",
            "content": {
                "heading": "Welcome",
                "subheading": "Tagline",
                "body": "Body copy",
                "cta_label": "Start",
                "items": ["a", "b"],
            },
        }
    )
    assert section.variant_id == "hero.split-media-right"
    assert section.content is not None
    assert section.content.heading == "Welcome"
    assert section.content.items == ("a", "b")  # coerced to a tuple (immutable)
    assert isinstance(section.content, SectionContent)


def test_section_new_fields_default_to_none() -> None:
    # backward-compatible: a section with neither new field still validates.
    data = _valid_app_spec().model_dump()
    sec = data["pages"][0]["sections"][0]
    assert sec.get("variant_id") is None
    assert sec.get("content") is None


def test_section_content_is_bounded() -> None:
    from disco.core.appkit import SectionContent

    with pytest.raises(ValidationError):
        SectionContent.model_validate({"heading": "x" * 5000})  # > heading cap
    with pytest.raises(ValidationError):
        SectionContent.model_validate({"items": ["ok", "y" * 5000]})  # item too long


def test_section_content_forbids_unknown_fields() -> None:
    from disco.core.appkit import SectionContent

    with pytest.raises(ValidationError):
        SectionContent.model_validate({"heading": "ok", "surprise": "no"})


def test_section_content_is_frozen() -> None:
    from disco.core.appkit import SectionContent

    content = SectionContent(heading="x")
    with pytest.raises(ValidationError):
        content.heading = "y"  # type: ignore[misc]


def test_app_spec_with_new_section_fields_round_trips(tmp_path: Path) -> None:
    data = _valid_app_spec().model_dump(mode="json")
    data["pages"][0]["sections"][0]["variant_id"] = "hero.centered-stacked"
    data["pages"][0]["sections"][0]["content"] = {"heading": "Hi", "items": ["one"]}
    spec = AppSpec.model_validate(data)
    _ = save_app_spec(tmp_path, spec)
    assert load_app_spec(tmp_path) == spec


def test_rejects_unknown_section_kind() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][0]["sections"][0]["kind"] = "carousel"
    with pytest.raises(ValidationError):
        AppSpec.model_validate(data)


def test_accepts_custom_section_kind() -> None:
    data = _valid_app_spec().model_dump()
    data["pages"][0]["sections"][0]["kind"] = "custom"
    assert AppSpec.model_validate(data).pages[0].sections[0].kind == "custom"


# ---- P1-5: save_* revalidates (no construction path can persist an invalid spec) -
#
# `frozen=True` + tuple fields block in-place mutation, but NON-construction paths
# (`model_copy(update=...)`, `model_construct(...)`, direct `__dict__` edits) build a
# model that SKIPPED the field + cross-field validators. `save_*` re-runs
# `model_validate` on the model's own dump before writing, so a tampered spec is
# REJECTED at save (ValidationError) and never lands on disk.


def test_save_app_spec_rejects_model_copy_duplicate_route(tmp_path: Path) -> None:
    spec = _valid_app_spec()
    # model_copy(update=) skips validation: build a duplicate-route spec.
    dupe_page = spec.pages[1].model_copy(update={"route": spec.pages[0].route})
    tampered = spec.model_copy(update={"pages": (spec.pages[0], dupe_page)})
    with pytest.raises(ValidationError, match="duplicate route"):
        _ = save_app_spec(tmp_path, tampered)
    # Nothing was written: `.disco/` isn't even created (mkdir runs after validation).
    assert not appspec_path(tmp_path).exists()
    assert not (tmp_path / ".disco").exists()


def test_save_app_spec_rejects_model_copy_duplicate_id(tmp_path: Path) -> None:
    spec = _valid_app_spec()
    dupe_page = spec.pages[1].model_copy(update={"id": spec.pages[0].id})
    tampered = spec.model_copy(update={"pages": (spec.pages[0], dupe_page)})
    with pytest.raises(ValidationError, match="duplicate page id"):
        _ = save_app_spec(tmp_path, tampered)
    assert not appspec_path(tmp_path).exists()


def test_save_app_spec_rejects_model_copy_bad_route(tmp_path: Path) -> None:
    spec = _valid_app_spec()
    bad_page = spec.pages[1].model_copy(update={"route": "no-leading-slash"})
    tampered = spec.model_copy(update={"pages": (spec.pages[0], bad_page)})
    with pytest.raises(ValidationError, match="route must start with"):
        _ = save_app_spec(tmp_path, tampered)
    assert not appspec_path(tmp_path).exists()


def test_save_app_spec_rejects_model_copy_over_cap_list(tmp_path: Path) -> None:
    spec = _valid_app_spec()
    template = spec.pages[0]
    # 51 > _MAX_PAGES (50): the max_length list cap re-fires on revalidation.
    over_cap = tuple(
        template.model_copy(update={"id": f"p{i}", "route": f"/p{i}"}) for i in range(51)
    )
    tampered = spec.model_copy(update={"pages": over_cap})
    with pytest.raises(ValidationError):
        _ = save_app_spec(tmp_path, tampered)
    assert not appspec_path(tmp_path).exists()


def test_save_app_spec_does_not_clobber_existing_on_tamper(tmp_path: Path) -> None:
    # A valid spec is on disk; a later tampered save must be rejected and must NOT
    # overwrite the previously-persisted valid file.
    good = _valid_app_spec()
    _ = save_app_spec(tmp_path, good)
    original = appspec_path(tmp_path).read_text(encoding="utf-8")
    dupe_page = good.pages[1].model_copy(update={"route": good.pages[0].route})
    tampered = good.model_copy(update={"pages": (good.pages[0], dupe_page)})
    with pytest.raises(ValidationError, match="duplicate route"):
        _ = save_app_spec(tmp_path, tampered)
    assert appspec_path(tmp_path).read_text(encoding="utf-8") == original
    assert load_app_spec(tmp_path) == good


def test_save_design_spec_rejects_model_copy_short_justification(tmp_path: Path) -> None:
    spec = _valid_design_spec()
    bad_just = spec.justifications[0].model_copy(update={"reason": "tiny"})
    tampered = spec.model_copy(update={"justifications": (bad_just,)})
    with pytest.raises(ValidationError, match="substantive"):
        _ = save_design_spec(tmp_path, tampered)
    assert not designspec_path(tmp_path).exists()


def test_save_design_spec_rejects_model_copy_bad_hex(tmp_path: Path) -> None:
    spec = _valid_design_spec()
    bad_palette = spec.palette.model_copy(update={"primary": "blue"})
    tampered = spec.model_copy(update={"palette": bad_palette})
    with pytest.raises(ValidationError, match="hex color"):
        _ = save_design_spec(tmp_path, tampered)
    assert not designspec_path(tmp_path).exists()


def test_save_still_persists_valid_spec_after_revalidation(tmp_path: Path) -> None:
    # The revalidation gate must NOT break the happy path: a valid spec still saves
    # and round-trips unchanged through both helpers.
    app = _valid_app_spec()
    design = _valid_design_spec()
    _ = save_app_spec(tmp_path, app)
    _ = save_design_spec(tmp_path, design)
    assert load_app_spec(tmp_path) == app
    assert load_design_spec(tmp_path) == design


def test_save_app_spec_rejects_wrong_spec_type(tmp_path: Path) -> None:
    """save_app_spec validates against AppSpec (the destination), not type(model):
    handing it a valid DesignSpec must be REJECTED, not written to appspec.json."""
    from disco.core.appkit.spec import appspec_path, save_app_spec

    design = _valid_design_spec()
    with pytest.raises(ValidationError):
        save_app_spec(tmp_path, design)  # type: ignore[arg-type]
    assert not appspec_path(tmp_path).exists()


def test_save_rejects_oversized_serialized_spec(tmp_path: Path) -> None:
    """A schema-valid spec that serializes beyond the loader's byte cap is rejected at
    save (write/read symmetry) rather than stranding an unreadable file on disk."""
    from disco.core.appkit.spec import (
        _MAX_BODY,
        _MAX_ITEM,
        _MAX_ITEMS,
        _MAX_SPEC_BYTES,
        AppSpec,
        Page,
        Section,
        SectionContent,
        appspec_path,
        save_app_spec,
    )

    # Build a valid AppSpec whose JSON exceeds the cap. Every free-form string field is
    # now length-capped (see the bounded-str aliases), so we inflate via the section
    # CONTENT slots — bounded individually (body <= _MAX_BODY, items <= _MAX_ITEMS each
    # <= _MAX_ITEM) but large in aggregate across many sections/pages.
    big_content = SectionContent(
        body="x" * _MAX_BODY,
        items=tuple("x" * _MAX_ITEM for _ in range(_MAX_ITEMS)),
    )
    sections = tuple(Section(id=f"s{i}", kind="custom", content=big_content) for i in range(30))
    pages = tuple(Page(id=f"p{i}", route=f"/p{i}", title="t", sections=sections) for i in range(50))
    spec = AppSpec(schema_version=1, app_kind="web_app", name="big", pages=pages)
    assert len(spec.model_dump_json().encode("utf-8")) > _MAX_SPEC_BYTES
    with pytest.raises(ValueError, match="too large"):
        save_app_spec(tmp_path, spec)
    assert not appspec_path(tmp_path).exists()


def test_entity_field_name_rejects_reserved_columns() -> None:
    """A field named like an implicit generated column (id/created_at/rowid) would
    produce a duplicate-column → invalid DDL; the spec must reject it."""
    from disco.core.appkit.spec import EntityField

    for bad in ("id", "created_at", "rowid"):
        with pytest.raises(ValidationError):
            EntityField(name=bad, type="str")
    # a normal name still works
    assert EntityField(name="email", type="str").name == "email"


def test_entity_field_name_rejects_js_prototype_keys() -> None:
    """A field name is emitted VERBATIM as an object key in the generated TSX
    (form-state + content objects). Lowercase Object.prototype / reserved identifiers
    pass `_IDENT_RE` (leading letter) but pollute/shadow the prototype → broken
    generated JS, so the spec must reject them."""
    from disco.core.appkit.spec import EntityField

    for bad in (
        "constructor",
        "prototype",
        "hasownproperty",
        "isprototypeof",
        "propertyisenumerable",
        "tostring",
        "tolocalestring",
        "valueof",
        "proto",
    ):
        with pytest.raises(ValidationError):
            EntityField(name=bad, type="str")
    # `__proto__` is already rejected by the pattern itself (leading underscore — the
    # identifier regex requires a leading LETTER), NOT by the reserved-name set.
    with pytest.raises(ValidationError):
        EntityField(name="__proto__", type="str")
    # normal names that merely CONTAIN a reserved substring still pass
    for ok in ("constructor_type", "valuation", "prototype_id", "email"):
        assert EntityField(name=ok, type="str").name == ok


# ---- bounded string-length caps ----------------------------------------------


def test_rejects_overlong_page_id() -> None:
    """An unbounded page.id could drive the generator into an over-NAME_MAX component
    filename; the spec caps every free-form string. A 300-char id is rejected."""
    from disco.core.appkit.spec import _ID_MAX, Page

    Page(id="a" * _ID_MAX, route="/x", title="t")  # exactly at the cap is fine
    with pytest.raises(ValidationError):
        Page(id="a" * 300, route="/x", title="t")


def test_rejects_overlong_section_id() -> None:
    from disco.core.appkit.spec import _ID_MAX, Section

    Section(id="a" * _ID_MAX, kind="custom")  # at the cap is fine
    with pytest.raises(ValidationError):
        Section(id="a" * 300, kind="custom")


def test_every_capped_spec_string_field_rejects_over_cap_and_accepts_normal() -> None:
    """Each free-form string field rejects over-cap input but accepts a normal value.

    Drives the exact cap of each `_*Str` alias: a string one char over the cap must
    raise, and a short normal value must validate."""
    from disco.core.appkit.spec import (
        Action,
        AppSpec,
        DesignSpec,
        Entity,
        EntityField,
        Justification,
        Page,
        Palette,
        Section,
        Typography,
    )

    # (constructor, kwargs that put an over-cap string in the field under test)
    over = "a" * 4001  # comfortably over every cap below

    # Section.id / content_ref / variant_id  (_IdStr = 64)
    with pytest.raises(ValidationError):
        Section(id=over, kind="custom")
    with pytest.raises(ValidationError):
        Section(id="s", kind="custom", content_ref=over)
    with pytest.raises(ValidationError):
        Section(id="s", kind="custom", variant_id=over)
    assert Section(id="s", kind="custom", content_ref="task", variant_id="v1").id == "s"

    # Page.id / route / title
    with pytest.raises(ValidationError):
        Page(id=over, route="/x", title="t")
    with pytest.raises(ValidationError):
        Page(id="p", route="/" + "a" * 300, title="t")
    with pytest.raises(ValidationError):
        Page(id="p", route="/x", title=over)
    assert Page(id="p", route="/x", title="Home").id == "p"

    # EntityField.name (still _IDENT_RE-validated) + type
    with pytest.raises(ValidationError):
        EntityField(name="a" * 65, type="str")  # snake_case but over _IdStr cap
    with pytest.raises(ValidationError):
        EntityField(name="ok", type=over)
    assert EntityField(name="title", type="str").name == "title"

    # Entity.id / name
    with pytest.raises(ValidationError):
        Entity(id=over, name="X")
    with pytest.raises(ValidationError):
        Entity(id="e", name=over)
    assert Entity(id="e", name="Task").id == "e"

    # Action.id / label / target
    with pytest.raises(ValidationError):
        Action(id=over, label="L", type="submit", target="task")
    with pytest.raises(ValidationError):
        Action(id="a", label=over, type="submit", target="task")
    with pytest.raises(ValidationError):
        Action(id="a", label="L", type="submit", target=over)
    assert Action(id="a", label="L", type="submit", target="task").id == "a"

    # AppSpec.app_kind / name
    with pytest.raises(ValidationError):
        AppSpec(schema_version=1, app_kind=over, name="n")
    with pytest.raises(ValidationError):
        AppSpec(schema_version=1, app_kind="web_app", name=over)
    assert AppSpec(schema_version=1, app_kind="web_app", name="App").name == "App"

    # Typography.heading_font / body_font (still _FONT_RE-validated)
    with pytest.raises(ValidationError):
        Typography(heading_font="a" * 121, body_font="Inter")
    with pytest.raises(ValidationError):
        Typography(heading_font="Inter", body_font="a" * 121)
    assert Typography(heading_font="Inter", body_font="Inter").body_font == "Inter"

    # Palette hex fields (also hex-validated; the cap is belt-and-suspenders)
    with pytest.raises(ValidationError):
        Palette(primary="#" + "a" * 8, surface="#ffffff", text="#000000")
    assert Palette(primary="#2563eb", surface="#ffffff", text="#0f172a").text == "#0f172a"

    # Justification.choice / reason (reason also has a MIN length)
    with pytest.raises(ValidationError):
        Justification(choice="a" * 121, reason="a substantive reason here")
    with pytest.raises(ValidationError):
        Justification(choice="sidebar", reason="a" * 2001)
    assert Justification(choice="sidebar", reason="a substantive reason here").choice == "sidebar"

    # DesignSpec.layout_family / component_style / density
    base_typo = Typography(heading_font="Inter", body_font="Inter")
    base_pal = Palette(primary="#2563eb", surface="#ffffff", text="#0f172a")
    for bad in ("layout_family", "component_style", "density"):
        kwargs = {"layout_family": "sidebar", "component_style": "soft", "density": "comfortable"}
        kwargs[bad] = "a" * 121
        with pytest.raises(ValidationError):
            DesignSpec(schema_version=1, typography=base_typo, palette=base_pal, **kwargs)
    assert (
        DesignSpec(
            schema_version=1,
            typography=base_typo,
            palette=base_pal,
            layout_family="sidebar",
            component_style="soft",
            density="comfortable",
        ).density
        == "comfortable"
    )


def test_worst_case_max_length_ids_yield_bounded_component_paths() -> None:
    """CRITICAL GUARANTEE: an AppSpec built at the id cap can NEVER generate a path
    over the filesystem NAME_MAX. Construct a spec whose page.id AND section.id are
    BOTH at `_ID_MAX` (and start with a digit, so `_pascal` prepends its growth "X"),
    generate the tree, and assert every emitted path — and every path segment — is
    safely under 255 bytes."""
    from disco.core.appkit import generate
    from disco.core.appkit.spec import _ID_MAX, AppSpec, Page, Section

    # ids EXACTLY at the cap, unique, and DIGIT-LEADING so `_pascal` adds its "X"
    # (the +1 growth) — the genuine worst case for filename length.
    def _capped_id(i: int) -> str:
        suffix = str(i)
        return suffix + "0" * (_ID_MAX - len(suffix))  # digit-leading, exactly _ID_MAX chars

    sections = tuple(
        Section(id=_capped_id(1000 + j), kind="custom") for j in range(_MAX_SECTIONS_FOR_TEST)
    )
    pages = tuple(
        Page(id=_capped_id(i), route=f"/p{i}", title="t", sections=sections)
        for i in range(_MAX_PAGES_FOR_TEST)
    )
    spec = AppSpec(schema_version=1, app_kind="web_app", name="worst case", pages=pages)
    assert all(len(p.id) == _ID_MAX for p in spec.pages)

    design = _valid_design_spec()
    files = generate(spec, design)
    assert files  # generated something
    for path in files:
        # full relative path must be well under NAME_MAX
        assert len(path.encode("utf-8")) < 255, (len(path), path)
        # and so must every individual path SEGMENT (the actual NAME_MAX subject)
        for segment in path.split("/"):
            assert len(segment.encode("utf-8")) < 255, (len(segment), segment)


# Small dimensions for the worst-case-path test: enough sections to exercise the
# collision-disambiguation suffix without building the full 50x30 max tree.
_MAX_PAGES_FOR_TEST = 3
_MAX_SECTIONS_FOR_TEST = 5
