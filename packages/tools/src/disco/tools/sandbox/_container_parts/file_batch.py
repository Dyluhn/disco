"""One-crossing deterministic file batches for container sandboxes.

The host uploads a random, immutable-by-contract manifest/payload directory and
starts one isolated Python helper in the guest.  The helper resolves and reads the
complete plan before its first mutation, then performs adjacent stale checks and
ordered atomic replacements/no-follow deletions.  Its bounded JSON response carries
the same exact transition evidence the portable per-file path produces.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import tarfile
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from ..base import SandboxError
from ..file_batch import (
    SandboxFileBatchFailure,
    SandboxFileBatchResult,
    SandboxFileChange,
    SandboxFileMutation,
)

if TYPE_CHECKING:
    from .._container import ContainerInstance


_FILE_BATCH_GUEST_SCRIPT = r"""
import errno
import hashlib
import json
import os
import posixpath
import secrets
import shutil
import stat
import sys

MAX_READ_BYTES = 32 * 1024 * 1024
root_arg, stage, expected_manifest_sha256 = sys.argv[1:4]
root = os.path.realpath(root_arg)


def short_exc(exc):
    message = " ".join(str(exc).split())[:500] or "no detail supplied"
    return {"type": type(exc).__name__, "message": message}


def failed(phase, error, kind, message, *, changes=(), path=None,
           failed_path_state=None, underlying=None, details=None):
    failure = {"phase": phase, "error": error, "kind": kind, "message": message}
    if path is not None:
        failure["path"] = path
    if failed_path_state is not None:
        failure["failed_path_state"] = failed_path_state
    if underlying is not None:
        failure["underlying"] = underlying
    if details is not None:
        failure["details"] = details
    return {"version": 1, "status": "failed", "changes": list(changes), "failure": failure}


