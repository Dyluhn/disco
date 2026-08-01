"""Section component emitters (`_emit_component`, `_emit_form_component`) + the
per-field form input renderer.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget, and to bring `_emit_component`
/ `_emit_form_component` under the 100-logical-line callable cap. See
`generator_parts/__init__.py` for the byte-identity contract this split must
hold: every helper below is a straight, in-order cut of the ORIGINAL single
string concatenation — no character added, removed, or reordered. Byte-identity
is verified externally (hash comparison against the pre-split generator), not by
a test in this tree.
"""

from __future__ import annotations

from ..spec import Entity, EntityField, Page, Section
from .design_tokens import _variant_layout
from .ids import _ts
from .semantic_attrs import _disco_field_attr, _disco_item_attrs, _disco_section_attrs, _render_body

# ---- non-form section components -----------------------------------------------


def _component_header(comp: str, comp_id: str) -> str:
    """The header shared by every non-form section component: the auto-generated
    banner, the content import, and the function/const-c opening lines."""
    return (
        "/* Auto-generated section component — do NOT hand-edit; regenerated from "
        ".disco/appspec.json. */\n"
        'import { CONTENT } from "../generated/content";\n\n'
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp_id)}] ?? {{}};\n"
    )


def _component_body_hero(section: Section, classes: str, disco_attrs: str) -> str:
    return (
        f"  return (\n"
        f"    <section className={_ts('hero ' + classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h1{_disco_field_attr('heading')}>{{c.heading}}</h1> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        f"        {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
        "        {c.ctaLabel ? (\n"
        f'          <p><a className="btn" href="#lead-form"{_disco_field_attr("cta_label")}>'
        "{c.ctaLabel}</a></p>\n"
        "        ) : null}\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _component_body_list_like(
    comp_id: str, section: Section, kind: str, classes: str, disco_attrs: str
) -> str:
    list_cls = "feature-grid" if kind in ("features", "gallery", "pricing") else "stacked-list"
    wrap_open = "<ul" if list_cls == "stacked-list" else "<div"
    wrap_close = "</ul>" if list_cls == "stacked-list" else "</div>"
    item_tag = "li" if list_cls == "stacked-list" else "div"
    return (
        f"  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n'
        + "  "
        + _render_body(comp_id)
        + f"        {wrap_open} className={_ts(list_cls)}>\n"
        "          {(c.items ?? []).map((item, i) => (\n"
        f'            <{item_tag} className="feature-item" key={{i}}'
        f"{_disco_field_attr('items')}"
        f"{_disco_item_attrs(section, index_expr='i', item_kind='item')}>"
        f"{{item}}</{item_tag}>\n"
        "          ))}\n"
        f"        {wrap_close}\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _component_body_cta(comp_id: str, section: Section, classes: str, disco_attrs: str) -> str:
    return (
        f"  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n'
        + "  "
        + _render_body(comp_id)
        + "        {c.ctaLabel ? (\n"
        f'          <p><a className="btn" href="#lead-form"{_disco_field_attr("cta_label")}>'
        "{c.ctaLabel}</a></p>\n"
        "        ) : null}\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _component_body_footer(section: Section, classes: str, disco_attrs: str) -> str:
    return (
        f"  return (\n"
        f"    <footer className={_ts('site-footer ' + classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n'
        f"        {{c.heading ? <p{_disco_field_attr('heading')}>{{c.heading}}</p> : null}}\n"
        f"        {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
        "      </div>\n"
        "    </footer>\n"
        "  );\n}\n"
    )


def _component_body_generic(comp_id: str, section: Section, classes: str, disco_attrs: str) -> str:
    # custom / table / anything else: a generic block. (Epic J: the generic branch
    # now also carries the data-appkit-section marker — previously MISSING here — so
    # verify_appkit_app section-coverage + click-to-edit work for custom sections too.)
    return (
        f"  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n' + "  " + _render_body(comp_id) + "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _emit_component(
    comp: str, page: Page, section: Section, lead: Entity, post_path: str = "/api/leads"
) -> str:
    comp_id = comp
    layout = _variant_layout(section)
    kind = section.kind
    classes = f"section kind-{kind} variant-{layout}"

    if kind == "form":
        return _emit_form_component(comp, comp_id, page, section, lead, classes, post_path)

    disco_attrs = _disco_section_attrs(section)
    header = _component_header(comp, comp_id)

    if kind == "hero":
        return header + _component_body_hero(section, classes, disco_attrs)

    if kind in ("features", "list", "gallery", "pricing", "testimonials", "faq"):
        return header + _component_body_list_like(comp_id, section, kind, classes, disco_attrs)

    if kind == "cta":
        return header + _component_body_cta(comp_id, section, classes, disco_attrs)

    if kind == "footer":
        return header + _component_body_footer(section, classes, disco_attrs)

    return header + _component_body_generic(comp_id, section, classes, disco_attrs)


def _input_for(field: EntityField) -> str:
    """A labeled input for one lead field. `text`/`message` → textarea; `email` name
    → email input; everything else → a text input. Required maps to `required`."""
    label = field.name.replace("_", " ").title()
    is_textarea = field.type.lower() in ("text", "message") or field.name.lower() == "message"
    input_type = "email" if field.name.lower() == "email" else "text"
    key = _ts(field.name)
    field_id = f"lead-field-{field.name.replace('_', '-')}"
    error_id = f"{field_id}-error"
    required = " required" if field.required else ""
    described_by = (
        f" aria-describedby={{fieldErrors[{key}] ? {_ts(error_id)} : undefined}}"
        if field.required
        else ""
    )
    invalid = f' aria-invalid={{fieldErrors[{key}] ? "true" : undefined}}'
    if is_textarea:
        control = (
            f"          <textarea id={_ts(field_id)} name={_ts(field.name)}"
            f' value={{form[{key}] ?? ""}}{required}{invalid}{described_by}\n'
            f"            onChange={{(e) => updateField({key}, e.target.value)}} />"
        )
    else:
        control = (
            f"          <input id={_ts(field_id)} type={_ts(input_type)}"
            f' name={_ts(field.name)} value={{form[{key}] ?? ""}}{required}'
            f"{invalid}{described_by}\n"
            f"            onChange={{(e) => updateField({key}, e.target.value)}} />"
        )
    error = (
        f"\n"
        f"          {{fieldErrors[{key}] ? (\n"
        f'            <span className="field-error" id={_ts(error_id)}>'
        f"{{fieldErrors[{key}]}}</span>\n"
        "          ) : null}"
        if field.required
        else ""
    )
    return f"        <label htmlFor={_ts(field_id)}>{label}\n{control}{error}\n        </label>"


# ---- the lead-capture form component --------------------------------------------


def _form_component_success(section: Section) -> tuple[str, str]:
    """(success_const, success_feedback): the DEFAULT_SUCCESS_MESSAGE const (empty
    when the section declares none) + the JSX shown on a successful submit."""
    success = section.content.success_message if section.content is not None else None
    if success is None:
        success_const = ""
        success_feedback = (
            '              <p className="form-status form-status-success">'
            "Thanks — we will be in touch.</p>\n"
        )
    else:
        success_const = f"const DEFAULT_SUCCESS_MESSAGE = {_ts(success)};\n\n"
        success_feedback = (
            '              <p className="form-status form-status-success"'
            f"{_disco_field_attr('success_message')}>"
            "{c.successMessage ?? DEFAULT_SUCCESS_MESSAGE}</p>\n"
        )
    return success_const, success_feedback


def _form_component_prelude(
    comp: str,
    comp_id: str,
    post_path: str,
    required_fields: list[str],
    recent_fields: list[str],
    labels: dict[str, str],
    success_const: str,
) -> str:
    return (
        "/* Auto-generated lead-capture component — do NOT hand-edit; regenerated from "
        ".disco/appspec.json. */\n"
        'import type { FormEvent } from "react";\n'
        'import { useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        'import { useSubmit } from "../hooks/useSubmit";\n\n'
        f"const REQUIRED_FIELDS = {_ts(required_fields)} as const;\n"
        f"const RECENT_FIELDS = {_ts(recent_fields)} as const;\n"
        f"const FIELD_LABELS: Record<string, string> = {_ts(labels)};\n\n"
        f"{success_const}"
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp_id)}] ?? {{}};\n"
        "  const [form, setForm] = useState<Record<string, string>>({});\n"
        "  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});\n"
        f"  const {{ state, submitted, submit, reset }} = useSubmit({_ts(post_path)});\n\n"
    )


def _form_component_handlers() -> str:
    """The three static (spec-independent) JS handlers every form component
    declares: field update, required-field validation, and submit."""
    return (
        "  function updateField(field: string, value: string) {\n"
        "    setForm((current) => ({ ...current, [field]: value }));\n"
        "    setFieldErrors((current) => {\n"
        "      if (!current[field]) return current;\n"
        "      const next = { ...current };\n"
        "      delete next[field];\n"
        "      return next;\n"
        "    });\n"
        '    if (state.kind === "success" || state.kind === "error") reset();\n'
        "  }\n\n"
        "  function validateRequired(): boolean {\n"
        "    const nextErrors: Record<string, string> = {};\n"
        "    for (const field of REQUIRED_FIELDS) {\n"
        '      if (!(form[field] ?? "").trim()) {\n'
        "        nextErrors[field] = `${FIELD_LABELS[field] ?? field} is required.`;\n"
        "      }\n"
        "    }\n"
        "    setFieldErrors(nextErrors);\n"
        "    return Object.keys(nextErrors).length === 0;\n"
        "  }\n\n"
        "  async function onSubmit(e: FormEvent<HTMLFormElement>) {\n"
        "    e.preventDefault();\n"
        '    if (state.kind === "submitting") return;\n'
        "    if (!validateRequired()) return;\n"
        "    const saved = await submit(form);\n"
        "    if (saved) setForm({});\n"
        "  }\n"
    )


def _form_component_jsx(
    section: Section, classes: str, disco_attrs: str, inputs: str, success_feedback: str
) -> str:
    return (
        f"  return (\n"
        f'    <section className={_ts(classes)} id="lead-form"'
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        '        <form className="lead-form" onSubmit={onSubmit} noValidate>\n' + inputs + "\n"
        f'          <button className="btn" type="submit" disabled={{state.kind === "submitting"}}'
        f"{_disco_field_attr('cta_label')}>\n"
        '            {state.kind === "submitting" ? "Sending…" : c.ctaLabel ?? "Submit"}\n'
        "          </button>\n"
        '          <div className="form-feedback" aria-live="polite">\n'
        '            {state.kind === "success" ? (\n'
        f"{success_feedback}"
        "            ) : null}\n"
        '            {state.kind === "error" ? (\n'
        '              <p className="form-status form-status-error">{state.message}</p>\n'
        "            ) : null}\n"
        "            {submitted.length > 0 ? (\n"
        '              <div className="recent-submissions">\n'
        "                <h3>Recently submitted</h3>\n"
        "                <ul>\n"
        "                  {submitted.map((entry) => (\n"
        "                    <li key={entry.id}>\n"
        '                      <div className="recent-submission-fields">\n'
        "                        {RECENT_FIELDS.map((field) => (\n"
        '                          <span className="recent-submission-field" key={field}>\n'
        "                            <strong>{FIELD_LABELS[field] ?? field}:</strong> "
        '{entry.values[field] ?? ""}\n'
        "                          </span>\n"
        "                        ))}\n"
        "                      </div>\n"
        "                    </li>\n"
        "                  ))}\n"
        "                </ul>\n"
        "              </div>\n"
        "            ) : null}\n"
        "          </div>\n"
        "        </form>\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _emit_form_component(
    comp: str,
    comp_id: str,
    page: Page,
    section: Section,
    lead: Entity,
    classes: str,
    post_path: str,
) -> str:
    inputs = "\n".join(_input_for(f) for f in lead.fields)
    disco_attrs = _disco_section_attrs(section)
    required_fields = [f.name for f in lead.fields if f.required]
    labels = {f.name: f.name.replace("_", " ").title() for f in lead.fields}
    recent_fields = [f.name for f in lead.fields[:2]]
    success_const, success_feedback = _form_component_success(section)
    return (
        _form_component_prelude(
            comp, comp_id, post_path, required_fields, recent_fields, labels, success_const
        )
        + _form_component_handlers()
        + _form_component_jsx(section, classes, disco_attrs, inputs, success_feedback)
    )
