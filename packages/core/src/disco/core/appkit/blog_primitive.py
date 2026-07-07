"""The AppKit `blog` primitive (Epic 5.3-lite): static posts + RSS.

An ADD-ON primitive: `app_add_primitive` validates `BlogSpec`, `apply_blog_spec`
folds the resolved posts into `AppSpec.blog`, and the host primitive regenerates
the tree. The lowering lives here and is called from the shared generators only
when `app.blog` is present, so no-blog output remains byte-identical.

Markdown scope is intentionally small and safe: headings, paragraphs, ordered and
unordered lists, plus simple inline strong/em/code/links. It lowers to structured
React data rendered as React nodes, not `dangerouslySetInnerHTML`, so user-authored
Markdown is never injected as raw HTML.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import UTC, datetime, time
from email.utils import format_datetime
from typing import Literal, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .primitives import (
    BLOG_PRIMITIVE_ID,
    DIRECTORY_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    RECORDS_PRIMITIVE_ID,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
    resolve_primitive,
)
from .recipes import SiteRecipe
from .spec import (
    MAX_BLOG_BODY_MD,
    MAX_BLOG_INDEX_TITLE,
    MAX_BLOG_POSTS,
    MAX_BLOG_SLUG,
    MAX_BLOG_SUMMARY,
    MAX_BLOG_TITLE,
    AppSpec,
    BlogPostMeta,
    DesignSpec,
    validate_blog_slug,
    validate_iso_date,
)

_BLOG_INDEX_ROUTE = "/blog"
_SUPPORTED_HOSTS = frozenset(
    {LEAD_GEN_PRIMITIVE_ID, DIRECTORY_PRIMITIVE_ID, RECORDS_PRIMITIVE_ID}
)


class BlogPostSpec(BaseModel):
    """One model-authored blog post."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=MAX_BLOG_SLUG)
    title: str = Field(min_length=1, max_length=MAX_BLOG_TITLE)
    date: str = Field(min_length=1, max_length=10)
    summary: str | None = Field(default=None, min_length=1, max_length=MAX_BLOG_SUMMARY)
    body_md: str = Field(min_length=1, max_length=MAX_BLOG_BODY_MD)

    @field_validator("slug")
    @classmethod
    def _slug_is_safe(cls, value: str) -> str:
        return validate_blog_slug(value, field="blog post slug")

    @field_validator("date")
    @classmethod
    def _date_is_iso(cls, value: str) -> str:
        return validate_iso_date(value, field="blog post date")

    @field_validator("title", "summary", "body_md")
    @classmethod
    def _text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("blog text fields must not be blank")
        return value


class BlogSpec(BaseModel):
    """The blog primitive's declarative fill spec."""

    model_config = ConfigDict(extra="forbid")

    posts: list[BlogPostSpec] = Field(min_length=1, max_length=MAX_BLOG_POSTS)
    index_page_title: str | None = Field(
        default=None, min_length=1, max_length=MAX_BLOG_INDEX_TITLE
    )

    @field_validator("index_page_title")
    @classmethod
    def _index_title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("blog index_page_title must not be blank")
        return value

    @model_validator(mode="after")
    def _slugs_unique(self) -> BlogSpec:
        seen: set[str] = set()
        for post in self.posts:
            if post.slug in seen:
                raise ValueError(f"duplicate blog post slug: {post.slug!r}")
            seen.add(post.slug)
        return self


def blog_routes_for(app: AppSpec) -> tuple[str, ...]:
    """The static SPA routes contributed by `app.blog`."""
    blog = app.blog
    if blog is None:
        return ()
    return (_BLOG_INDEX_ROUTE, *(f"{_BLOG_INDEX_ROUTE}/{post.slug}" for post in blog.posts))


def has_blog(app: AppSpec) -> bool:
    return app.blog is not None and bool(app.blog.posts)


