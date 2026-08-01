"""The `form` primitive's declarative spec models + shared constants.

Extracted verbatim from ``form_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Field names become SQL columns + TS object keys, exactly like EntityField.name
# (which re-validates them on fold): same identifier pattern + reserved sets as
# the spec module, duplicated as a PRE-validation so the model gets a precise
# FormSpec error instead of a nested EntityField one.
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_FIELD_NAMES = frozenset(
    {
        "id",
        "created_at",
        "rowid",
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

# `form_id` seeds the entity id, the section id, and (prefixed) the action id —
# cap it so every derived id stays inside the spec's 64-char `_IdStr` bound.
_FORM_ID_MAX = 48
_MAX_FORM_FIELDS = 12
_DEFAULT_SUCCESS_MESSAGE = "Thanks — we will be in touch."

FormFieldKind = Literal["text", "email", "textarea", "number", "checkbox"]

# FormField.kind ⇄ EntityField.type. The AppSpec is the single source of truth,
# so the kind must survive the fold: this mapping is BIJECTIVE over the five
# kinds (`_KIND_BY_ENTITY_TYPE` inverts it exactly), and the lowering derives
# the kind back from the folded entity's field types.
_ENTITY_TYPE_BY_KIND: dict[str, str] = {
    "text": "str",
    "email": "email",
    "textarea": "text",
    "number": "float",
    "checkbox": "bool",
}
_KIND_BY_ENTITY_TYPE: dict[str, str] = {v: k for k, v in _ENTITY_TYPE_BY_KIND.items()}


class FormField(BaseModel):
    """One declared form field: a safe snake_case `name` (becomes the D1 column and
    the TS object key), a human `label`, one of five input `kind`s, and whether the
    field is `required` (a required checkbox must be checked/true)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=48,
        description="snake_case field identifier (becomes the column + input name).",
    )
    label: str = Field(
        min_length=1,
        max_length=120,
        description="Human-facing input label.",
    )
    kind: FormFieldKind = Field(
        description="Input kind: text, email, textarea, number, or checkbox."
    )
    required: bool = Field(
        default=False,
        description="Whether the field must be provided (a checkbox must be checked).",
    )

    @field_validator("name")
    @classmethod
    def _name_is_safe_identifier(cls, value: str) -> str:
        if not _IDENT_RE.match(value):
            raise ValueError(
                "form field name must be a snake_case identifier "
                f"(pattern {_IDENT_RE.pattern!r}), got {value!r}"
            )
        if value in _RESERVED_FIELD_NAMES:
            raise ValueError(
                f"form field name {value!r} is reserved (implicit SQL columns / JS "
                f"prototype keys: {sorted(_RESERVED_FIELD_NAMES)}); choose another name"
            )
        return value

    @field_validator("label")
    @classmethod
    def _label_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form field label must be non-empty")
        return value


class FormSpec(BaseModel):
    """The form primitive's declarative spec — validated by `app_add_primitive`
    against this schema before `apply_form_spec` folds it into the AppSpec.
    `extra="forbid"` so an unknown key is a precise model-facing refusal."""

    model_config = ConfigDict(extra="forbid")

    form_id: str = Field(
        min_length=1,
        max_length=_FORM_ID_MAX,
        description="snake_case form identifier; seeds the entity/section/action ids "
        "and the D1 submissions table name.",
    )
    title: str = Field(
        min_length=1,
        max_length=120,
        description="The form's heading (also the folded entity's display name).",
    )
    fields: list[FormField] = Field(
        min_length=1,
        max_length=_MAX_FORM_FIELDS,
        description=f"1..{_MAX_FORM_FIELDS} form fields; names must be unique.",
    )
    page_id: str | None = Field(
        default=None,
        max_length=64,
        description="The page to place the form section on (omit for the first page).",
    )
    success_message: str = Field(
        default=_DEFAULT_SUCCESS_MESSAGE,
        min_length=1,
        max_length=300,
        description="Confirmation copy shown after a successful submission.",
    )

    @field_validator("form_id")
    @classmethod
    def _form_id_is_safe(cls, value: str) -> str:
        if not _IDENT_RE.match(value):
            raise ValueError(
                "form_id must be a snake_case identifier "
                f"(pattern {_IDENT_RE.pattern!r}), got {value!r}"
            )
        if value == "lead":
            raise ValueError(
                "form_id 'lead' is reserved — it is the lead-gen primitive's own "
                "capture entity; choose another form_id"
            )
        return value

    @field_validator("title", "success_message")
    @classmethod
    def _text_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _field_names_unique(self) -> FormSpec:
        seen: set[str] = set()
        for field in self.fields:
            if field.name in seen:
                raise ValueError(f"duplicate form field name: {field.name!r}")
            seen.add(field.name)
        return self
