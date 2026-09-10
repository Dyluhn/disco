"""Argument annotation introspection and provider-dialect argument repair.

This module owns everything the executor knows about *the shape a tool's
arguments are declared to have*: how a pydantic annotation is named for the
model, which keys a tool legitimately accepts, how a provider dialect's
list-wrapper is repaired, and how a deterministic example value is built.

It is deliberately free of any executor state. `validation_message` builds the
model-facing prose on top of it.
"""

from __future__ import annotations

import json
import logging
import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

# The executor's logger name is preserved so relocated log records stay
# byte-identical for anything (caplog, a log sink) keyed on it.
_LOG = logging.getLogger("disco.tools.executor")

# Friendly names for the common annotations so the model sees "string"/"integer"
# rather than "<class 'str'>". Falls back to the annotation's own __name__.
_TYPE_NAMES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

_NO_EXAMPLE = object()


def _field_type_name(annotation: Any) -> str:
    """Best-effort friendly name for a pydantic field's annotation."""
    if annotation in _TYPE_NAMES:
        return _TYPE_NAMES[annotation]
    name = getattr(annotation, "__name__", None)
    if isinstance(name, str) and name:
        return name
    # Unions / generics (Optional[str], list[str], ...) — render the typing repr,
    # trimmed of the typing/module noise so it stays readable.
    return str(annotation).replace("typing.", "")


def _valid_arg_keys(args_model: type[BaseModel]) -> set[str]:
    """Every key the model legitimately may pass: field names AND any aliases
    (pydantic accepts a field by either, depending on populate_by_name)."""
    keys: set[str] = set()
    for fname, finfo in args_model.model_fields.items():
        keys.add(fname)
        if finfo.alias:
            keys.add(finfo.alias)
        if finfo.validation_alias and isinstance(finfo.validation_alias, str):
            keys.add(finfo.validation_alias)
    return keys


def _list_annotation(annotation: Any) -> bool:
    ann = _unwrap_optional(annotation)
    return ann is list or get_origin(ann) is list


def _argument_keys_for_field(field_name: str, field_info: Any) -> tuple[str, ...]:
    keys: list[str] = [field_name]
    if isinstance(field_info.alias, str):
        keys.append(field_info.alias)
    if isinstance(field_info.validation_alias, str):
        keys.append(field_info.validation_alias)
    return tuple(dict.fromkeys(keys))


