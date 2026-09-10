"""AppKit EPIC E1 — the pure lead-gen generator.

Proves:
  * DETERMINISM — a fixed AppSpec + DesignSpec lowers to a byte-identical sorted
    {path: contents} tree (same specs in → identical bytes out), incl. across two
    independent constructions of the same specs (catches dict/set-ordering drift);
  * the generated tree is design_lint-CLEAN (the in-tree probe is in the tools
    package; here we assert the structural cleanliness invariants the linter keys
    off — real fonts, no AI-purple, no pill radius, no emoji, no grid-cols-3);
  * schema.sql is valid SQLite that round-trips a lead insert;
  * the new Section schema fields (variant_id / content) round-trip + are bounded;
  * the lead entity is derived (submit target / id 'lead' / synthesized).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from disco.core.appkit import (
    RECORDS_PRIMITIVE_ID,
    Action,
    DesignSpec,
    Entity,
    EntityField,
    Page,
    Section,
    SectionContent,
    check_drizzle_schema,
    default_lead_gen_app_spec,
    ensure_lead_entity,
    generate,
    get_recipe,
    resolve_lead_entity,
    synthesized_lead_entity,
)
from disco.core.appkit.semantic_metadata import METADATA_VERSION
from disco.core.appkit.spec import AppSpec

_LEAD_GEN_ACME_WORKER_SCHEMA_DIGEST = (
    "cfe5bfd3497d8e566d2e804286143694307a184ed68d230d4e01e83c3b165d13"
)

# The single most common generated-site tells the generator must never emit.
_AI_PURPLE = ("#7c3aed", "#8b5cf6", "#6366f1", "#a855f7")
_GENERIC_FONT_PRIMARY = ("font-family: inter", "family=Inter", "family=Geist")


def _design() -> DesignSpec:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe.to_design_spec()


def _app() -> AppSpec:
    return AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name="Acme Studio",
        pages=(
            Page(
                id="home",
                route="/",
                title="Home",
                sections=(
                    Section(
                        id="hero",
                        kind="hero",
                        variant_id="hero.asymmetric-editorial",
                        content=SectionContent(
                            heading="We build things",
                            subheading="On time.",
                            cta_label="Get a quote",
                        ),
                    ),
                    Section(
                        id="feat",
                        kind="features",
                        variant_id="features.alternating-rows",
                        content=SectionContent(
                            heading="What we do",
                            items=("Design", "Build", "Ship"),
                        ),
                    ),
                    Section(
                        id="signup",
                        kind="form",
                        content=SectionContent(heading="Contact", cta_label="Send"),
                    ),
                    Section(
                        id="foot",
                        kind="footer",
                        variant_id="footer.multi-column-sitemap",
                        content=SectionContent(heading="Acme Studio"),
                    ),
                ),
            ),
        ),
        entities=(
            Entity(
                id="lead",
                name="Lead",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="str", required=True),
                    EntityField(name="message", type="text", required=False),
                ),
            ),
        ),
        primary_actions=(Action(id="submit_lead", label="Send", type="submit", target="lead"),),
    )


# ---- determinism (golden snapshot) --------------------------------------------


def _digest(tree: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(tree):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(tree[path].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _digest_paths(tree: dict[str, str], paths: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for path in paths:
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(tree[path].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def test_generate_is_deterministic_byte_identical():
    # Two INDEPENDENT constructions of the same specs must yield byte-identical
    # trees — catches any dict/set-ordering nondeterminism in the emitters.
    a = generate(_app(), _design())
    b = generate(_app(), _design())
    assert a == b
    assert _digest(a) == _digest(b)


def test_generate_tree_is_sorted():
    tree = generate(_app(), _design())
    assert list(tree) == sorted(tree)


def test_generate_emits_the_expected_tree():
    tree = generate(_app(), _design())
    # the SPA
    for path in (
        "index.html",
        "src/main.tsx",
        "src/App.tsx",
        "src/api/client.ts",
        "src/hooks/useSubmit.ts",
        "src/styles.css",
        "src/db/schema.ts",
        "src/generated/content.ts",
        "src/generated/manifest.ts",
        # the Cloudflare side
        "worker/index.ts",
        "schema.sql",
        "wrangler.toml",
        # build glue
        "drizzle.config.ts",
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
    ):
        assert path in tree, path
    # one component per section (4 sections)
    comps = [p for p in tree if p.startswith("src/components/")]
    assert len(comps) == 4


def test_design_tokens_flow_into_css():
    design = _design()
    css = generate(_app(), design)["src/styles.css"]
    # palette → CSS custom properties; real heading font declared
    assert f"--color-primary: {design.palette.primary};" in css
    assert f"--color-surface: {design.palette.surface};" in css
    assert design.typography.heading_font in css


def test_variant_drives_component_class():
    tree = generate(_app(), _design())
    hero = next(tree[p] for p in tree if p.endswith("HeroSection.tsx"))
    assert "variant-asymmetric-editorial" in hero
    assert "kind-hero" in hero


def test_form_component_emits_reactive_submit_contract():
    # Guards the f-string brace-escaping in the lead form: the onChange handler must
    # be balanced JSX, not the `})}}` mis-escape a careless line-split produces.
    tree = generate(_app(), _design())
    form = next(tree[p] for p in tree if p.endswith("SignupSection.tsx"))
    client = tree["src/api/client.ts"]
    hook = tree["src/hooks/useSubmit.ts"]
    # fields stay bound through the form-state updater, never bare/injectable JS keys
    assert 'onChange={(e) => updateField("name", e.target.value)} />' in form
    assert 'value={form["name"] ?? ""}' in form
    assert "}})}}" not in form  # the broken double-escape must never appear
    assert 'import { useSubmit } from "../hooks/useSubmit";' in form
    assert 'useSubmit("/api/leads")' in form
    assert "function validateRequired(): boolean" in form
    assert 'fieldErrors["name"]' in form
    assert "if (!validateRequired()) return;" in form
    assert 'disabled={state.kind === "submitting"}' in form
    assert 'state.kind === "submitting" ? "Sending…"' in form
    assert 'aria-live="polite"' in form
    assert "Recently submitted" in form
    assert "submitted.map((entry)" in form
    assert "form-status-error" in form
    assert "fetch(" not in form
    # the single fetch chokepoint is typed and reads {error} on failure
    assert "export type ApiResult<T>" in client
    assert "export async function postJson<T>(path: string, body: unknown)" in client
    assert 'headers: { "Content-Type": "application/json" }' in client
    assert "errorFromBody(body)" in client
    assert "any" not in client
    assert "export type SubmitState" in hook
    assert '| { kind: "submitting" }' in hook
    assert 'setState({ kind: "error", message: result.error });' in hook
    assert "setSubmitted((current) => [entry, ...current]);" in hook
    assert "current.filter((item) => item.id !== entry.id)" in hook


def test_records_form_uses_entity_route_with_shared_submit_hook():
    app = AppSpec(
        schema_version=1,
        app_kind=RECORDS_PRIMITIVE_ID,
        name="Records Form",
        pages=(
            Page(
                id="home",
                route="/",
                title="Home",
                sections=(
                    Section(
                        id="signup",
                        kind="form",
                        content=SectionContent(heading="Add member", cta_label="Save"),
                    ),
                ),
            ),
        ),
        entities=(
            Entity(
                id="team_member",
                name="Team Member",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="email", required=True),
                ),
            ),
        ),
    )
    tree = generate(app, _design())
    form = next(tree[p] for p in tree if p.endswith("SignupSection.tsx"))
    assert "src/api/client.ts" in tree
    assert "src/hooks/useSubmit.ts" in tree
    assert 'useSubmit("/api/team_member")' in form


def test_lead_gen_worker_and_schema_outputs_stay_stable():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    tree = generate(default_lead_gen_app_spec("Acme", recipe), recipe.to_design_spec())
    assert (
        _digest_paths(tree, ("schema.sql", "worker/index.ts", "src/db/schema.ts"))
        == _LEAD_GEN_ACME_WORKER_SCHEMA_DIGEST
    )


def test_generated_jsx_braces_and_parens_balanced():
    tree = generate(_app(), _design())
    for path in tree:
        if not (path.endswith(".tsx") or path.endswith(".ts")):
            continue
        text = tree[path]
        assert text.count("{") == text.count("}"), f"{path}: unbalanced braces"
        assert text.count("(") == text.count(")"), f"{path}: unbalanced parens"


def test_content_lives_in_content_ts_not_components():
    tree = generate(_app(), _design())
    content_ts = tree["src/generated/content.ts"]
    assert "We build things" in content_ts
    # the component reads from CONTENT by id, so it does NOT embed the literal copy
    hero = next(tree[p] for p in tree if p.endswith("HeroSection.tsx"))
    assert "We build things" not in hero
    assert "CONTENT[" in hero


# ---- EPIC J: semantic edit metadata (data-disco-*) ----------------------------


def test_document_and_section_roots_carry_disco_semantic_attrs():
    # Every section root maps a UI click back to the AppSpec slot it renders: the
    # spec file, section id, and stable screen label (what the P8 selection agent reads).
    tree = generate(_app(), _design())
    assert f'<html lang="en" data-disco-version="{METADATA_VERSION}">' in tree["index.html"]
    for stem, section_id, screen_label in (
        ("HeroSection.tsx", "hero", "hero"),
        ("FeatSection.tsx", "feat", "feat"),
        ("SignupSection.tsx", "signup", "signup"),
        ("FootSection.tsx", "foot", "foot"),
    ):
        comp = next(tree[p] for p in tree if p.endswith(stem))
        assert 'data-disco-file=".disco/appspec.json"' in comp, stem
        assert f"data-disco-section={json.dumps(section_id)}" in comp, stem
        assert f"data-disco-screen-label={json.dumps(screen_label)}" in comp, stem
        # the Epic G marker is still present (coverage + back-compat)
        assert f"data-appkit-section={json.dumps(section_id)}" in comp, stem


def test_content_elements_carry_field_slot_tags():
    tree = generate(_app(), _design())
    hero = next(tree[p] for p in tree if p.endswith("HeroSection.tsx"))
    # the AppSpec slot name (cta_label, NOT the ctaLabel content key) tags each element
    assert 'data-disco-field="heading"' in hero
    assert 'data-disco-field="subheading"' in hero
    assert 'data-disco-field="cta_label"' in hero
    feat = next(tree[p] for p in tree if p.endswith("FeatSection.tsx"))
    # item rows carry the slot AND their 0-based index so a single item maps back
    assert 'data-disco-field="items"' in feat
    assert 'data-disco-collection="feat.items"' in feat
    assert "data-disco-index={i}" in feat
    assert 'data-disco-item-kind="item"' in feat
    form = next(tree[p] for p in tree if p.endswith("SignupSection.tsx"))
    assert 'data-disco-field="heading"' in form
    assert 'data-disco-field="cta_label"' in form  # the submit button


def test_field_tags_use_appspec_slot_names_the_tool_accepts():
    # cta_label is the AppSpec/app_update_content key; the generated content KEY is
    # ctaLabel. The semantic tag MUST be the spec slot name (cta_label) so a click
    # forms a valid app_update_content update — assert the tag is never `ctaLabel`.
    tree = generate(_app(), _design())
    blob = "\n".join(tree[p] for p in tree if p.endswith(".tsx"))
    assert 'data-disco-field="cta_label"' in blob
    assert 'data-disco-field="ctaLabel"' not in blob


def test_generic_section_branch_carries_appkit_and_disco_markers():
    # The custom/table/generic fallback branch previously MISSED data-appkit-section;
    # Epic J adds it (+ the disco attrs) so coverage + click-to-edit cover it too.
    app = AppSpec.model_validate(
        {
            **_app().model_dump(mode="json"),
            "pages": [
                {
                    "id": "home",
                    "route": "/",
                    "title": "Home",
                    "sections": [
                        {"id": "custom_block", "kind": "custom", "content": {"heading": "Custom"}},
                    ],
                }
            ],
        }
    )
    tree = generate(app, _design())
    comp = next(tree[p] for p in tree if p.endswith("Section.tsx"))
    assert 'data-appkit-section="custom_block"' in comp
    assert 'data-disco-section="custom_block"' in comp
    assert 'data-disco-screen-label="custom-block"' in comp
    assert 'data-disco-file=".disco/appspec.json"' in comp


def test_directory_listing_branch_carries_disco_semantic_attrs():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    from disco.core.appkit import default_directory_app_spec

    app = default_directory_app_spec("Acme Directory", recipe)
    tree = generate(app, recipe.to_design_spec())
    listing = next(tree[p] for p in tree if p.endswith("ListingsSection.tsx"))
    assert 'data-appkit-section="listings"' in listing
    assert 'data-disco-section="listings"' in listing
    assert 'data-disco-screen-label="listings"' in listing
    assert 'data-disco-field="heading"' in listing
    assert 'data-disco-field="subheading"' in listing
    assert 'data-disco-field="items"' in listing
    assert 'data-disco-collection="listings.items"' in listing
    assert "data-disco-index={i}" in listing
    assert 'data-disco-item-kind="item"' in listing


def test_disco_metadata_preserves_determinism_and_no_content_leak():
    # The additive attrs keep the tree byte-deterministic and never embed spec copy
    # into a component (content still lives in content.ts).
    a = generate(_app(), _design())
    b = generate(_app(), _design())
    assert a == b
    hero = next(a[p] for p in a if p.endswith("HeroSection.tsx"))
    assert "We build things" not in hero  # literal copy stays out of the component


# ---- design_lint cleanliness (structural invariants) --------------------------


def test_generated_tree_has_no_slop_tells():
    tree = generate(_app(), _design())
    blob = "\n".join(tree[p].lower() for p in sorted(tree))
    for purple in _AI_PURPLE:
        assert purple not in blob, f"AI-purple {purple} leaked"
    for tell in _GENERIC_FONT_PRIMARY:
        assert tell.lower() not in blob, f"generic-font tell {tell} leaked"
    # no pill radius, no 3-col grid tell, no rounded-full
    assert "9999px" not in blob
    assert "rounded-full" not in blob
    assert "grid-cols-3" not in blob
    assert "repeat(3," not in blob


def test_lint_clean_for_every_recipe():
    # The real linter lives in the tools package; import it lazily so core tests
    # don't hard-depend on it, and prove every recipe yields ZERO findings.
    from disco.core.appkit import RECIPES

    # design_lint arrives with the B2 tool-layer port — self-activate then
    # instead of failing the B1 core port on a not-yet-ported module.
    pytest.importorskip("disco.tools.builtin.design_lint")
    from disco.tools.builtin.design_lint import lint_design  # type: ignore

    for recipe in RECIPES:
        design = recipe.to_design_spec()
        prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
        app = default_lead_gen_app_spec(recipe.name, recipe)
        verdict = lint_design(generate(app, design), design, spec_present=True, spec_valid=True)
        assert verdict["ok"], (recipe.id, verdict["summary"], verdict["findings"])
        _ = prefs  # documented: variants come from the recipe via the default spec


# ---- BLOCKER 1: collision-free generated component naming ---------------------


def _app_with_colliding_section_ids() -> AppSpec:
    # Two pages whose ids normalize to the SAME PascalCase (`about-us` / `about_us`
    # → `AboutUs`), each carrying a section whose id ALSO normalizes the same
    # (`top-bar` / `top_bar` → `TopBar`). Both (page, section) pairs therefore
    # normalize to the SAME `AboutUsTopBarSection` — a valid AppSpec (raw ids are
    # globally unique) that the naive generator would lower to two same-named files.
    return AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name="Collide Co",
        pages=(
            Page(
                id="about-us",
                route="/about-us",
                title="About",
                sections=(Section(id="top-bar", kind="hero", content=SectionContent(heading="A")),),
            ),
            Page(
                id="about_us",
                route="/about_us",
                title="About 2",
                sections=(
                    Section(
                        id="top_bar",
                        kind="features",
                        content=SectionContent(heading="B", items=("x",)),
                    ),
                ),
            ),
        ),
        entities=(
            Entity(
                id="lead",
                name="Lead",
                fields=(EntityField(name="email", type="str", required=True),),
            ),
        ),
        primary_actions=(Action(id="submit_lead", label="Send", type="submit", target="lead"),),
    )


def test_normalized_name_collision_yields_distinct_components():
    # A VALID AppSpec whose page/section ids collide under PascalCase normalization
    # must still produce DISTINCT component files (no overwrite / duplicate decl).
    tree = generate(_app_with_colliding_section_ids(), _design())
    comps = sorted(p for p in tree if p.startswith("src/components/"))
    # two sections → two DISTINCT component files (not one overwritten file)
    assert len(comps) == 2
    assert len(set(comps)) == 2
    # the disambiguated name is deterministic: base claims the bare name, the second
    # gets the smallest integer inserted before the `Section` suffix
    assert comps == [
        "src/components/AboutUsTopBar2Section.tsx",
        "src/components/AboutUsTopBarSection.tsx",
    ]


def test_collision_app_tsx_imports_resolve_to_real_files():
    # Every component App.tsx imports must point at a file that actually exists in
    # the tree, and the two imports/usages must be DISTINCT (no duplicate symbol).
    tree = generate(_app_with_colliding_section_ids(), _design())
    app_tsx = tree["src/App.tsx"]
    import_names: list[str] = []
    for line in app_tsx.splitlines():
        if line.startswith("import ") and "./components/" in line:
            sym = line.split("import ", 1)[1].split(" from", 1)[0].strip()
            path = line.split('"./components/', 1)[1].rsplit('"', 1)[0]
            import_names.append(sym)
            assert f"src/components/{path}.tsx" in tree, line
    # no duplicate-declaration: every imported symbol is unique
    assert len(import_names) == len(set(import_names)) == 2
    # and each imported symbol is actually rendered
    for sym in import_names:
        assert f"<{sym} />" in app_tsx


def test_collision_tree_is_deterministic_and_lint_clean_tsx():
    # determinism survives the disambiguation (byte-identical across constructions)
    a = generate(_app_with_colliding_section_ids(), _design())
    b = generate(_app_with_colliding_section_ids(), _design())
    assert a == b
    # the generated TSX stays structurally balanced (no broken duplicate decl)
    for path, text in a.items():
        if path.endswith(".tsx") or path.endswith(".ts"):
            assert text.count("{") == text.count("}"), path
            assert text.count("(") == text.count(")"), path
    # content.ts + manifest key off the SAME disambiguated names (refs don't drift)
    content_ts = a["src/generated/content.ts"]
    manifest = a["src/generated/manifest.ts"]
    for name in ("AboutUsTopBarSection", "AboutUsTopBar2Section"):
        assert name in content_ts, name
        assert name in manifest, name


# ---- schema.sql validity ------------------------------------------------------


def test_schema_sql_is_valid_and_round_trips():
    tree = generate(_app(), _design())
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(tree["schema.sql"])  # raises on invalid SQL
        con.execute(
            "INSERT INTO leads (name, email, message) VALUES (?, ?, ?)",
            ("Ada", "ada@example.com", "hello"),
        )
        con.commit()
        row = con.execute("SELECT name, email, message FROM leads").fetchone()
        assert row == ("Ada", "ada@example.com", "hello")
        # required columns are NOT NULL
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO leads (email) VALUES (?)", ("x@y.z",))
    finally:
        con.close()


def test_schema_columns_track_entity_fields():
    app = _app().model_copy()
    # an entity with an extra field → an extra column, deterministically
    custom = AppSpec.model_validate(
        {
            **_app().model_dump(mode="json"),
            "entities": [
                {
                    "id": "lead",
                    "name": "Lead",
                    "fields": [
                        {"name": "name", "type": "str", "required": True},
                        {"name": "company", "type": "str", "required": False},
                        {"name": "headcount", "type": "int", "required": False},
                    ],
                }
            ],
        }
    )
    sql = generate(custom, _design())["schema.sql"]
    # identifiers are double-quoted (defense-in-depth)
    assert '"company" TEXT' in sql
    assert '"headcount" INTEGER' in sql
    _ = app


def test_drizzle_schema_tracks_entity_fields_and_config_is_minimal():
    custom = AppSpec.model_validate(
        {
            **_app().model_dump(mode="json"),
            "entities": [
                {
                    "id": "lead",
                    "name": "Lead",
                    "fields": [
                        {"name": "name", "type": "str", "required": True},
                        {"name": "company", "type": "str", "required": False},
                        {"name": "headcount", "type": "int", "required": False},
                        {"name": "budget", "type": "float", "required": False},
                    ],
                }
            ],
        }
    )
    tree = generate(custom, _design())
    schema_ts = tree["src/db/schema.ts"]
    assert 'export const leads = sqliteTable("leads", {' in schema_ts
    assert 'id: integer("id").primaryKey({ autoIncrement: true })' in schema_ts
    assert 'name: text("name").notNull()' in schema_ts
    assert 'company: text("company")' in schema_ts
    assert 'headcount: integer("headcount")' in schema_ts
    assert 'budget: real("budget")' in schema_ts
    assert "created_at" in schema_ts and ".default(sql`(datetime('now'))`)" in schema_ts
    assert tree["drizzle.config.ts"] == (
        'import { defineConfig } from "drizzle-kit";\n'
        "\n"
        "export default defineConfig({\n"
        '  dialect: "sqlite",\n'
        '  schema: "./src/db/schema.ts",\n'
        '  out: "./drizzle",\n'
        "});\n"
    )


def test_check_drizzle_schema_passes_on_generated_tree_and_catches_drift():
    tree = generate(_app(), _design())
    res = check_drizzle_schema(tree)
    assert res.passed, res.evidence

    missing_column = {
        **tree,
        "src/db/schema.ts": tree["src/db/schema.ts"].replace('  message: text("message"),\n', ""),
    }
    missing_res = check_drizzle_schema(missing_column)
    assert not missing_res.passed
    assert "columns" in missing_res.evidence

    pkg = json.loads(tree["package.json"])
    del pkg["dependencies"]["drizzle-orm"]
    missing_dep = {**tree, "package.json": json.dumps(pkg)}
    dep_res = check_drizzle_schema(missing_dep)
    assert not dep_res.passed
    assert "drizzle-orm" in dep_res.evidence


# ---- lead entity derivation ---------------------------------------------------


def test_resolve_lead_from_submit_target():
    assert resolve_lead_entity(_app()).id == "lead"


def test_resolve_lead_synthesized_when_absent():
    app = AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name="Bare",
        pages=(Page(id="home", route="/", title="Home", sections=()),),
    )
    lead = resolve_lead_entity(app)
    assert lead.id == "lead"
    assert {f.name for f in lead.fields} == {"name", "email", "message"}


def test_ensure_lead_entity_appends_when_missing():
    app = AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name="Bare",
        pages=(Page(id="home", route="/", title="Home", sections=()),),
    )
    assert not app.entities
    ensured = ensure_lead_entity(app)
    assert any(e.id == "lead" for e in ensured.entities)
    # idempotent
    assert ensure_lead_entity(ensured) == ensured


def test_synthesized_lead_entity_shape():
    e = synthesized_lead_entity()
    assert e.id == "lead"
    assert [f.name for f in e.fields] == ["name", "email", "message"]


# ---- worker / wrangler shape --------------------------------------------------


def test_worker_and_wrangler_wire_d1_and_spa():
    tree = generate(_app(), _design())
    worker = tree["worker/index.ts"]
    assert "/api/leads" in worker
    assert 'import { drizzle } from "drizzle-orm/d1";' in worker
    assert 'import { leads } from "../src/db/schema";' in worker
    assert "db.insert(leads).values(leadValues(rec)).run()" in worker
    assert "db.select().from(leads)" in worker
    assert "INSERT INTO" not in worker
    assert "/admin" in worker  # admin read-back route
    assert "env.ASSETS.fetch" in worker  # static asset serving
    wrangler = tree["wrangler.toml"]
    assert 'not_found_handling = "single-page-application"' in wrangler
    assert "[[d1_databases]]" in wrangler
    assert "[assets]" in wrangler


# ---- Epic I: Cloudflare export deliverables -----------------------------------


def test_cloudflare_export_emits_owner_guide_and_secret_template():
    tree = generate(_app(), _design())
    # the owner guide + secret template + gitignore are part of the tree
    assert "OWNER_GUIDE.md" in tree
    assert ".dev.vars.example" in tree
    assert ".gitignore" in tree
    guide = tree["OWNER_GUIDE.md"]
    # documents the exact deploy steps the owner runs (Disco never deploys)
    for step in ("wrangler d1 create", "wrangler secret put ADMIN_TOKEN", "wrangler deploy"):
        assert step in guide, step
    # local-CF verify path is documented too
    assert "wrangler dev" in guide and "db:local" in guide
    # the secret template carries a placeholder, never a real secret
    dev_vars = tree[".dev.vars.example"]
    assert "ADMIN_TOKEN=replace-me" in dev_vars
    # .gitignore keeps the real secret out of git but not the template
    gitignore = tree[".gitignore"]
    assert ".dev.vars" in gitignore
    assert "node_modules/" in gitignore and "dist/" in gitignore


def test_wrangler_routes_dynamic_paths_worker_first():
    # /api/* + /admin are routed worker-first so the SPA asset layer can't shadow them.
    wrangler = generate(_app(), _design())["wrangler.toml"]
    assert 'run_worker_first = ["/api/*", "/admin"]' in wrangler


def test_package_json_has_d1_and_cf_dev_scripts_with_consistent_db_name():
    tree = generate(_app(), _design())
    import json as _json

    pkg = _json.loads(tree["package.json"])
    scripts = pkg["scripts"]
    assert scripts["cf:dev"] == "wrangler dev"
    # the d1 init scripts target the SAME db name the wrangler binding declares
    wrangler = tree["wrangler.toml"]
    import re as _re

    m = _re.search(r'database_name = "([^"]+)"', wrangler)
    assert m is not None
    db_name = m.group(1)
    assert scripts["db:local"] == f"wrangler d1 execute {db_name} --local --file=./schema.sql"
    assert scripts["db:remote"] == f"wrangler d1 execute {db_name} --remote --file=./schema.sql"
    # the owner guide references the same db name (no drift across deliverables)
    assert db_name in tree["OWNER_GUIDE.md"]
    assert "src/db/schema.ts" in tree["OWNER_GUIDE.md"]
    assert pkg["dependencies"]["drizzle-orm"].startswith("^")
    assert pkg["devDependencies"]["drizzle-kit"].startswith("^")
    # wrangler v4.20+ for the array form of run_worker_first
    assert pkg["devDependencies"]["wrangler"].startswith("^4.")


# ---- P0/P1: generated worker security -----------------------------------------


def test_worker_read_endpoints_are_auth_gated_and_fail_closed():
    worker = generate(_app(), _design())["worker/index.ts"]
    # the read endpoints check a Bearer token and reject without it (401)
    assert "isAuthorized(request, env)" in worker
    assert "Authorization" in worker
    assert "Bearer " in worker
    assert "401" in worker
    # FAIL CLOSED: a missing ADMIN_TOKEN denies reads (no hardcoded default)
    assert "ADMIN_TOKEN" in worker
    assert "if (!expected) return false;" in worker
    # both reads guard before touching the DB
    assert worker.count("if (!isAuthorized(request, env)) {") == 2
    # POST submission stays public (no auth guard on the POST branch)
    post_branch = worker.split('request.method === "POST"')[1].split('request.method === "GET"')[0]
    assert "isAuthorized" not in post_branch


def test_worker_admin_render_html_escapes_lead_values():
    worker = generate(_app(), _design())["worker/index.ts"]
    # a safe escapeHtml exists and escapes the five HTML-significant chars
    assert "function escapeHtml(" in worker
    for piece in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert piece in worker
    # every rendered cell goes through escapeHtml — NO raw `.toString()` into HTML
    assert "escapeHtml(r[c]" in worker
    assert "escapeHtml(c)" in worker
    assert ".toString()" not in worker
    # the admin sign-in shell carries no lead data and prompts for the token
    assert "Admin sign-in" in worker
    assert 'type=\\"password\\"' in worker


def test_worker_post_validation_is_strict():
    worker = generate(_app(), _design())["worker/index.ts"]
    # body must be a JSON object (arrays/primitives rejected)
    assert "Array.isArray(body)" in worker
    assert "request body must be a JSON object" in worker
    # per-field TYPE + LENGTH + email-format checks
    assert 'typeof v !== "string"' in worker
    assert "MAX_FIELD_LEN" in worker
    assert "v.length > MAX_FIELD_LEN" in worker
    assert "EMAIL_RE.test(v)" in worker
    # unknown + required-missing rejected
    assert "unknown field:" in worker
    assert "missing required field:" in worker
    # the insert is wrapped: 400 for bad input, 500 with a SAFE message for DB errors
    assert "try {" in worker
    assert "could not save lead" in worker
    assert "}, 500)" in worker
    # values go through the generated Drizzle table, not a raw SQL string
    assert "leadValues(rec)" in worker
    assert "db.insert(leads).values" in worker
    assert "prepare(" not in worker


# ---- default lead-gen app spec ------------------------------------------------


# ---- P0: residual XSS — context-safe escaping of spec-derived values ----------


def _app_named(name: str) -> AppSpec:
    app = _app()
    return AppSpec.model_validate({**app.model_dump(mode="json"), "name": name})


def _cf_names(app: AppSpec) -> tuple[str, str]:
    """The generated Worker name + D1 database_name, read back from wrangler.toml."""
    import re as _re

    wrangler = generate(app, _design())["wrangler.toml"]
    worker = _re.search(r'(?m)^name = "([^"]+)"', wrangler)
    db = _re.search(r'database_name = "([^"]+)"', wrangler)
    assert worker is not None and db is not None
    return worker.group(1), db.group(1)


def test_cf_resource_names_namespaced_distinct_apps_and_idempotent():
    # SEC-10 defense-in-depth: two DISTINCT apps whose names SLUG-COLLIDE
    # ("My Site!" / "My Site" both normalize to `my-site`) must NOT map to the same
    # Cloudflare Worker + D1 names (which would clobber each other on one account) —
    # the deterministic per-app namespace suffix disambiguates them.
    a_worker, a_db = _cf_names(_app_named("My Site!"))
    b_worker, b_db = _cf_names(_app_named("My Site"))
    assert a_worker != b_worker  # distinct Worker names
    assert a_db != b_db  # distinct D1 database names
    # the slug BASE collided (proving the namespace, not the slug, is what separates them)
    assert a_worker.rsplit("-", 1)[0] == b_worker.rsplit("-", 1)[0] == "my-site"

    # IDEMPOTENT: the SAME spec regenerates byte-identical names (safe re-deploys).
    assert _cf_names(_app_named("My Site!")) == (a_worker, a_db)

    # every emitted name is a valid Cloudflare resource name (SEC-9: leading
    # alphanumeric, then [a-z0-9_-], ≤63 chars).
    import re as _re

    cf_ok = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
    for name in (a_worker, a_db, b_worker, b_db):
        assert cf_ok.match(name), name


def test_cf_resource_names_stable_across_content_and_structure_edits():
    # The namespace keys on the app's IDENTITY (name + kind), NOT its mutable content,
    # so editing copy or adding/removing sections must NEVER rename the live resources.
    base = _app_named("Acme Studio")
    base_names = _cf_names(base)
    # a content edit (different hero copy) keeps the SAME Worker + D1 names
    edited_content = AppSpec.model_validate(
        {
            **base.model_dump(mode="json"),
            "pages": (
                {
                    **base.pages[0].model_dump(mode="json"),
                    "sections": (
                        {
                            **base.pages[0].sections[0].model_dump(mode="json"),
                            "content": {"heading": "A brand-new headline"},
                        },
                        *[s.model_dump(mode="json") for s in base.pages[0].sections[1:]],
                    ),
                },
            ),
        }
    )
    assert _cf_names(edited_content) == base_names


def test_hostile_app_name_is_escaped_in_title():
    # AppSpec.name is intentionally free-form (names legitimately carry &/'), so a
    # hostile name MUST be HTML-escaped where it lands in the static index.html
    # <title> — never executable markup.
    payload = "</title><script>alert(1)</script>"
    html_out = generate(_app_named(payload), _design())["index.html"]
    # the raw breakout must NOT appear; the escaped form must
    assert "<script>alert(1)</script>" not in html_out
    assert "</title><script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out
    # exactly one real <title>…</title> pair (the payload didn't open/close tags)
    assert html_out.count("<title>") == 1
    assert html_out.count("</title>") == 1


def test_app_name_with_ampersand_and_quote_is_attribute_safe():
    # Legitimate names with & / ' / " must round-trip as escaped text, not break HTML.
    html_out = generate(_app_named('Ben & Jerry\'s "Best"'), _design())["index.html"]
    assert "Ben & Jerry" not in html_out  # the bare & must have been escaped
    assert "&amp;" in html_out
    assert "&#x27;" in html_out or "&#39;" in html_out


def test_hostile_font_name_is_rejected_by_spec():
    # A font family carrying CSS/URL/HTML metacharacters can't even be constructed —
    # the DesignSpec validator constrains the family to a safe charset.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DesignSpec.model_validate(
            {
                "schema_version": 1,
                "typography": {
                    "heading_font": '"</style><script>x</script>',
                    "body_font": "Newsreader",
                },
                "palette": {"primary": "#123456", "surface": "#ffffff", "text": "#111111"},
                "layout_family": "split",
                "component_style": "outlined",
                "density": "comfortable",
            }
        )


def test_multiword_font_is_url_encoded_in_href_not_broken_out():
    # A legitimate multi-word family must be URL-encoded into the Google Fonts href
    # (space → '+'), and the href must stay inside its quoted attribute.
    recipe = get_recipe("civic-service")  # uses "Libre Franklin" / "Lora"
    assert recipe is not None
    design = recipe.to_design_spec()
    html_out = generate(_app(), design)["index.html"]
    assert "family=Libre+Franklin" in html_out
    # no raw space inside the encoded family, no attribute/tag breakout via the href
    link = next(line for line in html_out.splitlines() if "fonts.googleapis.com/css2" in line)
    href = link.split('href="', 1)[1].split('"', 1)[0]  # value inside href="…"
    assert href.startswith("https://fonts.googleapis.com/css2?")
    assert "Libre Franklin" not in href  # the raw (unencoded) family never appears
    assert "<" not in href and ">" not in href  # no tag breakout in the URL


def test_font_name_is_quoted_and_charset_safe_in_css():
    css = generate(_app(), _design())["src/styles.css"]
    heading = _design().typography.heading_font
    assert f'"{heading}"' in css  # quoted family in font-family value


def test_hostile_section_content_is_safe_ts_literal():
    # A Section.content body with </script>, a backtick, a quote and a newline must
    # be emitted as an ESCAPED JSON/TS string literal in content.ts — never raw
    # interpolation that breaks the generated TS.
    payload = 'a</script>`${x}`"q"\nnewline\\back'
    app = AppSpec.model_validate(
        {
            **_app().model_dump(mode="json"),
            "pages": [
                {
                    "id": "home",
                    "route": "/",
                    "title": "Home",
                    "sections": [
                        {
                            "id": "hero",
                            "kind": "hero",
                            "content": {"heading": "Hi", "body": payload},
                        }
                    ],
                }
            ],
        }
    )
    content_ts = generate(app, _design())["src/generated/content.ts"]
    # the literal raw backtick/`${}`/`</script>` must not appear UNescaped — it is
    # a JSON string literal (double-quoted, backslash-escaped), so a backtick stays
    # literal inside quotes and the dangerous newline/quote are escaped.
    assert '"</script>' not in content_ts  # not sitting raw next to a quote-key
    assert "\\n" in content_ts  # the newline is escaped, not a real break
    assert '\\"q\\"' in content_ts  # inner quotes escaped
    # the generated TS must stay structurally balanced (no breakout)
    assert content_ts.count("{") == content_ts.count("}")
    assert content_ts.count("(") == content_ts.count(")")
    # the whole CONTENT object must be parseable as JSON after stripping the wrapper
    import json as _json

    obj_text = content_ts.split("= {", 1)[1].rsplit("};", 1)[0].strip().rstrip(",")
    parsed = _json.loads("{" + obj_text + "}")
    # the payload survived intact through the escaped literal (round-trips exactly)
    hero_id = next(iter(parsed))
    assert parsed[hero_id]["body"] == payload


def test_default_lead_gen_app_spec_is_complete():
    recipe = get_recipe("civic-service")
    assert recipe is not None
    app = default_lead_gen_app_spec("Townsville Services", recipe)
    kinds = [s.kind for p in app.pages for s in p.sections]
    assert kinds == ["hero", "features", "form", "footer"]
    assert any(e.id == "lead" for e in app.entities)
    assert any(a.type == "submit" for a in app.primary_actions)


# ---- WO-A2.3: host-service client shim ---------------------------------------

# Strings that must NEVER appear in the emitted tree (no sentinel bearer, no
# secret patterns, no sk_/whsec_).
_SECRET_PATTERNS = (
    "Bearer replace-me",
    "Bearer placeholder",
    "sk_live",
    "sk_test",
    "whsec_",
    "import.meta.env.DISCO_SVC",
    "import.meta.env.DISCO_",
)
# Paths the frontend bundle owns (must never import worker/disco-client.ts).
_FRONTEND_PREFIXES = ("src/", "index.html")


def _host_worker_tree() -> dict[str, str]:
    return {
        "worker/index.ts": (
            "interface Env {\n"
            "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
            "}\n"
            "export default { fetch(_req: Request, _env: Env): Response {\n"
            "  return new Response('ok');\n"
            "} };\n"
        ),
        "index.html": "<!doctype html>\n",
    }


def _install_host_worker_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    from disco.core.appkit import PrimitiveDefinition
    from disco.core.appkit import generator as generator_module
    from disco.core.appkit.primitives import HostService

    defn = PrimitiveDefinition(
        id="test_host_worker",
        default_app_spec=lambda name, recipe: _app(),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: _host_worker_tree(),
        host_contract=(HostService("svc.ping"),),
    )
    monkeypatch.setattr(generator_module, "resolve_primitive", lambda _kind: defn)


def test_disco_client_shim_emitted_for_host_worker(monkeypatch: pytest.MonkeyPatch):
    """A real Worker-shaped host-contract tree gets the shim and Env bindings."""
    _install_host_worker_primitive(monkeypatch)
    tree = generate(_app(), _design())
    assert "worker/disco-client.ts" in tree
    assert "DISCO_SVC_BUS?: string" in tree["worker/index.ts"]
    assert "DISCO_SVC_TOKEN?: string" in tree["worker/index.ts"]


def test_host_contract_without_worker_fails_loudly():
    """Stripe remains activation-blocked until its fill emits a real Worker."""
    from disco.core.appkit.stripe_primitive import STRIPE_PRIMITIVE_ID

    stripe_app = AppSpec(
        schema_version=1,
        app_kind=STRIPE_PRIMITIVE_ID,
        name="Stripe App",
        pages=(),
    )
    with pytest.raises(ValueError, match="must emit worker/index.ts"):
        generate(stripe_app, _design())


def test_disco_client_shim_not_emitted_for_lead_gen():
    """Lead-gen primitive has empty host_contract — shim must NOT appear."""
    tree = generate(_app(), _design())
    assert "worker/disco-client.ts" not in tree, (
        "lead_gen has empty host_contract — shim must not appear"
    )


def test_disco_client_shim_not_emitted_for_directory():
    """Directory primitive has empty host_contract — shim must NOT appear."""
    from disco.core.appkit import default_directory_app_spec

    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    tree = generate(default_directory_app_spec("Dir", recipe), recipe.to_design_spec())
    assert "worker/disco-client.ts" not in tree, (
        "directory has empty host_contract — shim must not appear"
    )


def test_disco_client_shim_content_invariants(monkeypatch: pytest.MonkeyPatch):
    """The shim's code meets every security and structural invariant."""
    _install_host_worker_primitive(monkeypatch)
    tree = generate(_app(), _design())
    shim = tree["worker/disco-client.ts"]

    # ---- required API shape ----
    assert "export async function svc(" in shim
    assert "env: { DISCO_SVC_BUS?: string; DISCO_SVC_TOKEN?: string }" in shim
    assert "service: string" in shim
    assert "payload: unknown" in shim
    assert "Promise<unknown>" in shim
    assert "SvcResult" not in shim
    assert "ok: true" not in shim

    # ---- env-only, never caller-supplied ----
    assert "env.DISCO_SVC_BUS" in shim
    assert "env.DISCO_SVC_TOKEN" in shim

    # ---- URL validation via URL constructor ----
    assert "new URL(bus)" in shim
    assert "busUrl.protocol" in shim
    assert "busUrl.username" in shim
    assert "busUrl.password" in shim
    assert "busUrl.search" in shim
    assert "busUrl.hash" in shim
    assert "busUrl.pathname" in shim
    assert "busUrl.origin" in shim

    # ---- token validation: current a4v1 + migrated a2v0 Bearer formats ----
    assert "TOKEN_RE" in shim
    assert "TOKEN_RE.test(token)" in shim

    # ---- fixed URL construction ----
    assert "/_disco/svc/" in shim
    assert "encodeURIComponent(service)" in shim

    # ---- POST JSON only; cache: no-store ----
    assert 'method: "POST"' in shim
    assert '"Content-Type": "application/json"' in shim
    assert 'cache: "no-store"' in shim

    # ---- request body as UTF-8 bytes, 64 KiB bound ----
    assert "TextEncoder" in shim
    assert "JSON.stringify(payload)" in shim
    assert "MAX_REQUEST_BYTES" in shim
    assert "bodyBytes.byteLength > MAX_REQUEST_BYTES" in shim
    assert "body: bodyBytes" in shim

    # ---- Authorization Bearer from env only ----
    assert '"Authorization"' in shim
    assert "Bearer" in shim

    # ---- workerd-supported manual redirects, rejected by the non-2xx gate ----
    assert 'redirect: "manual"' in shim

    # ---- AbortController timeout ----
    assert "AbortController" in shim
    assert "BUS_TIMEOUT_MS" in shim

    # ---- Content-Type check on response ----
    assert 'response.headers.get("Content-Type")' in shim
    assert 'mediaType !== "application/json"' in shim

    # ---- bounded streaming response (reader chunks, NOT arrayBuffer) ----
    assert "response.body.getReader()" in shim
    assert "reader.read()" in shim
    assert "await reader.cancel()" in shim
    assert "total > MAX_RESPONSE_BYTES" in shim
    assert "MAX_RESPONSE_BYTES" in shim
    assert "chunks.push(value)" in shim
    assert "arrayBuffer" not in shim

    # ---- top-level JSON object required on response ----
    assert "TextDecoder" in shim
    assert "{ fatal: true }" in shim
    assert "JSON.parse(text)" in shim
    assert "Array.isArray(parsed)" in shim
    assert 'typeof parsed !== "object"' in shim

    # ---- sanitized status-class errors for non-200 responses ----
    assert "statusClass" in shim
    assert '"4xx"' in shim
    assert '"5xx"' in shim

    # ---- sanitized errors: never echo bus/token/service/body/statusText ----
    assert 'throw new Error("host service unavailable");' in shim
    assert 'throw new Error("invalid service");' in shim
    assert 'throw new Error("invalid payload");' in shim
    assert 'throw new Error("payload too large");' in shim
    assert 'throw new Error("host service error");' in shim
    assert "response.statusText" not in shim
    assert "errObj" not in shim

    # ---- grammar + length cap ----
    assert "SERVICE_RE" in shim
    assert "MAX_SERVICE_LEN" in shim
    assert 'busUrl.protocol === "http:" && !isLoopback(busUrl.hostname)' in shim
    assert "const BUS_TIMEOUT_MS = 10000" in shim

    # ---- no sentinel bearer leaked ----
    for pat in _SECRET_PATTERNS:
        assert pat not in shim, f"forbidden pattern {pat!r} found in shim"

    # ---- structurally balanced ----
    assert shim.count("{") == shim.count("}")
    assert shim.count("(") == shim.count(")")


