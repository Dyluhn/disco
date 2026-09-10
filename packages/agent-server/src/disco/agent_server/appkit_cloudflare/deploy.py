"""AppKit EPIC O — the Cloudflare deploy planner + HARD-GATED executor.

This is the ONE place AppKit makes a REAL outward change (deploys a generated app
to a live Cloudflare account). The capability is built here, but a real mutation
can fire ONLY through ``execute_deploy`` with ALL of:

  1. ``cloudflare_export_ready`` (Epic I) PASS — re-checked FRESH at execute time;
  2. a CONNECTED account — an API token decryptable from the encrypted SecretStore;
  3. EXPLICIT owner confirmation — the exact phrase bound to the freshly-computed
     plan_hash (so a stale plan, or any digest drift, refuses);
  4. NOT an autonomous run — like ``request_custom_build``, refused outright when
     no human is present.

DRY-RUN IS THE DEFAULT. ``execute_deploy(dry_run=True)`` builds the plan and
returns it with ZERO side effects — the command runner is never called. The real
deploy is the owner's explicit, documented action; this module never runs it in
CI (tests drive a FAKE runner; the production path is owner-executed, Epic
M-style live run).

SECRET HANDLING: the API token + ADMIN_TOKEN live only in the encrypted
SecretStore. They are injected into the wrangler subprocess via ENV / stdin —
never argv, never a plan field, never a log. Every captured stdout/stderr is run
through ``redact_text`` before it enters a transcript or the persisted
deployment record. This module is leaf-pure of ``core``: deploy execution lives
in agent-server, ``disco.core`` stays a leaf (it only lends the pure
``cloudflare_export_ready`` model + the SecretStore).

This module is the public compatibility/export facade over
:mod:`disco.agent_server.appkit_cloudflare.deploy_parts`, which holds the
cohesive private implementation of the planner + executor so every public
symbol keeps its import path (``disco.agent_server.appkit_cloudflare.deploy.execute_deploy``
and friends).

NOT fully state-free, by necessity: the workspace guarded reader/writer choke
points (``read_workspace_file``, ``write_workspace_file``,
``_open_workspace_write_parent``, ``ensure_workspace_dir``, ``_stage_deploy_tree``,
``_deploy_lock_dir``, ``_deploy_stage_root``) are defined HERE, inline, rather than
in a ``deploy_parts`` sibling. A pre-existing regression suite
(``test_no_raw_workspace_reads_outside_guarded_reader`` /
``test_no_raw_workspace_writes_outside_guarded_writer``) asserts, via
``inspect.getsource`` on THIS MODULE OBJECT, that every raw
``read_text``/``read_bytes``/``write_text``/``write_bytes``/``mkdir`` call in the
package lives textually inside those choke-point functions' own source —
something ``inspect.getsource`` can only see when the implementation is
physically part of this file, not merely re-exported from a sibling. Each of
those functions' internal decomposition (needed to keep their own McCabe score
under budget) is nested as CLOSURES rather than top-level siblings, for the same
reason: a nested ``def`` is part of its enclosing function's source span (so the
regression suite still finds the raw calls) while NOT counting toward the
enclosing function's own cyclomatic complexity (the architecture scanner does
not descend into nested callables when scoring the enclosing one — each nested
closure is itself scored separately, and is small enough not to trip any cap).

Every other private helper lives in ``deploy_parts`` and is re-imported below
unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os as os
import secrets as _secrets
import shutil as shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, overload

from disco.core.appkit.spec import load_app_spec_from_bytes
from disco.core.llm.secrets import SecretStore as SecretStore

from ..workspace_process_fence import workspace_process_lock_dir
from .deploy_parts._bindings import _activate_stripe_worker as _activate_stripe_worker
from .deploy_parts._constants import (
    _APPSPEC_RELPATH,
    _DEPLOY_STAGE_DIR_ENV,
    _TREE_SKIP_DIRS,
    _TREE_SKIP_FILES,
)
from .deploy_parts._constants import _D1_ID_PLACEHOLDER as _D1_ID_PLACEHOLDER
from .deploy_parts._constants import _DENY_DEPLOY_FILE_RE as _DENY_DEPLOY_FILE_RE
from .deploy_parts._constants import (
    CF_ACCOUNT_SECRET as CF_ACCOUNT_SECRET,
)
from .deploy_parts._constants import (
    CF_TOKEN_SECRET as CF_TOKEN_SECRET,
)
from .deploy_parts._constants import (
    DeployMutationHook as DeployMutationHook,
)
from .deploy_parts._deploy_record import _new_record_rel as _new_record_rel
from .deploy_parts._deploy_record import _persist_record as _persist_record
from .deploy_parts._execute import execute_deploy as execute_deploy
from .deploy_parts._execute_phases import _deploy_env as _deploy_env
from .deploy_parts._execute_phases import _resolve_trusted_wrangler as _resolve_trusted_wrangler
from .deploy_parts._guards import (
    _assert_no_ancestor_wrangler_config as _assert_no_ancestor_wrangler_config,
)
from .deploy_parts._guards import _assert_no_plaintext_secrets as _assert_no_plaintext_secrets
from .deploy_parts._guards import (
    _assert_no_secret_shaped_files as _assert_no_secret_shaped_files,
)
from .deploy_parts._guards import _assert_wrangler_allowlisted as _assert_wrangler_allowlisted
from .deploy_parts._locks import _deploy_lock_path as _deploy_lock_path
from .deploy_parts._ownership import _has_ownership_record as _has_ownership_record
from .deploy_parts._ownership import _ownership_signature as _ownership_signature
from .deploy_parts._plan import _validate_resource_names as _validate_resource_names
from .deploy_parts._plan import build_plan as build_plan
from .deploy_parts._plan import confirmation_phrase as confirmation_phrase
from .deploy_parts._plan import connect_account as connect_account
from .deploy_parts._plan import connection_status as connection_status
from .deploy_parts._plan import disconnect_account as disconnect_account
from .deploy_parts._staging import _deploy_asset_dir_rel as _deploy_asset_dir_rel
from .deploy_parts._staging import _iter_tree_files as _iter_tree_files
from .deploy_parts._staging import _spec_digest as _spec_digest
from .deploy_parts._staging import (
    assert_deploy_tree_symlink_free as assert_deploy_tree_symlink_free,
)
from .deploy_parts._worker import _assert_stripe_trusted_tree as _assert_stripe_trusted_tree
from .deploy_parts._worker import _assert_worker_auth_verified as _assert_worker_auth_verified
from .deploy_parts._worker import _assert_worker_is_canonical as _assert_worker_is_canonical
from .deploy_parts._workspace import _guarded_write_target
from .deploy_parts._workspace import workspace_contained as workspace_contained
from .deploy_parts._wrangler_output import (
    _preflight_signals_worker_absent as _preflight_signals_worker_absent,
)
from .deploy_parts._wrangler_output import _substitute_database_id as _substitute_database_id
from .models import DeployExecutionResult as DeployExecutionResult
from .models import DeployRefused as DeployRefused
from .models import RefusalReason as RefusalReason

try:  # fcntl is POSIX-only; a non-POSIX host degrades to the in-process lock (logged).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    _fcntl = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

# ---- compatibility re-exports --------------------------------------------------
#
# A handful of pre-existing focused test suites reach past the public API into
# specific deploy internals (the SEC-* guards, the ownership/staging/worker
# helpers) or monkeypatch a handful of names at THIS module's attribute:
# ``build_plan``, ``execute_deploy``, ``_deploy_asset_dir_rel``, and ``_fcntl``.
# Every ``deploy_parts`` call site for those four resolves them through THIS
# module's own binding at call time (never a module-local import), so a patch
# here is actually observed — see each part module's docstring. The ``as name``
# re-export form above marks each name as an intentionally checked re-export
# rather than an accidental unused import. Grouped with the rest of the imports
# (rather than after the function definitions below) purely to keep this module
# PEP 8 import-position clean (E402) — every ``deploy_parts`` cross-reference
# back to a name DEFINED further down in this same file (``read_workspace_file``,
# ``write_workspace_file``, ``_stage_deploy_tree``, …) is a LAZY, function-local
# import there, so it never runs until long after this module has finished
# loading; the import ORDER here has no bearing on that.

# ---- the guarded reader -------------------------------------------------------


@overload
def read_workspace_file(
    path: Path, workspace_root: Path, *, text: Literal[True] = ...
) -> str | None: ...
@overload
def read_workspace_file(
    path: Path, workspace_root: Path, *, text: Literal[False]
) -> bytes | None: ...
def read_workspace_file(
    path: Path, workspace_root: Path, *, text: bool = True
) -> str | bytes | None:
    """The ONE choke-point reader for EVERY workspace/deployable file. It applies
    the shared containment guard (:func:`workspace_contained`) FIRST — resolving the
    path (following symlinks) and requiring it to stay STRICTLY inside the already
    *resolved* ``workspace_root`` — and only then reads. An escaping path (a
    workspace entry that IS, or traverses, a SYMLINK pointing at a host file — a
    SecretStore file, ``~/.config/disco``, a provider key) is REFUSED with
    :class:`DeployRefused` (``WORKSPACE_SYMLINK_ESCAPE``) BEFORE any open/read, so
    the symlink target's bytes are NEVER dereferenced.

    Every workspace-file read in this package (export-readiness, the secret-safety
    ``.dev.vars`` read, the spec digest, the tree digest, the db_id substitution
    read, and the host→sandbox push) routes through here, so NO workspace file is
    read anywhere without the containment guard and the per-site guards can never
    drift back in. A normal regular file inside the workspace reads fine; a MISSING
    file (or any read error) returns ``None`` so callers can treat absence as 'no
    file' (matching the previous ``_read_text`` behaviour)."""
    if not workspace_contained(path, workspace_root):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "A workspace path resolves outside the workspace (symlink escape): "
            f"{path.name}. Refusing to read it — the deploy fails closed.",
        )
    try:
        if text:
            return path.read_text(encoding="utf-8", errors="replace")
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


