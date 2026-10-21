"""Six grounded controls over the canonical specs; no second values store."""

from typing import Any

from disco.core.tweaks import TweakEditor, TweakField, TweakSpec, canonical_hex

from .owner_control_spec import OwnerControlSpec
from .spec import AppSpec, DesignSpec


def seed_owner_controls(
    app: AppSpec, design: DesignSpec, *, palette: tuple[str, ...] | None = None
) -> AppSpec:
    colors = tuple(
        dict.fromkeys(
            canonical_hex(color)
            for color in (
                design.palette.accent or design.palette.primary,
                design.palette.primary,
                *(palette if palette is not None else ("#a14b28", "#246b55", "#265e91")),
            )
        )
    )
    data = app.model_dump(mode="json")
    data["owner_controls"] = OwnerControlSpec(accent_palette=colors).model_dump()
    return AppSpec.model_validate(data)


def owner_tweak_values(app: AppSpec, design: DesignSpec) -> dict[str, Any]:
    if app.owner_controls is None:
        return {}
    return {
        "brand.name": app.name,
        "design.accent": canonical_hex(design.palette.accent or design.palette.primary),
        "design.text": canonical_hex(design.palette.text),
        "layout.density": design.density,
        "layout.width": design.content_width or 1080,
        "motion.reduced": design.reduced_motion or False,
    }


def owner_tweak_spec(app: AppSpec, design: DesignSpec) -> TweakSpec:
    if app.owner_controls is None:
        return TweakSpec()
    if app.app_kind != "lead_gen":
        raise ValueError("Owner controls currently require the lead_gen primitive")
    values = owner_tweak_values(app, design)
    colors = app.owner_controls.accent_palette
    # A later explicit app_set_design may choose another accent. Keep its current
    # value representable without silently changing that deliberate design.
    if values["design.accent"] not in colors:
        colors = (values["design.accent"], *colors[:4])
    fields = (
        TweakField(
            key="brand.name", label="Brand name", editor=TweakEditor.TEXT, controls_behavior=True
        ),
        TweakField(
            key="design.accent",
            label="Accent palette",
            editor=TweakEditor.PALETTE,
            affects=("design.palette.accent",),
            colors=colors,
        ),
        TweakField(
            key="design.text",
            label="Text color",
            editor=TweakEditor.COLOR,
            affects=("body.text", "headings.text"),
        ),
        TweakField(
            key="layout.density",
            label="Spacing",
            editor=TweakEditor.ENUM,
            affects=("design.density",),
            options=tuple(dict.fromkeys((design.density, "compact", "comfortable", "airy"))),
        ),
        TweakField(
            key="layout.width",
            label="Content width",
            editor=TweakEditor.INT,
            affects=("design.content_width",),
            min=640,
            max=1440,
            step=40,
        ),
        TweakField(
            key="motion.reduced",
            label="Reduce motion",
            editor=TweakEditor.BOOLEAN,
            controls_behavior=True,
        ),
    )
    return TweakSpec(
        fields=tuple(
            TweakField.model_validate({**field.model_dump(), "default": values[field.key]})
            for field in fields
        )
    )


def apply_owner_tweak(
    app: AppSpec,
    design: DesignSpec,
    key: str,
    value: Any,
) -> tuple[AppSpec, DesignSpec]:
    value = owner_tweak_spec(app, design).validate_value(key, value)
    app_data, design_data = app.model_dump(mode="json"), design.model_dump(mode="json")
    if key == "brand.name":
        if not value.strip() or len(value) > 120:
            raise ValueError("brand name must contain 1..120 characters")
        app_data["name"] = value
        for page in app_data["pages"]:
            for section in page["sections"]:
                for slot, text in (section.get("content") or {}).items():
                    if text == app.name:
                        section["content"][slot] = value
    elif key in {"design.accent", "design.text"}:
        design_data["palette"][key.split(".")[1]] = value
    else:
        field = {
            "layout.density": "density",
            "layout.width": "content_width",
            "motion.reduced": "reduced_motion",
        }[key]
        design_data[field] = value
    return AppSpec.model_validate(app_data), DesignSpec.model_validate(design_data)
