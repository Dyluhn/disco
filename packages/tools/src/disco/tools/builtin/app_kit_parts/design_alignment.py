"""Project a recipe's DesignSpec through the plan's committed visual direction."""

from __future__ import annotations

from disco.core.appkit.recipes import (
    CHOICE_COMPONENT_STYLE,
    CHOICE_DENSITY,
    CHOICE_PALETTE_ACCENT,
    CHOICE_PALETTE_PRIMARY,
    CHOICE_PALETTE_SURFACE,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
)
from disco.core.appkit.spec import DesignSpec, Justification, Palette, Typography
from disco.core.design import DesignDirection, to_brand_tokens


def _align_recipe_design_to_direction(design: DesignSpec, direction: DesignDirection) -> DesignSpec:
    """Project a recipe's layout through the plan's committed visual direction.

    The recipe still owns its section/layout system. The immutable direction owns
    the fonts, palette, surface treatment, and density that final verification
    treats as ground truth. Rebuilding a validated ``DesignSpec`` here keeps every
    emitted value inside the injection-safe spec boundary and replaces stale recipe
    justifications with truthful direction-derived ones.
    """

    theme = to_brand_tokens(direction)
    replaced_choices = {
        CHOICE_TYPOGRAPHY_HEADING,
        CHOICE_TYPOGRAPHY_BODY,
        CHOICE_PALETTE_PRIMARY,
        CHOICE_PALETTE_SURFACE,
        CHOICE_PALETTE_ACCENT,
        CHOICE_COMPONENT_STYLE,
        CHOICE_DENSITY,
    }
    retained = tuple(
        justification
        for justification in design.justifications
        if justification.choice not in replaced_choices
    )
    committed = (
        Justification(
            choice=CHOICE_TYPOGRAPHY_HEADING,
            reason=(
                f"{direction.font_pairing.heading.family} is the committed "
                f"{direction.id} display family."
            ),
        ),
        Justification(
            choice=CHOICE_TYPOGRAPHY_BODY,
            reason=(
                f"{direction.font_pairing.body.family} is the committed "
                f"{direction.id} reading family."
            ),
        ),
        Justification(
            choice=CHOICE_PALETTE_PRIMARY,
            reason=f"{direction.palette_seed} is the committed {direction.id} palette seed.",
        ),
        Justification(
            choice=CHOICE_PALETTE_SURFACE,
            reason=(
                f"{theme.bg} is the derived {direction.id} surface paired with "
                f"the committed {theme.text} text role."
            ),
        ),
        Justification(
            choice=CHOICE_PALETTE_ACCENT,
            reason=(
                f"{direction.accents[0].hex} is the committed "
                f"{direction.id} {direction.accents[0].name} accent."
            ),
        ),
        Justification(
            choice=CHOICE_COMPONENT_STYLE,
            reason=(
                f"{direction.surface_treatment.treatment} is the committed "
                f"{direction.id} surface treatment."
            ),
        ),
        Justification(
            choice=CHOICE_DENSITY,
            reason=f"{direction.density} is the committed {direction.id} information density.",
        ),
    )
    return DesignSpec(
        schema_version=design.schema_version,
        typography=Typography(
            heading_font=direction.font_pairing.heading.family,
            body_font=direction.font_pairing.body.family,
        ),
        palette=Palette(
            primary=direction.palette_seed,
            surface=theme.bg,
            text=theme.text,
            accent=direction.accents[0].hex,
        ),
        layout_family=design.layout_family,
        component_style=direction.surface_treatment.treatment,
        density=direction.density,
        justifications=(*retained, *committed),
    )