def normalize_list_item_wrappers(
    tool_name: str,
    args_model: type[BaseModel],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Repair provider dialects that encode list fields as {"item": [...]}."""

    if not isinstance(arguments, dict):
        # A provider dialect can emit LIST-shaped arguments; validation must see
        # them (and refuse with its own message), not crash the run task.
        _LOG.warning("non-dict arguments for %s: %s", tool_name, type(arguments).__name__)
        return arguments
    normalized: dict[str, Any] | None = None
    unwrapped: list[str] = []
    for field_name, field_info in args_model.model_fields.items():
        if not _list_annotation(field_info.annotation):
            continue
        for key in _argument_keys_for_field(field_name, field_info):
            value = arguments.get(key)
            if not isinstance(value, dict) or len(value) != 1:
                continue
            wrapper_key, wrapper_value = next(iter(value.items()))
            if wrapper_key not in {"item", "items"} or not isinstance(wrapper_value, list):
                continue
            if normalized is None:
                normalized = dict(arguments)
            normalized[key] = wrapper_value
            unwrapped.append(key)
    if normalized is None:
        return arguments
    _LOG.debug("unwrapped list item wrapper(s) for %s: %s", tool_name, sorted(unwrapped))
    return normalized


def _arg_surface(args_model: type[BaseModel]) -> str:
    """Concise one-line summary of a tool's accepted arguments: each as
    `name (type, required|optional)`. Small by construction — tool arg models
    are a handful of fields — so it never dumps a huge schema."""
    parts: list[str] = []
    for fname, finfo in args_model.model_fields.items():
        req = "required" if finfo.is_required() else "optional"
        parts.append(f"{fname} ({_field_type_name(finfo.annotation)}, {req})")
    return ", ".join(parts) if parts else "(takes no arguments)"


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` / `Optional[X]` -> `X` (the single non-None member). Leaves any
    other annotation untouched. Lets the nested-shape hint see through an optional
    nested-model field."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _base_type(annotation: Any) -> type | None:
    """The plain python type behind a (possibly Optional) annotation, or None when
    it is not a bare type (a Literal, a generic, a model, ...)."""
    ann = _unwrap_optional(annotation)
    return ann if isinstance(ann, type) else None


def _nested_model(annotation: Any) -> tuple[type[BaseModel], bool] | None:
    """If `annotation` is a pydantic model — or a list/tuple/set of one — return
    `(model, is_list)`; otherwise None. This is what lets the validation error show
    the EXPECTED nested shape (e.g. `steps: list[PlanProgressItem]`) for ANY tool,
    not just update_plan_progress."""
    ann = _unwrap_optional(annotation)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann, False
    if get_origin(ann) in (list, tuple, set, frozenset):
        for arg in get_args(ann):
            inner = _unwrap_optional(arg)
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return inner, True
    return None


def _model_shape(model: type[BaseModel]) -> str:
    """A concise one-line shape for a nested model: each field as `"name": <type>`,
    with a Literal field spelled out as its allowed values (`"state":
    "pending"|"active"|"done"`). Deterministic (model_fields order is stable)."""
    parts: list[str] = []
    for fname, finfo in model.model_fields.items():
        ann = finfo.annotation
        if get_origin(ann) is Literal:
            allowed = "|".join(json.dumps(v) for v in get_args(ann))
            parts.append(f'"{fname}": {allowed}')
        else:
            parts.append(f'"{fname}": <{_field_type_name(ann)}>')
    return "{" + ", ".join(parts) + "}"


def _resolved_annotation(annotation: Any) -> Any:
    """Strip an Optional wrapper and any `Annotated[...]` metadata layers."""
    ann = _unwrap_optional(annotation)
    while get_origin(ann) is Annotated:
        ann = get_args(ann)[0]
    return ann


def _union_example(ann: Any, i: int) -> Any:
    """The first non-None union member that yields a usable example."""
    for member in get_args(ann):
        if member is type(None):
            continue
        value = _example_value(member, i)
        if value is not _NO_EXAMPLE:
            return value
    return _NO_EXAMPLE


def _scalar_example(ann: Any, i: int) -> Any:
    """A deterministic example for a bare builtin annotation."""
    base = _base_type(ann)
    if base is bool:
        return True
    if base is int:
        return i + 1
    if base is float:
        return 0.0
    if base is str:
        return "..."
    return _NO_EXAMPLE


def _example_value(annotation: Any, i: int) -> Any:
    """Return a deterministic candidate value for an annotation.

    This is only an example builder; validation by the containing Pydantic model
    remains authoritative. Complex annotations are followed recursively so a
    nested model/discriminated union is never represented by an invalid string
    placeholder.
    """
    ann = _resolved_annotation(annotation)

    origin = get_origin(ann)
    if origin is Literal:
        allowed = list(get_args(ann))
        return allowed[i % len(allowed)] if allowed else _NO_EXAMPLE
    if origin is Union or origin is types.UnionType:
        return _union_example(ann, i)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        item = _model_example_item(ann, i)
        return item if item is not None else _NO_EXAMPLE
    return _scalar_example(ann, i)


def _model_example_item(model: type[BaseModel], i: int) -> dict[str, Any] | None:
    """Build one example and return it only when the model accepts it.

    Optional fields are omitted: an example should teach the smallest valid
    shape, not invent placeholder values for complex optional fields. Literal
    defaults are retained because they commonly carry a discriminator tag.
    """
    obj: dict[str, Any] = {}
    for fname, finfo in model.model_fields.items():
        ann = finfo.annotation
        if not finfo.is_required() and get_origin(ann) is not Literal:
            continue
        value = _example_value(ann, i)
        if value is _NO_EXAMPLE:
            return None
        obj[fname] = value
    try:
        validated = model.model_validate(obj)
    except ValidationError:
        return None
    # Preserve the minimal fields the example intentionally supplied while
    # normalizing nested BaseModels into JSON-safe dictionaries.
    return validated.model_dump(mode="json", exclude_unset=True)


def _allows_none(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return (origin is Union or origin is types.UnionType) and type(None) in get_args(annotation)
