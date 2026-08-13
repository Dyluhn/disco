"""Host-owned delivery classification for the virtual ``serve`` handoff."""

from __future__ import annotations

import logging
import posixpath
import shlex
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Literal

from ..build_platform import FREEFORM_ARTIFACT_PROFILE_ID, FREEFORM_PROFILE_ID
from ..events import (
    ActionEvent,
    Event,
    current_build_platform_admission,
    latest_workspace_run_intent,
)

if TYPE_CHECKING:
    from .engine import AgentLoop


_LOG = logging.getLogger("disco.loop")


_HTML_ENTRY_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "title",
        "meta",
        "link",
        "style",
        "script",
        "main",
        "header",
        "footer",
        "nav",
        "section",
        "article",
        "div",
        "h1",
        "canvas",
    }
)


class _HTMLArtifactProbe(HTMLParser):
    """Bounded content probe; filenames and model assertions are not enough."""

    def __init__(self, *, html_suffix: bool) -> None:
        super().__init__(convert_charrefs=False)
        self._html_suffix = html_suffix
        self._prefix_clear = True
        self.found = False

    def handle_data(self, data: str) -> None:
        if not self.found and data.strip():
            self._prefix_clear = False

    def handle_decl(self, decl: str) -> None:
        if self._prefix_clear and decl.strip().lower().startswith("doctype html"):
            self.found = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if self._prefix_clear and (
            normalized in {"html", "head", "body"}
            or (self._html_suffix and normalized in _HTML_ENTRY_TAGS)
        ):
            self.found = True


def looks_like_html_entry(path: str, content: bytes | str) -> bool:
    """Recognize HTML document bytes, including BOM/comment-prefixed entries."""

    text = (
        content.decode("utf-8-sig", errors="ignore")
        if isinstance(content, bytes)
        else content.lstrip("\ufeff")
    )
    suffix = posixpath.splitext(posixpath.normpath(path))[1].lower()
    probe = _HTMLArtifactProbe(html_suffix=suffix in {".html", ".htm"})
    try:
        probe.feed(text)
    except Exception:  # noqa: BLE001 - malformed markup cannot promote the target
        return False
    return probe.found


async def html_entry_present(loop: AgentLoop, path: str) -> bool:
    """Recognize an actual HTML entry from bytes, never from a model label."""

    sandbox = getattr(loop.executor, "sandbox", None)
    if sandbox is None:
        return False
    candidates = (path, f"{path.rstrip('/')}/index.html")
    for candidate in candidates:
        try:
            if not await sandbox.file_exists(candidate):
                continue
            content = await sandbox.read_file(candidate)
        except Exception:  # noqa: BLE001 - unavailable evidence cannot promote to app
            continue
        if looks_like_html_entry(candidate, content[:65_536]):
            return True
    return False


async def serve_path_missing(loop: AgentLoop, path: str) -> bool:
    """Return true only when the sandbox positively proves the path absent."""

    sandbox = getattr(loop.executor, "sandbox", None)
    if sandbox is None:
        return False
    try:
        return not await sandbox.file_exists(path)
    except Exception:  # noqa: BLE001 - handoff is a soft guard
        return False


def normalize_serve_path(path: str) -> str:
    from disco.core.workspace_paths import strip_redundant_workspace_prefix

    return strip_redundant_workspace_prefix(path.strip())


def serve_path_is_workspace_root(path: str) -> bool:
    return path in {"", ".", "./"}


def serve_root_refusal() -> str:
    return (
        "serve refused: serve takes the entry FILE path, not the workspace root. "
        "For a site pass 'index.html' (or your entry file). If index.html exists "
        "at the root it will be served from there."
    )


async def serve_path_verified_present(loop: AgentLoop, path: str) -> bool:
    """Return true only when the sandbox positively confirms the path exists."""

    sandbox = getattr(loop.executor, "sandbox", None)
    if sandbox is None:
        return False
    try:
        return bool(await sandbox.file_exists(path))
    except Exception:  # noqa: BLE001 - unverifiable evidence cannot grant an exception
        return False


async def coerce_serve_entry_path(
    loop: AgentLoop, raw_path: str
) -> tuple[str, bool, bool]:
    """Return the normalized path, whether it was root-like, and whether it was coerced."""

    path = normalize_serve_path(raw_path)
    if not serve_path_is_workspace_root(path):
        return path, False, False
    if await serve_path_verified_present(loop, "index.html"):
        _LOG.info(
            "serve path %r points at the workspace root; auto-coerced to index.html",
            raw_path,
        )
        return "index.html", True, True
    return path, True, False


@dataclass(frozen=True)
class ServeRequest:
    title: str
    path: str
    kind: Literal["app", "files"]
    url: str


def serve_argument_defect(
    arguments: dict[str, object],
    *,
    title: str,
    raw_path: str,
    path: str,
) -> tuple[str, str] | None:
    if not arguments:
        return "no_arguments", ""
    if not title:
        return "title_missing", ""
    if not raw_path:
        return "path_missing", ""
    offered_kind = arguments.get("kind")
    if offered_kind is not None and str(offered_kind).strip() not in {"app", "files"}:
        return "kind_invalid", str(offered_kind).strip()
    if not path and not serve_path_is_workspace_root(normalize_serve_path(raw_path)):
        return "path_unresolvable", raw_path
    return None


