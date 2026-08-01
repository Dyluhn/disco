"""`ContainerInstance.atomic_write` — the staged, SELinux-safe commit-via-rename.

CD-TOOLS-3/4: atomically commit `data` to `path` IN THE GUEST — stage to a
RANDOM tmp sibling (so a model can't pre-create a predictable symlink there)
then `mv -f` over the target (atomic on the same filesystem; mv replaces a
symlinked target rather than following it). The `.disco/` governed guard
already ran in the tool, so the target is never governed.
"""

from __future__ import annotations

import hashlib
import io
import logging
import posixpath
import secrets
import tarfile
import time
from typing import TYPE_CHECKING

from ..base import SandboxError

if TYPE_CHECKING:
    from .._container import ContainerInstance

_LOG = logging.getLogger(__name__)


def _stage_upload(instance: ContainerInstance, data: bytes, upload_name: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=upload_name)
        info.size = len(data)
        info.uid = info.gid = instance._workspace_uid
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    if not instance._container.put_archive("/tmp", buf.getvalue()):
        raise SandboxError("atomic_write staging failed")


def _recover_legacy_selinux_target(
    instance: ContainerInstance, path: str, target: str, parent: str, tmp_path: str, data: bytes
) -> None:
    """Compatibility recovery for files materialized by older rootless-Podman
    builds. Direct put_archive into the tmpfs gave those files
    container_runtime_tmpfs_t; the guest may read them but SELinux denies
    unlink/rename-over. The runtime archive API can replace that existing
    regular file. Verify the full digest before reporting success."""
    fallback = io.BytesIO()
    with tarfile.open(fileobj=fallback, mode="w") as tar:
        info = tarfile.TarInfo(name=posixpath.basename(target))
        info.size = len(data)
        info.uid = info.gid = instance._workspace_uid
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    if not instance._container.put_archive(parent, fallback.getvalue()):
        raise SandboxError(f"atomic_write {path!r} legacy overwrite failed")
    digest_rc, digest_out = instance._guest_run(["sha256sum", "--", target])
    expected_digest = hashlib.sha256(data).hexdigest()
    digest_parts = digest_out.decode("ascii", "replace").split(maxsplit=1)
    observed_digest = digest_parts[0] if digest_parts else ""
    if digest_rc != 0 or observed_digest != expected_digest:
        raise SandboxError(f"atomic_write {path!r} legacy overwrite verification failed")
    instance._guest_run(["rm", "-f", "--", tmp_path])
    _LOG.warning(
        "atomic_write recovered legacy SELinux-labeled target %s",
        target,
    )


async def atomic_write(instance: ContainerInstance, path: str, data: bytes) -> None:
    """CD-TOOLS-3/4: atomically commit `data` to `path` IN THE GUEST — stage to a RANDOM tmp
    sibling (so a model can't pre-create a predictable symlink there) then `mv -f` over the
    target (atomic on the same filesystem; mv replaces a symlinked target rather than following
    it). The .disco/ governed guard already ran in the tool, so the target is never governed."""
    instance._alive()

    def _aw() -> None:
        instance._resolve_guest_path(path)  # enforce the jail (raises on escape / unverifiable)
        # Replace the RESOLVED real target (follow a final symlink), matching ProcessSandbox's
        # _resolve().resolve() + os.replace and the backend's own write_file — so atomic_write
        # and write_file have identical symlink semantics (codex round-5 parity).
        # _guest_realpath is jail-checked by _resolve_guest_path above; for a new
        # file it is the lexical path.
        real_target = instance._guest_realpath(instance._container_path(path))
        if real_target is None:
            raise SandboxError(f"atomic_write {path!r}: cannot resolve real target")
        target = real_target
        parent = posixpath.dirname(target) or instance._ws
        tmp_name = f".disco-tmp-{secrets.token_hex(8)}"
        upload_name = f"disco-upload-{secrets.token_hex(8)}"
        instance._guest_run(["mkdir", "-p", "--", parent])
        target_was_file = instance._guest_run(["test", "-f", target])[0] == 0
        # Rootless Podman's Docker-compatible put_archive applies a
        # container-runtime SELinux label when extracting directly into a
        # tmpfs volume. The guest can read that file but cannot rename or
        # unlink it inside the volume, so every file_write strands a
        # .disco-tmp-* and fails. Extract into the container's own /tmp,
        # then have a guest process copy into the workspace. That copy gets
        # the workspace-compatible label; the final mv remains the only
        # commit and is still atomic on the workspace filesystem.
        upload_path = posixpath.join("/tmp", upload_name)
        tmp_path = posixpath.join(parent, tmp_name)
        _stage_upload(instance, data, upload_name)
        committed = False
        try:
            rc, _ = instance._guest_run(["cp", "--", upload_path, tmp_path])
            if rc != 0:
                raise SandboxError(f"atomic_write {path!r} guest staging failed (rc={rc})")
            # -T (--no-target-directory): a TRUE rename/replace. Without it, if `target` is an
            # existing directory or a symlink-to-directory, `mv` would move tmp INTO it
            # (succeeding while leaving target unchanged + stranding tmp). -T makes mv replace
            # the target as a non-directory, or fail (rc!=0) if it is a real dir.
            rc, _ = instance._guest_run(["mv", "-fT", "--", tmp_path, target])
            if rc != 0:
                if not target_was_file:
                    raise SandboxError(f"atomic_write {path!r} rename failed (rc={rc})")
                _recover_legacy_selinux_target(instance, path, target, parent, tmp_path, data)
            committed = True
        finally:
            instance._guest_run(["rm", "-f", "--", upload_path])
            if not committed:
                instance._guest_run(["rm", "-f", "--", tmp_path])

    await instance._guarded(_aw)