# ---- the guarded, descriptor-rooted write parent -------------------------------
#
# The helpers below implement ``_open_workspace_write_parent`` as a sequence of
# small, single-purpose top-level steps rather than one long body, so the
# function's own cyclomatic complexity stays small (the architecture scanner does
# not descend into a callee when scoring its caller). Only
# ``_stat_or_create_write_component`` — the ONE step that actually calls
# ``os.mkdir`` — is nested INSIDE ``_open_workspace_write_parent`` itself rather
# than living as a top-level sibling: a pre-existing regression suite inspects
# ``_open_workspace_write_parent``'s OWN source via ``inspect.getsource`` and
# requires the raw ``mkdir`` call to be textually part of it (see the module
# docstring). Nesting is otherwise unnecessary — a nested closure and a top-level
# sibling are equally invisible to the McCabe scorer of the enclosing function.


def _validate_workspace_write_rel(rel: Path | str) -> Path:
    """Reject an absolute or ``..``-escaping relative write path (fail closed)."""
    relative = Path(rel)
    if relative.is_absolute() or not relative.parts or any(part == ".." for part in relative.parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"A workspace WRITE path is absolute or escapes via '..': {relative}. "
            "Refusing to write it — the deploy fails closed.",
        )
    return relative


def _write_parent_directory_flags() -> int:
    """The ``O_NOFOLLOW | O_DIRECTORY`` (+ ``O_CLOEXEC`` where available) flags
    every directory in the write-parent chain is opened with. Fails closed on a
    host that cannot provide no-follow descriptor traversal."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "This host cannot provide no-follow descriptor traversal for workspace writes.",
        )
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _fd_identity(info: os.stat_result) -> tuple[int, int, int]:
    """The (device, inode, file-type) triple that proves a listed entry and an
    already-opened descriptor are the SAME filesystem object — the core of every
    TOCTOU check in this module."""
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _open_write_root(root: Path, directory_flags: int) -> tuple[int, os.stat_result]:
    """Open the workspace root by descriptor and prove the lexical listing and the
    opened descriptor name the SAME directory. Returns ``(root_fd, root_opened)``;
    the caller closes ``root_fd``."""
    try:
        root_listed = os.lstat(root)
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root is missing, replaced, or symlinked; refusing to write.",
        ) from exc
    root_opened = os.fstat(root_fd)
    if not stat.S_ISDIR(root_listed.st_mode) or _fd_identity(root_listed) != _fd_identity(
        root_opened
    ):
        os.close(root_fd)
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root changed before it could be opened safely.",
        )
    return root_fd, root_opened


def _open_write_component(
    current_fd: int, part: str, directory_flags: int, listed: os.stat_result
) -> int:
    """Open *part* under *current_fd* and prove it is still the SAME real
    directory that was just ``lstat``'d (no TOCTOU swap between the two calls)."""
    try:
        child_fd = os.open(part, directory_flags, dir_fd=current_fd)
    except OSError as exc:
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"Workspace directory {part!r} is not a stable real directory.",
        ) from exc
    opened_info = os.fstat(child_fd)
    if not stat.S_ISDIR(listed.st_mode) or _fd_identity(listed) != _fd_identity(opened_info):
        os.close(child_fd)
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"Workspace directory {part!r} changed during traversal.",
        )
    return child_fd


