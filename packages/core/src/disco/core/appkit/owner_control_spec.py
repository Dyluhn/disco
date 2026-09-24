"""Immutable creation-time owner-control vocabulary for canonical AppKit apps."""

from typing import Annotated

from disco.core.tweaks import canonical_hex
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Keep the fields directly declared on the public DesignSpec while sharing the
# constraints here. This preserves public-surface inspection and old JSON bytes.
OwnerContentWidth = Annotated[
    int | None,
    Field(ge=640, le=1440, multiple_of=40, strict=True, exclude_if=lambda value: value is None),
]
OwnerReducedMotion = Annotated[
    bool | None,
    Field(strict=True, exclude_if=lambda value: value is None),
]


class OwnerControlSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accent_palette: tuple[str, ...]

    @field_validator("accent_palette")
    @classmethod
    def validate_palette(cls, colors: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(canonical_hex(color) for color in colors)
        if not 2 <= len(canonical) <= 5 or len(set(canonical)) != len(canonical):
            raise ValueError("owner accent palette needs 2..5 unique colors")
        return canonical
