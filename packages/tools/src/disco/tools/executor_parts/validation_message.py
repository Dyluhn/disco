"""The model-facing explanation of a rejected tool call.

One authority: turning a pydantic rejection into the single self-correcting
string the model actually sees. The executor never composes this prose itself —
it hands over the tool name, the args model, the arguments and the errors, and
receives the finished message.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from .arguments import (
    _NO_EXAMPLE,
    _allows_none,
    _arg_surface,
    _example_value,
    _model_example_item,
    _model_shape,
    _nested_model,
    _valid_arg_keys,
)


def _nested_error_field(
    container_name: str,
    model: type[BaseModel],
    errors: list[dict[str, Any]],
) -> str | None:
    """The first nested field of `model` named by an error under `container_name`.

    Pydantic paths such as `steps.0.done_condition` identify both the list
    container and the nested field; only the nested field is returned.
    """
    nested_name: str | None = None
    for err in errors:
        loc = err.get("loc", ())
        if not loc or loc[0] != container_name:
            continue
        for part in loc[1:]:
            if isinstance(part, str) and part in model.model_fields:
                nested_name = part
                break
        if nested_name is not None:
            break
    return nested_name


def _nested_field_example(model: type[BaseModel], nested_name: str, finfo: Any) -> Any:
    """A candidate value for `nested_name`, validated as part of the whole model.

    Returning the sentinel means no example survived validation; guidance is then
    shown without one rather than coercing the rejected call.
    """
    value = _example_value(finfo.annotation, 0)
    base_item = _model_example_item(model, 0)
    if value is _NO_EXAMPLE or base_item is None:
        return _NO_EXAMPLE
    candidate = dict(base_item)
    candidate[nested_name] = value
    try:
        validated = model.model_validate(candidate)
    except ValidationError:
        return _NO_EXAMPLE
    return validated.model_dump(mode="json", exclude_unset=True).get(nested_name)


def _nested_field_clauses(finfo: Any, example: Any) -> list[str]:
    """The recovery clauses offered for one malformed nested field."""
    clauses: list[str] = []
    if example is not _NO_EXAMPLE:
        noun = "object" if isinstance(example, dict) else "value"
        clauses.append(f"send a {noun} such as {json.dumps(example)}")
    if _allows_none(finfo.annotation):
        clauses.append("set it to null, or omit it")
    elif not finfo.is_required():
        clauses.append("omit it")
    return clauses


def _nested_field_hint(
    container_name: str,
    model: type[BaseModel],
    errors: list[dict[str, Any]],
) -> str | None:
    """Explain a malformed field inside a nested model with a valid value.

    Pydantic paths such as `steps.0.done_condition` identify both the list
    container and the nested field. Build a candidate for that field, then
    validate it as part of the complete nested model before showing it. This
    keeps recovery guidance copyable without coercing the rejected call.
    """
    nested_name = _nested_error_field(container_name, model, errors)
    if nested_name is None:
        return None

    finfo = model.model_fields[nested_name]
    example = _nested_field_example(model, nested_name, finfo)

    path = f"{container_name}[*].{nested_name}"
    clauses = _nested_field_clauses(finfo, example)
    if not clauses:
        return None
    guidance = f"For {path!r}, " + "; alternatively ".join(clauses) + "."
    if isinstance(example, (dict, list)):
        guidance += " Do not send a string."
    return guidance


def _nested_shape_hint(field_name: str, model: type[BaseModel], is_list: bool) -> str:
    """A concise, copyable hint: the expected nested shape + ONE concrete example —
    so a model that mis-formats an array-of-objects (e.g. `steps: ["", "", ""]`)
    can see exactly what to send instead. Never the full JSON schema (kept small)."""
    shape = _model_shape(model)
    if is_list:
        items = [_model_example_item(model, 0), _model_example_item(model, 1)]
        prefix = f"The {field_name!r} argument must be a list of objects, each shaped {shape}."
        if all(item is not None for item in items):
            return f"{prefix} Example: {field_name}={json.dumps(items)}."
        return prefix
    item = _model_example_item(model, 0)
    prefix = f"The {field_name!r} argument must be an object shaped {shape}."
    return f"{prefix} Example: {field_name}={json.dumps(item)}." if item is not None else prefix


def _failure_reasons(unknown: list[str], errors: list[dict[str, Any]]) -> list[str]:
    """One human/model-readable clause per distinct reason the call was rejected."""
    reasons: list[str] = []
    if unknown:
        reasons.append(f"unexpected argument(s) {unknown} — not accepted by this tool")
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        etype = err.get("type", "")
        if etype == "missing":
            reasons.append(f"missing required argument {loc!r}")
        elif etype in {"extra_forbidden", "unexpected_keyword_argument"}:
            # already covered by `unknown` above; skip to avoid duplication
            continue
        else:
            reasons.append(f"argument {loc!r}: {err.get('msg', etype)}")
    if not reasons:
        reasons.append("arguments did not match the tool's schema")
    return reasons


def _unknown_key_fix(
    tool_name: str,
    args_model: type[BaseModel],
    unknown: list[str],
) -> str:
    """Spell out the likely fix so a weaker model can self-correct next turn."""
    required_names = [n for n, fi in args_model.model_fields.items() if fi.is_required()]
    if not required_names:
        return ""
    return f" Re-call {tool_name!r} using the correct key(s) {required_names} instead of {unknown}."


def _nested_hints(args_model: type[BaseModel], errors: list[dict[str, Any]]) -> str:
    """Nested-shape hints (GENERIC, all tools).

    When an error points at a field whose value is a model — or a list of models —
    but the model sent the wrong shape (e.g. `steps: ["", "", ""]` where each item
    must be an object), append the EXPECTED nested shape + one concrete example so
    the call is recoverable. Skip "missing"/unknown-key errors (explained
    elsewhere) and dedupe by field so three bad list items don't repeat the same
    hint thrice. Deterministic.
    """
    seen_fields: set[str] = set()
    hints = ""
    for err in errors:
        loc = err.get("loc", ())
        if not loc:
            continue
        top = loc[0]
        etype = err.get("type", "")
        if (
            not isinstance(top, str)
            or top in seen_fields
            or etype in {"missing", "extra_forbidden", "unexpected_keyword_argument"}
        ):
            continue
        finfo = args_model.model_fields.get(top)
        if finfo is None:
            continue
        nested = _nested_model(finfo.annotation)
        if nested is None:
            continue
        seen_fields.add(top)
        model, is_list = nested
        hints += " " + _nested_shape_hint(top, model, is_list)
        field_hint = _nested_field_hint(top, model, errors)
        if field_hint is not None:
            hints += " " + field_hint
    return hints


def _json_structure_is_open(raw: str) -> bool:
    """Whether a JSON-like object's strings or containers remain open at EOF."""
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != pairs[char]:
                return False

    return in_string or bool(stack)