def _revalidate_write_component(parent_fd: int, name: str, child_fd: int) -> None:
    """Prove one already-open ancestor descriptor still names the same directory
    entry under its parent — part of the publication-time revalidation."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"Workspace directory {name!r} changed during publication.",
        ) from exc
    if _fd_identity(current) != _fd_identity(os.fstat(child_fd)):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"Workspace directory {name!r} changed during publication.",
        )


def _revalidate_write_chain(
    root: Path, root_opened: os.stat_result, opened: list[tuple[int, str, int]]
) -> None:
    """Revalidate the ENTIRE open descriptor chain (root + every ancestor) by
    identity, immediately before/after publishing a write. A concurrent
    rename/symlink swap anywhere in the chain is caught here and fails closed."""
    try:
        current_root = os.lstat(root)
    except OSError as exc:
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root changed during record publication.",
        ) from exc
    if _fd_identity(current_root) != _fd_identity(root_opened):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root changed during record publication.",
        )
    for parent_fd, name, child_fd in opened:
        _revalidate_write_component(parent_fd, name, child_fd)


@contextlib.contextmanager
def _open_workspace_write_parent(
    rel: Path | str,
    workspace_root: Path,
) -> Iterator[tuple[Path, int, str, Callable[[], None]]]:
    """Open a write parent by descriptor, never by a re-resolved host path.

    Every ancestor stays open through publication and can be revalidated against
    its parent entry. A concurrent rename/symlink swap therefore either fails the
    identity check or leaves the write bound to the already-open safe directory;
    it can never redirect publication outside the lexical workspace root.
    """

    # lstat *part* under current_fd, creating it (mkdir 0o700) if absent. Fails
    # closed on any inspection/creation error other than a benign already-created
    # race. Nested — see the section banner above for why.
    def _stat_or_create_write_component(part: str, current_fd: int) -> os.stat_result:
        try:
            return os.stat(part, dir_fd=current_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"Workspace directory {part!r} could not be inspected safely.",
            ) from exc
        try:
            os.mkdir(part, 0o700, dir_fd=current_fd)
            os.fsync(current_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"Workspace directory {part!r} could not be created safely.",
            ) from exc
        try:
            return os.stat(part, dir_fd=current_fd, follow_symlinks=False)
        except OSError as exc:
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"Workspace directory {part!r} changed while being created.",
            ) from exc

    relative = _validate_workspace_write_rel(rel)
    directory_flags = _write_parent_directory_flags()
    root = Path(os.path.abspath(os.fspath(workspace_root)))
    root_fd, root_opened = _open_write_root(root, directory_flags)

    opened: list[tuple[int, str, int]] = []
    current_fd = root_fd
    try:
        for part in relative.parts[:-1]:
            listed = _stat_or_create_write_component(part, current_fd)
            child_fd = _open_write_component(current_fd, part, directory_flags, listed)
            opened.append((current_fd, part, child_fd))
            current_fd = child_fd

        def revalidate() -> None:
            _revalidate_write_chain(root, root_opened, opened)

        yield root.joinpath(*relative.parts), current_fd, relative.parts[-1], revalidate
    finally:
        for _parent_fd, _name, child_fd in reversed(opened):
            os.close(child_fd)
        os.close(root_fd)


def write_workspace_file(
    rel: Path | str, data: str | bytes, workspace_root: Path, *, exclusive: bool = False
) -> Path:
    """The ONE choke-point WRITER for EVERY workspace/state file (the WRITE-class
    twin of :func:`read_workspace_file`). It opens the lexical workspace root and
    every parent with descriptor-relative ``O_NOFOLLOW`` traversal, retaining and
    revalidating the entire descriptor chain through atomic publication. A planted
    or concurrently swapped symlink therefore fails closed and can never redirect
    the write outside the workspace. Every workspace/state write in this package
    routes through here. Returns the lexical absolute target after publication.

    SEC-16 (hardlink defence): the per-component symlink check catches a symlinked LEAF,
    but not a HARDLINK aliasing a host file. With ``exclusive`` the leaf is created via
    ``O_CREAT|O_EXCL`` (FAIL if it already exists — defeats a pre-planted file/hardlink at
    the target), and a non-exclusive overwrite first LSTATs the existing leaf and REFUSES a
    multi-link (``st_nlink > 1``) regular file, so a truncating write can never clobber a
    host file aliased into the record path."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with _open_workspace_write_parent(rel, workspace_root) as opened:
        target, directory_fd, target_name, revalidate = opened
        if not exclusive:
            try:
                existing = os.stat(target_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode) or existing.st_nlink > 1
            ):
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A workspace WRITE target is not a private regular file: {target_name}. "
                    "It could alias a host path — refusing to write (fail closed).",
                )
        temporary_name = f".{target_name}.{_secrets.token_hex(16)}.tmp"
        temporary_fd: int | None = None
        temporary_exists = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            temporary_exists = True
            view = memoryview(raw)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("workspace record write made no progress")
                view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            revalidate()
            if exclusive:
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise DeployRefused(
                        RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                        f"The deploy record path already exists ({target_name}); refusing to "
                        "overwrite a pre-existing file (fail closed).",
                    ) from exc
                os.unlink(temporary_name, dir_fd=directory_fd)
                temporary_exists = False
            else:
                os.replace(
                    temporary_name,
                    target_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                temporary_exists = False
            os.fsync(directory_fd)
            revalidate()
            return target
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_exists:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=directory_fd)