def strip_workspace_prefix(path):
    for prefix in ("/workspace/", "workspace/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return "" if path in ("/workspace", "workspace") else path


def canonical(path):
    return posixpath.normpath(strip_workspace_prefix(path))


def resolve(path):
    clean = strip_workspace_prefix(path)
    lexical = posixpath.normpath(posixpath.join(root, clean))
    prefix = root.rstrip("/") + "/"
    if lexical != root and not lexical.startswith(prefix):
        raise PermissionError("path escapes workspace")
    real = os.path.realpath(lexical)
    if real != root and not real.startswith(prefix):
        raise PermissionError("path escapes workspace via symlink")
    return "." if real == root else posixpath.relpath(real, root)


def path_parts(relpath):
    parts = [part for part in relpath.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise PermissionError("invalid workspace file path")
    return parts


def read_file(relpath):
    parts = path_parts(relpath)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    opened = []
    try:
        current = os.open(root, dir_flags)
        opened.append(current)
        for part in parts[:-1]:
            current = os.open(part, dir_flags, dir_fd=current)
            opened.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        opened.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("only regular, non-symlink files may be read")
        if info.st_size > MAX_READ_BYTES:
            raise OSError(errno.EFBIG, "file exceeds sandbox read cap")
        chunks = []
        remaining = MAX_READ_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_READ_BYTES:
            raise OSError(errno.EFBIG, "file grew beyond sandbox read cap")
        return data
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise PermissionError("symlink and non-regular paths are denied") from exc
        raise
    finally:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass


def open_parent(relpath):
    parts = path_parts(relpath)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    opened = []
    current = os.open(root, flags)
    opened.append(current)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=current)
                next_fd = os.open(part, flags, dir_fd=current)
            opened.append(next_fd)
            current = next_fd
        return opened, current, parts[-1]
    except BaseException:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def atomic_write(relpath, data):
    opened, parent, name = open_parent(relpath)
    tmp_name = ".disco-tmp-" + secrets.token_hex(8)
    descriptor = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(tmp_name, flags, 0o644, dir_fd=parent)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.close(descriptor)
        descriptor = None
        os.replace(tmp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(tmp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        for opened_fd in reversed(opened):
            os.close(opened_fd)


def delete_file(relpath):
    opened, parent, name = open_parent(relpath)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("only regular, non-symlink files may be deleted")
        os.unlink(name, dir_fd=parent)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def change(path, before, after):
    return {
        "path": path,
        "before_sha256": digest(before),
        "after_sha256": digest(after),
        "after_size_bytes": None if after is None else len(after),
    }


def load_manifest():
    with open(os.path.join(stage, "manifest.json"), "rb") as stream:
        manifest_bytes = stream.read()
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
        raise ValueError("batch manifest digest mismatch")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or set(manifest) != {"version", "mutations", "commit_last"}:
        raise ValueError("invalid batch manifest shape")
    if manifest["version"] != 1 or not isinstance(manifest["mutations"], list):
        raise ValueError("unsupported batch manifest")
    if not isinstance(manifest["commit_last"], list) or not all(
        isinstance(item, str) for item in manifest["commit_last"]
    ):
        raise ValueError("invalid commit_last")
    return manifest


def load_after(entry, index):
    if not isinstance(entry, dict) or set(entry) != {"path", "payload", "after_sha256"}:
        raise ValueError("invalid mutation entry")
    if not isinstance(entry["path"], str):
        raise ValueError("mutation path is not text")
    payload = entry["payload"]
    expected = entry["after_sha256"]
    if payload is None:
        if expected is not None:
            raise ValueError("deletion entry carried a digest")
        return None
    expected_name = "payload/" + str(index).zfill(6)
    if payload != expected_name or not isinstance(expected, str):
        raise ValueError("invalid mutation payload reference")
    with open(os.path.join(stage, payload), "rb") as stream:
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("mutation payload digest mismatch")
    return data


def prepare_plan(manifest):
    planned = {}
    for index, entry in enumerate(manifest["mutations"]):
        after = load_after(entry, index)
        raw = entry["path"]
        resolved = resolve(raw)
        if after is None and canonical(raw) != resolved:
            return None, failed(
                "prevalidation", "BATCH_DELETE_ALIAS_REFUSED",
                "deterministic_batch_prevalidation_failed",
                "deterministic batch refused before writing: deletion target "
                + repr(raw) + " resolves through an alias to " + repr(resolved)
                + ". Generated cleanup never follows symlinks into another resource; "
                "remove the alias or use a fresh workspace.", path=canonical(raw),
            )
        if resolved in planned and planned[resolved] != after:
            return None, failed(
                "prevalidation", "BATCH_PLAN_CONFLICT",
                "deterministic_batch_prevalidation_failed",
                "deterministic batch refused before writing: " + repr(raw)
                + " resolves to " + repr(resolved)
                + ", which has two different planned final states.", path=resolved,
            )
        planned[resolved] = after
    resolved_last = [resolve(path) for path in manifest["commit_last"]]
    if len(resolved_last) != len(set(resolved_last)):
        return None, failed(
            "prevalidation", "BATCH_PLAN_CONFLICT",
            "deterministic_batch_prevalidation_failed",
            "deterministic batch refused before writing: commit_last contains aliases.",
        )
    unknown = [path for path in resolved_last if path not in planned]
    if unknown:
        return None, failed(
            "prevalidation", "BATCH_PLAN_CONFLICT",
            "deterministic_batch_prevalidation_failed",
            "deterministic batch refused before writing: commit_last names unplanned path(s): "
            + ", ".join(unknown) + ".",
        )
    return (planned, resolved_last), None


def execute():
    try:
        manifest = load_manifest()
        prepared, refusal = prepare_plan(manifest)
    except Exception as exc:
        detail = short_exc(exc)
        return failed(
            "prevalidation", "BATCH_PATH_RESOLUTION_FAILED",
            "deterministic_batch_prevalidation_failed",
            "deterministic batch refused before writing because its plan could not be resolved: "
            + detail["type"] + ": " + detail["message"], underlying=detail,
        )
    if refusal is not None:
        return refusal
    planned, resolved_last = prepared
    before = {}
    for path in sorted(planned):
        try:
            before[path] = read_file(path)
        except Exception as exc:
            detail = short_exc(exc)
            return failed(
                "prevalidation", "BATCH_PREVALIDATION_READ_FAILED",
                "deterministic_batch_prevalidation_failed",
                "deterministic batch refused before writing because " + path
                + " could not be read: " + detail["type"] + ": " + detail["message"],
                path=path, underlying=detail,
            )
    effective = {path for path, after in planned.items() if before[path] != after}
    last_rank = {path: index for index, path in enumerate(resolved_last)}
    ordered = sorted(
        effective,
        key=lambda path: (1, last_rank[path]) if path in last_rank else (0, path),
    )
    changes = []
    for path in ordered:
        expected = before[path]
        intended = planned[path]
        try:
            current = read_file(path)
        except Exception as exc:
            detail = short_exc(exc)
            return failed(
                "commit", "BATCH_COMMIT_UNCERTAIN", "deterministic_batch_commit_uncertain",
                "the current state of " + path + " could not be checked adjacent to commit",
                changes=changes, path=path, failed_path_state="unknown", underlying=detail,
            )
        if current != expected:
            expected_digest = digest(expected)
            actual_digest = digest(current)
            operation = "Delete" if intended is None else "Write"
            message = (
                operation + " refused — " + path + " changed while this operation was being "
                "prepared. Nothing was written to this path. Read the current file, then "
                "re-apply the intended change to that revision."
            )
            return failed(
                "stale", "STALE_FILE_CONTEXT", "stale_file_context", message,
                changes=changes, path=path, failed_path_state="unchanged",
                details={"expected_sha256": expected_digest, "actual_sha256": actual_digest},
            )
        try:
            if intended is None:
                delete_file(path)
            else:
                atomic_write(path, intended)
        except Exception as exc:
            detail = short_exc(exc)
            observed_known = True
            try:
                observed = read_file(path)
            except Exception:
                observed = None
                observed_known = False
            if observed_known and observed == intended:
                state = "committed"
                changes.append(change(path, expected, intended))
                error = "BATCH_COMMIT_INTERRUPTED"
                kind = "deterministic_batch_commit_interrupted"
            elif observed_known and observed == expected:
                state = "unchanged"
                error = "BATCH_PARTIAL_COMMIT" if changes else "BATCH_COMMIT_FAILED"
                kind = (
                    "deterministic_batch_partial_commit"
                    if changes
                    else "deterministic_batch_failed"
                )
            else:
                state = "unknown"
                error = "BATCH_COMMIT_UNCERTAIN"
                kind = "deterministic_batch_commit_uncertain"
            return failed(
                "commit", error, kind,
                "deterministic batch backend failure at " + path,
                changes=changes, path=path, failed_path_state=state, underlying=detail,
            )
        changes.append(change(path, expected, intended))
    return {"version": 1, "status": "ok", "changes": changes}


try:
    result = execute()
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
finally:
    shutil.rmtree(stage, ignore_errors=True)
"""


def _archive_payload(
    stage_name: str,
    mutations: Sequence[SandboxFileMutation],
    commit_last: Sequence[str],
    *,
    uid: int,
) -> tuple[bytes, str]:
    manifest_mutations: list[dict[str, str | None]] = []
    payloads: list[tuple[str, bytes]] = []
    for index, mutation in enumerate(mutations):
        payload_name: str | None = None
        after_sha256: str | None = None
        if mutation.after is not None:
            payload_name = f"payload/{index:06d}"
            after_sha256 = hashlib.sha256(mutation.after).hexdigest()
            payloads.append((payload_name, mutation.after))
        manifest_mutations.append(
            {"path": mutation.path, "payload": payload_name, "after_sha256": after_sha256}
        )
    manifest = json.dumps(
        {"version": 1, "mutations": manifest_mutations, "commit_last": list(commit_last)},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for directory in (stage_name, f"{stage_name}/payload"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            info.uid = info.gid = uid
            info.mtime = int(time.time())
            archive.addfile(info)
        for name, data in (("manifest.json", manifest), *payloads):
            info = tarfile.TarInfo(f"{stage_name}/{name}")
            info.size = len(data)
            info.mode = 0o600
            info.uid = info.gid = uid
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), hashlib.sha256(manifest).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _parse_change(raw: object) -> SandboxFileChange:
    if not isinstance(raw, dict) or set(raw) != {
        "path",
        "before_sha256",
        "after_sha256",
        "after_size_bytes",
    }:
        raise SandboxError("file batch returned an invalid change record")
    path = raw["path"]
    before = raw["before_sha256"]
    after = raw["after_sha256"]
    size = raw["after_size_bytes"]
    if not isinstance(path, str) or not path or (before is not None and not _valid_sha256(before)):
        raise SandboxError("file batch returned invalid transition evidence")
    if after is not None and not _valid_sha256(after):
        raise SandboxError("file batch returned invalid transition evidence")
    if (after is None and size is not None) or (
        after is not None
        and (not isinstance(size, int) or isinstance(size, bool) or size < 0)
    ):
        raise SandboxError("file batch returned an invalid after-size")
    return SandboxFileChange(path, cast(str | None, before), cast(str | None, after), size)


def _optional_dict(value: object, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SandboxError(f"file batch returned invalid {field}")
    return cast(dict[str, Any], value)


def _parse_failure(raw: object) -> SandboxFileBatchFailure:
    if not isinstance(raw, dict):
        raise SandboxError("file batch returned an invalid failure record")
    required = {"phase", "error", "kind", "message"}
    allowed = required | {"path", "failed_path_state", "underlying", "details"}
    if not required.issubset(raw) or not set(raw).issubset(allowed):
        raise SandboxError("file batch returned an invalid failure shape")
    phase = raw["phase"]
    state = raw.get("failed_path_state")
    path = raw.get("path")
    if phase not in {"prevalidation", "stale", "commit"}:
        raise SandboxError("file batch returned an invalid failure phase")
    if state not in {None, "unchanged", "committed", "unknown"}:
        raise SandboxError("file batch returned an invalid failed-path state")
    if path is not None and not isinstance(path, str):
        raise SandboxError("file batch returned an invalid failed path")
    if not all(
        isinstance(raw[field], str) and raw[field]
        for field in ("error", "kind", "message")
    ):
        raise SandboxError("file batch returned invalid failure text")
    return SandboxFileBatchFailure(
        phase=phase,
        error=raw["error"],
        kind=raw["kind"],
        message=raw["message"],
        path=path,
        failed_path_state=state,
        underlying=_optional_dict(raw.get("underlying"), "underlying evidence"),
        details=_optional_dict(raw.get("details"), "failure details"),
    )


def parse_file_batch_result(payload: bytes) -> SandboxFileBatchResult:
    """Validate the guest's bounded protocol response before trusting its evidence."""
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxError("file batch returned malformed evidence") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("status") not in {
        "ok",
        "failed",
    }:
        raise SandboxError("file batch returned an invalid protocol envelope")
    expected_keys = {"version", "status", "changes"}
    if raw["status"] == "failed":
        expected_keys.add("failure")
    if set(raw) != expected_keys or not isinstance(raw.get("changes"), list):
        raise SandboxError("file batch returned an invalid protocol shape")
    changes = tuple(_parse_change(change) for change in raw["changes"])
    failure = _parse_failure(raw["failure"]) if raw["status"] == "failed" else None
    return SandboxFileBatchResult(changes=changes, failure=failure)


async def commit_file_batch(
    instance: ContainerInstance,
    mutations: Sequence[SandboxFileMutation],
    *,
    commit_last: Sequence[str],
) -> SandboxFileBatchResult:
    """Upload once and execute once; no mutation is retried after dispatch."""
    instance._alive()
    if not mutations:
        return SandboxFileBatchResult(changes=())
    stage_name = f"disco-file-batch-{secrets.token_hex(16)}"
    archive, manifest_sha256 = _archive_payload(
        stage_name,
        mutations,
        commit_last,
        uid=instance._workspace_uid,
    )

    def _commit() -> SandboxFileBatchResult:
        if not instance._container.put_archive("/tmp", archive):
            raise SandboxError("file batch staging failed before guest dispatch")
        rc, output = instance._guest_run(
            [
                "python3",
                "-I",
                "-c",
                _FILE_BATCH_GUEST_SCRIPT,
                instance._ws,
                f"/tmp/{stage_name}",
                manifest_sha256,
            ]
        )
        if rc != 0:
            raise SandboxError(f"file batch guest execution failed (rc={rc})")
        return parse_file_batch_result(output)

    return await instance._guarded(_commit)


__all__ = ["_FILE_BATCH_GUEST_SCRIPT", "commit_file_batch", "parse_file_batch_result"]