def _incomplete_raw_json(arguments: dict[str, Any]) -> str | None:
    """Return the decoder-preserved payload when a JSON object ended early.

    The OpenAI-compatible decoder deliberately preserves unparseable tool
    arguments as ``{"_raw": ...}`` so validation fails closed. A response
    capped in the middle of a large string is not an invented ``_raw`` tool
    argument, though: reporting it as one sends the model down the wrong repair
    path. Recognise only that exact internal envelope and only an object whose
    lexical structure is still open at EOF. Closed-but-invalid JSON and bare
    arrays retain the ordinary schema-error path.
    """
    if set(arguments) != {"_raw"} or not isinstance(arguments["_raw"], str):
        return None
    raw = arguments["_raw"].strip()
    if not raw.startswith("{") or not _json_structure_is_open(raw):
        return None

    return arguments["_raw"]


def _incomplete_json_message(
    tool_name: str,
    args_model: type[BaseModel],
    raw: str,
) -> str:
    """Truthful recovery for provider-truncated structured arguments."""
    msg = (
        f"arguments for {tool_name!r} were not executed: the provider response ended "
        f"with an incomplete JSON object (likely truncated; received {len(raw)} characters). "
        f"No tool ran. Re-call {tool_name!r} with a complete, smaller JSON object."
    )
    if tool_name == "file_write":
        msg += (
            " For large file content, use one bounded file_write call followed by "
            "bounded file_append calls."
        )
    return msg + f" Expected arguments: {_arg_surface(args_model)}."


def describe_validation_failure(
    tool_name: str,
    args_model: type[BaseModel],
    arguments: dict[str, Any],
    errors: list[dict[str, Any]],
) -> str:
    """Build a SELF-CORRECTING, model-facing validation error.

    The model only sees a failed ToolResult's `error`/`content` string (the
    structured `validation_errors`/`expected_schema` are not surfaced into the
    agent's message stream), so the actionable detail MUST live in this string.
    It names the unexpected key(s) the caller invented (e.g. `cmd`) AND the
    expected/required field(s) it should have used (e.g. `command`), pulled
    generically from the pydantic model — so EVERY tool benefits, not just shell.

    Deterministic for identical arguments (so byte-identical repeated bad calls
    still trip the loop's stuck detector as before).
    """
    if (raw := _incomplete_raw_json(arguments)) is not None:
        return _incomplete_json_message(tool_name, args_model, raw)

    valid_keys = _valid_arg_keys(args_model)
    unknown = sorted(k for k in arguments if k not in valid_keys)

    msg = (
        f"arguments for {tool_name!r} failed validation: "
        + "; ".join(_failure_reasons(unknown, errors))
        + f". Expected arguments: {_arg_surface(args_model)}."
    )
    if unknown:
        msg += _unknown_key_fix(tool_name, args_model, unknown)
    msg += _nested_hints(args_model, errors)
    if tool_name == "submit_plan":
        msg += ' steps must be a JSON array: {"steps": [{"title": "..."}]}.'
    if arguments:
        msg += f" You provided: {sorted(arguments)}."
    return msg
