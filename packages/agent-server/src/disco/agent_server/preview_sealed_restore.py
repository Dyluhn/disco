"""Immutable workspace and dependency mechanics for sealed Preview replay."""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from disco.core import DeliverableEvent

from .preview_manager import PreviewManager
from .preview_models import preview_requires_node_dependencies
from .preview_projection import (
    SealedPreviewRuntimeContract,
    derive_sealed_preview_runtime_contract,
)
from .workspace_commit import resolve_committed_workspace

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .build_loop_factory import BuildLoopFactory
    from .live_session_directory import LiveSessionDirectory
    from .project_runtime_service import ProjectRuntimeService
    from .workspace_service import WorkspaceCoordinator

_LOG = logging.getLogger(__name__)


def selected_finished_app_entry(events: list[Any], marker_seq: int) -> str | None:
    selected: str | None = None
    for event in events:
        if type(getattr(event, "seq", None)) is int and event.seq > marker_seq:
            break
        if isinstance(event, DeliverableEvent) and event.artifact_kind == "app":
            selected = event.path
    return selected


def sealed_runtime_contract(
    conversation_id: str,
    events: list[Any],
    committed: Any,
) -> SealedPreviewRuntimeContract | None:
    seal = committed.event.final_seal
    if seal is None or committed.event.seq is None:
        return None
    entry = selected_finished_app_entry(events, committed.event.seq)
    if entry is None:
        return None
    return derive_sealed_preview_runtime_contract(
        events,
        terminal_seq=seal.terminal_seq,
        conversation_id=conversation_id,
        version_seq=committed.event.version_seq,
        tree_digest=committed.event.tree_digest,
        app_entry=entry,
    )


def _source_workspace_relative_cwd(cwd: str, sandbox_instance_id: str | None) -> str | None:
    if not sandbox_instance_id:
        return None
    parts = tuple(part for part in cwd.split("/") if part)
    if sandbox_instance_id not in parts:
        return None
    sandbox_root = parts.index(sandbox_instance_id)
    return "/".join(parts[sandbox_root + 1 :])


def sealed_workspace_cwd(cwd: str | None, *, sandbox_instance_id: str | None = None) -> str:
    """Return a jailed workspace-relative cwd for dependency restoration."""

    if cwd is None or cwd in {"", ".", "/workspace", "/workspace/"}:
        return ""
    if cwd != cwd.strip() or "\x00" in cwd or "\\" in cwd:
        raise RuntimeError("sealed Preview cwd must be a clean POSIX workspace path")
    if cwd.startswith("/workspace/"):
        relative = cwd.removeprefix("/workspace/")
    elif cwd.startswith("/"):
        source_relative = _source_workspace_relative_cwd(cwd, sandbox_instance_id)
        if source_relative is None:
            raise RuntimeError("sealed Preview cwd must remain inside its source workspace")
        relative = source_relative
    else:
        relative = cwd
    parts = tuple(part for part in relative.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        if parts:
            raise RuntimeError("sealed Preview cwd must remain inside /workspace")
        return ""
    return "/".join(parts)


def sealed_path(cwd: str, name: str) -> str:
    return posixpath.join(cwd, name) if cwd else name


def _source_sandbox_instance_id(contract: Any) -> str | None:
    value = getattr(getattr(contract, "projection", None), "sandbox_instance_id", None)
    return value if isinstance(value, str) and value else None


def normalized_sealed_runtime_contract(
    contract: SealedPreviewRuntimeContract,
) -> SealedPreviewRuntimeContract:
    """Canonicalize every sealed launch cwd before any runtime dispatch."""

    relative_cwd = sealed_workspace_cwd(
        contract.cwd,
        sandbox_instance_id=_source_sandbox_instance_id(contract),
    )
    return replace(contract, cwd=f"./{relative_cwd}" if relative_cwd else None)


def _node_install_command(cwd: str, lock_name: str) -> str:
    cli_cwd = f"./{cwd}" if cwd else ""
    quoted = shlex.quote(cli_cwd)
    if lock_name in {"package-lock.json", "npm-shrinkwrap.json"}:
        prefix = f" --prefix {quoted}" if cli_cwd else ""
        return f"npm{prefix} ci --no-audit --no-fund"
    if lock_name == "pnpm-lock.yaml":
        directory = f" --dir {quoted}" if cli_cwd else ""
        return f"corepack pnpm{directory} install --frozen-lockfile"
    yarn_cwd = f" --cwd {quoted}" if cli_cwd else ""
    return f"corepack yarn{yarn_cwd} install --immutable"


async def _immutable_node_install(
    session: Any,
    cwd: str,
) -> tuple[str, str]:
    lock_names = (
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    )
    available = [
        sealed_path(cwd, name)
        for name in lock_names
        if await session.file_exists(sealed_path(cwd, name))
    ]
    if len(available) != 1:
        found = ", ".join(available) or "none"
        raise RuntimeError(
            "sealed Node Preview requires exactly one supported immutable lockfile "
            f"(found: {found})"
        )
    lock_path = available[0]
    return lock_path, _node_install_command(cwd, posixpath.basename(lock_path))


def _command_failure(result: Any, fallback: str, *, tail: bool = False) -> str:
    detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or fallback).strip()
    return detail[-1200:] if tail else detail[:500]