def apply_blog_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated BlogSpec into AppSpec.blog (dump → mutate → re-validate)."""
    if not isinstance(spec, BlogSpec):
        raise TypeError(f"apply_spec for {BLOG_PRIMITIVE_ID!r} needs a BlogSpec")

    host = resolve_primitive(app.app_kind)
    if host.id not in _SUPPORTED_HOSTS:
        supported = ", ".join(sorted(_SUPPORTED_HOSTS))
        raise ValueError(
            f"the blog primitive currently folds only into shared React AppKit hosts "
            f"({supported}); this app_kind {app.app_kind!r} lowers through {host.id!r}."
        )
    taken_routes = {page.route.rstrip("/") or "/" for page in app.pages}
    collisions = sorted(taken_routes & set(_routes_for_posts(spec.posts)))
    if collisions:
        raise ValueError(
            "the blog primitive needs routes that are already declared on the app: "
            f"{collisions}. Move or rename those pages before adding the blog."
        )

    posts = sorted(spec.posts, key=lambda post: post.date, reverse=True)
    data = app.model_dump(mode="json")
    data["blog"] = {
        "posts": [post.model_dump(mode="json") for post in posts],
        "index_page_title": spec.index_page_title,
    }
    return AppSpec.model_validate(data)


def _routes_for_posts(posts: list[BlogPostSpec]) -> tuple[str, ...]:
    return (_BLOG_INDEX_ROUTE, *(f"{_BLOG_INDEX_ROUTE}/{post.slug}" for post in posts))


BlogInlineKind = Literal["text", "strong", "em", "code", "link"]
BlogBlockKind = Literal["paragraph", "h2", "h3", "ul", "ol"]


class BlogInline(TypedDict, total=False):
    kind: BlogInlineKind
    text: str
    href: str


class BlogBlock(TypedDict, total=False):
    kind: BlogBlockKind
    inlines: list[BlogInline]
    items: list[list[BlogInline]]


_INLINE_RE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*|\[[^\]\n]+\]\([^) \n]+\))")
_LINK_RE = re.compile(r"^\[([^\]\n]+)\]\(([^) \n]+)\)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.+)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")


def _inline(kind: BlogInlineKind, text: str, href: str | None = None) -> BlogInline:
    node: BlogInline = {"kind": kind, "text": text}
    if href is not None:
        node["href"] = href
    return node


def _safe_href(raw: str) -> str | None:
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw
    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw
    return None


def _parse_inlines(text: str) -> list[BlogInline]:
    nodes: list[BlogInline] = []
    cursor = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > cursor:
            nodes.append(_inline("text", text[cursor:match.start()]))
        token = match.group(0)
        link = _LINK_RE.match(token)
        if link is not None:
            href = _safe_href(link.group(2))
            if href is not None:
                nodes.append(_inline("link", link.group(1), href))
            else:
                nodes.append(_inline("text", link.group(1)))
        elif token.startswith("**"):
            nodes.append(_inline("strong", token[2:-2]))
        elif token.startswith("*"):
            nodes.append(_inline("em", token[1:-1]))
        else:
            nodes.append(_inline("code", token[1:-1]))
        cursor = match.end()
    if cursor < len(text):
        nodes.append(_inline("text", text[cursor:]))
    return nodes or [_inline("text", "")]


def _markdown_blocks(markdown: str) -> list[BlogBlock]:
    lines = markdown.strip().splitlines()
    blocks: list[BlogBlock] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(
                {"kind": "paragraph", "inlines": _parse_inlines(" ".join(paragraph))}
            )
            paragraph.clear()

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            hashes = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[hashes:].strip()
            if text:
                blocks.append(
                    {
                        "kind": "h2" if hashes <= 2 else "h3",
                        "inlines": _parse_inlines(text),
                    }
                )
            i += 1
            continue
        ul = _UL_RE.match(raw)
        ol = _OL_RE.match(raw)
        if ul is not None or ol is not None:
            flush_paragraph()
            kind: BlogBlockKind = "ul" if ul is not None else "ol"
            items: list[list[BlogInline]] = []
            while i < len(lines):
                item_match = (
                    _UL_RE.match(lines[i]) if kind == "ul" else _OL_RE.match(lines[i])
                )
                if item_match is None:
                    break
                items.append(_parse_inlines(item_match.group(1).strip()))
                i += 1
            blocks.append({"kind": kind, "items": items})
            continue
        paragraph.append(stripped)
        i += 1
    flush_paragraph()
    return blocks


def emit_blog_files(app: AppSpec) -> dict[str, str]:
    """Return the generated blog-only files; empty when the app has no blog."""
    blog = app.blog
    if blog is None:
        return {}
    names = _post_component_names(blog.posts)
    files = {
        "src/generated/blog.ts": _emit_blog_data_ts(app),
        "src/blog/BlogIndexPage.tsx": _emit_blog_index_page_tsx(),
        "src/blog/BlogRichText.tsx": _emit_blog_rich_text_tsx(),
        "src/blog/blog.css": _emit_blog_css(),
        "public/rss.xml": _emit_rss_xml(app),
    }
    for i, post in enumerate(blog.posts):
        files[f"src/blog/{names[post.slug]}.tsx"] = _emit_blog_post_page_tsx(
            names[post.slug], i
        )
    return files


def _post_component_names(posts: tuple[BlogPostMeta, ...]) -> dict[str, str]:
    from .generator import _pascal

    used: set[str] = {"BlogIndexPage", "BlogRichText"}
    names: dict[str, str] = {}
    for post in posts:
        stem = f"Blog{_pascal(post.slug)}PostPage"
        name = stem
        suffix = 2
        while name in used:
            name = f"{stem}{suffix}"
            suffix += 1
        used.add(name)
        names[post.slug] = name
    return names


def _emit_blog_data_ts(app: AppSpec) -> str:
    from .generator import _ts

    blog = app.blog
    if blog is None:
        posts: list[dict[str, object]] = []
        title = "Blog"
    else:
        posts = [_post_record(post) for post in blog.posts]
        title = blog.index_page_title or "Blog"
    return (
        "/* Auto-generated blog content — regenerated from .disco/appspec.json. */\n"
        "export type BlogInline =\n"
        '  | { kind: "text"; text: string }\n'
        '  | { kind: "strong"; text: string }\n'
        '  | { kind: "em"; text: string }\n'
        '  | { kind: "code"; text: string }\n'
        '  | { kind: "link"; text: string; href: string };\n\n'
        "export type BlogBlock =\n"
        '  | { kind: "paragraph" | "h2" | "h3"; inlines: BlogInline[] }\n'
        '  | { kind: "ul" | "ol"; items: BlogInline[][] };\n\n'
        "export interface BlogPost {\n"
        "  slug: string;\n"
        "  title: string;\n"
        "  date: string;\n"
        "  route: string;\n"
        "  summary?: string;\n"
        "  blocks: BlogBlock[];\n"
        "}\n\n"
        f"export const BLOG_INDEX_TITLE = {_ts(title)};\n"
        f"export const BLOG_POSTS: BlogPost[] = {_ts(posts)};\n"
    )


def _post_record(post: BlogPostMeta) -> dict[str, object]:
    record: dict[str, object] = {
        "slug": post.slug,
        "title": post.title,
        "date": post.date,
        "route": f"{_BLOG_INDEX_ROUTE}/{post.slug}",
        "blocks": _markdown_blocks(post.body_md),
    }
    if post.summary is not None:
        record["summary"] = post.summary
    return record


def _emit_blog_index_page_tsx() -> str:
    return (
        "/* Auto-generated blog index page — regenerated from .disco/appspec.json. */\n"
        'import { BLOG_INDEX_TITLE, BLOG_POSTS } from "../generated/blog";\n'
        'import "./blog.css";\n\n'
        "export default function BlogIndexPage() {\n"
        "  return (\n"
        '    <main className="blog-page blog-index">\n'
        '      <section className="blog-shell">\n'
        "        <h1>{BLOG_INDEX_TITLE}</h1>\n"
        '        <ul className="blog-list">\n'
        "          {BLOG_POSTS.map((post) => (\n"
        "            <li key={post.slug}>\n"
        "              <a href={post.route}>{post.title}</a>\n"
        "              <time dateTime={post.date}>{post.date}</time>\n"
        "              {post.summary ? <p>{post.summary}</p> : null}\n"
        "            </li>\n"
        "          ))}\n"
        "        </ul>\n"
        "      </section>\n"
        "    </main>\n"
        "  );\n"
        "}\n"
    )


def _emit_blog_post_page_tsx(component: str, post_index: int) -> str:
    return (
        "/* Auto-generated blog post page — regenerated from .disco/appspec.json. */\n"
        'import { BLOG_POSTS } from "../generated/blog";\n'
        'import { BlogBody } from "./BlogRichText";\n'
        'import "./blog.css";\n\n'
        f"const POST = BLOG_POSTS[{post_index}];\n\n"
        f"export default function {component}() {{\n"
        "  return (\n"
        '    <main className="blog-page blog-post">\n'
        '      <article className="blog-shell">\n'
        "        <p><a className=\"blog-back\" href=\"/blog\">All posts</a></p>\n"
        "        <h1>{POST.title}</h1>\n"
        "        <time dateTime={POST.date}>{POST.date}</time>\n"
        "        {POST.summary ? <p className=\"blog-summary\">{POST.summary}</p> : null}\n"
        "        <BlogBody blocks={POST.blocks} />\n"
        "      </article>\n"
        "    </main>\n"
        "  );\n"
        "}\n"
    )


def _emit_blog_rich_text_tsx() -> str:
    return (
        "/* Auto-generated safe blog rich text renderer. No raw HTML injection. */\n"
        'import type { BlogBlock, BlogInline } from "../generated/blog";\n\n'
        "function InlineNode({ node }: { node: BlogInline }) {\n"
        '  if (node.kind === "strong") return <strong>{node.text}</strong>;\n'
        '  if (node.kind === "em") return <em>{node.text}</em>;\n'
        '  if (node.kind === "code") return <code>{node.text}</code>;\n'
        '  if (node.kind === "link") return <a href={node.href}>{node.text}</a>;\n'
        "  return <>{node.text}</>;\n"
        "}\n\n"
        "function Inlines({ nodes }: { nodes: BlogInline[] }) {\n"
        "  return <>{nodes.map((node, i) => <InlineNode node={node} key={i} />)}</>;\n"
        "}\n\n"
        "export function BlogBody({ blocks }: { blocks: BlogBlock[] }) {\n"
        "  return (\n"
        '    <div className="blog-body">\n'
        "      {blocks.map((block, i) => {\n"
        '        if (block.kind === "h2") {\n'
        "          return <h2 key={i}><Inlines nodes={block.inlines} /></h2>;\n"
        "        }\n"
        '        if (block.kind === "h3") {\n'
        "          return <h3 key={i}><Inlines nodes={block.inlines} /></h3>;\n"
        "        }\n"
        '        if (block.kind === "ul") {\n'
        "          return (\n"
        "            <ul key={i}>\n"
        "              {block.items.map((item, j) => (\n"
        "                <li key={j}><Inlines nodes={item} /></li>\n"
        "              ))}\n"
        "            </ul>\n"
        "          );\n"
        "        }\n"
        '        if (block.kind === "ol") {\n'
        "          return (\n"
        "            <ol key={i}>\n"
        "              {block.items.map((item, j) => (\n"
        "                <li key={j}><Inlines nodes={item} /></li>\n"
        "              ))}\n"
        "            </ol>\n"
        "          );\n"
        "        }\n"
        "        return <p key={i}><Inlines nodes={block.inlines} /></p>;\n"
        "      })}\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


def _emit_blog_css() -> str:
    return (
        "/* Blog primitive styles. Imported only by generated blog pages. */\n"
        ".blog-page { color: var(--color-text); background: var(--color-surface); }\n"
        ".blog-shell {\n"
        "  max-width: 760px; margin: 0 auto;\n"
        "  padding: calc(var(--space) * 3) var(--space);\n"
        "}\n"
        ".blog-shell h1 { margin: 0 0 var(--space); color: var(--color-primary); }\n"
        ".blog-shell time {\n"
        "  color: color-mix(in srgb, var(--color-text) 65%, transparent);\n"
        "  font-size: 0.95rem;\n"
        "}\n"
        ".blog-list {\n"
        "  list-style: none; margin: calc(var(--space) * 2) 0 0; padding: 0;\n"
        "  display: grid; gap: calc(var(--space) * 1.4);\n"
        "}\n"
        ".blog-list li {\n"
        "  border-top: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);\n"
        "  padding-top: var(--space);\n"
        "}\n"
        ".blog-list a {\n"
        "  color: var(--color-primary); font-weight: 700;\n"
        "  text-decoration-thickness: 0.08em;\n"
        "}\n"
        ".blog-list time { display: block; margin-top: 0.25rem; }\n"
        ".blog-summary {\n"
        "  font-size: 1.1rem;\n"
        "  color: color-mix(in srgb, var(--color-text) 78%, transparent);\n"
        "}\n"
        ".blog-body { margin-top: calc(var(--space) * 2); display: grid; gap: var(--space); }\n"
        ".blog-body h2, .blog-body h3 {\n"
        "  margin: calc(var(--space) * 1.5) 0 0; color: var(--color-primary);\n"
        "}\n"
        ".blog-body p, .blog-body ul, .blog-body ol { margin: 0; }\n"
        ".blog-body code {\n"
        "  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;\n"
        "  font-size: 0.95em;\n"
        "}\n"
        ".blog-back { color: var(--color-primary); }\n"
    )


def emit_app_tsx_with_blog_routes(
    app: AppSpec, names: dict[tuple[str, str], str]
) -> str:
    """A route-aware app shell for host pages plus the generated blog routes."""
    from .generator import _comp_name, _iter_sections, _ts

    blog = app.blog
    if blog is None:
        raise ValueError("emit_app_tsx_with_blog_routes requires app.blog")
    post_names = _post_component_names(blog.posts)
    imports: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
    imports.append('import BlogIndexPage from "./blog/BlogIndexPage";')
    imports.extend(
        f'import {post_names[post.slug]} from "./blog/{post_names[post.slug]}";'
        for post in blog.posts
    )
    page_funcs, route_entries = _host_page_routes(app, names)
    route_entries.append(f"  {_ts(_BLOG_INDEX_ROUTE)}: BlogIndexPage,")
    route_entries.extend(
        f"  {_ts(_BLOG_INDEX_ROUTE + '/' + post.slug)}: {post_names[post.slug]},"
        for post in blog.posts
    )
    fallback = "Page0" if app.pages else "BlogIndexPage"
    return (
        "/* Auto-generated route-aware app shell with blog routes — regenerated from "
        ".disco/appspec.json. */\n"
        'import { type ReactElement } from "react";\n'
        + "\n".join(imports)
        + "\n\n"
        + "\n".join(page_funcs)
        + "\n"
        "const ROUTES: Record<string, () => ReactElement> = {\n"
        + "\n".join(route_entries)
        + "\n};\n\n"
        "export default function App(): ReactElement {\n"
        '  const path = window.location.pathname.replace(/\\/+$/, "") || "/";\n'
        f"  const Page = ROUTES[path] ?? {fallback};\n"
        "  return (\n"
        '    <div className="app-main">\n'
        "      <Page />\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


def _host_page_routes(
    app: AppSpec, names: dict[tuple[str, str], str]
) -> tuple[list[str], list[str]]:
    from .generator import _comp_name, _ts

    page_funcs: list[str] = []
    route_entries: list[str] = []
    for i, page in enumerate(app.pages):
        fn = f"Page{i}"
        renders = "\n".join(
            f"      <{_comp_name(names, page, section)} />" for section in page.sections
        )
        body = (renders + "\n") if renders else ""
        page_funcs.append(
            f"function {fn}(): ReactElement {{\n"
            "  return (\n"
            "    <>\n"
            f"{body}"
            "    </>\n"
            "  );\n"
            "}\n"
        )
        norm_route = page.route.rstrip("/") or "/"
        route_entries.append(f"  {_ts(norm_route)}: {fn},")
    return page_funcs, route_entries


def _blog_abs_or_relative_url(app: AppSpec, route: str) -> str:
    if app.seo is None:
        return route
    from .generator import _seo_abs_url

    return _seo_abs_url(app.seo.base_url, route)


def _rss_date(value: str) -> str:
    dt = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time(), tzinfo=UTC)
    return format_datetime(dt, usegmt=True)


def _xml_text(value: str) -> str:
    import html

    return html.escape(value, quote=True)


def _emit_rss_xml(app: AppSpec) -> str:
    blog = app.blog
    if blog is None:
        return ""
    channel_title = f"{app.name} - {blog.index_page_title or 'Blog'}"
    items = []
    for post in blog.posts:
        url = _blog_abs_or_relative_url(app, f"{_BLOG_INDEX_ROUTE}/{post.slug}")
        description = post.summary or post.title
        items.append(
            "    <item>\n"
            f"      <title>{_xml_text(post.title)}</title>\n"
            f"      <link>{_xml_text(url)}</link>\n"
            f"      <guid isPermaLink=\"true\">{_xml_text(url)}</guid>\n"
            f"      <pubDate>{_rss_date(post.date)}</pubDate>\n"
            f"      <description>{_xml_text(description)}</description>\n"
            "    </item>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>{_xml_text(channel_title)}</title>\n"
        f"    <link>{_xml_text(_blog_abs_or_relative_url(app, _BLOG_INDEX_ROUTE))}</link>\n"
        f"    <description>{_xml_text(channel_title)}</description>\n"
        + "".join(items)
        + "  </channel>\n"
        "</rss>\n"
    )


def blog_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """WO-A3 verify hook for the applied blog primitive."""
    del design
    checks = [
        _check_blog_spec(app),
        _check_blog_index(app, tree),
        _check_blog_routes(app, tree),
        _check_blog_rss(app, tree),
    ]
    failed = sum(1 for check in checks if not check.passed)
    return PrimitiveVerifyResult(
        ok=failed == 0,
        detail=f"{len(checks) - failed} passed / {failed} failed",
        checks=tuple(checks),
    )


def _check_blog_spec(app: AppSpec | None) -> VerifyCheck:
    if app is None or app.blog is None:
        return VerifyCheck("blog_spec_present", False, "no AppSpec blog content found.")
    return VerifyCheck(
        "blog_spec_present",
        True,
        f"AppSpec carries {len(app.blog.posts)} blog post(s).",
    )


def _check_blog_index(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    if app is None or app.blog is None:
        return VerifyCheck("blog_index_lists_posts", False, "no blog posts to check.")
    index = tree.get("src/blog/BlogIndexPage.tsx")
    data = tree.get("src/generated/blog.ts")
    if index is None or data is None:
        return VerifyCheck(
            "blog_index_lists_posts",
            False,
            "missing src/blog/BlogIndexPage.tsx or src/generated/blog.ts.",
        )
    missing = [post.title for post in app.blog.posts if post.title not in data]
    maps_posts = "BLOG_POSTS.map" in index and "post.route" in index and "post.title" in index
    if missing or not maps_posts:
        return VerifyCheck(
            "blog_index_lists_posts",
            False,
            f"index/data does not list every post; missing titles: {missing}.",
        )
    return VerifyCheck(
        "blog_index_lists_posts",
        True,
        "blog index maps BLOG_POSTS and generated data carries every title.",
    )


def _check_blog_routes(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    if app is None or app.blog is None:
        return VerifyCheck("blog_post_routes", False, "no blog posts to check.")
    app_tsx = tree.get("src/App.tsx") or ""
    post_names = _post_component_names(app.blog.posts)
    missing: list[str] = []
    for route in blog_routes_for(app):
        if route not in app_tsx:
            missing.append(route)
    for post in app.blog.posts:
        path = f"src/blog/{post_names[post.slug]}.tsx"
        if path not in tree:
            missing.append(path)
    if missing:
        return VerifyCheck("blog_post_routes", False, f"missing blog routes/files: {missing}.")
    return VerifyCheck(
        "blog_post_routes",
        True,
        f"App.tsx routes /blog plus {len(app.blog.posts)} post page(s).",
    )


def _check_blog_rss(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    if app is None or app.blog is None:
        return VerifyCheck("blog_rss_xml", False, "no blog posts to check.")
    rss = tree.get("public/rss.xml")
    if rss is None:
        return VerifyCheck("blog_rss_xml", False, "missing public/rss.xml.")
    try:
        root = ET.fromstring(rss)
    except ET.ParseError as exc:
        return VerifyCheck("blog_rss_xml", False, f"public/rss.xml does not parse: {exc}.")
    titles = [node.text or "" for node in root.findall("./channel/item/title")]
    missing = [post.title for post in app.blog.posts if post.title not in titles]
    if missing:
        return VerifyCheck("blog_rss_xml", False, f"RSS missing post titles: {missing}.")
    return VerifyCheck("blog_rss_xml", True, "public/rss.xml parses and carries every post.")


def default_blog_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The blog primitive is an add-on, not a base scaffold."""
    del name, recipe
    raise ValueError(
        "the 'blog' primitive is an ADD-ON, not a base scaffold: app_create a base "
        "app first, then app_add_primitive(primitive_id='blog', spec={...})."
    )


def prepare_blog_app_spec(app: AppSpec) -> AppSpec:
    return app


def generate_blog(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    del app, design
    raise ValueError(
        "the 'blog' primitive does not generate a tree of its own; the host app's "
        "base primitive regenerates from the folded AppSpec."
    )


register_primitive(
    PrimitiveDefinition(
        id=BLOG_PRIMITIVE_ID,
        default_app_spec=default_blog_app_spec,
        prepare_app_spec=prepare_blog_app_spec,
        generate=generate_blog,
        tier="fillable",
        host_contract=(),
        spec_schema=BlogSpec,
        verify=blog_verify,
        apply_spec=apply_blog_spec,
    )
)


__all__ = [
    "BLOG_PRIMITIVE_ID",
    "BlogPostSpec",
    "BlogSpec",
    "apply_blog_spec",
    "blog_routes_for",
    "blog_verify",
    "emit_app_tsx_with_blog_routes",
    "emit_blog_files",
    "generate_blog",
    "has_blog",
]