def declared_delivery_defect(
    loop: AgentLoop, path: str, derived_kind: str
) -> tuple[str, str] | None:
    """Describe a conflict between host evidence and an already-governed shape."""

    declared_kind = getattr(loop, "_declared_delivery_kind", None)
    if declared_kind == "app" and derived_kind != "app":
        return (
            "declared_app_evidence_missing",
            (
                f"{path!r} is governed as an app, but it is neither an HTML entry "
                "nor the entry selected by the current managed app runtime"
            ),
        )
    if declared_kind == "files" and derived_kind != "files":
        return (
            "declared_files_contract_mismatch",
            f"{path!r} conflicts with the governed files delivery contract",
        )
    return None


def _governed_nonfreeform_delivery_kind(
    events: list[Event],
) -> Literal["app", "files"] | None:
    """Project an exact non-Freeform target contract without web assumptions."""

    admission = current_build_platform_admission(events)
    if admission is None or admission.profile_id in {
        FREEFORM_PROFILE_ID.canonical,
        FREEFORM_ARTIFACT_PROFILE_ID.canonical,
    }:
        return None
    contract = admission.verification_contract
    if contract is None:
        return None
    return "app" if contract.delivery.mode == "interactive" else "files"


def _normalized_workspace_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return "."
    normalized = posixpath.normpath(cwd)
    if normalized == "/workspace":
        return "."
    if normalized.startswith("/workspace/"):
        return normalized.removeprefix("/workspace/")
    return None if normalized.startswith("/") else normalized


def _workspace_relative_command_path(raw: str, cwd: str | None) -> str | None:
    value = raw.strip()
    if not value:
        return None
    normalized = posixpath.normpath(value)
    if normalized == "/workspace":
        normalized = "."
    elif normalized.startswith("/workspace/"):
        normalized = normalized.removeprefix("/workspace/")
    elif normalized.startswith("/"):
        return None
    normalized_cwd = _normalized_workspace_cwd(cwd)
    if normalized_cwd is None:
        return None
    return posixpath.normpath(posixpath.join(normalized_cwd, normalized))


def _command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./")):
        tokens.pop(0)
    return tokens


def _python_entry(tokens: list[str]) -> str | None:
    if "-m" not in tokens:
        return next((token for token in tokens[1:] if not token.startswith("-")), None)
    index = tokens.index("-m")
    if index + 1 >= len(tokens) or tokens[index + 1] == "http.server":
        return None
    return tokens[index + 1].replace(".", "/") + ".py"


def _module_server_entry(tokens: list[str]) -> str | None:
    module = next((token for token in tokens[1:] if not token.startswith("-")), None)
    return module.split(":", 1)[0].replace(".", "/") + ".py" if module else None


def preview_command_entry_path(arguments: dict[str, object]) -> str | None:
    """Return the executable artifact selected by a single preview command."""

    command = arguments.get("command")
    cwd = arguments.get("cwd")
    if not isinstance(command, str) or not command.strip():
        return None
    tokens = _command_tokens(command)
    if not tokens:
        return None
    executable = posixpath.basename(tokens[0]).lower()
    if executable.startswith("python"):
        candidate = _python_entry(tokens)
    elif executable in {"node", "bun", "deno", "ruby", "php"}:
        candidate = next((token for token in tokens[1:] if not token.startswith("-")), None)
    elif executable in {"uvicorn", "hypercorn", "gunicorn"}:
        candidate = _module_server_entry(tokens)
    else:
        candidate = tokens[0] if tokens[0].startswith(("./", "/workspace/")) else None
    return (
        _workspace_relative_command_path(candidate, cwd if isinstance(cwd, str) else None)
        if candidate is not None
        else None
    )


def current_preview_supports_app(events: list[Event], path: str) -> bool:
    """Use only a current custom runtime whose command selects this artifact."""

    intent = latest_workspace_run_intent(events)
    run_events = (
        [
            event
            for event in events
            if type(event.seq) is int
            and type(intent.seq) is int
            and event.seq > intent.seq
        ]
        if intent is not None and type(intent.seq) is int
        else events
    )
    if not run_events:
        return False
    from .finish.verify_gate_parts.host_claims import preview_selection_at

    through_seq = max((event.seq or 0 for event in run_events), default=0)
    selection = preview_selection_at(run_events, through_seq=through_seq)
    if selection is None or selection.launch_kind not in {"custom", "framework"}:
        return False
    source = next(
        (
            event
            for event in run_events
            if isinstance(event, ActionEvent) and event.id == selection.source_action_id
        ),
        None,
    )
    if source is None or source.tool_call.tool_name != "preview_start":
        return False
    entry = preview_command_entry_path(source.tool_call.arguments)
    artifact = _workspace_relative_command_path(path, None)
    return entry is not None and artifact is not None and entry == artifact


async def derived_serve_kind(
    loop: AgentLoop, path: str, events: list[Event]
) -> Literal["app", "files"]:
    """Derive delivery from the declared contract and current artifact evidence."""

    governed_kind = _governed_nonfreeform_delivery_kind(events)
    if governed_kind is not None:
        return governed_kind
    if getattr(loop, "_declared_delivery_kind", None) == "files":
        return "files"
    if await html_entry_present(loop, path):
        return "app"
    return "app" if current_preview_supports_app(events, path) else "files"


__all__ = [
    "ServeRequest",
    "coerce_serve_entry_path",
    "declared_delivery_defect",
    "derived_serve_kind",
    "looks_like_html_entry",
    "normalize_serve_path",
    "serve_argument_defect",
    "serve_path_is_workspace_root",
    "serve_path_missing",
    "serve_path_verified_present",
    "serve_root_refusal",
]