async def prepare_sealed_node_dependencies(
    session: Any,
    contract: SealedPreviewRuntimeContract,
) -> str | None:
    """Restore a Node runtime from an immutable, lockfile-owned graph."""

    if not preview_requires_node_dependencies(
        command=contract.command,
        framework=contract.framework,
    ):
        return None
    cwd = sealed_workspace_cwd(
        contract.cwd,
        sandbox_instance_id=_source_sandbox_instance_id(contract),
    )
    package_path = sealed_path(cwd, "package.json")
    if not await session.file_exists(package_path):
        return None
    try:
        package = json.loads((await session.read_file(package_path)).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("sealed Node Preview is missing a valid package.json") from exc
    if not isinstance(package, dict):
        raise RuntimeError("sealed Node Preview package.json must be an object")
    dependency_sections = (
        package.get("dependencies"),
        package.get("devDependencies"),
        package.get("optionalDependencies"),
    )
    if not any(isinstance(section, dict) and section for section in dependency_sections):
        return None
    lock_path, command = await _immutable_node_install(session, cwd)
    installed = await session.exec_shell(command, timeout_s=300)
    if getattr(installed, "exit_code", 1) != 0 or bool(getattr(installed, "timed_out", False)):
        detail = _command_failure(installed, "dependency installation failed", tail=True)
        raise RuntimeError(
            f"sealed Node Preview dependency restore from {lock_path} failed: {detail}"
        )
    dependency_dir = sealed_path(cwd, "node_modules")
    dependency_arg = shlex.quote(dependency_dir)
    probed = await session.exec_shell(
        f"test -d {dependency_arg} && test ! -L {dependency_arg}",
        timeout_s=30,
    )
    if getattr(probed, "exit_code", 1) != 0 or bool(
        getattr(probed, "timed_out", False)
    ):
        raise RuntimeError(
            "sealed Node Preview dependency restore did not produce node_modules; "
            "this package-manager layout is not yet supported for sealed replay"
        )
    return dependency_dir


def _dependency_stage(
    files: tuple[tuple[str, bytes], ...],
    dependency_dir: str,
    contract_id: str | None,
) -> tuple[str, str]:
    normalized = dependency_dir.strip("/")
    if not normalized or normalized.endswith("/.."):
        raise RuntimeError("sealed Preview dependency directory is invalid")
    prefix = normalized + "/"
    if any(path == normalized or path.startswith(prefix) for path, _data in files):
        raise RuntimeError("sealed workspace must not contain generated node_modules")
    if not contract_id:
        raise RuntimeError("sealed Preview dependency preservation requires authority")
    suffix = hashlib.sha256(contract_id.encode("utf-8")).hexdigest()[:24]
    staged = f".disco-preview-dependencies-{suffix}"
    staged_prefix = staged + "/"
    if any(path == staged or path.startswith(staged_prefix) for path, _data in files):
        raise RuntimeError("sealed workspace collides with dependency staging path")
    return normalized, staged


async def _clear_workspace(
    session: Any,
    *,
    dependency_dir: str | None,
    staged_dependency: str | None,
) -> None:
    if dependency_dir is None:
        command = "find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +"
        failure = "workspace clear failed"
    else:
        dep_arg = shlex.quote(dependency_dir)
        stage_arg = shlex.quote(staged_dependency or "")
        command = " && ".join(
            (
                f"test -d {dep_arg}",
                f"test ! -L {dep_arg}",
                f"test ! -e {stage_arg}",
                f"mv -- {dep_arg} {stage_arg}",
                f"find . -mindepth 1 -maxdepth 1 ! -name {stage_arg} -exec rm -rf -- {{}} +",
            )
        )
        failure = "dependency preservation failed"
    cleared = await session.exec_shell(command, timeout_s=30)
    if getattr(cleared, "exit_code", 1) != 0 or bool(getattr(cleared, "timed_out", False)):
        detail = _command_failure(cleared, failure)
        raise RuntimeError(f"sealed Preview {failure}: {detail}")


async def _restore_dependency(
    session: Any,
    dependency_dir: str,
    staged_dependency: str,
) -> None:
    parent_arg = shlex.quote(posixpath.dirname(dependency_dir) or ".")
    dep_arg = shlex.quote(dependency_dir)
    stage_arg = shlex.quote(staged_dependency)
    restored = await session.exec_shell(
        " && ".join(
            (
                f"mkdir -p -- {parent_arg}",
                f"test ! -e {dep_arg}",
                f"mv -- {stage_arg} {dep_arg}",
            )
        ),
        timeout_s=30,
    )
    if getattr(restored, "exit_code", 1) != 0 or bool(getattr(restored, "timed_out", False)):
        detail = _command_failure(restored, "dependency restore failed")
        raise RuntimeError(f"sealed Preview dependency restore failed: {detail}")


async def replace_with_sealed_workspace(
    session: Any,
    files: tuple[tuple[str, bytes], ...],
    *,
    dependency_dir: str | None = None,
    contract_id: str | None = None,
) -> None:
    """Replace the mutable workspace with exact sealed bytes."""

    normalized_dependency: str | None = None
    staged_dependency: str | None = None
    if dependency_dir is not None:
        normalized_dependency, staged_dependency = _dependency_stage(
            files, dependency_dir, contract_id
        )
    await _clear_workspace(
        session,
        dependency_dir=normalized_dependency,
        staged_dependency=staged_dependency,
    )
    for path, data in files:
        await session.write_file(path, data)
    if normalized_dependency is not None and staged_dependency is not None:
        await _restore_dependency(session, normalized_dependency, staged_dependency)
    for path, data in files:
        if await session.read_file(path) != data:
            raise RuntimeError(f"sealed Preview read-back changed: {path}")


def restore_succeeded(restored: Any) -> bool:
    return getattr(getattr(restored, "status", None), "value", None) in {
        "running",
        "unavailable",
    }


class SealedPreviewRestorer:
    """Materialize and launch immutable bytes in a Preview-owned sandbox."""

    def __init__(
        self,
        live_sessions: LiveSessionDirectory,
        store: SqliteEventStore,
        projects: ProjectRuntimeService,
        loop_factory: BuildLoopFactory,
        workspace: WorkspaceCoordinator,
    ) -> None:
        self._live_sessions = live_sessions
        self._store = store
        self._projects = projects
        self._loop_factory = loop_factory
        self._workspace = workspace

    def _verified_files(
        self,
        conversation_id: str,
        version_seq: int,
    ) -> tuple[tuple[str, bytes], ...]:
        store = self._projects.current_project_store()
        with store.open_verified_version(conversation_id, version_seq) as verified:
            return tuple((entry.path, verified.read_bytes(entry.path)) for entry in verified.files)

    async def _launch(
        self,
        conversation_id: str,
        host_session: Any,
        contract: SealedPreviewRuntimeContract,
        files: tuple[tuple[str, bytes], ...],
    ) -> bool:
        preview_session: Any | None = None
        manager: PreviewManager | None = None
        try:
            previous = getattr(host_session, "_preview_manager", None)
            if previous is not None:
                await previous.aclose()
                if getattr(host_session, "_preview_manager", None) is previous:
                    host_session._preview_manager = None
            preview_session = self._loop_factory.create_preview_session(conversation_id)
            await replace_with_sealed_workspace(preview_session, files)
            launch_contract = normalized_sealed_runtime_contract(contract)
            dependency_dir = await prepare_sealed_node_dependencies(
                preview_session,
                launch_contract,
            )
            if dependency_dir is not None:
                await replace_with_sealed_workspace(
                    preview_session,
                    files,
                    dependency_dir=dependency_dir,
                    contract_id=contract.contract_id,
                )
            manager = PreviewManager(preview_session, owns_sandbox=True)
            host_session._preview_manager = manager
            restored = await manager.restore_sealed(launch_contract)
            if restore_succeeded(restored):
                return True
        except Exception:  # noqa: BLE001 - unavailable preview is not product failure
            _LOG.warning(
                "isolated sealed Preview restore failed for %s",
                conversation_id,
                exc_info=True,
            )
        if manager is not None:
            if getattr(host_session, "_preview_manager", None) is manager:
                host_session._preview_manager = None
            await manager.aclose()
        elif preview_session is not None:
            await preview_session.destroy()
        return False

    async def restore(
        self,
        conversation_id: str,
        events: list[Any],
        committed: Any,
        contract: SealedPreviewRuntimeContract,
    ) -> bool:
        """Recheck authority, then launch without touching the build workspace."""

        async with self._workspace.lock(conversation_id):
            files = self._verified_files(conversation_id, committed.event.version_seq)
            host_session = self._live_sessions.live_session(conversation_id)
            if host_session is None:
                self._loop_factory.loop_for(conversation_id)
                host_session = self._live_sessions.live_session(conversation_id)
            if host_session is None:
                return False
            current_events = await self._store.get_events(conversation_id)
            current_store = self._projects.current_project_store()
            current_committed = resolve_committed_workspace(
                current_events,
                current_store,
                conversation_id,
            )
            current_contract = sealed_runtime_contract(
                conversation_id,
                current_events,
                current_committed,
            )
            if current_contract is None or current_contract.contract_id != contract.contract_id:
                return False
            return await self._launch(
                conversation_id,
                host_session,
                current_contract,
                files,
            )
