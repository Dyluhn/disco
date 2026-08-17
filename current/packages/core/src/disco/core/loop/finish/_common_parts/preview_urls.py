"""Deliverable-path safety and preview-URL/port targeting helpers. Extracted
verbatim from `finish/common.py` (module logical LOC reduction); pure,
stdlib + imported types only.
"""

from __future__ import annotations

import posixpath

from ...preview_target import PREVIEW_PORTS


def _safe_deliverable_file_path(path: str, *, app_root: bool = False) -> str | None:
    raw = (path or "").strip()
    if not raw:
        return None
    # File/shell tools publicly accept canonical /workspace-rooted paths. Strip
    # only that exact capability root, then apply the same traversal jail as a
    # relative path. Other absolute paths remain forbidden.
    if raw == "/workspace":
        raw = "."
    elif raw.startswith("/workspace/"):
        raw = raw.removeprefix("/workspace/")
    norm = posixpath.normpath(raw)
    if norm in ("", "."):
        return "index.html" if app_root else None
    if norm.startswith("/") or norm == ".." or norm.startswith("../"):
        return None
    if app_root:
        base = norm.rstrip("/")
        if posixpath.basename(base) == "index.html":
            return base
        return posixpath.normpath(posixpath.join(base, "index.html"))
    return norm


def _url_targets_preview(url: str, target_key: tuple[str, int] | None) -> bool:
    """Does a browser observation `url` address the CURRENT preview target?

    The preview platform assigns a RANDOM port — there is NO fixed :8000 inside the
    sandbox. When the live preview port has been resolved (`target_key`, derived from
    `_detect_preview_url` via `_preview_key` — backend-aware: never the agent-server's
    :8000 on the shared-host/process backend) an observation counts ONLY if its url
    resolves to the SAME (host, port) preview key — a foreign / wrong-port observation
    does NOT satisfy the browser gate.

    When the preview is undetectable (`target_key is None` — sandbox-less / legacy
    backend / the pure-reader unit tests) the historical :8000 acceptance is kept as a
    safe fallback rather than asserting a wrong port (on an ISOLATED backend :8000 IS
    the app; the resolver returns it as the target_key there, so this also matches)."""
    if target_key is None:
        return url.startswith("http://127.0.0.1:8000") or url.startswith("http://localhost:8000")
    return _preview_key(url) == target_key


# Preview ports the user-visible deliverable may serve on (ordered by preference;
# 8000 is Disco's canonical user-visible port INSIDE an isolated sandbox). SINGLE
# SOURCE OF TRUTH now lives in `preview_target.PREVIEW_PORTS` alongside the
# backend-aware resolver; re-exported here under the historical name so the
# `verify_web_app` tool's existing `from ...finish import _PREVIEW_PORTS` keeps
# working (the gate's preview detection and the tool's auto-detect share BOTH the
# port set AND the resolver).
_PREVIEW_PORTS: tuple[int, ...] = PREVIEW_PORTS


def _preview_key(url: str) -> tuple[str, int] | None:
    """Normalize a preview URL to a comparable (host, port) key. Loopback aliases
    (localhost / 127.0.0.1 / 0.0.0.0 / ::1 / empty host) collapse to one host so a
    verdict on http://localhost:8000/ matches a target of http://127.0.0.1:8000/.
    Returns None for an empty / unparseable URL (a verdict with no usable url can
    never bind to a target)."""
    from urllib.parse import urlsplit

    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "http://" + u
    try:
        parts = urlsplit(u)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ""):
        host = "127.0.0.1"
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (host, port)
