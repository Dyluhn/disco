"""P9A tests: the TweakSpec typed definition layer — per-editor constraints, the grounded/
justification rules, exact numeric bounds, canonical hex, and validate_value coercion."""

from __future__ import annotations

import pytest
from disco.core.tweaks import (
    TweakEditor,
    TweakField,
    TweakSpec,
    canonical_hex,
)
from pydantic import ValidationError


def _f(**kw) -> TweakField:
    base = {"key": "k", "label": "L", "editor": TweakEditor.BOOLEAN, "controls_behavior": True}
    return TweakField(**{**base, **kw})


# --- editors + happy paths ----------------------------------------------------
def test_boolean_enum_palette_numeric_text_color_construct() -> None:
    _f(editor=TweakEditor.BOOLEAN)
    _f(editor=TweakEditor.ENUM, options=("a", "b"))
    _f(editor=TweakEditor.PALETTE, colors=("#fff", "#000"))
    _f(editor=TweakEditor.INT, min=0, max=10, step=2)
    _f(editor=TweakEditor.FLOAT, min=0.0, max=1.0, step=0.25)
    _f(editor=TweakEditor.TEXT, affects=("hero.headline", "hero.subhead"))
    _f(editor=TweakEditor.COLOR, controls_behavior=True)


# --- per-editor constraints ---------------------------------------------------
def test_enum_requires_unique_nonempty_options() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.ENUM)
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.ENUM, options=("a", "a"))


def test_palette_requires_2_to_5_unique_colors() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.PALETTE, colors=("#fff",))  # 1
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.PALETTE, colors=("#111", "#222", "#333", "#444", "#555", "#666"))  # 6
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.PALETTE, colors=("#ffffff", "#fff"))  # duplicate after canonicalize


def test_palette_colors_canonicalized() -> None:
    f = _f(editor=TweakEditor.PALETTE, colors=("#FFF", "#010203"))
    assert f.colors == ("#ffffff", "#010203")


def test_cross_editor_constraints_rejected() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.BOOLEAN, options=("a",))
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.TEXT, affects=("a", "b"), colors=("#fff", "#000"))
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.BOOLEAN, min=0, max=1, step=1)


# --- numeric exactness --------------------------------------------------------
def test_numeric_requires_min_max_step() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=0, max=10)  # no step


def test_numeric_min_le_max_step_positive() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=10, max=0, step=1)
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=0, max=10, step=0)


def test_int_min_max_step_must_be_integral() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=0.0, max=10.5, step=1)


def test_unreachable_max_rejected() -> None:
    # (max-min) not a whole number of steps → max unreachable
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=0, max=10, step=3)
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.FLOAT, min=0.0, max=1.0, step=0.3)


def test_nan_inf_bool_rejected_in_bounds() -> None:
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.FLOAT, min=0.0, max=float("inf"), step=0.1)
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=False, max=10, step=1)  # bool not int


# --- grounded / justification rule --------------------------------------------
def test_ungrounded_field_rejected() -> None:
    with pytest.raises(ValidationError):
        TweakField(
            key="k", label="L", editor=TweakEditor.BOOLEAN
        )  # no controls_behavior, no affects


def test_text_color_need_behavior_or_two_fields() -> None:
    with pytest.raises(ValidationError):  # text, 1 field, no behavior
        TweakField(key="k", label="L", editor=TweakEditor.TEXT, affects=("hero.headline",))
    TweakField(key="k", label="L", editor=TweakEditor.TEXT, affects=("a.b", "c.d"))  # 2 fields ok
    TweakField(key="k", label="L", editor=TweakEditor.COLOR, controls_behavior=True)  # behavior ok


def test_boolean_with_single_affect_is_grounded() -> None:
    TweakField(key="k", label="L", editor=TweakEditor.BOOLEAN, affects=("lead_form.phone",))


# --- P9A code-review fixes: affects validation + palette set bypass -----------
def test_affects_paths_must_be_lexical() -> None:
    for bad in (" ", "Hero.Title", "hero..x", "1bad"):
        with pytest.raises(ValidationError):
            TweakField(key="k", label="L", editor=TweakEditor.BOOLEAN, affects=(bad,))


def test_duplicate_affects_rejected_so_text_cannot_fake_two_fields() -> None:
    with pytest.raises(ValidationError):  # duplicate paths don't satisfy ">=2 fields"
        TweakField(
            key="k", label="L", editor=TweakEditor.TEXT, affects=("hero.title", "hero.title")
        )


def test_palette_set_input_rejected_not_silently_uncanonicalized() -> None:
    with pytest.raises(ValidationError):  # a set bypasses canonicalization → must be rejected
        _f(editor=TweakEditor.PALETTE, colors={"#FFF", "#ffffff"})


# --- key/label + default ------------------------------------------------------
def test_key_pattern_and_label_enforced() -> None:
    with pytest.raises(ValidationError):
        _f(key="Bad Key")
    with pytest.raises(ValidationError):
        _f(label="  ")


def test_default_validated_against_editor() -> None:
    _f(editor=TweakEditor.ENUM, options=("a", "b"), default="a")
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.ENUM, options=("a", "b"), default="z")
    _f(editor=TweakEditor.INT, min=0, max=10, step=2, default=4)
    with pytest.raises(ValidationError):
        _f(editor=TweakEditor.INT, min=0, max=10, step=2, default=3)  # not step-aligned


# --- canonical_hex ------------------------------------------------------------
def test_canonical_hex() -> None:
    assert canonical_hex("#FFF") == "#ffffff"
    assert canonical_hex("#AbCdEf") == "#abcdef"
    for bad in ("white", "rgb(0,0,0)", "#ffff", "#12", "#1234567f", 123):
        with pytest.raises(ValueError):
            canonical_hex(bad)


# --- TweakSpec + validate_value -----------------------------------------------
SPEC = TweakSpec(
    fields=(
        _f(key="lead_form.include_phone", editor=TweakEditor.BOOLEAN, affects=("lead_form.phone",)),
        _f(
            key="hero.cta_count",
            editor=TweakEditor.INT,
            min=1,
            max=5,
            step=1,
            affects=("hero.ctas",),
        ),
        _f(
            key="brand.tone",
            editor=TweakEditor.ENUM,
            options=("calm", "bold"),
            affects=("design.tone",),
        ),
        _f(
            key="brand.accent",
            editor=TweakEditor.PALETTE,
            colors=("#0a84ff", "#e2725b"),
            affects=("design.accent",),
        ),
    )
)


def test_spec_unique_keys() -> None:
    with pytest.raises(ValidationError):
        TweakSpec(fields=(_f(key="x.y", affects=("a",)), _f(key="x.y", affects=("a",))))


def test_validate_value_coerces_per_editor() -> None:
    assert SPEC.validate_value("lead_form.include_phone", "true") is True
    assert SPEC.validate_value("lead_form.include_phone", False) is False
    assert SPEC.validate_value("hero.cta_count", "3") == 3
    assert SPEC.validate_value("brand.tone", "bold") == "bold"
    assert SPEC.validate_value("brand.accent", "#E2725B") == "#e2725b"


def test_validate_value_rejects_bad() -> None:
    for key, bad in [
        ("lead_form.include_phone", "yes"),  # not true/false
        ("hero.cta_count", "9"),  # out of range
        ("hero.cta_count", True),  # bool not int
        ("brand.tone", "spicy"),  # not an option
        ("brand.accent", "#000000"),  # not a swatch
        ("nope.key", "x"),  # unknown tweak
    ]:
        with pytest.raises(ValueError):
            SPEC.validate_value(key, bad)