def ensure_workspace_dir(rel: Path | str, workspace_root: Path) -> Path:
    """The ONE guarded ``mkdir -p`` for the package: create ``workspace_root/rel``,
    materialising each component as a REAL directory and REFUSING
    (:class:`DeployRefused` ``WORKSPACE_SYMLINK_ESCAPE``) if any existing component is
    a SYMLINK or already exists as a non-directory. No raw ``mkdir`` of a
    workspace/state path lives outside this helper, so a planted symlinked component
    can never be created (or written) through. Returns the absolute target dir."""
    target = _guarded_write_target(rel, workspace_root)
    cur = workspace_root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A workspace path component is a symlink: {part}. Refusing to create through it.",
            )
        if cur.exists():
            if not cur.is_dir():
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A workspace path component exists but is not a directory: {part}.",
                )
        else:
            cur.mkdir()
    return target


# ---- immutable staged copy of the deploy tree (SEC-6/SEC-7/CORR-2 + SEC-2/SEC-8) --
#
# ``_assert_no_symlinked_dirs`` is a top-level sibling (not nested in
# ``_stage_deploy_tree``): it carries no raw write/mkdir call itself, so unlike
# ``_stage_tree_file``/``_stage_trusted_input`` below it does not need to stay
# textually part of ``_stage_deploy_tree``'s own source for the workspace-write
# regression suite (see the section banner above ``_open_workspace_write_parent``).


