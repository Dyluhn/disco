"""TweakSpec — the typed DEFINITION layer for owner controls (P9A).

Tweaks are the small set of knobs the platform exposes to a non-technical owner (toggle a
phone field, pick a curated accent, set a count). This module defines what a tweak IS — its
editor + constraints — separately from the current VALUES (``AppSpec.tweaks``). P9B persists
``.disco/tweaks.json``, P9C's ``app_set_tweak`` validates a value against this schema, P9D
renders the TweakPanel. Pure, frozen value objects + validation; no runtime imports.

Design rules (gpt-5.5-gated): a tweak must be GROUNDED — it controls behavior or >=1 field;
a TEXT/COLOR tweak must control behavior or >=2 fields (single-place copy/color belongs in
app_update_content / app_set_design, not a tweak). Numeric bounds are exact (integer-scaled,
never float modulo); colors are a strict #RGB/#RRGGBB hex subset normalized to #rrggbb;
palettes are 2-5 unique curated swatches and a value must be one of them.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class TweakEditor(str, Enum):
    TEXT = "text"
    COLOR = "color"  # a free canonical hex
    INT = "int"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"  # one of curated string options
    PALETTE = "palette"  # one of 2-5 curated color swatches


_NUMERIC = frozenset({TweakEditor.INT, TweakEditor.FLOAT})
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")  # e.g. lead_form.include_phone
_HEX3 = re.compile(r"^#[0-9a-fA-F]{3}$")
_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")


def canonical_hex(value: Any) -> str:
    """``#RGB`` / ``#RRGGBB`` → lowercase ``#rrggbb``. Rejects names, rgb(), alpha, malformed."""
    if not isinstance(value, str):
        raise ValueError(f"color must be a hex string, got {type(value).__name__}")
    v = value.strip()
    if _HEX3.match(v):
        return f"#{v[1] * 2}{v[2] * 2}{v[3] * 2}".lower()
    if _HEX6.match(v):
        return v.lower()
    raise ValueError(f"color must be #RGB or #RRGGBB hex, got {value!r}")


