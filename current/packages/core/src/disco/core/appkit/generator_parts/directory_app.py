"""AppKit EPIC N — the DIRECTORY primitive.

A directory site is a genuinely DIFFERENT shape from lead-gen: a multi-route
static site (a home page + a `/directory` listing page), a searchable/filterable
listing section, and NO server-side data plane (no /api/leads, no D1, no admin
read-back). It reuses the SHARED, shape-agnostic emitters (design tokens/CSS,
index/main/tsconfig/vite, the section components, content.ts, manifest) and adds
only the directory-specific pieces below — a route-aware App shell, a searchable
listing component, a static-only Worker, and a D1-free wrangler/package/owner
guide. Deliberately explicit + small (the anti-over-generalization guardrail): no
generic CRUD/API/auth DSL — that waits until a second primitive needs the same
mechanism.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

import json

from ..primitives import DIRECTORY_PRIMITIVE_ID
from ..recipes import SiteRecipe
from ..spec import Action, AppSpec, DesignSpec, Entity, EntityField, Page, Section, SectionContent
from .app_shell import _emit_content_ts, _emit_index_html, _emit_main_tsx, _seo_files
from .components import _emit_component
from .design_tokens import _emit_styles_css, _variant_layout
from .ids import _comp_name, _component_names, _html_text, _iter_sections, _slug, _ts
from .lead_entity import synthesized_lead_entity
from .project_files import (
    _emit_gitignore,
    _emit_manifest_ts,
    _emit_package_lock_json,
    _emit_tsconfig,
    _emit_vite_config,
)
from .semantic_attrs import _disco_field_attr, _disco_item_attrs, _disco_section_attrs


def _identity_app_spec(app: AppSpec) -> AppSpec:
    """A no-op `prepare_app_spec` for primitives that persist the spec verbatim (the
    directory primitive has no synthesized-entity step like lead-gen's lead entity)."""
    return app


def _emit_static_worker_ts() -> str:
    """A STATIC-ONLY Cloudflare Worker: every request falls through to the built-asset
    layer. A directory site has no server data plane — no /api/leads, no D1, no admin
    read-back — so the worker is the minimal SPA-asset passthrough. Emitted (rather
    than omitted) so the `worker/index.ts` path is present after a same-path overwrite
    of a prior lead-gen app, leaving no stale lead worker behind."""
    return (
        "/* Auto-generated Cloudflare Worker (Epic N): STATIC directory site — serves the\n"
        "   built assets only. No lead API, no D1 database, no admin read-back: a\n"
        "   directory primitive has no server-side data plane, so every request falls\n"
        "   through to the static-asset layer (the SPA index.html for client routes). */\n"
        "export interface Env {\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "}\n\n"
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_directory_schema_sql() -> str:
    """A table-free `schema.sql` placeholder. The directory primitive is fully static
    (no D1), but the path is kept present so re-generating over a previous lead-gen app
    at the same workspace path leaves NO stale lead schema behind."""
    return (
        "-- Auto-generated (Epic N): the directory primitive is a STATIC site — it has\n"
        "-- NO D1 tables. This file is intentionally present (and table-free) so\n"
        "-- regenerating over a previous lead-gen app at the same path leaves no stale\n"
        "-- schema behind.\n"
    )


def _emit_directory_wrangler_toml(app: AppSpec) -> str:
    """`wrangler.toml` for the static directory site: Workers Static Assets only — no
    `[[d1_databases]]` binding and no `run_worker_first` (there are no dynamic
    Worker-first routes), so the SPA asset layer serves everything."""
    name = _slug(app.name)
    return (
        f'name = "{name}"\n'
        'compatibility_date = "2024-09-23"\n'
        'main = "worker/index.ts"\n'
        "\n"
        "# Workers Static Assets: serve the built SPA, falling back to index.html for\n"
        "# client routes (single-page-application not_found handling). A directory site\n"
        "# is fully STATIC — there is no Worker-first dynamic route and no D1 binding.\n"
        "[assets]\n"
        'directory = "./dist"\n'
        'binding = "ASSETS"\n'
        'not_found_handling = "single-page-application"\n'
    )


def _emit_directory_package_json(app: AppSpec) -> str:
    """`package.json` for the static directory site — the same SPA build glue as
    lead-gen MINUS the D1 (`db:local` / `db:remote`) scripts, which have no meaning
    without a database."""
    pkg = {
        "name": _slug(app.name),
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
            # Local CF emulation (workerd) of the static worker + built assets, so the
            # owner can verify the SPA + static serving before deploying.
            "cf:dev": "wrangler dev",
            "deploy": "wrangler deploy",
        },
        "dependencies": {
            "react": "^18.3.1",
            "react-dom": "^18.3.1",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "typescript": "^5.5.4",
            "vite": "^5.4.2",
            "wrangler": "^4.20.0",
        },
    }
    return json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"