def _assert_no_symlinked_dirs(root_path: Path, dirs: list[str]) -> None:
    """Refuse the staging walk if any subdirectory AT THIS LEVEL is a symlink."""
    for d in dirs:
        if (root_path / d).is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A symlinked DIRECTORY is present in the deploy tree: "
                f"{(root_path / d).name}. Refusing to stage/deploy — wrangler "
                "would follow it (fail closed).",
            )


def _stage_deploy_tree(workspace: Path) -> Path:
    """Build an IMMUTABLE, symlink-free, hardlink-free STAGED COPY of the
    deploy-relevant workspace, and run the build + wrangler against THAT — never the
    live mutable workspace (SEC-6/SEC-7/CORR-2). This is the single choke point that
    defeats the check-then-use (TOCTOU) races the per-site symlink guards each closed
    round-by-round: once the bytes are copied into a fresh tree we own, an attacker
    can no longer swap a checked path to a symlink between our check and wrangler's
    open.

    The copy is a WHOLE-TREE validation (SEC-2/SEC-8/SEC-17/SEC-18): it walks every
    deploy-followed tree (worker source + config, not just assets) and FAILS CLOSED on

      * a symlinked workspace ROOT (``Path.is_symlink`` on the root),
      * ANY symlink — file OR directory, at any depth (lstat per entry; never the
        silent ``os.walk(followlinks=False)`` skip),
      * ANY hardlink / multi-link regular file (``st_nlink > 1``) — a hardlink to a
        host file outside the workspace would otherwise be copied/published.

    Only REGULAR files (minus the regenerated/internal ``_TREE_SKIP_DIRS`` /
    ``_TREE_SKIP_FILES``) are copied, byte for byte, into a private temp dir. Returns
    the staged root; the caller MUST ``shutil.rmtree`` it when done."""

    # Copy one non-skipped tree file into the staged copy, refusing a symlinked or
    # hardlinked source (fail closed). Nested (rather than a top-level sibling) so
    # the raw mkdir/write_bytes calls below stay part of THIS function's own
    # source span — see the section banner above ``_open_workspace_write_parent``.
    def _stage_tree_file(src: Path, workspace_root: Path, staged: Path) -> None:
        st = src.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A symlinked FILE is present in the deploy tree: {src.name}. "
                "Refusing to stage/deploy — wrangler follows symlinks and could "
                "publish a host-secret target (fail closed).",
            )
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A multi-link (hardlinked) file is present in the deploy tree: "
                f"{src.name} (st_nlink={st.st_nlink}). A hardlink can alias a host "
                "file outside the workspace — refusing to stage/deploy (fail "
                "closed).",
            )
        rel = src.relative_to(workspace_root)
        dest = staged / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy the BYTES only (never the link / metadata that could re-introduce
        # a special file). The READ routes through the guarded reader (the one
        # workspace-read choke point); the WRITE targets the fresh temp staging
        # tree we own (not a workspace path), so it needs no workspace guard.
        data = read_workspace_file(src, workspace_root, text=False)
        dest.write_bytes(data if data is not None else b"")

    # Walk *workspace* (minus the skipped subtrees/files) and copy every regular
    # file into *staged*, failing closed on any symlink or hardlink encountered.
    def _copy_walked_tree_into_stage(workspace: Path, workspace_root: Path, staged: Path) -> None:
        for root, dirs, names in os.walk(workspace):
            # Prune the regenerated/internal subtrees BEFORE the symlink check, so a
            # symlinked node_modules/.git/.disco (never deployed) is simply ignored.
            dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
            root_path = Path(root)
            _assert_no_symlinked_dirs(root_path, dirs)
            for name in names:
                if name in _TREE_SKIP_FILES:
                    continue
                _stage_tree_file(root_path / name, workspace_root, staged)

    # Freeze one trusted-reconstruction input (an otherwise digest-skipped .disco
    # file) into the staged tree, if present.
    def _stage_trusted_input(
        workspace: Path, workspace_root: Path, staged: Path, relpath: str
    ) -> None:
        src = workspace / relpath
        if not src.exists():
            return
        st = src.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A trusted deployment input is not a single-link regular file: {relpath}.",
            )
        data = read_workspace_file(src, workspace_root, text=False)
        if data is not None:
            dest = staged / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    # Freeze the trusted reconstruction inputs (the AppSpec, the DesignSpec, and
    # any selected primitive provenance file) under the otherwise-pruned .disco
    # tree, so pre/post-build verification cannot race a live rewrite.
    def _copy_trusted_inputs_into_stage(
        workspace: Path, workspace_root: Path, staged: Path
    ) -> None:
        trusted_inputs = [
            _APPSPEC_RELPATH,
            ".disco/designspec.json",
            ".disco/primitives/stripe.json",
        ]
        raw_app = read_workspace_file(workspace / _APPSPEC_RELPATH, workspace_root)
        if raw_app:
            with contextlib.suppress(Exception):
                if load_app_spec_from_bytes(raw_app).webhooks is not None:
                    trusted_inputs.append(".disco/primitives/webhook.json")
        for relpath in trusted_inputs:
            _stage_trusted_input(workspace, workspace_root, staged, relpath)

    workspace_root = workspace.resolve()
    if workspace.is_symlink():
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root is a symlink; refusing to stage/deploy through it (fail closed).",
        )
    staged = Path(tempfile.mkdtemp(prefix="disco-deploy-stage-", dir=str(_deploy_stage_root())))
    try:
        _copy_walked_tree_into_stage(workspace, workspace_root, staged)
        _copy_trusted_inputs_into_stage(workspace, workspace_root, staged)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return staged


