"""Bounded browser evidence path policy extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from disco.core.workspace_paths import strip_redundant_workspace_prefix

from ..events import (
    NormalizationError,
    normalize_events,
)
from ._api_types import (
    _BROWSER_EVIDENCE_TOOLS,
    _WS_MANIFEST_MAX_BYTES,
    TERMINAL_STATES,
    BrowserEvidenceCollectionError,
)


def _file_entry(data: bytes) -> dict[str, Any]:
    """A single workspace-manifest entry: existence + identity (size, sha256) plus the
    decoded text content. The OutputTruthOracle reads ``content`` for must_contain
    substring checks; a binary / oversized file keeps present+size+sha256 with empty
    content (binary deliverables carry no text assertions)."""
    entry: dict[str, Any] = {
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) <= _WS_MANIFEST_MAX_BYTES:
        try:
            entry["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            entry["content"] = ""
            entry["binary"] = True
    else:
        entry["content"] = ""
        entry["truncated"] = True
    return entry


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except (ValueError, TypeError):
            return {}
    return p if isinstance(p, dict) else {}


def _current_browser_evidence_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        normalized = normalize_events(events)
    except NormalizationError as exc:
        raise BrowserEvidenceCollectionError(
            "browser screenshot references could not be read from the durable event log",
            {"error": str(exc)},
        ) from exc
    # A version restore starts a new workspace-evidence generation. The immutable
    # event log deliberately retains older browser receipts, but their `.pmx`
    # screenshot paths name bytes in the superseded workspace tree. Resolve only
    # references from the current generation against the current ProjectStore
    # snapshot. Using the restore *mutation* as the fence (rather than the later
    # success marker) also prevents pre-restore proof from surviving a partial or
    # failed restore.
    restore_fence = max(
        (
            event["seq"]
            for event in normalized
            if (
                (
                    event.get("kind") == "workspace_mutation"
                    and event.get("operation") == "version.restore"
                )
                or event.get("kind") == "workspace_restored"
            )
            and type(event.get("seq")) is int
        ),
        default=0,
    )
    if not restore_fence:
        return normalized
    return [
        event
        for event in normalized
        if type(event.get("seq")) is int and event["seq"] > restore_fence
    ]


def _nested_screenshot_paths(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, list):
        for child in value:
            found.update(_nested_screenshot_paths(child))
        return found
    if not isinstance(value, dict):
        return found
    for key, child in value.items():
        if key != "screenshot_path" and not key.endswith("_screenshot_path"):
            found.update(_nested_screenshot_paths(child))
            continue
        if child in (None, ""):
            continue
        if not isinstance(child, str):
            raise BrowserEvidenceCollectionError(
                "successful browser observation has a malformed screenshot path",
                {"field": key, "value_type": type(child).__name__},
            )
        found.add(validate_browser_evidence_relpath(child))
    return found


def _host_verdict_screenshot_path(
    event: dict[str, Any],
    *,
    required: bool,
) -> str | None:
    if event.get("verified") is not True or event.get("verdict") != "pass":
        return None
    screenshot_path = event.get("screenshot_path")
    if not isinstance(screenshot_path, str) or not screenshot_path:
        if not required:
            return None
        raise BrowserEvidenceCollectionError(
            "passing host verifier verdict has no admissible screenshot path",
            {
                "field": "screenshot_path",
                "value_type": type(screenshot_path).__name__,
            },
        )
    try:
        return validate_browser_evidence_relpath(screenshot_path)
    except BrowserEvidenceCollectionError:
        if required:
            raise
        return None


def _observation_screenshot_paths(event: dict[str, Any]) -> set[str]:
    if event.get("kind") != "observation":
        return set()
    result = event.get("tool_result")
    if (
        not isinstance(result, dict)
        or result.get("tool_name") not in _BROWSER_EVIDENCE_TOOLS
        or result.get("success") is not True
    ):
        return set()
    return _nested_screenshot_paths(result.get("structured"))


def _referenced_screenshot_paths(
    events: list[dict[str, Any]], *, require_verified_host_screenshot: bool = False
) -> list[str]:
    """Return sorted unique screenshot paths explicitly claimed by successful probes.

    ``verify_appkit_app`` can carry interaction screenshots in nested dictionaries,
    so keys named ``screenshot_path`` or ending in ``_screenshot_path`` are followed
    recursively. Other strings (including human-readable ``content`` and base64
    fields) are deliberately ignored.  A host ``verifier_verdict`` is a separate,
    host-owned proof path: only an exact verified/pass verdict is eligible, and such
    a verdict MUST name one admissible screenshot or collection fails closed.
    """
    found: set[str] = set()
    normalized = _current_browser_evidence_events(events)
    for event in normalized:
        if event.get("kind") == "verifier_verdict":
            screenshot_path = _host_verdict_screenshot_path(
                event,
                required=require_verified_host_screenshot,
            )
            if screenshot_path is not None:
                found.add(screenshot_path)
            continue
        found.update(_observation_screenshot_paths(event))
    return sorted(found)


def validate_browser_evidence_relpath(path: str) -> str:
    """Validate the product-owned screenshot namespace and exact POSIX spelling.

    Browser evidence retention must not become an arbitrary workspace-file copier:
    the product daemon owns only direct PNG children of ``.pmx/screenshots``.
    """
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) != 3
        or parts[:2] != [".pmx", "screenshots"]
        or not parts[2].endswith(".png")
    ):
        raise BrowserEvidenceCollectionError(
            "successful browser observation references a path outside the product "
            "screenshot namespace",
            {"path": path},
        )
    return path


def _jailed_browser_evidence_path(workspace: Path, rel: str, *, conversation_id: str) -> Path:
    """Resolve one validated screenshot path without permitting symlink escape."""
    validate_browser_evidence_relpath(rel)
    try:
        workspace = workspace.resolve(strict=True)
        resolved = (workspace / rel).resolve(strict=True)
    except OSError as exc:
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot is missing from the workspace snapshot",
            {
                "conversation_id": conversation_id,
                "path": rel,
                "error": type(exc).__name__,
            },
        ) from exc
    if not resolved.is_relative_to(workspace) or not resolved.is_file():
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot escapes the workspace snapshot",
            {"conversation_id": conversation_id, "path": rel},
        )
    return resolved


def _successful_observation_refs(events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Successful observation identifiers keyed both ways the event stream exposes them."""
    call_ids: set[str] = set()
    action_ids: set[str] = set()
    for e in events:
        if e.get("kind") != "observation":
            continue
        p = _payload(e)
        tr = p.get("tool_result") or {}
        if not isinstance(tr, dict) or not bool(tr.get("success", True)):
            continue
        call_id = tr.get("call_id")
        if call_id is not None:
            call_ids.add(str(call_id))
        action_id = p.get("action_id") or e.get("action_id")
        if action_id is not None:
            action_ids.add(str(action_id))
    return call_ids, action_ids