def test_disco_client_shim_never_imported_from_frontend(
    monkeypatch: pytest.MonkeyPatch,
):
    """No src/ or index.html file imports or references the disco-client shim."""
    _install_host_worker_primitive(monkeypatch)
    tree = generate(_app(), _design())
    for path, contents in tree.items():
        if not any(path.startswith(p) for p in _FRONTEND_PREFIXES):
            continue
        assert "disco-client" not in contents, f"frontend file {path} must not import disc-client"
        assert "DISCO_SVC_BUS" not in contents, f"frontend file {path} must not reference host bus"
        assert "DISCO_SVC_TOKEN" not in contents, (
            f"frontend file {path} must not reference host token"
        )


def test_no_import_meta_env_host_service():
    """Vite browser bundle cannot receive bindings via import.meta.env.

    No generated file, in any primitive, may reference host service env vars
    through the Vite import.meta.env path (browser code has no env bindings).
    The shim reads DISCO_SVC_BUS / DISCO_SVC_TOKEN only from Worker env."""
    tree = generate(_app(), _design())
    blob = "\n".join(tree.values())
    assert "import.meta.env.DISCO_SVC" not in blob
    assert "import.meta.env.DISCO_" not in blob


def test_dev_vars_example_is_byte_stable_for_unrelated_primitive():
    """A2.3 does not globally alter the existing lead-gen secret template."""
    tree = generate(_app(), _design())
    dev_vars = tree[".dev.vars.example"]
    assert "DISCO_SVC_BUS" not in dev_vars
    assert "DISCO_SVC_TOKEN" not in dev_vars


