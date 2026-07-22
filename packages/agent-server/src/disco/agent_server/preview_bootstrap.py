"""Shared body-only preview-capability redemption helpers."""

from __future__ import annotations

import json
import secrets
import urllib.parse

MAX_PREVIEW_REDEMPTION_BODY_BYTES = 32 * 1024
_MAX_SIGNED_VALUE_CHARS = 16 * 1024
PREVIEW_REDEMPTION_CONTENT_TYPE = "application/x-www-form-urlencoded"


def cross_site_iframe_headers(headers: dict[str, str]) -> bool:
    return (
        headers.get("sec-fetch-dest", "").strip().lower() == "iframe"
        and headers.get("sec-fetch-site", "").strip().lower() == "cross-site"
    )


def preview_redemption_content_type(value: str | None) -> bool:
    if value is None:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == PREVIEW_REDEMPTION_CONTENT_TYPE


def preview_navigation_document(
    target: str, *, clear_storage: bool = True
) -> tuple[bytes, dict[str, str]]:
    """Return a locked document that moves a redeemed POST to its exact target.

    Live-host redemption keeps the storage purge as a migration fence for the
    stable ``p2`` origin. Path previews use their fresh, dedicated ``p3s``
    origin and must not purge another active tab's origin storage.
    """

    nonce = secrets.token_urlsafe(18)
    safe_target = json.dumps(target).replace("<", "\\u003c").replace(">", "\\u003e")
    document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        "<title>Opening isolated preview</title></head><body>"
        f'<script nonce="{nonce}">'
        "if(window.parent!==window){window.parent.postMessage("
        '"disco-preview-bootstrap-ready","*")}'
        f"window.location.replace({safe_target})</script>"
        "<noscript>JavaScript is required to open this isolated preview.</noscript>"
        "</body></html>"
    ).encode()
    response_headers = {
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors *"
        ),
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }
    if clear_storage:
        # The stable live-host origin predates the service-worker request gate.
        # Keep this explicit migration fence until that origin is versioned.
        response_headers["clear-site-data"] = '"storage"'
    return document, response_headers


def preview_storage_reset_document(handoff: str) -> tuple[bytes, dict[str, str]]:
    """Clear a recycled origin before body-posting its one-use final handoff."""

    nonce = secrets.token_urlsafe(18)
    encoded_body = json.dumps(urllib.parse.urlencode({"handoff": handoff})).replace("<", "\\u003c")
    document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        "<title>Preparing isolated preview</title></head><body>"
        f'<script nonce="{nonce}">'
        "if(window.parent!==window){window.parent.postMessage("
        '"disco-preview-bootstrap-ready","*")} '
        f"fetch(location.pathname,{{method:'POST',credentials:'same-origin',cache:'no-store',"
        "headers:{'content-type':'application/x-www-form-urlencoded'},"
        f"body:{encoded_body}}})"
        ".then(response=>{if(!response.ok){throw new Error('preview reset failed')}"
        "return response.json()})"
        ".then(payload=>window.location.replace(payload.target))"
        ".catch(()=>{document.body.textContent='Preview isolation could not be established'})"
        "</script><noscript>JavaScript is required to open this isolated preview.</noscript>"
        "</body></html>"
    ).encode()
    return document, {
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors *"
        ),
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
        "clear-site-data": '"cache", "cookies", "storage"',
    }


def parse_preview_redemption(body: bytes) -> str | None:
    if not body or len(body) > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
        return None
    try:
        payload = urllib.parse.parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if set(payload) != {"intent"} or len(payload["intent"]) != 1:
        return None
    intent = payload["intent"][0]
    if not intent or len(intent) > _MAX_SIGNED_VALUE_CHARS:
        return None
    return intent


def parse_preview_storage_handoff(body: bytes) -> str | None:
    if not body or len(body) > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
        return None
    try:
        payload = urllib.parse.parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if set(payload) != {"handoff"} or len(payload["handoff"]) != 1:
        return None
    handoff = payload["handoff"][0]
    if not handoff or len(handoff) > _MAX_SIGNED_VALUE_CHARS:
        return None
    return handoff