def _emit_directory_owner_guide_md(app: AppSpec) -> str:
    """`OWNER_GUIDE.md` for the static directory site — owner-run deploy INSTRUCTIONS
    (Disco generates + locally verifies; the owner runs every Cloudflare command).
    No D1 / secret steps: a directory site is fully static, so the flow is just build
    + deploy."""
    app_name = _html_text(app.name)
    return (
        f"# Deploy guide — {app_name}\n"
        "\n"
        "This is a Cloudflare-ready STATIC directory site: a Vite/React SPA served by\n"
        "Cloudflare Workers Static Assets. It has no server-side data plane — no lead\n"
        "API, no D1 database, no admin read-back — so deploying is just build + publish.\n"
        "\n"
        "**Disco generated and LOCALLY verified this site** (design-clean; route + section\n"
        "coverage; a static-only Worker that serves the built assets). Disco does **not**\n"
        "deploy — the steps below are the ones **you** run.\n"
        "\n"
        "## Prerequisites\n"
        "\n"
        "- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (the free plan\n"
        "  covers Workers Static Assets).\n"
        "- [Node.js](https://nodejs.org/) 18+ and npm.\n"
        "- Wrangler (installed as a dev dependency): `npm install`, then `npx wrangler\n"
        "  login` to authenticate.\n"
        "\n"
        "## 1. Verify locally with the Cloudflare emulator (recommended before deploy)\n"
        "\n"
        "```sh\n"
        "npm install\n"
        "npm run build                      # build the SPA into ./dist\n"
        "npm run cf:dev                     # serve the Worker + built assets locally\n"
        "```\n"
        "\n"
        "Open the printed local URL and click through `/` and `/directory` — the listing\n"
        "page's search box filters the entries client-side.\n"
        "\n"
        "## 2. Deploy to Cloudflare\n"
        "\n"
        "```sh\n"
        "npm run build                                       # build the SPA\n"
        "npx wrangler deploy                                 # publish the Worker + assets\n"
        "```\n"
        "\n"
        "After deploy, visit your `*.workers.dev` URL (or your custom domain); the static\n"
        "assets and the SPA's client-side routes are served by the Workers asset layer.\n"
        "\n"
        "## Rollback\n"
        "\n"
        "- **Roll back** a bad deploy from the Cloudflare dashboard (Workers & Pages →\n"
        "  your Worker → Deployments → roll back) or by re-running `npx wrangler deploy`\n"
        "  from a known-good checkout.\n"
    )