def test_byte_stability_preserved_for_lead_gen_non_host_service():
    """Lead-gen primitive with empty host_contract must have unchanged critical
    paths (schema, worker, drizzle)."""
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    tree = generate(default_lead_gen_app_spec("Acme", recipe), recipe.to_design_spec())
    assert (
        _digest_paths(tree, ("schema.sql", "worker/index.ts", "src/db/schema.ts"))
        == _LEAD_GEN_ACME_WORKER_SCHEMA_DIGEST
    )


def test_disco_client_shim_deterministic(monkeypatch: pytest.MonkeyPatch):
    """The shim (when emitted) is deterministic: same spec → byte-identical tree."""
    _install_host_worker_primitive(monkeypatch)
    app = _app()
    a = generate(app, _design())
    b = generate(app, _design())
    assert a == b
    assert "worker/disco-client.ts" in a
    assert a["worker/disco-client.ts"] == b["worker/disco-client.ts"]


def test_generate_preserves_primitive_key_order(monkeypatch: pytest.MonkeyPatch):
    """The dispatcher must not globally reorder unrelated primitive output."""
    from disco.core.appkit import PrimitiveDefinition
    from disco.core.appkit import generator as generator_module

    defn = PrimitiveDefinition(
        id="order_fixture",
        default_app_spec=lambda name, recipe: _app(),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: {"z-last": "z", "a-first": "a"},
    )
    monkeypatch.setattr(generator_module, "resolve_primitive", lambda _kind: defn)
    assert list(generate(_app(), _design())) == ["z-last", "a-first"]


