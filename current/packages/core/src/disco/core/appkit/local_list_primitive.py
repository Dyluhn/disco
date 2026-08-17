"""The strict AppKit ``local_list`` base primitive.

``local_list`` is the deliberately small answer for notes, todos, checklists, and
other add/list/delete requests that do not need a server data plane.  AppKit owns
all emitted source.  The generated React component stores only a bounded array of
bounded strings in browser ``localStorage``; it never calls an API, accepts raw
HTML, reads a secret, or emits model-authored source.

The primitive is intentionally explicit rather than a generic application DSL.
It reuses the stable Vite/design emitters and owns one reviewed interactive-list
component plus a static Cloudflare asset worker.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .local_verify import STATIC_CF_EXPORT_FILES, cloudflare_export_ready_static
from .primitives import (
    LOCAL_LIST_PRIMITIVE_ID,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
)
from .recipes import SiteRecipe
from .spec import AppSpec, DesignSpec, Page, Section, SectionContent
from .worker_inspect import inspect_static_worker

_MAX_ITEMS = 100
_MAX_ITEM_LENGTH = 120


def default_local_list_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """Return the single-page, browser-local default selected by ``app_create``."""
    title = name.strip() or "My List"
    prefs = {item.kind: item.variant_id for item in recipe.preferred_section_variants}
    return AppSpec(
        schema_version=1,
        app_kind=LOCAL_LIST_PRIMITIVE_ID,
        name=title,
        pages=(
            Page(
                id="home",
                route="/",
                title="Home",
                sections=(
                    Section(
                        id="local_list",
                        kind="list",
                        variant_id=prefs.get("list"),
                        content=SectionContent(
                            heading=title,
                            subheading=(
                                "Add what matters, keep it in this browser, and remove "
                                "items when they are done."
                            ),
                            cta_label="Add item",
                        ),
                    ),
                ),
            ),
        ),
    )


def prepare_local_list_app_spec(app: AppSpec) -> AppSpec:
    """Validate the bounded base shape and persist it without hidden widening.

    Static sections may be added through ``app_add_section``, but at least one
    ``list`` section must remain so the selected primitive always delivers the
    behavior it advertises.  Data-plane declarations are rejected: this base is
    browser-local by contract, not a quiet fallback to a server-backed app.
    """
    if app.app_kind != LOCAL_LIST_PRIMITIVE_ID:
        raise ValueError(
            f"local_list primitive requires app_kind={LOCAL_LIST_PRIMITIVE_ID!r}, "
            f"got {app.app_kind!r}"
        )
    if not any(section.kind == "list" for page in app.pages for section in page.sections):
        raise ValueError("local_list primitive requires at least one list section")
    if app.roles or app.entities or app.stripe is not None or app.webhooks is not None:
        raise ValueError(
            "local_list is browser-local and cannot declare roles, entities, Stripe, "
            "or webhooks; use records for a server data plane"
        )
    unsupported_folded = tuple(
        name for name in ("seo", "blog", "analytics") if getattr(app, name, None) is not None
    )
    if unsupported_folded:
        raise ValueError(
            "local_list has unsupported folded primitive metadata: "
            f"{', '.join(unsupported_folded)}. This base does not lower those "
            "features; refusing instead of accepting a spec-only change with no "
            "matching output."
        )
    return AppSpec.model_validate(app.model_dump(mode="json"))


def _storage_key(app: AppSpec, page: Page, section: Section) -> str:
    """A stable, content-edit-invariant key scoped to this app/list location."""
    identity = "\0".join((app.name, page.id, section.id)).encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return f"disco.local-list.{suffix}"


def _emit_local_list_component(app: AppSpec, page: Page, section: Section, comp: str) -> str:
    from .generator import _disco_field_attr, _disco_section_attrs, _ts, _variant_layout

    classes = f"section local-list variant-{_variant_layout(section)}"
    section_attrs = _disco_section_attrs(section)
    input_id = "local-list-input-" + _storage_key(app, page, section).rsplit(".", 1)[-1]
    return (
        "/* Auto-generated local_list component — AppKit-owned; do NOT hand-edit. */\n"
        'import { type FormEvent, useEffect, useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        f"const STORAGE_KEY = {_ts(_storage_key(app, page, section))};\n"
        f"const INPUT_ID = {_ts(input_id)};\n"
        f"const MAX_ITEMS = {_MAX_ITEMS};\n"
        f"const MAX_ITEM_LENGTH = {_MAX_ITEM_LENGTH};\n\n"
        "function boundedItems(value: unknown): string[] {\n"
        "  if (!Array.isArray(value)) return [];\n"
        "  const out: string[] = [];\n"
        "  for (const item of value) {\n"
        '    if (typeof item !== "string") continue;\n'
        "    const normalized = item.trim().slice(0, MAX_ITEM_LENGTH);\n"
        "    if (normalized && out.length < MAX_ITEMS) out.push(normalized);\n"
        "  }\n"
        "  return out;\n"
        "}\n\n"
        "function readStoredItems(seedItems: string[]): string[] {\n"
        "  try {\n"
        "    const raw = window.localStorage.getItem(STORAGE_KEY);\n"
        "    return raw === null ? boundedItems(seedItems) : boundedItems(JSON.parse(raw));\n"
        "  } catch {\n"
        "    return boundedItems(seedItems);\n"
        "  }\n"
        "}\n\n"
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        "  const [items, setItems] = useState<string[]>(() => readStoredItems(c.items ?? []));\n"
        '  const [draft, setDraft] = useState<string>("");\n\n'
        "  useEffect(() => {\n"
        "    try {\n"
        "      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));\n"
        "    } catch {\n"
        "      // Storage can be disabled or full; the in-memory list remains usable.\n"
        "    }\n"
        "  }, [items]);\n\n"
        "  function addItem(event: FormEvent<HTMLFormElement>): void {\n"
        "    event.preventDefault();\n"
        "    const value = draft.trim().slice(0, MAX_ITEM_LENGTH);\n"
        "    if (!value) return;\n"
        "    setItems((current) =>\n"
        "      current.length >= MAX_ITEMS ? current : [...current, value]\n"
        "    );\n"
        '    setDraft("");\n'
        "  }\n\n"
        "  function deleteItem(index: number): void {\n"
        "    setItems((current) => current.filter((_item, candidate) => candidate !== index));\n"
        "  }\n\n"
        "  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{section_attrs}>\n"
        '      <div className="local-list-panel">\n'
        f"        {{c.heading ? <h1{_disco_field_attr('heading')}>{{c.heading}}</h1> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        '        <form className="local-list-form" onSubmit={addItem}>\n'
        "          <label htmlFor={INPUT_ID}>New item</label>\n"
        '          <div className="local-list-controls">\n'
        '            <input id={INPUT_ID} name="item" type="text" required\n'
        '              autoComplete="off" maxLength={MAX_ITEM_LENGTH} value={draft}\n'
        '              placeholder="Add an item"\n'
        "              onChange={(event) => setDraft(event.target.value)} />\n"
        '            <button className="btn" type="submit" disabled={items.length >= MAX_ITEMS}>\n'
        '              {c.ctaLabel ?? "Add item"}\n'
        "            </button>\n"
        "          </div>\n"
        "        </form>\n"
        '        <p className="local-list-count" aria-live="polite">\n'
        "          {items.length} of {MAX_ITEMS} items\n"
        "        </p>\n"
        "        {items.length === 0 ? (\n"
        '          <p className="local-list-empty">No items yet. Add the first one above.</p>\n'
        "        ) : (\n"
        '          <ul className="local-list-items">\n'
        "            {items.map((item, index) => (\n"
        '              <li className="local-list-row" key={`${item}-${index}`}>\n'
        '                <span className="local-list-text">{item}</span>\n'
        '                <button className="btn btn-secondary" type="button"\n'
        "                  aria-label={`Delete ${item}`} onClick={() => deleteItem(index)}>\n"
        "                  Delete\n"
        "                </button>\n"
        "              </li>\n"
        "            ))}\n"
        "          </ul>\n"
        "        )}\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n"
        "}\n"
    )


def _emit_static_section_component(section: Section, comp: str) -> str:
    """Render non-list AppSpec sections as inert, React-escaped content."""
    from .generator import _disco_field_attr, _disco_section_attrs, _ts

    attrs = _disco_section_attrs(section)
    return (
        "/* Auto-generated static local_list section — AppKit-owned; do NOT hand-edit. */\n"
        'import { CONTENT } from "../generated/content";\n\n'
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        "  return (\n"
        f'    <section className="section" id={_ts(section.id)} '
        f"data-appkit-section={_ts(section.id)}{attrs}>\n"
        f"      {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f"      {{c.subheading ? <p{_disco_field_attr('subheading')}>"
        "{c.subheading}</p> : null}\n"
        f"      {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
        '      {c.items?.length ? <ul className="stacked-list">\n'
        "        {c.items.map((item, index) => <li key={index}>{item}</li>)}\n"
        "      </ul> : null}\n"
        "    </section>\n"
        "  );\n"
        "}\n"
    )


def _emit_local_index_html(app: AppSpec) -> str:
    from .generator import _html_text

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"    <title>{_html_text(app.name)}</title>\n"
        "  </head>\n"
        "  <body>\n"
        '    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.tsx"></script>\n'
        "  </body>\n"
        "</html>\n"
    )


def _emit_local_package_json(app: AppSpec) -> str:
    """Match the reviewed lock root while exposing no database scripts."""
    from .generator import _slug

    package = {
        "name": _slug(app.name),
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
            "cf:dev": "wrangler dev",
            "deploy": "wrangler deploy",
        },
        # The checked-in AppKit lock is shared. Drizzle packages remain pinned in
        # that reviewed graph but are not imported by this browser-local runtime.
        "dependencies": {
            "drizzle-orm": "^0.44.2",
            "react": "^18.3.1",
            "react-dom": "^18.3.1",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "drizzle-kit": "^0.31.4",
            "typescript": "^5.5.4",
            "vite": "^5.4.2",
            "wrangler": "^4.20.0",
        },
    }
    return json.dumps(package, indent=2, ensure_ascii=False) + "\n"


def _emit_local_worker_ts() -> str:
    return (
        "/* Auto-generated static asset worker for local_list. No API or data binding. */\n"
        "export interface Env {\n"
        "  ASSETS: { fetch: (request: Request) => Promise<Response> };\n"
        "}\n\n"
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_local_schema_sql() -> str:
    return (
        "-- local_list stores bounded strings in browser localStorage.\n"
        "-- There is intentionally no database table or server data plane.\n"
    )


def _emit_local_wrangler_toml(app: AppSpec) -> str:
    from .generator import _slug

    return (
        f'name = "{_slug(app.name)}"\n'
        'compatibility_date = "2024-09-23"\n'
        'main = "worker/index.ts"\n\n'
        "[assets]\n"
        'directory = "./dist"\n'
        'binding = "ASSETS"\n'
        'not_found_handling = "single-page-application"\n'
    )


def _emit_local_owner_guide(app: AppSpec) -> str:
    from .generator import _html_text

    return (
        f"# Deploy guide — {_html_text(app.name)}\n\n"
        "This Vite/React site keeps its list only in the current browser's "
        "localStorage. It has no API, database, account, or secret configuration.\n\n"
        "## Verify locally\n\n"
        "```sh\n"
        "npm ci\n"
        "npm run build\n"
        "npm run preview\n"
        "```\n\n"
        "Add and delete an item, reload the page, and confirm the remaining items persist.\n\n"
        "## Deploy\n\n"
        "```sh\n"
        "npm run build\n"
        "npx wrangler deploy\n"
        "```\n\n"
        "Browser storage is per origin and per browser profile. Clearing site data "
        "clears the list.\n"
    )


def _emit_local_styles(design: DesignSpec) -> str:
    from .generator import _emit_styles_css

    return _emit_styles_css(design) + (
        "\n.local-list { padding: calc(var(--space) * 4) 0; }\n"
        ".local-list-panel { max-width: 44rem; }\n"
        ".local-list-panel h1 { margin: 0 0 var(--space); color: var(--color-primary); "
        "font-size: clamp(2rem, 7vw, 4rem); }\n"
        ".local-list-form { display: grid; gap: 0.5rem; margin-top: calc(var(--space) * 2); }\n"
        ".local-list-form label { font-weight: 700; }\n"
        ".local-list-controls { display: flex; gap: 0.75rem; align-items: stretch; }\n"
        ".local-list-controls input { flex: 1; min-width: 0; padding: 0.7rem; font: inherit; "
        "color: var(--color-text); background: var(--color-surface); border: 1px solid "
        "color-mix(in srgb, var(--color-text) 30%, transparent); border-radius: var(--radius); }\n"
        ".local-list-count { color: var(--color-accent); font-size: 0.9rem; }\n"
        ".local-list-items { list-style: none; margin: var(--space) 0 0; padding: 0; "
        "display: grid; gap: 0.65rem; }\n"
        ".local-list-row { display: flex; align-items: center; justify-content: space-between; "
        "gap: var(--space); padding: var(--space); border: 1px solid "
        "color-mix(in srgb, var(--color-text) 14%, transparent); border-radius: var(--radius); }\n"
        ".local-list-text { overflow-wrap: anywhere; }\n"
        ".local-list-empty { padding: var(--space) 0; }\n"
        "@media (max-width: 36rem) {\n"
        "  .local-list-controls, .local-list-row { align-items: stretch; "
        "flex-direction: column; }\n"
        "}\n"
    )


def generate_local_list(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a local_list AppSpec into a deterministic, API-free Vite tree."""
    app = prepare_local_list_app_spec(app)
    from .generator import (
        _comp_name,
        _component_names,
        _emit_app_tsx,
        _emit_content_ts,
        _emit_gitignore,
        _emit_main_tsx,
        _emit_manifest_ts,
        _emit_package_lock_json,
        _emit_tsconfig,
        _emit_vite_config,
        _iter_sections,
    )

    names = _component_names(app)
    files: dict[str, str] = {
        ".gitignore": _emit_gitignore(),
        "OWNER_GUIDE.md": _emit_local_owner_guide(app),
        "index.html": _emit_local_index_html(app),
        "package-lock.json": _emit_package_lock_json(app),
        "package.json": _emit_local_package_json(app),
        "schema.sql": _emit_local_schema_sql(),
        "src/App.tsx": _emit_app_tsx(app, names),
        "src/generated/content.ts": _emit_content_ts(app, names),
        "src/generated/manifest.ts": _emit_manifest_ts(app, design, names),
        "src/main.tsx": _emit_main_tsx(),
        "src/styles.css": _emit_local_styles(design),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "worker/index.ts": _emit_local_worker_ts(),
        "wrangler.toml": _emit_local_wrangler_toml(app),
    }
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        files[f"src/components/{comp}.tsx"] = (
            _emit_local_list_component(app, page, section, comp)
            if section.kind == "list"
            else _emit_static_section_component(section, comp)
        )
    return dict(sorted(files.items()))


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    failed = sum(not check.passed for check in checks)
    return PrimitiveVerifyResult(
        ok=failed == 0,
        detail=f"{len(checks) - failed} passed / {failed} failed",
        checks=tuple(checks),
    )