def _emit_directory_listing_component(comp: str, page: Page, section: Section) -> str:
    """A SEARCHABLE/filterable directory listing component (the directory primitive's
    distinguishing section): a controlled search input filters the section's `items`
    (from content.ts) client-side. Reads copy from CONTENT by id like every other
    section, so updating content never regenerates this component."""
    layout = _variant_layout(section)
    classes = f"section kind-list variant-{layout} directory"
    disco_attrs = _disco_section_attrs(section)
    return (
        "/* Auto-generated directory listing component — do NOT hand-edit; regenerated "
        "from .disco/appspec.json. */\n"
        'import { useMemo, useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        "  const items = c.items ?? [];\n"
        '  const [query, setQuery] = useState<string>("");\n'
        "  const filtered = useMemo(\n"
        "    () => items.filter((item) => item.toLowerCase().includes(query.toLowerCase())),\n"
        "    [items, query]\n"
        "  );\n"
        "  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        '      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        '        <label className="directory-search">\n'
        "          <span>Search</span>\n"
        '          <input type="text" name="q" value={query}\n'
        '            placeholder="Filter listings"\n'
        "            onChange={(e) => setQuery(e.target.value)} />\n"
        "        </label>\n"
        '        <ul className="stacked-list">\n'
        "          {filtered.map((item, i) => (\n"
        f'            <li className="feature-item" key={{i}}{_disco_field_attr("items")}'
        f"{_disco_item_attrs(section, index_expr='i', item_kind='item')}>{{item}}</li>\n"
        "          ))}\n"
        "        </ul>\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _emit_directory_app_tsx(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    """A ROUTE-AWARE app shell: each page renders ONLY its own sections, selected by
    `window.location.pathname`. A directory site is multi-route (`/` + `/directory`),
    so unlike the single-page lead-gen shell this dispatches per route. (Lead-gen
    keeps its flat single-page `_emit_app_tsx` — byte-identical — so its output is
    unchanged.)"""
    imports: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
    page_funcs: list[str] = []
    route_entries: list[str] = []
    for i, page in enumerate(app.pages):
        fn = f"Page{i}"
        renders = "\n".join(
            f"      <{_comp_name(names, page, section)} />" for section in page.sections
        )
        body = (renders + "\n") if renders else ""
        page_funcs.append(
            f"function {fn}(): ReactElement {{\n  return (\n    <>\n{body}    </>\n  );\n}}\n"
        )
        # Normalize the ROUTES key the SAME way the browser path is normalized below
        # (trim trailing slashes except root `/`), so a schema-valid trailing-slash
        # route like `/directory/` resolves to its page instead of falling through to
        # the fallback. `rstrip("/") or "/"` mirrors the JS `replace(/\/+$/, "") || "/"`.
        norm_route = page.route.rstrip("/") or "/"
        route_entries.append(f"  {_ts(norm_route)}: {fn},")
    fallback = "Page0" if app.pages else "() => null"
    return (
        "/* Auto-generated route-aware app shell (Epic N) — regenerated from "
        ".disco/appspec.json. */\n"
        'import { type ReactElement } from "react";\n'
        + "\n".join(imports)
        + "\n\n"
        + "\n".join(page_funcs)
        + "\n"
        "const ROUTES: Record<string, () => ReactElement> = {\n" + "\n".join(route_entries) + "\n"
        "};\n\n"
        "export default function App(): ReactElement {\n"
        '  const path = window.location.pathname.replace(/\\/+$/, "") || "/";\n'
        f"  const Page = ROUTES[path] ?? {fallback};\n"
        "  return (\n"
        '    <div className="app-main">\n'
        "      <Page />\n"
        "    </div>\n"
        "  );\n}\n"
    )


def _generate_directory(app_spec: AppSpec, design_spec: DesignSpec) -> dict[str, str]:
    """Lower the two specs into a complete STATIC directory-site tree (Epic N).

    Reuses the shared, shape-agnostic emitters (index/main/styles/content/manifest/
    tsconfig/vite) and adds the directory-specific files: a route-aware App shell, a
    static-only Worker, a table-free schema.sql, a D1-free wrangler/package/owner
    guide, and a searchable listing component for each `list` section."""
    from ..blog_primitive import emit_app_tsx_with_blog_routes, emit_blog_files, has_blog

    names = _component_names(app_spec)
    package_json = _emit_directory_package_json(app_spec)
    files: dict[str, str] = {
        "index.html": _emit_index_html(app_spec, design_spec),
        "package.json": package_json,
        "package-lock.json": _emit_package_lock_json(app_spec, package_json=package_json),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "wrangler.toml": _emit_directory_wrangler_toml(app_spec),
        "schema.sql": _emit_directory_schema_sql(),
        "worker/index.ts": _emit_static_worker_ts(),
        "src/main.tsx": _emit_main_tsx(),
        "src/App.tsx": (
            emit_app_tsx_with_blog_routes(app_spec, names)
            if has_blog(app_spec)
            else _emit_directory_app_tsx(app_spec, names)
        ),
        "src/styles.css": _emit_styles_css(design_spec),
        "src/generated/content.ts": _emit_content_ts(app_spec, names),
        "src/generated/manifest.ts": _emit_manifest_ts(app_spec, design_spec, names),
        "OWNER_GUIDE.md": _emit_directory_owner_guide_md(app_spec),
        ".gitignore": _emit_gitignore(),
    }
    # Non-form sections reuse the shared component emitter; the `list` section becomes
    # the directory's searchable listing component. The lead arg is unused for these
    # (no form section in a directory site), so a synthesized default is harmless.
    lead = synthesized_lead_entity()
    for page, section in _iter_sections(app_spec):
        comp = _comp_name(names, page, section)
        if section.kind == "list":
            files[f"src/components/{comp}.tsx"] = _emit_directory_listing_component(
                comp, page, section
            )
        else:
            files[f"src/components/{comp}.tsx"] = _emit_component(comp, page, section, lead)
    # F5.3: {} when app_spec.seo is None — the no-seo tree is byte-identical.
    files.update(emit_blog_files(app_spec))
    files.update(_seo_files(app_spec))
    return dict(sorted(files.items()))


def default_directory_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A sensible DEFAULT directory AppSpec for `app_create` when only a brief is
    given: a home page (hero → features → faq → footer) plus a `/directory` listing
    page (a searchable `list` section → footer), a `listing` entity describing the
    directory's record shape, and a nav action to the directory route. Section
    `variant_id`s follow the recipe's preferred layouts where the kind matches.
    Deterministic for a given (name, recipe)."""
    title = name.strip() or "Your Directory"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    home = Page(
        id="home",
        route="/",
        title="Home",
        sections=(
            Section(
                id="hero",
                kind="hero",
                variant_id=prefs.get("hero"),
                content=SectionContent(heading=title, subheading=recipe.summary),
            ),
            Section(
                id="about",
                kind="features",
                variant_id=prefs.get("features"),
                content=SectionContent(
                    heading="What you'll find",
                    items=("Curated listings", "Searchable categories", "Always up to date"),
                ),
            ),
            Section(
                id="faq",
                kind="faq",
                variant_id=prefs.get("faq"),
                content=SectionContent(
                    heading="Questions",
                    items=("How do I get listed?", "How often is the directory updated?"),
                ),
            ),
            Section(
                id="footer",
                kind="footer",
                variant_id=prefs.get("footer"),
                content=SectionContent(heading=title),
            ),
        ),
    )
    directory = Page(
        id="directory",
        route="/directory",
        title="Directory",
        sections=(
            Section(
                id="listings",
                kind="list",
                variant_id=prefs.get("list"),
                content=SectionContent(
                    heading="Browse listings",
                    subheading="Search and filter the directory.",
                    items=(
                        "Acme Studio — Design",
                        "Northwind Labs — Research",
                        "Globex Co — Manufacturing",
                        "Initech — Software",
                    ),
                ),
            ),
            Section(
                id="directory_footer",
                kind="footer",
                variant_id=prefs.get("footer"),
                content=SectionContent(heading=title),
            ),
        ),
    )
    return AppSpec(
        schema_version=1,
        app_kind=DIRECTORY_PRIMITIVE_ID,
        name=title,
        pages=(home, directory),
        entities=(
            Entity(
                id="listing",
                name="Listing",
                fields=(
                    EntityField(name="title", type="str", required=True),
                    EntityField(name="category", type="str", required=False),
                    EntityField(name="summary", type="text", required=False),
                    EntityField(name="url", type="url", required=False),
                ),
            ),
        ),
        primary_actions=(
            Action(id="browse", label="Browse the directory", type="nav", target="/directory"),
        ),
    )