def test_disco_client_ts_executes_security_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Execute the emitted TypeScript with Node's TS loader; this is not a substring test."""
    _install_host_worker_primitive(monkeypatch)
    shim_path = tmp_path / "disco-client.ts"
    shim_path.write_text(generate(_app(), _design())["worker/disco-client.ts"])
    harness_path = tmp_path / "harness.mjs"
    harness_path.write_text(
        """
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const { svc } = await import(pathToFileURL(process.argv[2]).href);
const token = "a4v1.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
const env = { DISCO_SVC_BUS: "https://bus.example", DISCO_SVC_TOKEN: token };
let timeoutSeen = 0;
globalThis.setTimeout = (_fn, ms) => { timeoutSeen = ms; return 1; };
globalThis.clearTimeout = () => {};
const errorOf = async (fn) => {
  try { await fn(); } catch (error) { return String(error); }
  throw new Error("expected rejection");
};
const assertSanitized = (message) => {
  const secrets = ["secret-url", token, "status-secret", "body-secret"];
  for (const secret of secrets) assert.equal(message.includes(secret), false);
};

let request;
globalThis.fetch = async (url, init) => {
  request = { url, init };
  const headers = { "Content-Type": "Application/JSON; Charset=UTF-8" };
  return new Response('{"ok":true}', { headers });
};
assert.deepEqual(await svc(env, "svc.ping", { value: 1 }), { ok: true });
assert.equal(timeoutSeen, 10000);
assert.equal(request.url, "https://bus.example/_disco/svc/svc.ping");
assert.equal(request.init.method, "POST");
assert.equal(request.init.redirect, "manual");
assert.equal(request.init.cache, "no-store");
assert.equal(new TextDecoder().decode(request.init.body), '{"value":1}');
globalThis.fetch = async () => new Response(null, {
  status: 302, headers: { Location: "https://redirect-secret.example" },
});
assertSanitized(await errorOf(() => svc(env, "svc.ping", {})));
globalThis.fetch = async () => new Response('{"ok":true}', {
  headers: { "Content-Type": "application/json" },
});

const badBuses = ["http://example.com", "ftp://localhost",
  "https://user:pass@bus.example", "https://bus.example/path"];
for (const bus of badBuses) {
  const error = await errorOf(
    () => svc({ ...env, DISCO_SVC_BUS: bus }, "svc.ping", {}),
  );
  assert.equal(error.includes("host service unavailable"), true);
}
for (const bus of ["http://localhost", "http://127.0.0.2", "http://[::1]"]) {
  assert.deepEqual(await svc({ ...env, DISCO_SVC_BUS: bus }, "svc.ping", {}), { ok: true });
}
assert.equal((await errorOf(() => svc(env, "Svc.Ping", {}))).includes("invalid service"), true);
const invalidPayload = await errorOf(
  () => svc(env, "svc.ping", { toJSON: () => [] }),
);
assert.equal(invalidPayload.includes("invalid payload"), true);

let cancelled = 0;
const cancellable = () => new ReadableStream({
  start(controller) {
    controller.enqueue(new TextEncoder().encode("body-secret"));
  },
  cancel() { cancelled += 1; },
});
globalThis.fetch = async () => new Response(cancellable(), {
  status: 500,
  statusText: "status-secret",
  headers: { "Content-Type": "application/json" },
});
let message = await errorOf(() => svc(env, "svc.ping", {}));
assert.equal(cancelled, 1); assertSanitized(message);
globalThis.fetch = async () => new Response(cancellable(), {
  headers: { "Content-Type": "text/plain" },
});
message = await errorOf(() => svc(env, "svc.ping", {}));
assert.equal(cancelled, 2); assertSanitized(message);
const jsonHeaders = { "Content-Type": "application/json" };
globalThis.fetch = async () => new Response(null, { headers: jsonHeaders });
const nullBody = await errorOf(() => svc(env, "svc.ping", {}));
assert.equal(nullBody.includes("host service error"), true);
globalThis.fetch = async () => new Response(
  new Uint8Array([0xc3, 0x28]), { headers: jsonHeaders },
);
assertSanitized(await errorOf(() => svc(env, "svc.ping", {})));
globalThis.fetch = async () => new Response(
  new Uint8Array(256 * 1024 + 1), { headers: jsonHeaders },
);
assertSanitized(await errorOf(() => svc(env, "svc.ping", {})));
globalThis.fetch = async () => { throw new Error(`secret-url ${token}`); };
assertSanitized(await errorOf(() => svc(env, "svc.ping", {})));
globalThis.fetch = async () => new Response(new ReadableStream({
  pull(controller) { controller.error(new Error("body-secret")); },
}), { headers: jsonHeaders });
assertSanitized(await errorOf(() => svc(env, "svc.ping", {})));
"""
    )
    result = subprocess.run(
        ["node", "--experimental-strip-types", str(harness_path), str(shim_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