def _gather_list_sources(app: AppSpec, tree: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """The component source text for each `list` section (in page/section iteration
    order), and the component path of any `list` section whose source is missing."""
    from .generator import _comp_name, _component_names

    names = _component_names(app)
    list_sources: list[str] = []
    missing: list[str] = []
    for page in app.pages:
        for section in page.sections:
            if section.kind != "list":
                continue
            path = f"src/components/{_comp_name(names, page, section)}.tsx"
            source = tree.get(path)
            if source is None:
                missing.append(path)
            else:
                list_sources.append(source)
    return list_sources, missing


def _source_contract_check(list_sources: list[str], missing: list[str]) -> VerifyCheck:
    """Each list section has a controlled add form, rendered item list, and
    per-item delete control."""
    source_tokens = (
        "useState<string[]>",
        "onSubmit={addItem}",
        "value={draft}",
        "onChange={(event) => setDraft(event.target.value)}",
        "items.map((item, index)",
        "onClick={() => deleteItem(index)}",
    )
    source_ok = (
        bool(list_sources)
        and not missing
        and all(all(token in source for token in source_tokens) for source in list_sources)
    )
    return VerifyCheck(
        "local_list_source_contract",
        source_ok,
        (
            "each list section has a controlled add form, rendered item list, and "
            "per-item delete control."
            if source_ok
            else "missing or incomplete local-list component source"
            + (f": {', '.join(missing)}" if missing else ".")
        ),
    )


def _storage_bounds_check(list_sources: list[str]) -> VerifyCheck:
    """localStorage reads/writes are exception-safe and normalize at most
    `_MAX_ITEMS` strings of `_MAX_ITEM_LENGTH` characters."""
    bound_tokens = (
        f"const MAX_ITEMS = {_MAX_ITEMS};",
        f"const MAX_ITEM_LENGTH = {_MAX_ITEM_LENGTH};",
        "window.localStorage.getItem(STORAGE_KEY)",
        "boundedItems(JSON.parse(raw))",
        "window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items))",
        "item.trim().slice(0, MAX_ITEM_LENGTH)",
        "out.length < MAX_ITEMS",
        "current.length >= MAX_ITEMS ? current",
        "maxLength={MAX_ITEM_LENGTH}",
    )
    bounds_ok = bool(list_sources) and all(
        all(token in source for token in bound_tokens) and source.count("catch {") >= 2
        for source in list_sources
    )
    return VerifyCheck(
        "local_list_storage_bounds",
        bounds_ok,
        (
            f"localStorage reads/writes are exception-safe and normalize at most "
            f"{_MAX_ITEMS} strings of {_MAX_ITEM_LENGTH} characters."
            if bounds_ok
            else "the localStorage path is missing corruption/quota handling or hard bounds."
        ),
    )


def _browser_local_only_check(tree: Mapping[str, str], list_sources: list[str]) -> VerifyCheck:
    """The browser runtime contains no network client, raw-HTML sink, external URL,
    or secret binding."""
    browser_sources = [
        tree.get("index.html") or "",
        tree.get("src/App.tsx") or "",
        tree.get("src/main.tsx") or "",
        *list_sources,
    ]
    forbidden = (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "dangerouslySetInnerHTML",
        "ADMIN_TOKEN",
        "DISCO_SVC_TOKEN",
        "http://",
        "https://",
    )
    local_only = all(token not in source for source in browser_sources for token in forbidden)
    return VerifyCheck(
        "local_list_browser_local_only",
        local_only,
        (
            "browser runtime contains no network client, raw-HTML sink, external URL, "
            "or secret binding."
            if local_only
            else "browser runtime contains a forbidden network/raw-HTML/secret surface."
        ),
    )


def _static_worker_check(tree: Mapping[str, str]) -> VerifyCheck:
    """The Worker is an asset-only passthrough with no API or D1 binding."""
    worker = tree.get("worker/index.ts")
    worker_ok, worker_reasons = inspect_static_worker(worker or "")
    return VerifyCheck(
        "local_list_static_worker",
        worker is not None and worker_ok,
        (
            "the Worker is an asset-only passthrough with no API or D1 binding."
            if worker is not None and worker_ok
            else "; ".join(worker_reasons) or "worker/index.ts is missing."
        ),
    )


def _cloudflare_export_check(tree: Mapping[str, str]) -> VerifyCheck:
    """The STATIC Cloudflare-export deliverables/config are complete."""
    export_files: dict[str, str | None] = {path: tree.get(path) for path in STATIC_CF_EXPORT_FILES}
    export_files[".dev.vars"] = tree.get(".dev.vars")
    export_result = cloudflare_export_ready_static(export_files)
    return VerifyCheck(export_result.name, export_result.passed, export_result.evidence)


def local_list_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Fail-closed source checks for persistence, bounds, deletion, and local-only IO."""
    del design
    if app is None:
        return _result(
            [
                VerifyCheck(
                    "local_list_source_contract",
                    False,
                    "no .disco/appspec.json — run app_create first.",
                )
            ]
        )

    list_sources, missing = _gather_list_sources(app, tree)
    checks: list[VerifyCheck] = [
        _source_contract_check(list_sources, missing),
        _storage_bounds_check(list_sources),
        _browser_local_only_check(tree, list_sources),
        _static_worker_check(tree),
        _cloudflare_export_check(tree),
    ]
    return _result(checks)


register_primitive(
    PrimitiveDefinition(
        id=LOCAL_LIST_PRIMITIVE_ID,
        default_app_spec=default_local_list_app_spec,
        prepare_app_spec=prepare_local_list_app_spec,
        generate=generate_local_list,
        tier="template_only",
        verify=local_list_verify,
    )
)


__all__ = [
    "LOCAL_LIST_PRIMITIVE_ID",
    "default_local_list_app_spec",
    "generate_local_list",
    "local_list_verify",
    "prepare_local_list_app_spec",
]