# ---- server-controlled lock/staging directories -------------------------------


def _deploy_lock_dir() -> Path:
    """The server-controlled directory the cross-process deploy lockfiles live in —
    NEVER the untrusted workspace, so the sandboxed build agent can't read, delete, or
    pre-plant a lockfile to defeat the guard. Resolution: an explicit
    ``DISCO_DEPLOY_LOCK_DIR`` override, else ``$XDG_STATE_HOME/disco/deploy-locks``
    (or ``$XDG_CONFIG_HOME/disco/deploy-locks``), else a fixed subdir of the system temp
    dir. Created (parents, 0o700) on first use."""
    return workspace_process_lock_dir()


def _deploy_stage_root() -> Path:
    """The server-controlled PARENT directory the immutable per-deploy staging trees are
    ``mkdtemp``'d under. Resolution: an explicit ``DISCO_DEPLOY_STAGE_DIR`` override, else the
    system temp dir. Created (parents, 0o700) on first use. NEVER the untrusted workspace —
    the staged tree's ancestors are part of wrangler's config-redirect discovery path (it
    walks UP from cwd), so they must be a location WE control + can verify clean."""
    override = os.environ.get(_DEPLOY_STAGE_DIR_ENV, "").strip()
    base = Path(override) if override else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


__all__ = [
    "CF_ACCOUNT_SECRET",
    "CF_TOKEN_SECRET",
    "assert_deploy_tree_symlink_free",
    "build_plan",
    "confirmation_phrase",
    "connect_account",
    "connection_status",
    "disconnect_account",
    "ensure_workspace_dir",
    "execute_deploy",
    "read_workspace_file",
    "workspace_contained",
    "write_workspace_file",
]
