"""Splicing the folded form(s) submission plane into a host Worker.

Extracted verbatim from ``form_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import Entity
from .fold import _entity_table
from .lowering import _field_kind, _form_const_name


def _replace_once(source: str, anchor: str, replacement: str) -> str:
    """Replace an anchor that MUST occur exactly once — a changed base emitter that
    breaks the anchor is a hard error here, never a silently-unwired form plane."""
    count = source.count(anchor)
    if count != 1:
        raise ValueError(
            f"generator invariant broken: expected exactly one occurrence of "
            f"{anchor!r} in the emitted worker, found {count}"
        )
    return source.replace(anchor, replacement)


def _form_handler_names(forms: tuple[Entity, ...]) -> dict[str, str]:
    from ..generator import _pascal

    names: dict[str, str] = {}
    used: set[str] = set()
    for entity in forms:
        base = f"submitAppForm{_pascal(_entity_table(entity))}"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}{suffix}"
            suffix += 1
        used.add(name)
        names[entity.id] = name
    return names


_VALIDATE_APP_FORM_TS = (
    "type AppFormCheck =\n"
    "  | { ok: true; rec: Record<string, unknown> }\n"
    "  | { ok: false; error: string };\n\n"
    "// Mirror the spec'd field kinds + required flags. Any mismatch → 422 at the\n"
    "// call site (the lead route keeps its own 400 contract untouched).\n"
    "function validateAppForm(body: unknown, meta: AppFormMeta): AppFormCheck {\n"
    '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
    '    return { ok: false, error: "request body must be a JSON object" };\n'
    "  }\n"
    "  const rec = body as Record<string, unknown>;\n"
    "  for (const key of Object.keys(rec)) {\n"
    "    if (!(key in meta.fields)) {\n"
    "      return { ok: false, error: `unknown field: ${key}` };\n"
    "    }\n"
    "  }\n"
    "  for (const [key, rule] of Object.entries(meta.fields)) {\n"
    "    const v = rec[key];\n"
    "    if (rule.required) {\n"
    "      const missing =\n"
    '        rule.kind === "checkbox"\n'
    "          ? v !== true\n"
    '          : v === undefined || v === null || v === "";\n'
    "      if (missing) {\n"
    "        return { ok: false, error: `missing required field: ${key}` };\n"
    "      }\n"
    "    }\n"
    "    if (v === undefined || v === null) continue;\n"
    '    if (rule.kind === "number") {\n'
    '      if (typeof v !== "number" || !Number.isFinite(v)) {\n'
    "        return { ok: false, error: `field must be a number: ${key}` };\n"
    "      }\n"
    '    } else if (rule.kind === "checkbox") {\n'
    '      if (typeof v !== "boolean") {\n'
    "        return { ok: false, error: `field must be a boolean: ${key}` };\n"
    "      }\n"
    "    } else {\n"
    '      if (typeof v !== "string") {\n'
    "        return { ok: false, error: `field must be a string: ${key}` };\n"
    "      }\n"
    "      if (v.length > MAX_FIELD_LEN) {\n"
    "        return { ok: false, error: `field too long: ${key}` };\n"
    "      }\n"
    '      if (rule.kind === "email" && v !== "" && !EMAIL_RE.test(v)) {\n'
    "        return { ok: false, error: `invalid email: ${key}` };\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "  return { ok: true, rec };\n"
    "}\n\n"
)


def _form_value_line(field_name: str, kind: str) -> str:
    from ..generator import _ts

    key = _ts(field_name)
    if kind == "checkbox":
        return f"    {key}: rec[{key}] === true ? 1 : rec[{key}] === false ? 0 : null,"
    if kind == "number":
        return f'    {key}: typeof rec[{key}] === "number" ? rec[{key}] : null,'
    return f'    {key}: typeof rec[{key}] === "string" ? rec[{key}] : null,'


def _emit_form_worker_defs(
    forms: tuple[Entity, ...], route_by_entity_id: dict[str, str] | None = None
) -> str:
    """The Worker's form plane: per-form validation metadata, the shared kind/
    required validator, one insert handler per form, and the route table. Appended
    to the lead worker just before `export default` (the lead code is untouched)."""
    from ..generator import _ts

    handler_names = _form_handler_names(forms)
    metas: list[str] = []
    handlers: list[str] = []
    routes: list[str] = []
    for entity in forms:
        route = (
            route_by_entity_id[entity.id]
            if route_by_entity_id is not None
            else f"/api/{_entity_table(entity)}"
        )
        const = _form_const_name(entity)
        fn = handler_names[entity.id]
        rules = {
            f.name: {"kind": _field_kind(f.type), "required": f.required} for f in entity.fields
        }
        metas.append(f"  {_ts(route)}: {{ fields: {_ts(rules)} }},")
        value_lines = "\n".join(
            _form_value_line(f.name, _field_kind(f.type)) for f in entity.fields
        )
        handlers.append(
            f"async function {fn}(env: Env, body: unknown): Promise<Response> {{\n"
            f"  const check = validateAppForm(body, APP_FORM_META[{_ts(route)}]);\n"
            "  if (!check.ok) return json({ error: check.error }, 422);\n"
            "  const rec = check.rec;\n"
            "  const values = {\n"
            f"{value_lines}\n"
            f"  }} as typeof {const}.$inferInsert;\n"
            "  try {\n"
            "    const db = drizzle(env.DB);\n"
            f"    await db.insert({const}).values(values).run();\n"
            "  } catch {\n"
            '    return json({ error: "could not save the submission" }, 500);\n'
            "  }\n"
            "  return json({ ok: true }, 201);\n"
            "}\n"
        )
        routes.append(f"  {_ts(route)}: {fn},")
    return (
        "// ---- Form primitive (F3.1): app-form submission plane --------------------\n"
        "// NOTE: NO spam protection here — that hardening is a separate,\n"
        "// security-classed work order (see disco.core.appkit.form_primitive).\n"
        "interface AppFormFieldRule {\n"
        "  kind: string;\n"
        "  required: boolean;\n"
        "}\n\n"
        "interface AppFormMeta {\n"
        "  fields: Record<string, AppFormFieldRule>;\n"
        "}\n\n"
        "const APP_FORM_META: Record<string, AppFormMeta> = {\n"
        + "\n".join(metas)
        + "\n};\n\n"
        + _VALIDATE_APP_FORM_TS
        + "\n".join(handlers)
        + "\n"
        "const APP_FORM_ROUTES: Record<string, (env: Env, body: unknown) => "
        "Promise<Response>> = {\n" + "\n".join(routes) + "\n};\n\n"
    )


_FORM_DISPATCH_TS = (
    "    // Form primitive (F3.1): app-form submissions (422 on validation mismatch).\n"
    "    const appFormRoute = APP_FORM_ROUTES[url.pathname];\n"
    '    if (appFormRoute && request.method === "POST") {\n'
    "      let body: unknown;\n"
    "      try {\n"
    "        body = await request.json();\n"
    "      } catch {\n"
    '        return json({ error: "invalid JSON" }, 400);\n'
    "      }\n"
    "      return appFormRoute(env, body);\n"
    "    }\n"
)

_FORM_AUTH_DISPATCH_TS = (
    "    // Form primitive (F3.1): public app-form submissions "
    "(422 on validation mismatch).\n"
    "    const appFormRoute = APP_FORM_ROUTES[rawPath];\n"
    '    if (appFormRoute && request.method === "POST") {\n'
    "      const contentTypeError = requireJsonContentType(request);\n"
    "      if (contentTypeError !== null) return contentTypeError;\n"
    "      const parsed = await readJsonBody(request);\n"
    "      if (!parsed.ok) return parsed.response;\n"
    "      return appFormRoute(env, parsed.body);\n"
    "    }\n"
)


def lower_form_worker_ts(lead: Entity, forms: tuple[Entity, ...]) -> str:
    """`worker/index.ts`: the lead worker (byte-identical base emitter) with the
    form plane spliced in at three exactly-once anchors — the schema import, the
    pre-`export default` definition block, and the pre-ASSETS route dispatch.
    Empty `forms` → the base output, unchanged."""
    from ..generator import _emit_worker_ts

    base = _emit_worker_ts(lead)
    if not forms:
        return base
    consts = ", ".join(["leads", *(_form_const_name(e) for e in forms)])
    out = _replace_once(
        base,
        'import { leads } from "../src/db/schema";\n',
        f'import {{ {consts} }} from "../src/db/schema";\n',
    )
    out = _replace_once(
        out,
        "export default {\n",
        _emit_form_worker_defs(forms) + "export default {\n",
    )
    return _replace_once(
        out,
        "    return env.ASSETS.fetch(request);\n",
        _FORM_DISPATCH_TS + "    return env.ASSETS.fetch(request);\n",
    )


def lower_form_records_worker_ts(
    worker_ts: str,
    forms: tuple[Entity, ...],
    route_by_entity_id: dict[str, str],
    *,
    auth_enabled: bool,
) -> str:
    """Splice the shared app-form submission plane into a records Worker.

    Records keeps its own `/api/<table>` route table. Folded forms live under
    `/api/forms/<form_id>` and use the same 422 validation contract as lead_gen
    folded forms. Empty `forms` → the base worker byte-identically.
    """
    if not forms:
        return worker_ts
    defs = _emit_form_worker_defs(forms, route_by_entity_id)
    out = _replace_once(worker_ts, "export default {\n", defs + "export default {\n")
    if auth_enabled:
        return _replace_once(
            out,
            "    const route = ROUTES[rawPath];\n",
            _FORM_AUTH_DISPATCH_TS + "    const route = ROUTES[rawPath];\n",
        )
    return _replace_once(
        out,
        "    return env.ASSETS.fetch(request);\n",
        _FORM_DISPATCH_TS + "    return env.ASSETS.fetch(request);\n",
    )
