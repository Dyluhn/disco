"""The SPA shell + SEO emitters: content.ts, App.tsx, main.tsx, SEO head/crawler
files, and index.html.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

import json

from .. import semantic_metadata as _md
from ..spec import AppSpec, DesignSpec
from .design_tokens import _google_fonts_href
from .ids import _comp_name, _html_text, _iter_sections, _ts


def _emit_content_ts(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    entries: list[str] = []
    has_success_message = any(
        section.content is not None and section.content.success_message is not None
        for _page, section in _iter_sections(app)
    )
    for page, section in _iter_sections(app):
        comp_id = _comp_name(names, page, section)
        content = section.content
        slot: dict[str, object] = {}
        if content is not None:
            if content.eyebrow is not None:
                slot["eyebrow"] = content.eyebrow
            if content.heading is not None:
                slot["heading"] = content.heading
            if content.subheading is not None:
                slot["subheading"] = content.subheading
            if content.body is not None:
                slot["body"] = content.body
            if content.cta_label is not None:
                slot["ctaLabel"] = content.cta_label
            if content.items:
                slot["items"] = list(content.items)
            if has_success_message and content.success_message is not None:
                slot["successMessage"] = content.success_message
        entries.append(f"  {_ts(comp_id)}: {_ts(slot)},")
    body = "\n".join(entries)
    success_message_field = "  successMessage?: string;\n" if has_success_message else ""
    return (
        "/* Auto-generated section content — regenerated from .disco/appspec.json. */\n"
        "export interface SectionContent {\n"
        "  eyebrow?: string;\n"
        "  heading?: string;\n"
        "  subheading?: string;\n"
        "  body?: string;\n"
        "  ctaLabel?: string;\n"
        "  items?: string[];\n"
        f"{success_message_field}"
        "}\n\n"
        "export const CONTENT: Record<string, SectionContent> = {\n" + body + "\n};\n"
    )


def _emit_app_tsx(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    imports: list[str] = []
    renders: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
        renders.append(f"      <{comp} />")
    return (
        "/* Auto-generated app shell — regenerated from .disco/appspec.json. */\n"
        + "\n".join(imports)
        + "\n\n"
        "export default function App() {\n"
        "  return (\n"
        '    <div className="app-main">\n' + "\n".join(renders) + "\n"
        "    </div>\n"
        "  );\n}\n"
    )


def _emit_main_tsx() -> str:
    return (
        'import React from "react";\n'
        'import { createRoot } from "react-dom/client";\n'
        'import App from "./App";\n'
        'import "./styles.css";\n\n'
        'const el = document.getElementById("root");\n'
        "if (el) {\n"
        "  createRoot(el).render(\n"
        "    <React.StrictMode>\n"
        "      <App />\n"
        "    </React.StrictMode>\n"
        "  );\n"
        "}\n"
    )


def _seo_abs_url(base_url: str, route: str) -> str:
    """Join a validated http(s) base URL and a spec-validated ABSOLUTE route
    (always starts with '/') with exactly one slash between them — a trailing-
    slash base_url can never produce a '//' in sitemap/OG URLs."""
    return base_url.rstrip("/") + route


def _seo_head_extras(app: AppSpec) -> str:
    """The Epic F5.3 `<head>` additions: meta description, the OG tags, and a
    JSON-LD ``WebSite`` block. Returns the EMPTY STRING when `app.seo` is None —
    `_emit_index_html` interpolates this directly, so the no-seo index.html is
    PROVABLY byte-identical to the pre-F5.3 output (the hard constraint).

    Every user-provided value lands attribute-escaped (`_html_text`). The JSON-LD
    payload is json.dumps output (never hand-built JSON) with `&`/`<`/`>` forced
    to \\uXXXX escapes afterwards — a legal transform inside JSON string
    literals — so a hostile description can never close the `<script>` tag or
    open markup inside it."""
    seo = app.seo
    if seo is None:
        return ""
    site_name = seo.site_name or app.name
    canonical = _seo_abs_url(seo.base_url, "/")
    lines = [
        f'    <meta name="description" content="{_html_text(seo.site_description)}" />\n',
        f'    <meta property="og:title" content="{_html_text(site_name)}" />\n',
        f'    <meta property="og:description" content="{_html_text(seo.site_description)}" />\n',
        '    <meta property="og:type" content="website" />\n',
        f'    <meta property="og:url" content="{_html_text(canonical)}" />\n',
    ]
    if seo.social_image_url is not None:
        lines.append(
            f'    <meta property="og:image" content="{_html_text(seo.social_image_url)}" />\n'
        )
    ld_payload = (
        json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": site_name,
                "url": canonical,
                "description": seo.site_description,
            },
            ensure_ascii=False,
        )
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    lines.append(f'    <script type="application/ld+json">{ld_payload}</script>\n')
    return "".join(lines)


def _seo_files(app: AppSpec) -> dict[str, str]:
    """The Epic F5.3 crawler files — an EMPTY dict when `app.seo` is None (the
    no-seo tree gains no files; the tree builders `files.update(...)` this).

    Emitted under `public/` because Vite copies `publicDir` verbatim into
    `dist/`, which is what wrangler's `[assets]` layer serves — a root-level
    robots.txt would never reach production (a false affordance). sitemap.xml
    carries one `<url>` per AppSpec page, absolute via `_seo_abs_url` (no
    double-slash joins); `<loc>` values are escaped (routes are already
    charset-constrained by the spec — belt-and-suspenders for the base URL)."""
    seo = app.seo
    if seo is None:
        return {}
    routes = [page.route for page in app.pages]
    if app.blog is not None:
        from ..blog_primitive import blog_routes_for

        routes.extend(blog_routes_for(app))
    entries = "".join(
        f"  <url>\n    <loc>{_html_text(_seo_abs_url(seo.base_url, route))}</loc>\n  </url>\n"
        for route in routes
    )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}"
        "</urlset>\n"
    )
    robots = f"User-agent: *\nAllow: /\n\nSitemap: {_seo_abs_url(seo.base_url, '/sitemap.xml')}\n"
    return {"public/robots.txt": robots, "public/sitemap.xml": sitemap}


def _emit_index_html(app: AppSpec, design: DesignSpec) -> str:
    href = _google_fonts_href(design)
    ver = _md.attr(_md.DataDiscoAttr.VERSION, _md.METADATA_VERSION)
    return (
        "<!doctype html>\n"
        f'<html lang="en"{ver}>\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"    <title>{_html_text(app.name)}</title>\n"
        '    <link rel="preconnect" href="https://fonts.googleapis.com" />\n'
        '    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n'
        f'    <link rel="stylesheet" href="{href}" />\n'
        # F5.3: "" when app.seo is None — the no-seo output is byte-identical.
        f"{_seo_head_extras(app)}"
        "  </head>\n"
        "  <body>\n"
        '    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.tsx"></script>\n'
        "  </body>\n"
        "</html>\n"
    )
