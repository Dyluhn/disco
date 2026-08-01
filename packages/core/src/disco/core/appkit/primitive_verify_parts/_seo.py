"""SEO primitive: the `seo_verify` hook and its check-cluster helpers.

Extracted from ``primitive_verify`` to keep that module's facade under the
module logical-line budget and each check under its own complexity/length
cap. Pure ports: every check name and evidence string is byte-identical to
the original ``seo_verify``.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping

from ..primitive_verify import _result
from ..primitives import PrimitiveVerifyResult, VerifyCheck
from ..spec import AppSpec, DesignSpec, SeoMeta


def _check_seo_head_metadata(
    tree: Mapping[str, str], seo: SeoMeta, site_name: str, base: str
) -> VerifyCheck:
    """The seo_head_metadata check, pure port of the `if index is None / else`
    decision cluster in the original `seo_verify`."""
    index = tree.get("index.html")
    head_reasons: list[str] = []
    if index is None:
        head_reasons.append("no index.html in the workspace")
    else:
        desc = html.escape(seo.site_description, quote=True)
        title = html.escape(site_name, quote=True)
        url = html.escape(base + "/", quote=True)
        expected = [
            f'<meta name="description" content="{desc}" />',
            f'<meta property="og:title" content="{title}" />',
            f'<meta property="og:description" content="{desc}" />',
            '<meta property="og:type" content="website" />',
            f'<meta property="og:url" content="{url}" />',
        ]
        missing = [tag for tag in expected if tag not in index]
        if seo.social_image_url is not None:
            image = html.escape(seo.social_image_url, quote=True)
            if f'<meta property="og:image" content="{image}" />' not in index:
                missing.append("og:image")
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', index)
        if match is None:
            missing.append("JSON-LD WebSite block")
        else:
            try:
                ld = json.loads(match.group(1))
            except json.JSONDecodeError:
                missing.append("parseable JSON-LD")
            else:
                if (
                    ld.get("@type") != "WebSite"
                    or ld.get("name") != site_name
                    or ld.get("description") != seo.site_description
                    or ld.get("url") != base + "/"
                ):
                    missing.append("JSON-LD values matching app.seo")
        if missing:
            head_reasons.append("missing/mismatched head metadata: " + ", ".join(missing))
    head_ok = not head_reasons
    return VerifyCheck(
        "seo_head_metadata",
        head_ok,
        "index.html head carries description, OG tags, and JSON-LD from app.seo."
        if head_ok
        else "; ".join(head_reasons),
    )


def _check_seo_robots_txt(tree: Mapping[str, str], base: str) -> VerifyCheck:
    """The seo_robots_txt check, pure port of the block of the same name in the
    original `seo_verify`."""
    robots = tree.get("public/robots.txt")
    sitemap_url = f"{base}/sitemap.xml"
    robots_ok = (
        robots is not None
        and "User-agent: *\n" in robots
        and "Allow: /\n" in robots
        and f"Sitemap: {sitemap_url}\n" in robots
    )
    return VerifyCheck(
        "seo_robots_txt",
        robots_ok,
        "public/robots.txt exists and points at the generated sitemap."
        if robots_ok
        else "public/robots.txt is missing or does not allow all + reference the sitemap.",
    )


def _check_seo_sitemap_xml(tree: Mapping[str, str], app: AppSpec, base: str) -> VerifyCheck:
    """The seo_sitemap_xml check, pure port of the block of the same name in the
    original `seo_verify`."""
    sitemap = tree.get("public/sitemap.xml")
    expected_locs = [f"{base}{page.route}" for page in app.pages]
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap or "")
    sitemap_ok = sitemap is not None and locs == expected_locs
    return VerifyCheck(
        "seo_sitemap_xml",
        sitemap_ok,
        "public/sitemap.xml lists every AppSpec page route."
        if sitemap_ok
        else (
            "public/sitemap.xml is missing or has wrong routes: "
            f"expected {expected_locs}, got {locs}"
        ),
    )


def seo_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Pure checks for SEO metadata + robots/sitemap output."""
    del design
    if app is None or app.seo is None:
        return _result(
            [
                VerifyCheck(
                    "seo_head_metadata",
                    False,
                    "the AppSpec has no folded seo metadata to verify.",
                ),
                VerifyCheck(
                    "seo_robots_txt",
                    False,
                    "the AppSpec has no folded seo metadata to derive robots.txt.",
                ),
                VerifyCheck(
                    "seo_sitemap_xml",
                    False,
                    "the AppSpec has no folded seo metadata to derive sitemap.xml.",
                ),
            ]
        )

    seo = app.seo
    base = seo.base_url.rstrip("/")
    site_name = seo.site_name or app.name

    return _result(
        [
            _check_seo_head_metadata(tree, seo, site_name, base),
            _check_seo_robots_txt(tree, base),
            _check_seo_sitemap_xml(tree, app, base),
        ]
    )
