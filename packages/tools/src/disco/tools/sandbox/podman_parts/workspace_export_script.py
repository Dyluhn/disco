"""The guest-side Python script `PodmanSandboxInstance.export_workspace_archive`
streams into the sandbox and runs there to produce the bounded workspace tar.

Kept as its own module (not inline in `..podman`) purely because embedded
script text counts as logical source under the module-size budget — moving it
changes nothing about what runs or how; the string is imported back into
`..podman` unchanged and referenced as `podman._WORKSPACE_EXPORT_SCRIPT` (some
tests read it via that module attribute).

One guest process streams the bounded workspace as tar. Keeping the filter in
the guest prevents runtime credential files from crossing the sandbox boundary;
archive.py validates every member again before it reaches durable storage.
"""

from __future__ import annotations

WORKSPACE_EXPORT_SCRIPT = r"""
import json, os, stat, sys, tarfile

root = os.path.realpath(sys.argv[1])
max_depth = int(sys.argv[2])
max_bytes = int(sys.argv[3])
excluded = {
    'node_modules', '.pnpm-store', '.npm', '.yarn', '.cache', '.venv',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
}
safe_templates = {'example', 'sample', 'dist', 'template'}
skipped = []
preserve = []
failures = 0

def secret_path(rel):
    for name in rel.lower().split('/'):
        for stem in ('.dev.vars', '.env'):
            if name == stem:
                return True
            if name.startswith(stem + '.') and name.rsplit('.', 1)[-1] not in safe_templates:
                return True
    return False

def failure(rel, reason, keep=True):
    global failures
    skipped.append(f'{rel}: {reason}')
    if keep:
        preserve.append(rel)
    failures += 1
    if failures >= 10:
        raise RuntimeError(
            f'{failures} consecutive failures (last: {rel!r}: {reason}) '
            '— transport presumed dead, aborting snapshot'
        )

if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
    raise RuntimeError('workspace export requires no-follow directory descriptors')
open_flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW
directory_flags = open_flags | os.O_DIRECTORY

def same_object(before, after):
    return (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)) == (
        after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)
    )

def walk(directory_fd, rel_root, depth, archive):
    global failures
    if depth > max_depth:
        raise RuntimeError(
            f'workspace depth exceeded the {max_depth}-level cap at {rel_root!r}'
        )
    try:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        if not rel_root:
            raise RuntimeError(f'workspace traversal failed: {exc}') from exc
        failure(rel_root, f'list failed: {exc}')
        return

    for entry in entries:
        name = entry.name
        rel = f'{rel_root}/{name}' if rel_root else name
        if name in excluded or secret_path(rel):
            continue
        try:
            listed = entry.stat(follow_symlinks=False)
        except OSError as exc:
            failure(rel, f'lstat failed: {exc}')
            continue
        if stat.S_ISLNK(listed.st_mode):
            skipped.append(f'{rel}: symlink excluded')
            continue
        if stat.S_ISDIR(listed.st_mode):
            if depth + 1 > max_depth:
                raise RuntimeError(
                    f'workspace depth exceeded the {max_depth}-level cap at {rel!r}'
                )
            child_fd = None
            try:
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                opened = os.fstat(child_fd)
                if not stat.S_ISDIR(opened.st_mode) or not same_object(listed, opened):
                    os.close(child_fd)
                    failure(rel, 'directory changed before it could be archived')
                    continue
            except OSError as exc:
                if child_fd is not None:
                    os.close(child_fd)
                failure(rel, f'open directory failed: {exc}')
                continue
            try:
                walk(child_fd, rel, depth + 1, archive)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(listed.st_mode):
            skipped.append(f'{rel}: non-regular entry excluded')
            continue
        # A hardlink can give a path-filtered runtime secret an innocent alias.
        # Snapshot only uniquely linked regular files; caches that legitimately
        # use hardlinks are excluded above and source artifacts fail closed.
        if listed.st_nlink != 1:
            skipped.append(f'{rel}: hardlinked entry excluded')
            continue
        fd = None
        try:
            fd = os.open(name, open_flags, dir_fd=directory_fd)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not same_object(listed, opened):
                os.close(fd)
                failure(rel, 'file changed before it could be archived')
                continue
            if opened.st_nlink != 1:
                os.close(fd)
                skipped.append(f'{rel}: hardlinked entry excluded')
                continue
            if opened.st_size > max_bytes:
                os.close(fd)
                skipped.append(
                    f'{rel}: {opened.st_size} bytes exceeds the {max_bytes}-byte cap'
                )
                failures += 1
                if failures >= 10:
                    raise RuntimeError(
                        f'{failures} consecutive failures (last: {rel!r}: oversized) '
                        '— transport presumed dead, aborting snapshot'
                    )
                continue
            member = tarfile.TarInfo(rel)
            member.size = opened.st_size
            member.mode = 0o600
            member.mtime = int(opened.st_mtime)
            with os.fdopen(fd, 'rb') as source:
                archive.addfile(member, source)
                after = os.fstat(source.fileno())
            before_version = (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
            )
            after_version = (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            )
            if after_version != before_version:
                raise RuntimeError(f'{rel!r} changed while it was being archived')
            failures = 0
        except OSError as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            failure(rel, f'read failed: {exc}')

try:
    root_fd = os.open(root, directory_flags)
except OSError as exc:
    raise RuntimeError(f'workspace root is not a directory: {exc}') from exc
try:
    root_info = os.fstat(root_fd)
    if not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError('workspace root is not a directory')
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        walk(root_fd, '', 0, archive)
finally:
    os.close(root_fd)

print(
    '__DISCO_WORKSPACE_EXPORT_META__'
    + json.dumps({'skipped': skipped, 'preserve': preserve}, separators=(',', ':')),
    file=sys.stderr,
)
"""