def _last_terminal(rows: list[dict[str, Any]]) -> str | None:
    result: str | None = None
    for r in rows:
        if r.get("kind") != "status":
            continue
        st = str(_payload(r).get("status"))
        if st in TERMINAL_STATES:
            result = st
        elif st == "RUNNING":
            result = None
    return result


def _norm_rel(path: str) -> str:
    """Canonical workspace-relative key using the product's workspace-prefix contract.

    The agent is explicitly taught that the guest root is ``/workspace``, so actions often
    spell ``/workspace/index.html`` while successful tool observations report the product-
    resolved ``index.html``.  These are the same jailed path.  Reuse the product's prefix
    rule, then retain the adapter's narrow legacy normalization.  Deliberately do NOT
    collapse dot segments here: a traversal-shaped action and a clean observation must
    disagree and fail closed instead of being joined by the evidence collector.
    """
    s = strip_redundant_workspace_prefix(str(path))
    if s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _needs_resolved_write_proof(path: str) -> bool:
    """Whether ``path`` relies on the guest-only workspace alias.

    A relative action path is already in the snapshot namespace, so historical event logs
    without structured mutator output can retain the action-content SHA fallback.  A
    ``workspace/``-prefixed path is ambiguous outside the product resolver; require the
    successful observation's canonical path and digest before calling it content-proven.
    """
    s = str(path)
    return s in {"workspace", "/workspace"} or s.startswith(("workspace/", "/workspace/"))


def _choose_alternative(
    options: list[dict[str, Any]], preferred_option_id: str | None
) -> str | None:
    """Pick an AlternativeOption id deterministically: the caller's `preferred_option_id`
    (scenario override) when it names a valid option, else a recommended option (a
    `recommended`/`recommendation` truthy flag, defensively — the option may not carry one),
    else the FIRST valid option. None when there is no option with a non-empty id."""
    valid = [o for o in options if isinstance(o, dict) and o.get("id")]
    if not valid:
        return None
    if preferred_option_id is not None:
        for o in valid:
            if str(o["id"]) == str(preferred_option_id):
                return str(o["id"])
    for o in valid:
        if o.get("recommended") or o.get("recommendation"):
            return str(o["id"])
    return str(valid[0]["id"])


# Snapshot acceptance proof levels — PROVEN/authoritative vs NON-authoritative — recorded
# per declared file so the OutputTruthOracle can proof-gate a content mismatch (a mismatch on
# a non-authoritative capture is unreliable and must NOT be a definitive product failure).
_PROOF_BY_EXPECTED_KIND = {
    "sha": "raw_sha",
    "rendered": "rendered_readback",
    "absent": "absent",
    "present_unproven": "unproven_extended_stability",
    "unknown": "unknown",
}


def _proof_level(expected: tuple[Any, ...] | None) -> str:
    """Map a declared file's `_agent_declared_expected` class to its acceptance proof level.
    A file with no event signal at all (`expected` None) is `unknown` (non-authoritative)."""
    kind = expected[0] if expected else "unknown"
    return _PROOF_BY_EXPECTED_KIND.get(kind, "unknown")


def _manifest_lookup(manifest: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The manifest entry for a declared path under either its exact key or its
    normalized relpath (the snapshot walk keys by the leading-slash-free relpath)."""
    entry = manifest.get(path)
    if entry is None:
        entry = manifest.get(_norm_rel(path))
    return entry if isinstance(entry, dict) else None


def _manifest_present(manifest: dict[str, Any], path: str) -> bool:
    return _manifest_lookup(manifest, path) is not None


def _manifest_sha(manifest: dict[str, Any], path: str) -> str | None:
    entry = _manifest_lookup(manifest, path)
    return entry.get("sha256") if entry else None


def _manifest_ident(manifest: dict[str, Any], path: str) -> tuple[Any, Any] | None:
    """A change-detection identity for content-stability: (size, sha256), or None when the
    file is absent (absence is itself a stable-able state)."""
    entry = _manifest_lookup(manifest, path)
    if entry is None:
        return None
    return (entry.get("size"), entry.get("sha256"))


_SHELL_META_TOKENS = frozenset({"&&", "||", ";", "|", "|&", "<", ">", ">>", "<<"})
_SHELL_META_SUBSTRINGS = ("&&", "||", ";", "|", "<", ">")
