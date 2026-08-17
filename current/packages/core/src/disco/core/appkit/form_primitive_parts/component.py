"""The React form component emitter for a folded `form` section.

Extracted verbatim from ``form_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import Entity, Section
from .fold import _entity_table
from .lowering import _field_kind
from .model import _DEFAULT_SUCCESS_MESSAGE


def _display_label(field_name: str, label: str | None) -> str:
    return label if label else field_name.replace("_", " ").title()


def _form_input_for(section_id: str, name: str, kind: str, label: str, required: bool) -> str:
    """One labeled, controlled input for a form field (mirrors the lead form's
    `_input_for` accessibility shape: required flag, aria-invalid, described-by
    error span). Checkbox binds `checks`/`toggleField`; the rest bind `form`/
    `updateField`."""
    from ..generator import _ts

    key = _ts(name)
    field_id = f"app-form-{section_id}-{name}".replace("_", "-")
    error_id = f"{field_id}-error"
    required_attr = " required" if required else ""
    invalid = f' aria-invalid={{fieldErrors[{key}] ? "true" : undefined}}'
    described = f" aria-describedby={{fieldErrors[{key}] ? {_ts(error_id)} : undefined}}"
    error = (
        f"\n"
        f"          {{fieldErrors[{key}] ? (\n"
        f'            <span className="field-error" id={_ts(error_id)}>'
        f"{{fieldErrors[{key}]}}</span>\n"
        "          ) : null}"
    )
    if kind == "checkbox":
        control = (
            f'          <input id={_ts(field_id)} type="checkbox" name={key}'
            f" checked={{checks[{key}] === true}}{required_attr}{invalid}{described}\n"
            f"            onChange={{(e) => toggleField({key}, e.target.checked)}} />"
        )
    elif kind == "textarea":
        control = (
            f"          <textarea id={_ts(field_id)} name={key}"
            f' value={{form[{key}] ?? ""}}{required_attr}{invalid}{described}\n'
            f"            onChange={{(e) => updateField({key}, e.target.value)}} />"
        )
    else:
        input_type = {"email": "email", "number": "number"}.get(kind, "text")
        control = (
            f"          <input id={_ts(field_id)} type={_ts(input_type)} name={key}"
            f' value={{form[{key}] ?? ""}}{required_attr}{invalid}{described}\n'
            f"            onChange={{(e) => updateField({key}, e.target.value)}} />"
        )
    # The label is USER text: emit it as a JSX EXPRESSION holding a JSON string
    # literal (`{"Full Name"}`), so a `<`/`{`/backtick in a label is inert text and
    # can never break out into the component markup.
    return (
        f"        <label htmlFor={_ts(field_id)}>{{{_ts(label)}}}\n{control}{error}\n"
        "        </label>"
    )


_FORM_COMPONENT_HANDLERS_TS = (
    "  function clearFieldState(field: string) {\n"
    "    setFieldErrors((current) => {\n"
    "      if (!current[field]) return current;\n"
    "      const next = { ...current };\n"
    "      delete next[field];\n"
    "      return next;\n"
    "    });\n"
    '    if (state.kind === "success" || state.kind === "error") setState({ kind: "idle" });\n'
    "  }\n\n"
    "  function updateField(field: string, value: string) {\n"
    "    setForm((current) => ({ ...current, [field]: value }));\n"
    "    clearFieldState(field);\n"
    "  }\n\n"
    "  function toggleField(field: string, value: boolean) {\n"
    "    setChecks((current) => ({ ...current, [field]: value }));\n"
    "    clearFieldState(field);\n"
    "  }\n\n"
    "  function validateRequired(): boolean {\n"
    "    const nextErrors: Record<string, string> = {};\n"
    "    for (const field of REQUIRED_FIELDS) {\n"
    "      const missing = CHECKBOX_FIELDS.includes(field)\n"
    "        ? checks[field] !== true\n"
    '        : !(form[field] ?? "").trim();\n'
    "      if (missing) {\n"
    "        nextErrors[field] = `${FIELD_LABELS[field] ?? field} is required.`;\n"
    "      }\n"
    "    }\n"
    "    for (const field of NUMBER_FIELDS) {\n"
    '      const raw = (form[field] ?? "").trim();\n'
    '      if (raw !== "" && Number.isNaN(Number(raw))) {\n'
    "        nextErrors[field] = `${FIELD_LABELS[field] ?? field} must be a number.`;\n"
    "      }\n"
    "    }\n"
    "    setFieldErrors(nextErrors);\n"
    "    return Object.keys(nextErrors).length === 0;\n"
    "  }\n\n"
    "  function buildPayload(): Record<string, unknown> {\n"
    "    const payload: Record<string, unknown> = {};\n"
    "    for (const field of TEXT_FIELDS) {\n"
    '      const value = form[field] ?? "";\n'
    '      if (value !== "") payload[field] = value;\n'
    "    }\n"
    "    for (const field of NUMBER_FIELDS) {\n"
    '      const raw = (form[field] ?? "").trim();\n'
    '      if (raw !== "") payload[field] = Number(raw);\n'
    "    }\n"
    "    for (const field of CHECKBOX_FIELDS) {\n"
    "      payload[field] = checks[field] === true;\n"
    "    }\n"
    "    return payload;\n"
    "  }\n\n"
    "  async function onSubmit(e: FormEvent<HTMLFormElement>) {\n"
    "    e.preventDefault();\n"
    '    if (state.kind === "submitting") return;\n'
    "    if (!validateRequired()) return;\n"
    '    setState({ kind: "submitting" });\n'
    "    const result = await postJson<{ ok: true }>(POST_PATH, buildPayload());\n"
    "    if (result.ok) {\n"
    '      setState({ kind: "success" });\n'
    "      setForm({});\n"
    "      setChecks({});\n"
    "    } else {\n"
    '      setState({ kind: "error", message: result.error });\n'
    "    }\n"
    "  }\n"
)


def emit_app_form_component(
    comp: str, section: Section, entity: Entity, *, post_path: str | None = None
) -> str:
    """The React component for a folded form section: one control per entity field
    (kind derived back from the field type), inline required validation, a typed
    payload POSTed through the shared `postJson` client to the form's Worker route,
    and the spec'd success message on success. Self-contained state machine — it
    deliberately does NOT reuse `useSubmit` (whose values are string-only; number/
    checkbox fields need a typed payload), so the shared hook stays byte-identical."""
    from ..generator import (
        _disco_field_attr,
        _disco_section_attrs,
        _ts,
        _variant_layout,
    )

    layout = _variant_layout(section)
    classes = f"section kind-form variant-{layout} app-form"
    if post_path is None:
        post_path = f"/api/{_entity_table(entity)}"
    disco_attrs = _disco_section_attrs(section)
    kinds = {f.name: _field_kind(f.type) for f in entity.fields}
    labels = {f.name: _display_label(f.name, f.label) for f in entity.fields}
    required = [f.name for f in entity.fields if f.required]
    text_fields = [n for n, k in kinds.items() if k in ("text", "email", "textarea")]
    number_fields = [n for n, k in kinds.items() if k == "number"]
    checkbox_fields = [n for n, k in kinds.items() if k == "checkbox"]
    content = section.content
    success = (
        content.success_message
        if content is not None and content.success_message is not None
        else _DEFAULT_SUCCESS_MESSAGE
    )
    inputs = "\n".join(
        _form_input_for(section.id, f.name, kinds[f.name], labels[f.name], f.required)
        for f in entity.fields
    )
    return (
        "/* Auto-generated form component (form primitive F3.1) — do NOT hand-edit; "
        "regenerated from .disco/appspec.json.\n"
        "   NOTE: no spam protection — that hardening is a separate, security-classed "
        "work order. */\n"
        'import type { FormEvent } from "react";\n'
        'import { useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        'import { postJson } from "../api/client";\n\n'
        f"const POST_PATH = {_ts(post_path)};\n"
        f"const REQUIRED_FIELDS: string[] = {_ts(required)};\n"
        f"const TEXT_FIELDS: string[] = {_ts(text_fields)};\n"
        f"const NUMBER_FIELDS: string[] = {_ts(number_fields)};\n"
        f"const CHECKBOX_FIELDS: string[] = {_ts(checkbox_fields)};\n"
        f"const FIELD_LABELS: Record<string, string> = {_ts(labels)};\n"
        f"const DEFAULT_SUCCESS_MESSAGE = {_ts(success)};\n\n"
        "type SubmitPhase =\n"
        '  | { kind: "idle" }\n'
        '  | { kind: "submitting" }\n'
        '  | { kind: "success" }\n'
        '  | { kind: "error"; message: string };\n\n'
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        "  const [form, setForm] = useState<Record<string, string>>({});\n"
        "  const [checks, setChecks] = useState<Record<string, boolean>>({});\n"
        "  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});\n"
        '  const [state, setState] = useState<SubmitPhase>({ kind: "idle" });\n\n'
        + _FORM_COMPONENT_HANDLERS_TS
        + "  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        '      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        '        <form className="lead-form app-form-fields" onSubmit={onSubmit} noValidate>\n'
        + inputs
        + "\n"
        f'          <button className="btn" type="submit" disabled={{state.kind === "submitting"}}'
        f"{_disco_field_attr('cta_label')}>\n"
        '            {state.kind === "submitting" ? "Sending…" : c.ctaLabel ?? "Submit"}\n'
        "          </button>\n"
        '          <div className="form-feedback" aria-live="polite">\n'
        '            {state.kind === "success" ? (\n'
        '              <p className="form-status form-status-success"'
        f"{_disco_field_attr('success_message')}>"
        "{c.successMessage ?? DEFAULT_SUCCESS_MESSAGE}</p>\n"
        "            ) : null}\n"
        '            {state.kind === "error" ? (\n'
        '              <p className="form-status form-status-error">{state.message}</p>\n'
        "            ) : null}\n"
        "          </div>\n"
        "        </form>\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )
