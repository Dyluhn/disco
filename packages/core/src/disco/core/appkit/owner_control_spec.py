"""Immutable creation-time owner-control vocabulary for canonical AppKit apps."""

from disco.core.tweaks import canonical_hex
from pydantic import BaseModel, ConfigDict, Field, field_validator


class OwnerDesignOptions(BaseModel):
    """Optional bounded display settings; absent keeps existing output byte stable."""

    content_width: int | None = Field(
        default=None,
        ge=640,
        le=1440,
        multiple_of=40,
        strict=True,
        exclude_if=lambda value: value is None,
    )
    reduced_motion: bool | None = Field(
        default=None,
        strict=True,
        exclude_if=lambda value: value is None,
    )


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
