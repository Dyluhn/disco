"""The AppKit `form` primitive (Epic F3.1, scaffold half): a declarative,
validated form that folds into an existing Cloudflare/D1 app.

An ADD-ON primitive, not a base scaffold: `app_add_primitive` validates a
`FormSpec`, `apply_form_spec` folds it into the AppSpec (a submissions Entity +
a `form` Section wired to it via `content_ref` + a `submit` primary action), and
the HOST app's own base primitive regenerates the whole tree from the folded
spec. The lowering rides the EXISTING lead_gen emitters: `lower_form_*` wrap
`_emit_schema_sql` / `_emit_worker_ts` / `_emit_drizzle_schema_ts` and are
byte-identical pass-throughs when the app declares no forms — the three
pre-existing primitives' `generate()` output is unchanged by construction.

The generated surface per form:

* a D1 submissions table in `schema.sql` (+ the matching Drizzle table in
  `src/db/schema.ts`);
* a Worker submission route whose validation MIRRORS the declared field kinds +
  required flags (422 on any mismatch — the generalization of lead_gen's capture
  route, which keeps its own byte-identical 400 contract);
* a React form component with one control per field (text/email/textarea/
  number/checkbox), inline required validation, and the spec'd success message.

Host scope (deliberate): `lead_gen` and legacy fallback kinds keep the original
`POST /api/<table>` form route; `records` apps use `POST /api/forms/<form_id>` so
records keeps exclusive ownership of `/api/<table>` CRUD routes. `directory` is
still refused because it is a static site with no D1/Worker data plane; silently
adding a form there would require upgrading the app shape and all deploy/verify
contracts, not just folding a section. `hello` is also refused: it is the
mount-proof primitive.

SECURITY SCOPE — READ THIS: this scaffold has NO spam protection, NO captcha,
NO rate limiting, and NO upload handling. The `template_only` tier upgrade and
the adversarial spam/upload harness are a SEPARATE, security-classed work
order; until that lands the primitive stays `tier="fillable"` and the generated
endpoint must not be treated as hardened.

Layering: `disco.core` is the leaf package (.importlinter). This module imports
pydantic + the stdlib + sibling core modules only. `generator.py` force-imports
it at the end of its module body (like records/hello), so the registry is
populated before anything generates; generator internals are imported lazily
inside functions (the records_primitive convention) to keep import order a
non-issue.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .primitives import (
    FORM_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    RECORDS_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
    resolve_primitive,
)
from .spec import AppSpec, DesignSpec, Entity, Section

if TYPE_CHECKING:
    from .recipes import SiteRecipe

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


# ---- fold: FormSpec → AppSpec ---------------------------------------------------


def _entity_table(entity: Entity) -> str:
    from .generator import _table_name

    return _table_name(entity)


def apply_form_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated FormSpec into the AppSpec (the house dance: dump → mutate →
    re-validate). Appends the submissions Entity, a `form` Section wired to it via
    `content_ref` (inserted before a trailing footer), and a `submit` primary
    action. Raises ValueError with actionable guidance on any refusal —
    `app_add_primitive` converts raised errors into model-facing refusals."""
    if not isinstance(spec, FormSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {FORM_PRIMITIVE_ID!r} needs a FormSpec")

    base = resolve_primitive(app.app_kind)
    if base.id not in {LEAD_GEN_PRIMITIVE_ID, RECORDS_PRIMITIVE_ID}:
        if base.id == "directory":
            raise ValueError(
                "the form primitive cannot fold into directory apps yet: directory is "
                "a static site with no D1 binding, schema.sql data plane, or dynamic "
                "submission Worker route. Supported hosts are lead_gen-shaped apps and "
                "records apps; directory support must first upgrade the app shape to emit "
                "a Worker + D1 schema for the form instead of shipping a build that cannot "
                "verify or deploy."
            )
        if base.id == "hello":
            raise ValueError(
                "the form primitive cannot fold into hello apps: hello is the "
                "mount-proof minimal primitive, not a Cloudflare/D1 host. Create a "
                "lead_gen or records app first, then add the form primitive."
            )
        raise ValueError(
            f"the form primitive can fold only into lead_gen-shaped apps or records "
            f"apps; this app's app_kind {app.app_kind!r} lowers through {base.id!r}."
        )
    if not app.pages:
        raise ValueError("the app has no pages to place the form section on")

    page_ids = [p.id for p in app.pages]
    if spec.page_id is None:
        target_page_id = page_ids[0]
    elif spec.page_id in page_ids:
        target_page_id = spec.page_id
    else:
        raise ValueError(f"unknown page_id {spec.page_id!r}; known page ids: {', '.join(page_ids)}")

    if any(e.id == spec.form_id for e in app.entities):
        raise ValueError(
            f"an entity with id {spec.form_id!r} already exists in this app — "
            "the form was likely already added; pick another form_id"
        )
    table = _entity_table(Entity(id=spec.form_id, name=spec.title, fields=()))
    taken_tables = {_entity_table(e) for e in app.entities}
    if base.id == LEAD_GEN_PRIMITIVE_ID:
        from .generator import resolve_lead_entity

        taken_tables.add(_entity_table(resolve_lead_entity(app)))
    if table in taken_tables:
        raise ValueError(
            f"the form's submissions table {table!r} collides with an existing "
            "entity's table — pick another form_id"
        )
    target_page = next(p for p in app.pages if p.id == target_page_id)
    if any(s.id == spec.form_id for s in target_page.sections):
        raise ValueError(
            f"a section with id {spec.form_id!r} already exists on page "
            f"{target_page_id!r} — pick another form_id"
        )
    action_id = f"submit_{spec.form_id}"
    if any(a.id == action_id for a in app.primary_actions):
        raise ValueError(f"a primary action with id {action_id!r} already exists")

    entity_json: dict[str, object] = {
        "id": spec.form_id,
        "name": spec.title,
        "fields": [
            {
                "name": f.name,
                "type": _ENTITY_TYPE_BY_KIND[f.kind],
                "required": f.required,
                "label": f.label,
            }
            for f in spec.fields
        ],
    }
    section_json: dict[str, object] = {
        "id": spec.form_id,
        "kind": "form",
        "content_ref": spec.form_id,
        "content": {
            "heading": spec.title,
            "cta_label": "Submit",
            "success_message": spec.success_message,
        },
    }
    action_json: dict[str, object] = {
        "id": action_id,
        "label": spec.title,
        "type": "submit",
        "target": spec.form_id,
    }

    data = app.model_dump(mode="json")
    data["entities"] = [*data["entities"], entity_json]
    for page in data["pages"]:
        if page["id"] != target_page_id:
            continue
        sections = list(page["sections"])
        insert_at = len(sections)
        if sections and sections[-1].get("kind") == "footer":
            insert_at -= 1  # keep a trailing footer last
        sections.insert(insert_at, section_json)
        page["sections"] = sections
    data["primary_actions"] = [*data["primary_actions"], action_json]
    return AppSpec.model_validate(data)


# ---- lowering: the folded AppSpec → the generated form surface -------------------
#
# Called from `_generate_lead_gen`. Every `lower_form_*` is a byte-identical
# pass-through of the base emitter when `forms` is empty — the non-regression
# guarantee for the three pre-existing primitives.


def form_submission_entities_for(
    app: AppSpec, *, reserved_entities: tuple[Entity, ...] = ()
) -> tuple[Entity, ...]:
    """The app's form-submission entities, in entity order: entities targeted by a
    `form` section's `content_ref` (the fold's marker), excluding any reserved
    entities owned by the host primitive (for lead_gen, the resolved lead entity).
    Raises if two forms would lower to the same D1 table, or collide with a reserved
    entity's table (invalid tree otherwise)."""
    targets = {
        s.content_ref
        for p in app.pages
        for s in p.sections
        if s.kind == "form" and s.content_ref is not None
    }
    reserved_ids = {e.id for e in reserved_entities}
    forms = tuple(e for e in app.entities if e.id in targets and e.id not in reserved_ids)
    seen: dict[str, str] = {_entity_table(e): e.id for e in reserved_entities}
    for entity in forms:
        table = _entity_table(entity)
        if table in seen:
            raise ValueError(
                f"form entity {entity.id!r} lowers to table {table!r}, which entity "
                f"{seen[table]!r} already owns — rename the form"
            )
        seen[table] = entity.id
    return forms


def form_entities_for(app: AppSpec, lead: Entity) -> tuple[Entity, ...]:
    """Lead-gen compatibility wrapper: exclude the resolved lead entity so a form
    section pointing at the lead renders the classic capture form."""
    return form_submission_entities_for(app, reserved_entities=(lead,))


def form_route_for(app: AppSpec, entity: Entity) -> str:
    """The public POST route for one folded form entity.

    lead_gen and legacy lead-shaped apps keep the original `/api/<table>` route.
    records apps use `/api/forms/<form_id>` so records retains exclusive ownership
    of its `/api/<table>` CRUD route table.
    """
    base = resolve_primitive(app.app_kind)
    if base.id == RECORDS_PRIMITIVE_ID:
        return f"/api/forms/{entity.id}"
    return f"/api/{_entity_table(entity)}"


def _field_kind(field_type: str) -> str:
    """The FormField kind a folded entity field's type maps back to (`text` for any
    unrecognized hand-authored type — the safe string fallback)."""
    return _KIND_BY_ENTITY_TYPE.get(field_type.strip().lower(), "text")


def emit_form_schema_sql(forms: tuple[Entity, ...]) -> str:
    """The form submissions table blocks that can be appended to any D1-backed
    host primitive. Empty `forms` → empty string."""
    from .generator import _sql_type, _table_name

    blocks: list[str] = []
    for entity in forms:
        cols = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
        for field in entity.fields:
            nullable = " NOT NULL" if field.required else ""
            cols.append(f'  "{field.name}" {_sql_type(field.type)}{nullable}')
        cols.append("  \"created_at\" TEXT NOT NULL DEFAULT (datetime('now'))")
        body = ",\n".join(cols)
        blocks.append(
            f"-- Form primitive (F3.1): submissions table for the {entity.id!r} form.\n"
            f'CREATE TABLE IF NOT EXISTS "{_table_name(entity)}" (\n'
            f"{body}\n"
            ");\n"
        )
    return "\n".join(blocks)


def lower_form_schema_sql(lead: Entity, forms: tuple[Entity, ...]) -> str:
    """`schema.sql`: the lead table (byte-identical base emitter) plus one
    submissions table per form. Empty `forms` → the base output, unchanged."""
    from .generator import _emit_schema_sql

    base = _emit_schema_sql(lead)
    if not forms:
        return base
    return "\n".join([base, emit_form_schema_sql(forms)])


def _form_const_name(entity: Entity) -> str:
    """The Drizzle export const for a form's table. The `form_` prefix keeps it a
    valid, non-reserved TS identifier that can never collide with `leads`."""
    return f"form_{_entity_table(entity)}"


def emit_form_drizzle_ts(forms: tuple[Entity, ...]) -> str:
    """The Drizzle sqliteTable exports for folded forms. Empty `forms` → empty string."""
    from .generator import _drizzle_factory, _table_name, _ts

    blocks: list[str] = []
    for entity in forms:
        cols = ['  id: integer("id").primaryKey({ autoIncrement: true }),']
        for field in entity.fields:
            chain = ".notNull()" if field.required else ""
            cols.append(
                f"  {field.name}: {_drizzle_factory(field.type)}({_ts(field.name)}){chain},"
            )
        cols.append("  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),")
        blocks.append(
            f"/* Form primitive (F3.1): submissions table for the {entity.id!r} form. */\n"
            f"export const {_form_const_name(entity)} = "
            f"sqliteTable({_ts(_table_name(entity))}, {{\n" + "\n".join(cols) + "\n});\n"
        )
    return "\n".join(blocks)


def lower_form_drizzle_ts(lead: Entity, forms: tuple[Entity, ...]) -> str:
    """`src/db/schema.ts`: the lead table (byte-identical base emitter) plus one
    Drizzle sqliteTable export per form. Empty `forms` → the base output."""
    from .generator import _emit_drizzle_schema_ts

    base = _emit_drizzle_schema_ts(lead)
    if not forms:
        return base
    return "\n".join([base, emit_form_drizzle_ts(forms)])


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
    from .generator import _pascal

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
    from .generator import _ts

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
    from .generator import _ts

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
    from .generator import _emit_worker_ts

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


# ---- the React form component -----------------------------------------------------


def _display_label(field_name: str, label: str | None) -> str:
    return label if label else field_name.replace("_", " ").title()


def _form_input_for(section_id: str, name: str, kind: str, label: str, required: bool) -> str:
    """One labeled, controlled input for a form field (mirrors the lead form's
    `_input_for` accessibility shape: required flag, aria-invalid, described-by
    error span). Checkbox binds `checks`/`toggleField`; the rest bind `form`/
    `updateField`."""
    from .generator import _ts

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
    from .generator import (
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


# ---- registration -----------------------------------------------------------------

from .primitive_verify import form_verify  # noqa: E402


def default_form_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The form primitive is an ADD-ON, never a base scaffold — `app_create` must
    not scaffold it. Raising here (the executor maps it to a failed tool result)
    keeps the affordance honest instead of silently aliasing to lead_gen."""
    del name, recipe
    raise ValueError(
        "the 'form' primitive is an ADD-ON, not a base scaffold: app_create a base "
        "app first (e.g. primitive_id='lead_gen'), then "
        "app_add_primitive(primitive_id='form', spec={...})."
    )


def prepare_form_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the form primitive never owns a base app to normalize."""
    return app


def generate_form(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Never called: the HOST app's base primitive regenerates the whole tree from
    the folded AppSpec (the WO-A1 fold-into-AppSpec contract)."""
    del app, design
    raise ValueError(
        "the 'form' primitive does not generate a tree of its own; the host app's "
        "base primitive regenerates from the folded AppSpec."
    )


register_primitive(
    PrimitiveDefinition(
        id=FORM_PRIMITIVE_ID,
        default_app_spec=default_form_app_spec,
        prepare_app_spec=prepare_form_app_spec,
        generate=generate_form,
        tier="fillable",
        host_contract=(),
        spec_schema=FormSpec,
        verify=form_verify,
        apply_spec=apply_form_spec,
    )
)


__all__ = [
    "FORM_PRIMITIVE_ID",
    "FormField",
    "FormSpec",
    "apply_form_spec",
    "emit_form_drizzle_ts",
    "emit_form_schema_sql",
    "emit_app_form_component",
    "form_entities_for",
    "form_route_for",
    "form_submission_entities_for",
    "form_verify",
    "lower_form_drizzle_ts",
    "lower_form_records_worker_ts",
    "lower_form_schema_sql",
    "lower_form_worker_ts",
]
