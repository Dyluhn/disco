"""Epic F3.1 (scaffold half) — the `form` AppKit primitive.

Covers, at the core layer:

* NON-REGRESSION (the hard constraint): the three pre-existing primitives (plus
  hello, the records-auth variant, and the legacy `app_kind` fallback) generate
  BYTE-IDENTICAL trees — pinned by sha256 hashes computed at pristine
  ``9baf7316`` (the exact base commit of this branch, before any F3.1 change);
* FormSpec validation: bounds, unknown keys, unique field names, reserved names;
* apply_form_spec fold correctness: entity/section/action appended, footer kept
  last, unknown-page refusal listing known ids, host-scope + collision refusals,
  lead resolution unperturbed;
* lowering: the folded AppSpec generates the D1 submissions table, the 422
  Worker route mirroring field kinds + required, the React form component with
  every field, and leaves the lead plane + content.ts intact;
* the folded tree still PASSES the local_verify gates (schema/drizzle);
* registration: addable (spec_schema + apply_spec), tier fillable, real verify hook.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from disco.core.appkit.form_primitive import (
    FormSpec,
    apply_form_spec,
    form_entities_for,
)
from disco.core.appkit.generator import generate, resolve_lead_entity
from disco.core.appkit.local_verify import (
    check_drizzle_schema,
    check_schema_sql,
)
from disco.core.appkit.primitives import get_primitive
from disco.core.appkit.recipes import get_recipe
from disco.core.appkit.records_primitive import default_records_auth_app_spec
from disco.core.appkit.spec import (
    Action,
    AppSpec,
    Entity,
    EntityField,
    Page,
    Section,
)
from pydantic import ValidationError

# ---- shared fixtures --------------------------------------------------------------

_RECIPE = get_recipe("editorial-ledger")
assert _RECIPE is not None
_DESIGN = _RECIPE.to_design_spec()


def _lead_gen_app() -> AppSpec:
    prim = get_primitive("lead_gen")
    assert prim is not None
    return prim.prepare_app_spec(prim.default_app_spec("Acme Studio", _RECIPE))


def _form_spec(**overrides: object) -> FormSpec:
    payload: dict[str, object] = {
        "form_id": "quote_request",
        "title": "Request a quote",
        "fields": [
            {"name": "full_name", "label": "Full name", "kind": "text", "required": True},
            {"name": "email", "label": "Email", "kind": "email", "required": True},
            {"name": "details", "label": "Project details", "kind": "textarea"},
            {"name": "budget", "label": "Budget (USD)", "kind": "number"},
            {"name": "subscribe", "label": "Subscribe", "kind": "checkbox", "required": True},
        ],
        "success_message": "Got it — we'll send a quote within two business days.",
    }
    payload.update(overrides)
    return FormSpec.model_validate(payload)


def _folded_tree() -> tuple[AppSpec, dict[str, str]]:
    folded = apply_form_spec(_lead_gen_app(), _form_spec())
    return folded, generate(folded, _DESIGN)


# ---- 1. non-regression: pre-existing primitives are byte-identical -----------------
#
# The golden hashes below pin the complete deterministic generated tree.  WO-F4.1
# intentionally adds the reviewed package-lock.json to Vite-capable outputs; any
# subsequent byte change still requires an explicit re-pin here.

_GOLDEN_HASHES = {
    "lead_gen": "e43ba2d918404a98e0963b227e1d53ab45d7de0c12cd1c914fc51476a8d3755d",
    "directory": "300fb2487a86fee8f2d47ce8fc5a4426010e43584e565744f3b76813df75bebb",
    "records": "243337de655694182ef2c6af2e4648081586ef07a1a0462d0421c2050789054b",
    # WO-F4.1 hardens the shared auth CSRF comparison from host-only to exact
    # scheme+host+port origin equality; that intentional byte change is re-pinned.
    "records_auth": "b7c500495acd0d411f92a69f2c45f12177012beddc85749b304152105b6cf0b1",
    "hello": "8c1b9a6863df3dfefac330ec444327248ab8968d530c601b0f42ee9f4241a4f6",
    "legacy_lead_fallback": "e66debb1eddf8ba2c4002ecde5288c009457d1be9ccf93d69df59e207b16d56c",
}


def _tree_hash(tree: dict[str, str]) -> str:
    payload = json.dumps(tree, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_tree_hash(prim_id: str) -> str:
    prim = get_primitive(prim_id)
    assert prim is not None
    app = prim.prepare_app_spec(prim.default_app_spec("Snapshot App", _RECIPE))
    return _tree_hash(generate(app, _DESIGN))


@pytest.mark.parametrize("prim_id", ["lead_gen", "directory", "records", "hello"])
def test_existing_primitive_output_byte_identical(prim_id: str):
    assert _default_tree_hash(prim_id) == _GOLDEN_HASHES[prim_id]


def test_records_auth_output_byte_identical():
    prim = get_primitive("records")
    assert prim is not None
    app = prim.prepare_app_spec(default_records_auth_app_spec("Shift Calendar", _RECIPE))
    assert _tree_hash(generate(app, _DESIGN)) == _GOLDEN_HASHES["records_auth"]


def _legacy_lead_app() -> AppSpec:
    """A pre-Epic-N `app_kind` (falls back to lead_gen) with a submit action, an
    EXTRA unreferenced entity, and a classic form section WITHOUT `content_ref` —
    pins lead resolution order, the extra-entity-ignored behavior, and the
    classic lead-capture component path all at once."""
    return AppSpec(
        schema_version=1,
        app_kind="web_app",
        name="Legacy Pin",
        pages=(
            Page(
                id="home",
                route="/",
                title="Home",
                sections=(
                    Section(id="hero", kind="hero"),
                    Section(id="contact", kind="form"),
                    Section(id="footer", kind="footer"),
                ),
            ),
        ),
        entities=(
            Entity(
                id="enquiry",
                name="Enquiry",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="email", required=True),
                    EntityField(name="message", type="text"),
                ),
            ),
            Entity(
                id="widget",
                name="Widget",
                fields=(EntityField(name="title", type="str", required=True),),
            ),
        ),
        primary_actions=(Action(id="send", label="Send", type="submit", target="enquiry"),),
    )


def test_legacy_fallback_output_byte_identical():
    assert (
        _tree_hash(generate(_legacy_lead_app(), _DESIGN))
        == (_GOLDEN_HASHES["legacy_lead_fallback"])
    )


def test_unfolded_specs_serialize_unchanged():
    """The additive spec fields (SectionContent.success_message, EntityField.label)
    are `exclude_if`-hidden when unset, so a pre-F3.1 AppSpec's JSON dump — and
    therefore its spec digest / manifest — is unchanged."""
    data = _lead_gen_app().model_dump(mode="json")
    for entity in data["entities"]:
        for field in entity["fields"]:
            assert "label" not in field  # EntityField.label hidden when unset
    for page in data["pages"]:
        for section in page["sections"]:
            content = section.get("content") or {}
            assert "success_message" not in content  # slot hidden when unset


# ---- 2. FormSpec validation ---------------------------------------------------------


def test_form_spec_happy_path_parses():
    spec = _form_spec()
    assert spec.form_id == "quote_request"
    assert [f.kind for f in spec.fields] == [
        "text",
        "email",
        "textarea",
        "number",
        "checkbox",
    ]
    assert spec.page_id is None


def test_form_spec_default_success_message_is_sensible():
    spec = FormSpec.model_validate(
        {
            "form_id": "contact_us",
            "title": "Contact us",
            "fields": [{"name": "email", "label": "Email", "kind": "email"}],
        }
    )
    assert spec.success_message  # non-empty default
    assert len(spec.success_message) <= 300


def test_form_spec_unknown_key_refused():
    with pytest.raises(ValidationError, match="captcha"):
        FormSpec.model_validate(
            {
                "form_id": "f",
                "title": "T",
                "fields": [{"name": "email", "label": "E", "kind": "email"}],
                "captcha": True,
            }
        )


def test_form_spec_duplicate_field_names_refused():
    with pytest.raises(ValidationError, match="duplicate form field name"):
        _form_spec(
            fields=[
                {"name": "email", "label": "Email", "kind": "email"},
                {"name": "email", "label": "Email again", "kind": "text"},
            ]
        )


@pytest.mark.parametrize("bad_id", ["Kebab-Case", "1starts_with_digit", "has space", "lead"])
def test_form_spec_bad_form_id_refused(bad_id: str):
    with pytest.raises(ValidationError):
        _form_spec(form_id=bad_id)


@pytest.mark.parametrize("bad_name", ["id", "created_at", "rowid", "constructor", "Bad-Name"])
def test_form_spec_reserved_or_unsafe_field_name_refused(bad_name: str):
    with pytest.raises(ValidationError):
        _form_spec(fields=[{"name": bad_name, "label": "X", "kind": "text"}])


def test_form_spec_field_count_bounds():
    with pytest.raises(ValidationError):
        _form_spec(fields=[])
    too_many = [{"name": f"field_{i}", "label": f"Field {i}", "kind": "text"} for i in range(13)]
    with pytest.raises(ValidationError):
        _form_spec(fields=too_many)


def test_form_spec_unknown_kind_refused():
    with pytest.raises(ValidationError):
        _form_spec(fields=[{"name": "file", "label": "Upload", "kind": "file"}])


# ---- 3. apply_form_spec fold --------------------------------------------------------


def test_fold_appends_entity_section_and_action():
    app = _lead_gen_app()
    folded = apply_form_spec(app, _form_spec())

    entity = next(e for e in folded.entities if e.id == "quote_request")
    assert entity.name == "Request a quote"
    assert [(f.name, f.type, f.required) for f in entity.fields] == [
        ("full_name", "str", True),
        ("email", "email", True),
        ("details", "text", False),
        ("budget", "float", False),
        ("subscribe", "bool", True),
    ]
    assert [f.label for f in entity.fields] == [
        "Full name",
        "Email",
        "Project details",
        "Budget (USD)",
        "Subscribe",
    ]

    page = folded.pages[0]
    section = next(s for s in page.sections if s.id == "quote_request")
    assert section.kind == "form"
    assert section.content_ref == "quote_request"
    assert section.content is not None
    assert section.content.heading == "Request a quote"
    assert section.content.success_message == (
        "Got it — we'll send a quote within two business days."
    )
    # a trailing footer stays LAST; the form slots in before it
    assert page.sections[-1].kind == "footer"

    action = next(a for a in folded.primary_actions if a.id == "submit_quote_request")
    assert action.type == "submit"
    assert action.target == "quote_request"


def test_fold_targets_explicit_page_and_refuses_unknown_page():
    app = _lead_gen_app()
    folded = apply_form_spec(app, _form_spec(page_id="home"))
    assert any(s.id == "quote_request" for s in folded.pages[0].sections)

    with pytest.raises(ValueError, match=r"unknown page_id.*known page ids: home"):
        apply_form_spec(app, _form_spec(page_id="pricing"))


def test_fold_refused_on_static_or_mount_proof_hosts():
    for prim_id, msg in (
        ("directory", "static site with no D1"),
        ("hello", "mount-proof"),
    ):
        prim = get_primitive(prim_id)
        assert prim is not None
        host = prim.prepare_app_spec(prim.default_app_spec("Host App", _RECIPE))
        with pytest.raises(ValueError, match=msg):
            apply_form_spec(host, _form_spec())


def test_fold_records_host_generates_namespaced_form_route():
    prim = get_primitive("records")
    assert prim is not None
    host = prim.prepare_app_spec(prim.default_app_spec("Host App", _RECIPE))
    folded = apply_form_spec(host, _form_spec())
    tree = generate(folded, _DESIGN)

    assert any(e.id == "quote_request" for e in folded.entities)
    assert 'CREATE TABLE IF NOT EXISTS "quote_requests"' in tree["schema.sql"]
    assert '"/api/forms/quote_request"' in tree["worker/index.ts"]
    assert '"/api/quote_requests"' not in tree["worker/index.ts"]
    assert (
        'const POST_PATH = "/api/forms/quote_request";'
        in tree["src/components/HomeQuoteRequestSection.tsx"]
    )


def test_fold_reapply_refused_as_collision():
    folded = apply_form_spec(_lead_gen_app(), _form_spec())
    with pytest.raises(ValueError, match="already exists"):
        apply_form_spec(folded, _form_spec())


def test_fold_refuses_table_collision_with_lead():
    # form_id 'leads' → table 'leads' == the lead entity's table
    with pytest.raises(ValueError, match="collides"):
        apply_form_spec(_lead_gen_app(), _form_spec(form_id="leads"))


def test_fold_does_not_perturb_lead_resolution():
    app = _lead_gen_app()
    lead_before = resolve_lead_entity(app)
    folded = apply_form_spec(app, _form_spec())
    assert resolve_lead_entity(folded).id == lead_before.id

    # Even a host WITHOUT any submit action keeps its lead: the form's own
    # submit action never becomes the lead target (the _form_target_ids skip).
    data = app.model_dump(mode="json")
    data["primary_actions"] = []
    no_action_host = AppSpec.model_validate(data)
    folded2 = apply_form_spec(no_action_host, _form_spec())
    assert resolve_lead_entity(folded2).id == lead_before.id


def test_fold_revalidates_through_appspec():
    folded = apply_form_spec(_lead_gen_app(), _form_spec())
    # a genuinely re-validated AppSpec instance (not a mutated dump)
    assert isinstance(folded, AppSpec)
    _ = AppSpec.model_validate(folded.model_dump(mode="json"))


# ---- 4. lowering through the lead_gen base primitive --------------------------------


def test_folded_tree_has_submissions_table_with_kinds_and_required():
    _, tree = _folded_tree()
    schema = tree["schema.sql"]
    # the lead table is untouched and FIRST; the form table follows
    assert schema.index('CREATE TABLE IF NOT EXISTS "leads"') < schema.index(
        'CREATE TABLE IF NOT EXISTS "quote_requests"'
    )
    assert '"full_name" TEXT NOT NULL' in schema
    assert '"budget" REAL' in schema
    assert '"subscribe" INTEGER NOT NULL' in schema
    assert '"details" TEXT' in schema


def test_folded_tree_worker_route_mirrors_kinds_required_and_422():
    _, tree = _folded_tree()
    worker = tree["worker/index.ts"]
    # the form plane
    assert '"/api/quote_requests"' in worker
    assert "APP_FORM_ROUTES" in worker
    assert "return json({ error: check.error }, 422);" in worker
    # required + kind mirroring in the emitted validator
    assert "missing required field" in worker
    assert "field must be a number" in worker
    assert "field must be a boolean" in worker
    assert '"required": true' in worker
    assert '"kind": "checkbox"' in worker
    # the lead plane is still intact (byte-level lead contract untouched)
    assert 'url.pathname === "/api/leads" && request.method === "POST"' in worker
    assert "const REQUIRED: string[]" in worker
    # imports widened to include the form table const
    assert 'import { leads, form_quote_requests } from "../src/db/schema";' in worker


def test_folded_tree_drizzle_has_form_table():
    _, tree = _folded_tree()
    drizzle = tree["src/db/schema.ts"]
    assert 'export const leads = sqliteTable("leads"' in drizzle
    assert 'export const form_quote_requests = sqliteTable("quote_requests"' in drizzle
    assert 'budget: real("budget"),' in drizzle
    # subscribe is a REQUIRED checkbox in the fixture → NOT NULL / .notNull()
    assert 'subscribe: integer("subscribe").notNull(),' in drizzle


def test_folded_tree_component_has_all_fields_and_success_message():
    _, tree = _folded_tree()
    comp = tree["src/components/HomeQuoteRequestSection.tsx"]
    for name in ("full_name", "email", "details", "budget", "subscribe"):
        assert f'name="{name}"' in comp
    assert 'type="email"' in comp
    assert 'type="number"' in comp
    assert 'type="checkbox"' in comp
    assert "<textarea" in comp
    assert 'const POST_PATH = "/api/quote_requests";' in comp
    assert "function validateRequired(): boolean" in comp
    assert "Got it — we'll send a quote within two business days." in comp
    assert 'disabled={state.kind === "submitting"}' in comp
    assert 'aria-live="polite"' in comp
    # wired into the app shell
    assert "HomeQuoteRequestSection" in tree["src/App.tsx"]


def test_folded_tree_keeps_classic_lead_form_and_content_ts_shape():
    _, tree = _folded_tree()
    # the original lead-capture section still posts through useSubmit("/api/leads")
    assert 'useSubmit("/api/leads")' in tree["src/components/HomeContactSection.tsx"]
    # success_message is now also in content.ts so app_update_content can reach it;
    # the component keeps a spec-derived default literal for deterministic regeneration.
    assert "successMessage" in tree["src/generated/content.ts"]
    assert (
        "Got it — we'll send a quote within two business days." in tree["src/generated/content.ts"]
    )


def test_folded_tree_passes_local_verify_gates():
    folded, tree = _folded_tree()
    lead = resolve_lead_entity(folded)
    schema_res = check_schema_sql(tree["schema.sql"], lead)
    assert schema_res.passed, schema_res.evidence
    drizzle_res = check_drizzle_schema(tree)
    assert drizzle_res.passed, drizzle_res.evidence


def test_form_entities_derivation_and_table_collision_guard():
    folded, _ = _folded_tree()
    lead = resolve_lead_entity(folded)
    forms = form_entities_for(folded, lead)
    assert [e.id for e in forms] == ["quote_request"]
    # unfolded app → no forms
    assert form_entities_for(_lead_gen_app(), lead) == ()


# ---- 5. registration ------------------------------------------------------------------


def test_form_primitive_registered_as_addable_fillable():
    prim = get_primitive("form")
    assert prim is not None
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    assert prim.verify is not None
    assert prim.spec_schema is FormSpec
    assert prim.apply_spec is apply_form_spec


def test_form_primitive_is_not_a_base_scaffold():
    prim = get_primitive("form")
    assert prim is not None
    with pytest.raises(ValueError, match="ADD-ON"):
        _ = prim.default_app_spec("X", _RECIPE)
    with pytest.raises(ValueError, match="does not generate"):
        _ = prim.generate(_lead_gen_app(), _DESIGN)
