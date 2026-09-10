"""Typed project-import parsing and workspace materialization."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import posixpath
import re
import shutil
import socket
import tempfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import urlsplit

from disco.core import EventSource, LLMMessage, MessageEvent
from disco.core.host_egress import EgressDenied, validate_untrusted_url
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import ProjectStore, is_runtime_secret_path
from starlette.datastructures import UploadFile
from starlette.requests import Request

from ..auth import require_admin_session
from ..runtime import ConversationRuntime
from ..title_service import fallback_title

_GIT_CLONE_TIMEOUT_S = 120
CloneGit = Callable[[str, Path], Awaitable[None]]

# `git_url` is caller-supplied and NOT admin-gated (see `_json_source`), so the
# only transports it may name are the ones that cannot reach the host itself.
# `https` is the whole allowlist:
#   * `ext::` runs a SHELL COMMAND (`ext::sh -c '…'`) — remote code execution,
#     unaffected by `--depth 1`.
#   * `file://` / a bare path clones the host filesystem into the workspace.
#   * `ssh://` + scp-style `git@host:path` would authenticate as the SERVER's
#     unix user against arbitrary internal hosts. Nothing in the product
#     configures git credentials or known_hosts (the import dialog's field is
#     `<input type="url">` with an https placeholder), so ssh import could only
#     ever succeed by borrowing host key material — a privilege escalation, not
#     a feature. Add it only together with a per-owner credential surface.
#   * plain `http://` is omitted deliberately: it is the transport every cloud
#     metadata endpoint speaks, and dropping it costs nothing a self-host needs.
_ALLOWED_GIT_SCHEMES = ("https",)
# Defence in depth for (b): even if the parse above were bypassed, git itself
# refuses every transport outside this list.
_GIT_ALLOW_PROTOCOL = ":".join(_ALLOWED_GIT_SCHEMES)
# Mirrors `tools/mcp/stdio.py::_SAFE_PASSTHROUGH`: the subprocess boundary is not
# a trust boundary, so DISCO_SECRET_KEY and every *_API_KEY stay behind.
_GIT_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SystemRoot")
_GIT_FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_HOST_PATH_RE = re.compile(r"/(?:home|root|Users|tmp|var|etc|opt|srv|private)(?:/[^\s'\"]*)?")


@dataclass(frozen=True)
class ImportLimits:
    zip_bytes: int
    tree_bytes: int
    files: int


@dataclass(frozen=True)
class _ImportFile:
    src: Path
    rel: str
    size: int


@dataclass(frozen=True)
class ImportStats:
    files: int
    bytes: int
    largest_path: str | None
    largest_bytes: int


@dataclass(frozen=True)
class ImportSource:
    kind: str
    label: str
    title_seed: str
    root: Path | None = None
    zip_bytes: bytes | None = None
    skip_git_dir: bool = False


class ImportRejected(ValueError):
    def __init__(self, status_code: int, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.status_code = status_code
        self.reason = reason
        self.message = message or reason


def _reject(status_code: int, reason: str, message: str | None = None) -> NoReturn:
    raise ImportRejected(status_code, reason, message)


def _cap_check(files: int, total_bytes: int, limits: ImportLimits) -> None:
    if files > limits.files:
        _reject(413, "too_many_files", f"import has {files} files; max is {limits.files}")
    if total_bytes > limits.tree_bytes:
        _reject(
            413,
            "tree_too_large",
            f"import has {total_bytes} bytes; max is {limits.tree_bytes}",
        )


def _safe_zip_rel(raw_name: str) -> str:
    raw = raw_name.replace("\\", "/")
    norm = posixpath.normpath(raw)
    parts = PurePosixPath(norm).parts
    if (
        norm in {"", ".", ".."}
        or posixpath.isabs(norm)
        or norm.startswith("../")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        _reject(400, "zip_slip", f"zip entry escapes the project root: {raw_name!r}")
    if is_runtime_secret_path(norm):
        _reject(
            400,
            "runtime_secret_file",
            f"import contains forbidden runtime secret path: {raw_name!r}",
        )
    return norm


def _repo_name_from_url(git_url: str) -> str:
    trimmed = git_url.rstrip("/").split("/")[-1] or "imported project"
    if trimmed.endswith(".git"):
        trimmed = trimmed[:-4]
    return trimmed or "imported project"


def _scan_import_tree(
    src: Path,
    *,
    skip_git_dir: bool,
    limits: ImportLimits,
) -> tuple[list[_ImportFile], ImportStats]:
    root = src.resolve()
    files: list[_ImportFile] = []
    total_bytes = 0
    largest_path: str | None = None
    largest_bytes = 0
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if is_runtime_secret_path(rel):
            _reject(
                400,
                "runtime_secret_file",
                f"import contains forbidden runtime secret path: {rel!r}",
            )
        if skip_git_dir and ".git" in PurePosixPath(rel).parts:
            continue
        size = path.stat().st_size
        files.append(_ImportFile(src=path, rel=rel, size=size))
        total_bytes += size
        if size > largest_bytes:
            largest_path, largest_bytes = rel, size
        _cap_check(len(files), total_bytes, limits)
    if not files:
        _reject(400, "no_files", "import source did not contain any files")
    return files, ImportStats(len(files), total_bytes, largest_path, largest_bytes)


def _copy_scanned_tree(files: list[_ImportFile], workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for item in files:
        dest = workspace / item.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.src, dest)


def _zip_entries(
    archive: zipfile.ZipFile,
    limits: ImportLimits,
) -> tuple[list[tuple[zipfile.ZipInfo, str]], str | None, int]:
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    total_bytes = 0
    largest_path: str | None = None
    largest_bytes = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        rel = _safe_zip_rel(info.filename)
        total_bytes += int(info.file_size)
        entries.append((info, rel))
        if info.file_size > largest_bytes:
            largest_path, largest_bytes = rel, int(info.file_size)
        _cap_check(len(entries), total_bytes, limits)
    if not entries:
        _reject(400, "no_files", "zip did not contain any files")
    return entries, largest_path, largest_bytes


def _extract_zip_entries(
    archive: zipfile.ZipFile,
    entries: list[tuple[zipfile.ZipInfo, str]],
    workspace: Path,
    limits: ImportLimits,
) -> int:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    root = workspace.resolve()
    actual_total = 0
    for info, rel in entries:
        data = archive.read(info)
        actual_total += len(data)
        _cap_check(len(entries), actual_total, limits)
        dest = workspace / rel
        if not dest.resolve().is_relative_to(root):
            _reject(400, "zip_slip", f"zip entry escapes the project root: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return actual_total


def _materialize_zip(
    zip_bytes: bytes,
    workspace: Path,
    limits: ImportLimits,
) -> ImportStats:
    if len(zip_bytes) > limits.zip_bytes:
        _reject(
            413,
            "zip_too_large",
            f"zip upload has {len(zip_bytes)} bytes; max is {limits.zip_bytes}",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        _reject(400, "invalid_zip", str(exc))
    with archive:
        entries, largest_path, largest_bytes = _zip_entries(archive, limits)
        total = _extract_zip_entries(archive, entries, workspace, limits)
    return ImportStats(len(entries), total, largest_path, largest_bytes)


def _validated_git_url(raw: str) -> str:
    """The caller-supplied clone URL, or ``ImportRejected`` before any use of it.

    Allowlist, never denylist: exactly the schemes in `_ALLOWED_GIT_SCHEMES` are
    accepted and everything else — `ext::` (shell execution), `file://`, a bare
    path, scp-style `git@host:path` (no scheme at all) — is rejected by naming
    what IS permitted.
    """
    url = raw.strip()
    if not url:
        _reject(400, "git_url_invalid", "git_url must not be empty")
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        _reject(400, "git_url_invalid", "git_url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        _reject(400, "git_url_invalid", f"git_url is not a valid URL: {exc}")
    allowed = ", ".join(f"{scheme}://" for scheme in _ALLOWED_GIT_SCHEMES)
    if parsed.scheme.lower() not in _ALLOWED_GIT_SCHEMES:
        _reject(
            400,
            "git_url_scheme",
            f"git_url must start with one of: {allowed} (got {parsed.scheme.lower() or 'no'} "
            "scheme)",
        )
    if not parsed.hostname:
        _reject(400, "git_url_invalid", "git_url must name a host")
    if parsed.username or parsed.password:
        # The URL is echoed back to the caller and into the seed message.
        _reject(400, "git_url_credentials", "git_url must not embed credentials")
    return url


async def _require_public_git_host(git_url: str) -> None:
    """Route the clone target through the SAME host-egress policy every other
    untrusted URL uses, so a `git_url` cannot be used as an SSRF probe against
    loopback/private/link-local addresses (169.254.169.254 and friends).

    A host that does not resolve is NOT a policy denial — git will fail on its
    own — so the DNS-failure case is allowed through unchanged. That also keeps
    the check inert on an offline test host.
    """
    try:
        await asyncio.to_thread(validate_untrusted_url, git_url)
    except EgressDenied as exc:
        if isinstance(exc.__cause__, socket.gaierror):
            return
        _reject(400, "git_url_blocked", "git_url does not resolve to a public address")


def _git_clone_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _GIT_ENV_PASSTHROUGH if key in os.environ}
    env.setdefault("PATH", _GIT_FALLBACK_PATH)
    env["GIT_ALLOW_PROTOCOL"] = _GIT_ALLOW_PROTOCOL
    env["GIT_TERMINAL_PROMPT"] = "0"
    # HOME is withheld and both config layers are pinned to /dev/null: a
    # `url.<base>.insteadOf` line in the host's gitconfig could otherwise rewrite
    # a validated https URL into `ext::` after our parse has run.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _sanitized_git_error(raw: str, dest: Path) -> str:
    text = raw
    for host_path in (str(dest), str(dest.parent)):
        if host_path:
            text = text.replace(host_path, "<import-dir>")
    return _HOST_PATH_RE.sub("<path>", text)[:1000]


async def clone_git_url(git_url: str, dest: Path) -> None:
    url = _validated_git_url(git_url)
    await _require_public_git_host(url)
    git = shutil.which("git")
    if git is None:
        _reject(400, "git_missing", "git is not installed on the agent-server host")
    try:
        proc = await asyncio.create_subprocess_exec(
            git,
            "clone",
            "--depth",
            "1",
            "--",
            url,
            str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=_git_clone_env(),
        )
    except OSError as exc:
        _reject(400, "git_clone_failed", f"failed to launch git: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GIT_CLONE_TIMEOUT_S)
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise ImportRejected(400, "git_clone_failed", "git clone timed out") from exc
    if proc.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip() or "git clone failed"
        _reject(400, "git_clone_failed", _sanitized_git_error(detail, dest))


async def _multipart_source(request: Request) -> ImportSource:
    form = await request.form()
    try:
        uploads = [value for _key, value in form.multi_items() if isinstance(value, UploadFile)]
        if len(uploads) != 1:
            _reject(400, "invalid_request", "multipart import requires exactly one zip file")
        upload = uploads[0]
        filename = upload.filename or "project.zip"
        if not filename.lower().endswith(".zip"):
            _reject(400, "invalid_zip", "uploaded project must be a .zip file")
        return ImportSource(
            kind="zip",
            label=filename,
            title_seed=Path(filename).stem or filename,
            zip_bytes=await upload.read(),
        )
    finally:
        await form.close()


def _path_source(request: Request, path_value: str) -> ImportSource:
    require_admin_session(request)
    root = Path(path_value).expanduser()
    if not root.exists():
        _reject(400, "path_not_found", f"path does not exist: {path_value}")
    if not root.is_dir():
        _reject(400, "path_not_directory", f"path is not a directory: {path_value}")
    return ImportSource(kind="path", label=str(root), title_seed=root.name or str(root), root=root)


async def _json_source(request: Request) -> ImportSource:
    try:
        body: Any = await request.json()
    except Exception as exc:  # noqa: BLE001
        _reject(400, "invalid_request", f"request body must be JSON or multipart: {exc}")
    if not isinstance(body, dict):
        _reject(400, "invalid_request", "JSON body must be an object")
    path_value = body.get("path")
    git_url_value = body.get("git_url")
    supplied = [
        value for value in (path_value, git_url_value) if isinstance(value, str) and value.strip()
    ]
    if len(supplied) != 1:
        _reject(400, "invalid_request", "supply exactly one of path or git_url")
    if isinstance(path_value, str) and path_value.strip():
        return _path_source(request, path_value)
    assert isinstance(git_url_value, str)
    # Validate at PARSE time, before a workspace is created and before any
    # subprocess can be spawned; `clone_git_url` re-validates for callers that
    # reach it directly.
    git_url = _validated_git_url(git_url_value)
    return ImportSource(
        kind="git",
        label=git_url,
        title_seed=_repo_name_from_url(git_url),
        skip_git_dir=True,
    )


async def parse_import_source(request: Request) -> ImportSource:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        return await _multipart_source(request)
    return await _json_source(request)


async def _materialize_source(
    source: ImportSource,
    workspace: Path,
    limits: ImportLimits,
    clone_git: CloneGit,
) -> ImportStats:
    if source.kind == "zip":
        assert source.zip_bytes is not None
        return _materialize_zip(source.zip_bytes, workspace, limits)
    if source.kind == "git":
        with tempfile.TemporaryDirectory(prefix="disco-import-") as tmp:
            clone_root = Path(tmp) / "repo"
            await clone_git(source.label, clone_root)
            files, stats = _scan_import_tree(
                clone_root,
                skip_git_dir=True,
                limits=limits,
            )
            _copy_scanned_tree(files, workspace)
            return stats
    assert source.root is not None
    files, stats = _scan_import_tree(
        source.root,
        skip_git_dir=source.skip_git_dir,
        limits=limits,
    )
    _copy_scanned_tree(files, workspace)
    return stats


async def _created_at_for(
    store: SqliteEventStore,
    conversation_id: str,
    owner_id: str,
) -> str | None:
    with contextlib.suppress(Exception):
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id,
            limit=500,
            cursor=None,
        )
        row = next(
            (summary for summary in summaries if summary.conversation_id == conversation_id),
            None,
        )
        if row is not None:
            return row.created_at
    return None


def _import_message(stats: ImportStats, source_label: str) -> str:
    largest = (
        f"{stats.largest_path} ({stats.largest_bytes:,} bytes)"
        if stats.largest_path is not None
        else "n/a"
    )
    return (
        f"Imported {stats.files} files from {source_label}; "
        f"largest: {largest}. The workspace is already pre-populated."
    )


async def import_project(
    request: Request,
    *,
    owner_id: str,
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    project_store: ProjectStore,
    limits: ImportLimits,
    clone_git: CloneGit,
) -> dict[str, Any]:
    source = await parse_import_source(request)
    title = fallback_title(source.title_seed) or "Imported project"
    conversation_id = f"conv_{uuid.uuid4().hex}"
    workspace = project_store.path_for(conversation_id)
    try:
        stats = await _materialize_source(source, workspace, limits, clone_git)
    except ImportRejected:
        if workspace.exists():
            shutil.rmtree(workspace)
        raise
    except Exception as exc:
        if workspace.exists():
            shutil.rmtree(workspace)
        raise ImportRejected(500, "import_failed", str(exc)) from exc

    store.create_conversation(
        conversation_id,
        owner_id=owner_id,
        title=title,
        surface="build",
    )
    runtime.settings._set_surface(conversation_id, "build")
    created_at = await _created_at_for(store, conversation_id, owner_id)
    project_store.write_manifest(
        conversation_id,
        title=title,
        owner_id=owner_id,
        created_at=created_at,
        file_count=stats.files,
        total_bytes=stats.bytes,
        imported=True,
    )
    with contextlib.suppress(Exception):
        project_store.cut_version(conversation_id, trigger="import")
    await store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=_import_message(stats, source.label)),
        ),
    )
    return {
        "conversation_id": conversation_id,
        "files": stats.files,
        "bytes": stats.bytes,
        "title": title,
    }