def _is_real(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _step_aligned(value: float, lo: float, step: float) -> bool:
    """(value - lo) is an exact integer multiple of step — integer-scaled, no float modulo."""
    q = (Decimal(str(value)) - Decimal(str(lo))) / Decimal(str(step))
    return q == q.to_integral_value()


class TweakField(BaseModel):
    """One owner control: its editor + constraints + what it grounds onto (``affects``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    editor: TweakEditor
    affects: tuple[str, ...] = ()  # LEXICAL AppSpec paths it controls (validated vs spec in P9B/C)
    controls_behavior: bool = False
    default: Any = None
    options: tuple[str, ...] | None = None  # ENUM
    colors: tuple[str, ...] | None = None  # PALETTE swatches
    min: float | None = None  # INT/FLOAT
    max: float | None = None
    step: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # reject bool numeric bounds BEFORE pydantic coerces False->0.0 (default may be a
            # bool — that's valid for the boolean editor — so only guard min/max/step here).
            for k in ("min", "max", "step"):
                if isinstance(data.get(k), bool):
                    raise ValueError(f"{k} must be a number, not a bool")
            # canonicalize palette swatches up front (frozen model can't mutate post-construction).
            # Require an ORDERED list/tuple — reject set/frozenset (pydantic would coerce those to
            # a tuple AFTER this hook, bypassing canonicalization + losing swatch order).
            if data.get("editor") in (TweakEditor.PALETTE, "palette"):
                cols = data.get("colors")
                if cols is not None:
                    if not isinstance(cols, (list, tuple)):
                        raise ValueError("palette colors must be an ordered list/tuple")
                    data = {**data, "colors": tuple(canonical_hex(c) for c in cols)}
        return data

    @model_validator(mode="after")
    def _validate(self) -> TweakField:
        if not _KEY_RE.match(self.key):
            raise ValueError(f"key must match dotted-path pattern, got {self.key!r}")
        if not self.label.strip():
            raise ValueError("label must be non-empty")
        # affects are lexical AppSpec paths — same dotted form as a key, and unique (so a
        # TEXT/COLOR tweak can't satisfy ">=2 fields" with a repeated path).
        for path in self.affects:
            if not _KEY_RE.match(path):
                raise ValueError(f"affects path {path!r} must be a dotted lexical path")
        if len(set(self.affects)) != len(self.affects):
            raise ValueError("affects paths must be unique")
        ed = self.editor
        # reject constraints irrelevant to the editor
        if self.options is not None and ed is not TweakEditor.ENUM:
            raise ValueError("options only valid for enum")
        if self.colors is not None and ed is not TweakEditor.PALETTE:
            raise ValueError("colors only valid for palette")
        if (self.min, self.max, self.step) != (None, None, None) and ed not in _NUMERIC:
            raise ValueError("min/max/step only valid for int/float")
        # GROUNDED invariant
        if not self.controls_behavior and not self.affects:
            raise ValueError(
                f"tweak {self.key!r} is ungrounded: needs controls_behavior or affects"
            )
        if (
            ed in (TweakEditor.TEXT, TweakEditor.COLOR)
            and not self.controls_behavior
            and len(self.affects) < 2
        ):
            raise ValueError(
                f"{ed.value} tweak {self.key!r} must control behavior or >=2 fields "
                "(else use a direct edit)"
            )
        # per-editor constraints
        if ed is TweakEditor.ENUM:
            if not self.options:
                raise ValueError("enum requires non-empty options")
            if len(set(self.options)) != len(self.options):
                raise ValueError("enum options must be unique")
        if ed is TweakEditor.PALETTE:
            cols = self.colors or ()
            if not 2 <= len(cols) <= 5:
                raise ValueError("palette requires 2..5 colors")
            if len(set(cols)) != len(cols):
                raise ValueError("palette colors must be unique")
        if ed in _NUMERIC:
            self._validate_numeric()
        # default must itself be a valid value for this field
        if self.default is not None:
            coerce_tweak_value(self, self.default)
        return self

    def _validate_numeric(self) -> None:
        lo, hi, step = self.min, self.max, self.step
        if lo is None or hi is None or step is None:
            raise ValueError("int/float require min/max/step")
        if not (_is_real(lo) and _is_real(hi) and _is_real(step)):
            raise ValueError("min/max/step must be finite numbers (no bool/NaN/inf)")
        if self.editor is TweakEditor.INT and not all(
            float(x).is_integer() for x in (lo, hi, step)
        ):
            raise ValueError("int min/max/step must be integral")
        if step <= 0:
            raise ValueError("step must be > 0")
        if lo > hi:
            raise ValueError("min must be <= max")
        if not _step_aligned(hi, lo, step):
            raise ValueError("(max - min) must be a whole number of steps (reachable max)")


def coerce_tweak_value(field: TweakField, value: Any) -> Any:
    """Coerce + validate a raw value for ``field``'s editor → the canonical value, or ValueError.
    Narrow, editor-specific coercion only (legacy app_set_tweak passes strings)."""
    ed = field.editor
    if ed is TweakEditor.TEXT:
        if isinstance(value, str):
            return value
        raise ValueError("text value must be a string")
    if ed is TweakEditor.COLOR:
        return canonical_hex(value)
    if ed is TweakEditor.PALETTE:
        v = canonical_hex(value)
        if v not in (field.colors or ()):
            raise ValueError(f"{v} is not one of the palette swatches {field.colors}")
        return v
    if ed is TweakEditor.BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ValueError("boolean value must be a bool or 'true'/'false'")
    if ed is TweakEditor.ENUM:
        if value in (field.options or ()):
            return value
        raise ValueError(f"{value!r} not in enum options {field.options}")
    # numeric
    num = _to_int(value) if ed is TweakEditor.INT else _to_float(value)
    assert field.min is not None and field.max is not None and field.step is not None
    if not field.min <= num <= field.max:
        raise ValueError(f"{num} out of range [{field.min}, {field.max}]")
    if not _step_aligned(num, field.min, field.step):
        raise ValueError(f"{num} is not aligned to step {field.step} from {field.min}")
    return num


def _to_int(value: Any) -> int:
    if _is_int(value):
        return value
    if isinstance(value, str) and re.match(r"^-?\d+$", value.strip()):
        return int(value.strip())
    raise ValueError(f"int value must be an integer or all-digit string, got {value!r}")


def _to_float(value: Any) -> float:
    if _is_real(value):
        return float(value)
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            raise ValueError(f"float value must be numeric, got {value!r}") from None
        if math.isfinite(f):
            return f
    raise ValueError(f"float value must be a finite number, got {value!r}")


class TweakSpec(BaseModel):
    """A registry of an app's owner-facing tweak definitions (unique keys)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: tuple[TweakField, ...] = ()

    @model_validator(mode="after")
    def _unique_keys(self) -> TweakSpec:
        keys = [f.key for f in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError("tweak keys must be unique")
        return self

    def get(self, key: str) -> TweakField | None:
        return next((f for f in self.fields if f.key == key), None)

    def validate_value(self, key: str, value: Any) -> Any:
        """Coerce+validate ``value`` for the tweak ``key`` → canonical value, or ValueError
        (unknown key included — never silently accept an undefined tweak)."""
        field = self.get(key)
        if field is None:
            raise ValueError(f"unknown tweak {key!r}")
        return coerce_tweak_value(field, value)
