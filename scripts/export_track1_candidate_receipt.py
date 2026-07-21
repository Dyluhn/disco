#!/usr/bin/env python3
"""Candidate receipt — an enforceable, re-runnable certification that one EXACT,
operator-named, clean commit carries the Export Track-1 recovery work.

SALVAGED (owner-adjudicated 2026-07-17) from the failed campaign's repaired receipt.
The corrections it keeps:

1. IDENTITY   — the operator supplies ``--expect-head``; ``git rev-parse HEAD`` must
                match it exactly. NO commit hash is hardcoded anywhere in this script
                (a receipt cannot embed the SHA of the commit that carries it).
2. PRISTINE   — the worktree must be empty of BOTH tracked modifications AND untracked
                files (``--untracked-files=all``), so the commit IS what is on disk.
3. BINDING    — every recovery file is bound by a COMPLETE hash inventory
                (``docs/export-track1-recovery-inventory.json``). Scope is derived from
                GIT (``base..HEAD``), never a hand-kept list; MISSING / ADDITIONAL /
                STALE / CHANGED entries are all rejected.
4. EXTERNAL   — external evidence files (e.g. the 104-case adversarial probe) are
                STRICTLY OPTIONAL and NON-AUTHORITATIVE (owner policy: the probe is
                discovery evidence, not a certification oracle). When the inventory
                declares them, their bytes are SHA-256 verified so the evidence trail
                is tamper-evident — but this receipt NEVER executes them and holds NO
                expected result matrix for them.
5. ORDERING   — every gate fails CLOSED and all gates run BEFORE the proof suites
                execute; a tripped gate yields an explicit NOT-RUN record.
6. EVIDENCE   — a structured JSON receipt (``--evidence-dir``) carries the exact
                candidate SHA and per-check results.

The receipt PROVES a candidate; it does not define product policy.

Exit: 0 iff every check passes; 1 otherwise. No network. Read-only apart from the
optional evidence JSON and ``--emit-inventory`` (an authoring aid, not a proof — the
protection is human review of the inventory diff plus the operator-supplied HEAD).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INVENTORY_REL = "docs/export-track1-recovery-inventory.json"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One receipt line: a named, pass/fail assertion with human detail and the
    structured data that backs it (so evidence is not prose-only)."""

    name: str
    ok: bool
    detail: str
    data: dict[str, object] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_SYSTEM_GIT_CANDIDATES = (Path("/usr/bin/git"), Path("/bin/git"))


def resolve_system_git() -> Path:
    """Resolve Git independently of PATH; prefer the conventional system binary."""
    fallback: Path | None = None
    for candidate in _SYSTEM_GIT_CANDIDATES:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            if resolved.stat().st_uid == 0:
                return resolved
            fallback = fallback or resolved
    if fallback is not None:
        return fallback
    raise OSError("no trusted system Git found at /usr/bin/git or /bin/git")


def git_runtime_identity() -> dict[str, object]:
    path = resolve_system_git()
    st = path.stat()
    return {
        "realpath": str(path),
        "sha256": sha256_file(path),
        "owner_uid": st.st_uid,
        "root_owned": st.st_uid == 0,
        "mode": stat.S_IMODE(st.st_mode),
    }


def sanitized_git_env() -> tuple[dict[str, str], list[str]]:
    removed = sorted(key for key in os.environ if key.upper().startswith("GIT_"))
    return ({key: value for key, value in os.environ.items() if key not in removed}, removed)


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo``, returning stripped stdout. Raises on nonzero."""
    env, _ = sanitized_git_env()
    return subprocess.run(
        [str(resolve_system_git()), "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def git_repository_identity(repo: Path) -> dict[str, object]:
    """Require ``repo`` to be the canonical root of one specific Git worktree.

    ``git -C <nested>`` legitimately inherits an enclosing repository.  That is useful
    interactively but unsafe for a certificate whose path is itself part of the claim,
    so the top level must resolve to the supplied repository exactly.  Linked worktrees
    are supported by recording Git's absolute git-dir rather than assuming ``.git`` is a
    directory.
    """
    expected = repo.resolve(strict=True)
    shown = Path(git(repo, "rev-parse", "--show-toplevel"))
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    try:
        canonical_shown = shown.resolve(strict=True)
        canonical_git_dir = git_dir.resolve(strict=True)
    except OSError as exc:
        raise OSError(f"Git repository identity is not resolvable: {exc}") from exc
    if canonical_shown != expected:
        raise OSError(f"repository root mismatch: supplied={expected}, git={canonical_shown}")
    return {
        "repo": str(expected),
        "show_toplevel": str(shown),
        "canonical_toplevel": str(canonical_shown),
        "absolute_git_dir": str(git_dir),
        "canonical_git_dir": str(canonical_git_dir),
    }


def load_inventory(repo: Path) -> dict[str, object]:
    return json.loads((repo / INVENTORY_REL).read_text())


_UNTRUSTED_ENV_PREFIXES = ("PYTHON", "PYTEST", "LD_", "DYLD_", "COVERAGE")
_EXPLICIT_PYTEST_PLUGINS = (
    "pytest_asyncio.plugin",
    "anyio.pytest_plugin",
    "_hypothesis_pytestplugin",
)
_REQUIRED_LAUNCH_FLAGS = (
    "isolated",
    "no_site",
    "safe_path",
    "dont_write_bytecode",
    "pycache_prefix_devnull",
)
_RUNTIME_HASH_SCHEMA = "export-track1-no-site-runtime-tree/v2"


def sanitized_python_env() -> tuple[dict[str, str], list[str]]:
    """Return an inherited environment with interpreter/import injection removed."""
    removed = sorted(key for key in os.environ if key.upper().startswith(_UNTRUSTED_ENV_PREFIXES))
    env = {key: value for key, value in os.environ.items() if key not in removed}
    # Defense in depth alongside ``python -I -B``; values are fixed by the receipt.
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env, removed


def trusted_launch_flags() -> dict[str, bool]:
    return {
        "isolated": bool(sys.flags.isolated),
        "no_site": bool(sys.flags.no_site),
        "safe_path": bool(sys.flags.safe_path),
        "dont_write_bytecode": bool(sys.dont_write_bytecode),
        "pycache_prefix_devnull": sys.pycache_prefix == os.devnull,
    }


def trusted_launch_isolated() -> bool:
    flags = trusted_launch_flags()
    return all(flags[name] for name in _REQUIRED_LAUNCH_FLAGS)


def derived_venv_site_packages(python: Path) -> Path:
    """Derive the venv tree from the invoked path without resolving its base-Python link."""
    invoked = Path(os.path.abspath(python))
    venv = invoked.parent.parent
    if not (venv / "pyvenv.cfg").is_file():
        raise OSError(f"interpreter is not invoked through a venv: {invoked}")
    site_packages = (
        venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / ("site-packages")
    )
    if not site_packages.is_dir() or site_packages.is_symlink():
        raise OSError(f"derived venv site-packages is missing or symlinked: {site_packages}")
    return site_packages


def complete_tree_sha256(root: Path) -> str:
    """Hash every directory, regular-file byte, native module, pyc, and symlink target.

    Stable permission/type metadata is included; mutable timestamps and ownership are not,
    so the digest is deterministic across byte-identical installations.
    """
    digest = hashlib.sha256()
    resolved_root = root.resolve(strict=True)

    def framed(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def walk(directory: Path, relative: Path) -> None:
        entries = sorted(os.scandir(directory), key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            rel = relative / entry.name
            st = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(st.st_mode)
            framed(rel.as_posix().encode("utf-8", "surrogateescape"))
            framed(mode.to_bytes(4, "big"))
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(entry.path)
                if os.path.isabs(target):
                    raise OSError(f"absolute symlink in runtime tree: {entry.path} -> {target}")
                try:
                    resolved_target = Path(entry.path).resolve(strict=True)
                    resolved_relative = resolved_target.relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise OSError(
                        f"runtime symlink is dangling, cyclic, or escapes the runtime root: "
                        f"{entry.path} -> {target}"
                    ) from exc
                framed(b"symlink")
                framed(os.fsencode(target))
                # The referent is also reached and byte-hashed through its ordinary
                # in-root directory entry. Binding its resolved relative name here
                # makes the link relationship itself unambiguous.
                framed(resolved_relative.as_posix().encode("utf-8", "surrogateescape"))
            elif stat.S_ISDIR(st.st_mode):
                framed(b"directory")
                walk(Path(entry.path), rel)
            elif stat.S_ISREG(st.st_mode):
                framed(b"file")
                framed(st.st_size.to_bytes(8, "big"))
                with open(entry.path, "rb", buffering=0) as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            else:
                raise OSError(f"unsupported entry in runtime tree: {entry.path}")

    root_stat = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise OSError(f"runtime root is not a directory: {root}")
    framed(b"runtime-root-directory")
    framed(stat.S_IMODE(root_stat.st_mode).to_bytes(4, "big"))
    walk(root, Path())
    return digest.hexdigest()


def complete_python_runtime_identity(python: Path, repo: Path) -> tuple[bool, dict[str, object]]:
    """Hash the entire no-site bootstrap's third-party executable surface."""
    try:
        site_packages = derived_venv_site_packages(python)
        runtime_sha = complete_tree_sha256(site_packages)
    except OSError as exc:
        return False, {"error": str(exc), "launch_flags": trusted_launch_flags()}
    return True, {
        "site_packages_layout": (
            f"venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        ),
        "runtime_hash_schema": _RUNTIME_HASH_SCHEMA,
        "complete_runtime_sha256": runtime_sha,
        "launch_flags": trusted_launch_flags(),
        "operator_tcb": (
            "trusted operator and uncompromised pre-launch interpreter/dependency "
            "integrity, repository metadata, stdlib/native host, and toolchain; "
            "continuous same-UID races are outside the certificate boundary"
        ),
    }


def trusted_launch_policy() -> dict[str, object]:
    """Location-independent policy frozen in acceptance metadata.

    Actual interpreter, Git, and dependency-tree identities are run evidence, not
    portable manifest constants.  The candidate freezes this algorithm and ``uv.lock``;
    certification records the observed identities and requires byte equality pre/post.
    """
    return {
        "schema": "export-track1-trusted-launch-policy/v1",
        "required_python_flags": {name: True for name in _REQUIRED_LAUNCH_FLAGS},
        "runtime_hash_schema": _RUNTIME_HASH_SCHEMA,
        "runtime_root": (
            f"venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        ),
        "runtime_tree": (
            "all directory/file/symlink entries, modes, regular-file bytes, and symlink "
            "targets, hashed as raw per-run bytes for strict pre/post mutation detection"
        ),
        "site_processing": "disabled (-S); .pth bytes remain included in the per-run digest",
        "bytecode": (
            "writes disabled (-B) and cache prefix pinned to /dev/null; ordinary "
            "__pycache__ is neither loaded nor written"
        ),
        "git": "fixed /usr/bin/git or /bin/git; all inherited GIT_* removed",
        "operator_tcb": (
            "trusted operator and uncompromised pre-launch host, repository metadata, "
            "interpreter/dependencies, and toolchain; the certificate proves truthful "
            "execution, not resistance to a user controlling those prerequisites or "
            "continuous same-UID replacement races"
        ),
    }


def check_complete_runtime_trust(
    inventory: dict[str, object],
    identity_ok: bool,
    identity: dict[str, object],
    cli_trusted: str | None,
) -> Check:
    """Capture the raw dependency tree and optionally compare an operator host pin.

    A host digest is intentionally never read from the checked-in inventory: native
    wheels, pyc filenames, and editable-path bytes make that value host-specific.  The
    mandatory guarantee is exact pre/post equality; this optional CLI pin can add a
    reviewed host-local trust root without contaminating portable candidate metadata.
    """
    del inventory
    expected = cli_trusted
    actual = identity.get("complete_runtime_sha256")
    if expected is None:
        ok = bool(identity_ok and isinstance(actual, str) and len(actual) == 64)
        detail = (
            f"captured raw no-site runtime digest {actual}; pre-launch dependency integrity "
            "is operator TCB and the digest must remain identical post-run"
        )
    elif isinstance(expected, str) and len(expected) == 64:
        ok = bool(identity_ok and actual == expected)
        detail = f"complete venv site-packages actual={actual} expected={expected}"
    else:
        ok = False
        detail = f"invalid operator runtime trust digest: {expected!r}"
    return Check(
        "complete no-site Python runtime baseline is captured",
        ok,
        detail,
        {"python_runtime": identity, "trust_root": expected},
    )


def check_git_runtime_boundary(identity: dict[str, object] | None = None) -> Check:
    """Record the fixed Git binary; pre-launch integrity is an explicit operator TCB."""
    try:
        actual = identity or git_runtime_identity()
    except OSError as exc:
        return Check("fixed system Git identity is captured", False, str(exc), {})
    path = actual.get("realpath")
    digest = actual.get("sha256")
    ok = bool(
        isinstance(path, str)
        and Path(path) in {candidate.resolve() for candidate in _SYSTEM_GIT_CANDIDATES}
        and isinstance(digest, str)
        and len(digest) == 64
    )
    return Check(
        "fixed system Git identity is captured",
        ok,
        f"actual={actual}; pre-launch toolchain integrity is operator TCB",
        {"actual": actual, "operator_tcb": "pre-launch system Git integrity"},
    )


def check_repository_identity(repo: Path, identity: dict[str, object] | None = None) -> Check:
    """Fail closed if the supplied checkout is merely nested inside another repo."""
    try:
        actual = identity or git_repository_identity(repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        return Check("canonical Git repository identity is exact", False, str(exc), {})
    expected = str(repo.resolve())
    ok = (
        actual.get("repo") == expected
        and actual.get("canonical_toplevel") == expected
        and isinstance(actual.get("canonical_git_dir"), str)
        and bool(actual.get("canonical_git_dir"))
    )
    return Check(
        "canonical Git repository identity is exact",
        ok,
        f"repository identity={actual}",
        {"actual": actual},
    )


def check_index_entry_flags(repo: Path) -> Check:
    """Reject index entries hidden behind assume-unchanged/skip-worktree semantics.

    With ``git ls-files -v``, an ordinary cached entry carries the exact ``H`` tag.
    Lowercase tags and every other tag have alternate index semantics and are refused.
    This complements normal porcelain cleanliness; under the trusted-operator boundary
    it is not intended to defeat malicious repository configuration/stat-cache forgery.
    """
    try:
        raw = git(repo, "ls-files", "-v", "-z")
    except subprocess.CalledProcessError as exc:
        return Check("Git index entries have ordinary H flags", False, str(exc), {})
    records = [record for record in raw.split("\0") if record]
    ambiguous = [
        record for record in records if len(record) < 3 or record[0] != "H" or record[1] != " "
    ]
    ok = bool(records) and not ambiguous
    return Check(
        "Git index entries have ordinary H flags",
        ok,
        f"tracked={len(records)} ambiguous={len(ambiguous)}"
        + (f"; first={ambiguous[0]!r}" if ambiguous else ""),
        {
            "tracked_count": len(records),
            "ambiguous": ambiguous,
            "listing_sha256": hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest(),
        },
    )


# ---------------------------------------------------------------------------
# Gate 1 — identity: HEAD is EXACTLY the operator-named commit
# ---------------------------------------------------------------------------


def check_head(repo: Path, expect_head: str) -> Check:
    """The operator names the candidate; Git is the truth. No hash is hardcoded here."""
    want = expect_head.strip().lower()
    if len(want) != 40 or not all(c in "0123456789abcdef" for c in want):
        return Check(
            "HEAD is the operator-named candidate commit",
            False,
            f"--expect-head must be a full 40-hex sha; got {expect_head!r}",
            {"expected": expect_head, "actual": None},
        )
    head = git(repo, "rev-parse", "HEAD").lower()
    return Check(
        "HEAD is the operator-named candidate commit",
        head == want,
        f"HEAD={head} (operator expected {want})",
        {"expected": want, "actual": head},
    )


# ---------------------------------------------------------------------------
# Gate 2 — pristine: no tracked modifications AND no untracked files
# ---------------------------------------------------------------------------


def check_base_sha(repo: Path, base_sha: str) -> Check:
    """The scope base must be a REAL, exact, 40-hex commit distinct from HEAD.

    Independent verification demonstrated a GREEN bypass: a symbolic base (``HEAD``)
    makes the git-derived range empty, so an empty inventory 'covers' it while
    binding NOTHING. Fail closed on anything but a resolvable full sha that is not
    HEAD itself."""
    want = base_sha.strip().lower()
    if len(want) != 40 or not all(c in "0123456789abcdef" for c in want):
        return Check(
            "scope base is an exact 40-hex commit",
            False,
            f"base_sha must be a full 40-hex sha (symbolic refs are a bypass); got {base_sha!r}",
            {"base_sha": base_sha},
        )
    try:
        resolved = git(repo, "rev-parse", f"{want}^{{commit}}").lower()
    except subprocess.CalledProcessError:
        return Check(
            "scope base is an exact 40-hex commit",
            False,
            f"base_sha does not resolve to a commit: {want}",
            {"base_sha": want},
        )
    head = git(repo, "rev-parse", "HEAD").lower()
    ok = resolved == want and resolved != head
    return Check(
        "scope base is an exact 40-hex commit",
        ok,
        f"base={want} resolved={resolved} head={head}"
        + ("" if ok else " — base must resolve to itself and differ from HEAD"),
        {"base_sha": want, "resolved": resolved, "head": head},
    )


def check_worktree_pristine(repo: Path) -> Check:
    """A commit only certifies what is on disk if the worktree adds nothing to it.

    ``--untracked-files=all`` lists untracked files individually (not collapsed to a
    directory), so an untracked file cannot hide inside an untracked directory.
    """
    porcelain = git(repo, "status", "--porcelain", "--untracked-files=all")
    entries = [ln for ln in porcelain.splitlines() if ln.strip()]
    tracked = [ln for ln in entries if not ln.startswith("??")]
    untracked = [ln for ln in entries if ln.startswith("??")]
    return Check(
        "worktree is pristine (no tracked edits, no untracked files)",
        not entries,
        f"{len(tracked)} tracked change(s), {len(untracked)} untracked file(s)"
        + (f"; first: {entries[0]!r}" if entries else ""),
        {
            "tracked_changes": tracked,
            "untracked_files": untracked,
            "porcelain_sha256": hashlib.sha256(porcelain.encode()).hexdigest(),
        },
    )


# ---------------------------------------------------------------------------
# Gate 3 — binding: a COMPLETE hash inventory over the Git-derived recovery scope
# ---------------------------------------------------------------------------


def recovery_scope(repo: Path, base_sha: str) -> set[str]:
    """The recovery's on-disk file scope, derived from GIT (``base..HEAD``).

    ``--diff-filter=d`` drops DELETIONS (a deleted file has no on-disk bytes to bind);
    deletions are bound SEPARATELY by ``recovery_deletions`` + a tombstone list, so an
    undeclared deletion can never slip through the inventory silently (C9-06). Deriving
    scope from Git is what makes ADDITIONAL detection real: the inventory is checked
    against the repository's own account of what the recovery changed, so it cannot
    silently under-declare.
    """
    out = git(repo, "diff", "--name-only", "--diff-filter=d", f"{base_sha}..HEAD")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def recovery_deletions(repo: Path, base_sha: str) -> set[str]:
    """The paths DELETED between ``base`` and HEAD (``--diff-filter=D``)."""
    out = git(repo, "diff", "--name-only", "--diff-filter=D", f"{base_sha}..HEAD")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def interpreter_identity() -> dict[str, object]:
    """The ACTUAL interpreter running this receipt — path, realpath, version, and (when
    readable) SHA-256 (C9-06: no ``--python`` override, so the recorded interpreter is
    the one that runs the proof suites)."""
    exe = Path(sys.executable)
    real = exe.resolve()
    sha = None
    try:
        sha = hashlib.sha256(real.read_bytes()).hexdigest()
    except OSError:
        sha = None
    return {
        "executable": str(exe),
        "realpath": str(real),
        "version": sys.version.split()[0],
        "sha256": sha,
    }


def check_interpreter_identity_unchanged(initial: dict[str, object]) -> Check:
    """Re-read and bind the exact interpreter after governed execution."""
    final = interpreter_identity()
    ok = final == initial
    return Check(
        "post-run: interpreter identity unchanged",
        ok,
        f"initial={initial}; final={final}",
        {"initial": initial, "final": final},
    )


def check_base_ancestry(repo: Path, base_sha: str) -> Check:
    """The scope base must be an ANCESTOR of HEAD (C9-06): a base that is not an
    ancestor cannot define a meaningful ``base..HEAD`` scope."""
    try:
        env, _ = sanitized_git_env()
        subprocess.run(
            [
                str(resolve_system_git()),
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                base_sha,
                "HEAD",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return Check(
            "scope base is an ancestor of HEAD",
            False,
            f"base {base_sha} is not an ancestor of HEAD",
            {"base_sha": base_sha},
        )
    return Check(
        "scope base is an ancestor of HEAD", True, f"{base_sha} is an ancestor of HEAD", {}
    )


def check_no_undeclared_deletions(repo: Path, inventory: dict[str, object], base_sha: str) -> Check:
    """Every path deleted in ``base..HEAD`` must be declared as an explicit tombstone in
    the inventory's ``deletions`` list (C9-06): an undeclared deletion — a file the
    recovery removed without recording it — fails closed."""
    declared = inventory.get("deletions", [])
    declared_set = {str(x) for x in declared} if isinstance(declared, list) else set()
    actual = recovery_deletions(repo, base_sha)
    undeclared = sorted(actual - declared_set)
    stale = sorted(declared_set - actual)
    ok = not undeclared and not stale
    return Check(
        "deletions are declared exactly (no undeclared/stale tombstones)",
        ok,
        f"deleted={sorted(actual)}; undeclared={undeclared}; stale_tombstones={stale}",
        {"deleted": sorted(actual), "undeclared": undeclared, "stale": stale},
    )


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open an absolute directory path component-by-component without following links."""
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise OSError("path must be absolute and normalized")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, flags | nofollow, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags | nofollow, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fd_is_within(fd: int, ancestor_fd: int) -> bool:
    """Compare directory inode ancestry using pinned descriptors, not path strings."""
    ancestor = os.fstat(ancestor_fd)
    cur = os.dup(fd)
    try:
        while True:
            here = os.fstat(cur)
            if (here.st_dev, here.st_ino) == (ancestor.st_dev, ancestor.st_ino):
                return True
            parent = os.open(
                "..",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
                dir_fd=cur,
            )
            parent_stat = os.fstat(parent)
            if (parent_stat.st_dev, parent_stat.st_ino) == (here.st_dev, here.st_ino):
                os.close(parent)
                return False
            os.close(cur)
            cur = parent
    finally:
        os.close(cur)


def _evidence_fd_is_outside_repo(evidence_fd: int, repo: Path) -> bool:
    """Re-evaluate the pinned directory's *current* ancestry.

    A directory fd cannot be retargeted through a pathname symlink, but a same-user
    process can rename the opened directory itself into the checkout. Rechecking the
    descriptor immediately around the atomic write closes that practical race. As with
    every same-UID filesystem check, an attacker able to race arbitrary renames at
    instruction granularity is outside this receipt's trust boundary.
    """
    if os.fstat(evidence_fd).st_nlink == 0:
        return False
    repo_fd = _open_directory_chain(repo.resolve(), create=False)
    try:
        return not _fd_is_within(evidence_fd, repo_fd)
    finally:
        os.close(repo_fd)


def secure_evidence_directory(evidence_dir: Path, repo: Path) -> tuple[Check, int | None]:
    """Open/create and pin a non-symlink evidence directory outside ``repo``.

    The returned directory fd is the write authority.  Callers retain it through the
    final atomic ``openat``/``renameat`` write, so renaming or retargeting a pathname
    after this check cannot redirect the receipt.
    """
    if not evidence_dir.is_absolute():
        return (
            Check(
                "evidence dir is absolute, outside the checkout, and not a symlink",
                False,
                f"evidence_dir={evidence_dir} is not absolute",
                {"evidence_dir": str(evidence_dir)},
            ),
            None,
        )
    # Avoid creating even an empty output directory inside the checkout.  This is an
    # early refusal only; the descriptor/inode ancestry check below remains authoritative
    # for aliasing and races.
    try:
        evidence_dir.relative_to(repo.resolve())
    except ValueError:
        pass
    else:
        return (
            Check(
                "evidence dir is absolute, outside the checkout, and not a symlink",
                False,
                f"evidence_dir={evidence_dir} is lexically inside the checkout",
                {"evidence_dir": str(evidence_dir), "inside_repo": True},
            ),
            None,
        )
    evidence_fd: int | None = None
    repo_fd: int | None = None
    try:
        evidence_fd = _open_directory_chain(evidence_dir, create=True)
        repo_fd = _open_directory_chain(repo.resolve(), create=False)
        inside = _fd_is_within(evidence_fd, repo_fd)
        if inside:
            os.close(evidence_fd)
            evidence_fd = None
        return (
            Check(
                "evidence dir is absolute, outside the checkout, and not a symlink",
                not inside,
                f"evidence_dir={evidence_dir} (inside_repo={inside}; opened O_NOFOLLOW)",
                {"evidence_dir": str(evidence_dir), "inside_repo": inside},
            ),
            evidence_fd,
        )
    except OSError as exc:
        if evidence_fd is not None:
            os.close(evidence_fd)
        return (
            Check(
                "evidence dir is absolute, outside the checkout, and not a symlink",
                False,
                f"refused evidence directory {evidence_dir}: {exc}",
                {"evidence_dir": str(evidence_dir), "error": str(exc)},
            ),
            None,
        )
    finally:
        if repo_fd is not None:
            os.close(repo_fd)


def check_evidence_dir_outside(evidence_dir: Path | None, repo: Path) -> Check:
    """Validate through pinned directory descriptors; close the probe fd afterward."""
    if evidence_dir is None:
        return Check(
            "evidence dir is absolute, outside the checkout, and not a symlink",
            True,
            "no --evidence-dir supplied (nothing written into the tree)",
            {},
        )
    check, fd = secure_evidence_directory(evidence_dir, repo)
    if fd is not None:
        os.close(fd)
    return check


def check_interpreter_trust(
    inventory: dict[str, object],
    interpreter: dict[str, object],
    cli_trusted: str | None,
) -> Check:
    """A trust root MUST be supplied and the ACTUAL running interpreter's realpath sha256
    MUST equal it (C9-06 P5-3, owner round): recording whatever interpreter ran the
    receipt is not proof — an attacker-controlled launching interpreter can fabricate
    proof output. The trust root is the operator-supplied ``--trusted-interpreter-sha256``
    (preferred) or the inventory's ``trusted_interpreter_sha256``. With NEITHER supplied,
    the certification FAILS CLOSED (no more green-with-untrusted-interpreter)."""
    expected = cli_trusted or inventory.get("trusted_interpreter_sha256")
    actual = interpreter.get("sha256")
    if not expected:
        return Check(
            "interpreter matches an explicit operator trust root",
            False,
            "NO trust root supplied — pass --trusted-interpreter-sha256 <hex> (or declare "
            "trusted_interpreter_sha256 in the inventory). Recording the interpreter is "
            f"not trust; the running realpath is {interpreter.get('realpath')} "
            f"(sha256={actual}).",
            {"interpreter": interpreter, "trust_root": None},
        )
    ok = isinstance(expected, str) and bool(actual) and actual == expected
    return Check(
        "interpreter matches an explicit operator trust root",
        ok,
        f"actual={actual} expected={expected}"
        + ("" if ok else " — the running interpreter is NOT the trusted one"),
        {"interpreter": interpreter, "trust_root": expected},
    )


def check_inventory_binding(repo: Path, inventory: dict[str, object], base_sha: str) -> list[Check]:
    """Reject MISSING, ADDITIONAL, STALE, or CHANGED entries.

    The inventory necessarily EXCLUDES ITSELF (a file cannot state its own hash). That
    is sound here because Gates 1+2 already bind every tracked byte — including the
    inventory's — to the operator-named commit.
    """
    checks: list[Check] = []
    files_obj = inventory.get("files")
    assert isinstance(files_obj, dict), "inventory 'files' must be an object"
    declared: dict[str, str] = {str(k): str(v) for k, v in files_obj.items()}

    try:
        scope = recovery_scope(repo, base_sha)
    except subprocess.CalledProcessError as exc:
        return [
            Check(
                "recovery scope derived from git",
                False,
                f"cannot derive scope from {base_sha}..HEAD: {exc.stderr.strip()}",
                {"base_sha": base_sha},
            )
        ]

    scope.discard(INVENTORY_REL)  # excludes-self (documented above)

    checks.append(
        Check(
            "recovery scope is non-empty",
            bool(scope),
            f"{len(scope)} file(s) in {base_sha}..HEAD"
            + ("" if scope else " — an empty scope binds nothing and certifies nothing"),
            {"scope_count": len(scope)},
        )
    )
    additional = sorted(scope - set(declared))
    stale = sorted(set(declared) - scope)
    checks.append(
        Check(
            "inventory covers the git-derived recovery scope exactly",
            not additional and not stale,
            f"{len(declared)} declared vs {len(scope)} in scope; "
            f"additional(unbound)={additional}; stale(not-in-scope)={stale}",
            {
                "declared_count": len(declared),
                "scope_count": len(scope),
                "additional_unbound": additional,
                "stale_not_in_scope": stale,
                "base_sha": base_sha,
            },
        )
    )

    missing: list[str] = []
    changed: list[dict[str, str]] = []
    for rel, expected in sorted(declared.items()):
        path = repo / rel
        if not path.is_file():
            missing.append(rel)
            continue
        actual = sha256_file(path)
        if actual != expected:
            changed.append({"path": rel, "expected": expected, "actual": actual})
    checks.append(
        Check(
            "every inventory entry exists and is byte-unchanged",
            not missing and not changed,
            f"{len(declared) - len(missing) - len(changed)}/{len(declared)} verified; "
            f"missing={missing}; changed={[c['path'] for c in changed]}",
            {"missing": missing, "changed": changed, "verified": len(declared)},
        )
    )
    return checks


# ---------------------------------------------------------------------------
# Gate 4 — OPTIONAL external evidence: SHA-256 pinned, NEVER executed
# ---------------------------------------------------------------------------


def check_external_evidence(inventory: dict[str, object]) -> list[Check]:
    """External evidence files (the 104-case probe among them) are OPTIONAL and
    NON-AUTHORITATIVE (owner policy). When the inventory declares them, their bytes
    are verified so the evidence trail is tamper-evident; when it declares none,
    that is a recorded fact, not a failure. This receipt NEVER executes external
    evidence and holds no expected result matrix for it."""
    declared = inventory.get("external_evidence")
    if not declared:
        return [
            Check(
                "external evidence (optional, non-authoritative)",
                True,
                "none declared — external evidence is optional by owner policy",
                {"declared": []},
            )
        ]
    assert isinstance(declared, list), "inventory 'external_evidence' must be a list"
    checks: list[Check] = []
    for entry in declared:
        assert isinstance(entry, dict)
        path = Path(str(entry["path"]))
        expected = str(entry["sha256"])
        if not path.is_absolute():
            checks.append(
                Check(
                    f"external evidence bytes pinned: {path.name}",
                    False,
                    f"external evidence must be an ABSOLUTE path (it lives outside the "
                    f"repo by definition); got {path}",
                    {"path": str(path), "expected": expected, "actual": None},
                )
            )
            continue
        if not path.is_file():
            checks.append(
                Check(
                    f"external evidence bytes pinned: {path.name}",
                    False,
                    f"declared evidence missing: {path}",
                    {"path": str(path), "expected": expected, "actual": None},
                )
            )
            continue
        actual = sha256_file(path)
        checks.append(
            Check(
                f"external evidence bytes pinned: {path.name}",
                actual == expected,
                f"{actual} (expected {expected}); NEVER executed by this receipt",
                {"path": str(path), "expected": expected, "actual": actual},
            )
        )
    return checks


def check_proof_suite_binding(repo: Path, inventory: dict[str, object]) -> Check:
    """Every pinned proof suite must be a repo-relative, inventory-BOUND file that is
    NOT a declared external-evidence path.

    Independent verification demonstrated a GREEN bypass: ``expected_proof.tests``
    accepted arbitrary paths, so the byte-pinned-but-never-executed external probe
    (or any unpinned out-of-repo file) could be handed to pytest and executed. Proof
    suites must therefore live inside the repo AND inside the hash inventory."""
    expected = inventory.get("expected_proof")
    assert isinstance(expected, dict), "inventory 'expected_proof' must be an object"
    tests_obj = expected.get("tests")
    assert isinstance(tests_obj, list), "expected_proof 'tests' must be a list"
    files_obj = inventory.get("files")
    assert isinstance(files_obj, dict)
    declared = {str(k) for k in files_obj}
    evidence_paths = set()
    raw_external = inventory.get("external_evidence")
    external_entries = raw_external if isinstance(raw_external, list) else []
    for entry in external_entries:
        if isinstance(entry, dict):
            try:
                evidence_paths.add(Path(str(entry["path"])).resolve())
            except OSError:
                pass
    problems: list[str] = []
    repo_root = repo.resolve()
    if not tests_obj:
        problems.append("expected_proof.tests is EMPTY — a vacuous proof proves nothing")
    for raw in tests_obj:
        rel = str(raw)
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            problems.append(f"not repo-relative: {rel}")
            continue
        resolved = (repo / p).resolve()
        if not resolved.is_relative_to(repo_root):
            problems.append(f"escapes the repo: {rel}")
            continue
        if resolved in evidence_paths:
            problems.append(f"names declared external evidence (never executable): {rel}")
            continue
        if not resolved.is_file():
            problems.append(f"missing: {rel}")
            continue
        if rel not in declared:
            problems.append(f"not bound by the inventory: {rel}")
    return Check(
        "proof suites are repo-relative, inventory-bound, and never external evidence",
        not problems,
        (
            f"{len(tests_obj)} pinned suite(s); problems={problems}"
            if problems
            else f"{len(tests_obj)} pinned suite(s), all bound"
        ),
        {"problems": problems, "tests": [str(t) for t in tests_obj]},
    )


def gate_checks(
    repo: Path,
    expect_head: str,
    inventory: dict[str, object],
    base_sha: str,
    evidence_dir: Path | None,
    cli_trusted_interpreter: str | None = None,
    evidence_location_check: Check | None = None,
    initial_git_identity: dict[str, object] | None = None,
    initial_repo_identity: dict[str, object] | None = None,
) -> list[Check]:
    """Every fail-closed gate, evaluated BEFORE any execution step."""
    checks = [
        check_git_runtime_boundary(initial_git_identity),
        check_repository_identity(repo, initial_repo_identity),
        check_index_entry_flags(repo),
        check_head(repo, expect_head),
        check_base_sha(repo, base_sha),
        check_base_ancestry(repo, base_sha),
        check_worktree_pristine(repo),
        evidence_location_check or check_evidence_dir_outside(evidence_dir, repo),
        check_interpreter_trust(inventory, interpreter_identity(), cli_trusted_interpreter),
    ]
    checks.extend(check_inventory_binding(repo, inventory, base_sha))
    checks.append(check_no_undeclared_deletions(repo, inventory, base_sha))
    checks.extend(check_external_evidence(inventory))
    checks.append(check_proof_suite_binding(repo, inventory))
    return checks


def post_run_rechecks(
    repo: Path,
    expect_head: str,
    inventory: dict[str, object],
    base_sha: str,
    initial_git_identity: dict[str, object] | None = None,
    initial_repo_identity: dict[str, object] | None = None,
    initial_interpreter_identity: dict[str, object] | None = None,
) -> list[Check]:
    """After the proof suites execute, RE-VERIFY that nothing shifted underfoot
    (C9-06): HEAD is still the operator-named commit, the worktree is still pristine,
    the inventory bytes are still bound, and the git-derived scope is unchanged. Each is
    named ``post-run: …`` so the receipt records both the pre-run gate and its post-run
    counterpart."""
    try:
        final_git_identity = git_runtime_identity()
    except OSError as exc:
        final_git_identity = {"error": str(exc)}
    git_unchanged = initial_git_identity is not None and final_git_identity == initial_git_identity
    try:
        final_repo_identity = git_repository_identity(repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        final_repo_identity = {"error": str(exc)}
    repo_unchanged = (
        initial_repo_identity is not None and final_repo_identity == initial_repo_identity
    )
    interpreter_check = (
        check_interpreter_identity_unchanged(initial_interpreter_identity)
        if initial_interpreter_identity is not None
        else Check(
            "post-run: interpreter identity unchanged",
            False,
            "initial interpreter identity was not captured",
            {"initial": {}, "final": interpreter_identity()},
        )
    )
    checks = [
        Check(
            "post-run: fixed system Git identity unchanged",
            git_unchanged,
            f"initial={initial_git_identity}; final={final_git_identity}",
            {"initial": initial_git_identity or {}, "final": final_git_identity},
        ),
        Check(
            "post-run: canonical Git repository identity unchanged",
            repo_unchanged,
            f"initial={initial_repo_identity}; final={final_repo_identity}",
            {"initial": initial_repo_identity or {}, "final": final_repo_identity},
        ),
        interpreter_check,
        Check(
            "post-run: Git index entries still have ordinary H flags",
            check_index_entry_flags(repo).ok,
            "re-checked after proof execution",
            {},
        ),
        Check(
            "post-run: HEAD unchanged",
            check_head(repo, expect_head).ok,
            "re-checked after proof execution",
            {},
        ),
        Check(
            "post-run: worktree still pristine",
            check_worktree_pristine(repo).ok,
            "re-checked after proof execution",
            {},
        ),
    ]
    binding = check_inventory_binding(repo, inventory, base_sha)
    checks.append(
        Check(
            "post-run: inventory bytes + scope unchanged",
            all(c.ok for c in binding),
            "re-checked the complete inventory binding after proof execution",
            {},
        )
    )
    # C9-06 P5-1: re-pin the EXTERNAL evidence too — a hash-bound proof could overwrite it
    # between the pre-run gate and now.
    external = check_external_evidence(inventory)
    checks.append(
        Check(
            "post-run: external evidence still byte-pinned",
            all(c.ok for c in external),
            "re-verified declared external evidence after proof execution",
            {},
        )
    )
    return checks


def evidence_write_boundary_check(
    repo: Path,
    evidence_fd: int,
    expect_head: str,
    inventory: dict[str, object],
    base_sha: str,
    *,
    require_candidate_integrity: bool,
    initial_git_identity: dict[str, object] | None = None,
    initial_repo_identity: dict[str, object] | None = None,
    initial_interpreter_identity: dict[str, object] | None = None,
) -> Check:
    """Re-prove location and, for a potentially GREEN receipt, candidate integrity.

    A truthful RED receipt remains useful evidence of a dirty/drifted candidate, so it
    may be written outside the repo even though its recorded candidate checks are red.
    """
    try:
        outside = _evidence_fd_is_outside_repo(evidence_fd, repo)
        head_ok = check_head(repo, expect_head).ok
        pristine = check_worktree_pristine(repo).ok
        binding = check_inventory_binding(repo, inventory, base_sha)
        external = check_external_evidence(inventory)
        current_git_identity = git_runtime_identity()
        git_unchanged = (
            initial_git_identity is not None and current_git_identity == initial_git_identity
        )
        current_repo_identity = git_repository_identity(repo)
        repo_unchanged = (
            initial_repo_identity is not None and current_repo_identity == initial_repo_identity
        )
        current_interpreter_identity = interpreter_identity()
        interpreter_unchanged = (
            initial_interpreter_identity is not None
            and current_interpreter_identity == initial_interpreter_identity
        )
        index_ok = check_index_entry_flags(repo).ok
        bound = (
            all(check.ok for check in [*binding, *external])
            and git_unchanged
            and repo_unchanged
            and interpreter_unchanged
            and index_ok
        )
        integrity_ok = head_ok and pristine and bound
        ok = outside and (integrity_ok or not require_candidate_integrity)
        detail = (
            f"outside_repo={outside}, head_unchanged={head_ok}, "
            f"worktree_pristine={pristine}, bindings_intact={bound}"
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        ok = False
        detail = f"write-boundary recheck failed closed: {exc}"
        outside = head_ok = pristine = bound = git_unchanged = False
        repo_unchanged = interpreter_unchanged = index_ok = False
    return Check(
        "evidence write boundary remains outside repo with candidate intact",
        ok,
        detail,
        {
            "outside_repo": outside,
            "head_unchanged": head_ok,
            "worktree_pristine": pristine,
            "bindings_intact": bound,
            "git_unchanged": git_unchanged,
            "repository_identity_unchanged": repo_unchanged,
            "interpreter_identity_unchanged": interpreter_unchanged,
            "index_flags_ordinary": index_ok,
            "candidate_integrity_required": require_candidate_integrity,
        },
    )


# ---------------------------------------------------------------------------
# Execution step (reached ONLY when every gate passed)
# ---------------------------------------------------------------------------


def controlled_python_paths(python: Path, repo: Path) -> list[Path]:
    """Paths explicitly importable under the controlled no-site launch; no .pth."""
    repo_root = repo.resolve()
    paths = [derived_venv_site_packages(python)]
    packages = repo_root / "packages"
    if packages.is_dir():
        for package in sorted(packages.iterdir()):
            source = package / "src"
            if source.is_dir():
                resolved = source.resolve()
                resolved.relative_to(repo_root)
                paths.append(resolved)
    scripts = repo_root / "scripts"
    if scripts.is_dir():
        resolved_scripts = scripts.resolve()
        resolved_scripts.relative_to(repo_root)
        paths.append(resolved_scripts)
    return paths


def controlled_pytest_command(python: Path, repo: Path, arguments: list[str]) -> list[str]:
    paths = [str(path) for path in controlled_python_paths(python, repo)]
    bootstrap = (
        "import sys;"
        f"sys.path[:0]={paths!r};"
        "import pytest;"
        "raise SystemExit(pytest.main(sys.argv[1:]))"
    )
    plugin_args = [item for plugin in _EXPLICIT_PYTEST_PLUGINS for item in ("-p", plugin)]
    return [
        str(python),
        "-I",
        "-S",
        "-P",
        "-B",
        "-X",
        f"pycache_prefix={os.devnull}",
        "-c",
        bootstrap,
        *plugin_args,
        *arguments,
    ]


def run_proof_tests(
    repo: Path,
    inventory: dict[str, object],
    python: Path,
    trusted_runtime_sha256: str | None = None,
) -> Check:
    """Run the pinned proof suites — exact counts required.

    Parse the structured junit XML (``-q`` terminal scraping is unreliable); the exit
    code is checked too — a suite that errors during collection can still emit XML.
    """
    expected = inventory.get("expected_proof")
    assert isinstance(expected, dict)
    tests_obj = expected["tests"]
    assert isinstance(tests_obj, list), "expected_proof 'tests' must be a list"
    tests = [str(t) for t in tests_obj]
    want_passed = int(str(expected["passed"]))
    want_failed = int(str(expected["failed"]))

    runtime_ok, runtime_identity = complete_python_runtime_identity(python, repo)
    runtime_trust = check_complete_runtime_trust(
        inventory, runtime_ok, runtime_identity, trusted_runtime_sha256
    )
    if not runtime_trust.ok:
        return Check(
            f"proof suites == {want_passed} passed / {want_failed} failed",
            False,
            f"{runtime_trust.detail}; refusing to execute proof suites",
            {
                "python_runtime": runtime_identity,
                "runtime_trust": runtime_trust.data,
                "executed": False,
            },
        )

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as jf:
        junit = Path(jf.name)
    env, removed_env = sanitized_python_env()
    argv = controlled_pytest_command(
        python,
        repo,
        ["-p", "no:cacheprovider", *tests, "-q", f"--junitxml={junit}"],
    )
    proc = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, check=False)
    try:
        root = ET.parse(junit).getroot()
        ts = root if root.tag == "testsuite" else root.find("testsuite")
    except ET.ParseError:
        ts = None
    junit.unlink(missing_ok=True)
    if ts is None:
        # A crashed proof process (SIGSEGV, OOM-kill) leaves no parseable junit; that
        # is a RED check with structured evidence, never an unhandled traceback.
        return Check(
            f"proof suites == {want_passed} passed / {want_failed} failed",
            False,
            f"proof run produced no parseable junit (pytest exit {proc.returncode}); "
            "the run crashed or was killed — refusing to infer any result",
            {
                "argv": argv[1:],
                "returncode": proc.returncode,
                "junit": None,
                "python_runtime": runtime_identity,
                "removed_environment_keys": removed_env,
            },
        )
    total = int(ts.get("tests", "-1"))
    failed = int(ts.get("failures", "-1"))
    errors = int(ts.get("errors", "-1"))
    skipped = int(ts.get("skipped", "-1"))
    passed = total - failed - errors - skipped
    post_runtime_ok, post_runtime_identity = complete_python_runtime_identity(python, repo)
    runtime_unchanged = post_runtime_ok and post_runtime_identity.get(
        "complete_runtime_sha256"
    ) == runtime_identity.get("complete_runtime_sha256")
    ok = (
        passed == want_passed
        and failed == want_failed
        and errors == 0
        and skipped == 0
        and proc.returncode == 0
        and runtime_unchanged
    )
    return Check(
        f"proof suites == {want_passed} passed / {want_failed} failed",
        ok,
        f"{passed} passed, {failed} failed, {errors} errors, {skipped} skipped "
        f"(junit tests={total}), pytest exit {proc.returncode}",
        {
            "argv": argv[1:],
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "junit_tests": total,
            "python_runtime": runtime_identity,
            "post_run_python_runtime": post_runtime_identity,
            "runtime_unchanged": runtime_unchanged,
            "removed_environment_keys": removed_env,
        },
    )


# ---------------------------------------------------------------------------
# Inventory authoring (convenience; NOT a proof — see module docstring)
# ---------------------------------------------------------------------------


def emit_inventory(repo: Path, base_sha: str, template: dict[str, object]) -> dict[str, object]:
    scope = sorted(recovery_scope(repo, base_sha) - {INVENTORY_REL})
    out = dict(template)
    out["base_sha"] = base_sha
    out["excludes_self"] = INVENTORY_REL
    out["files"] = {rel: sha256_file(repo / rel) for rel in scope}
    # Explicit deletion tombstones (C9-06): every path removed in base..HEAD, so the
    # certification's no-undeclared-deletion gate has an exact declared set.
    out["deletions"] = sorted(recovery_deletions(repo, base_sha) - {INVENTORY_REL})
    # Declare the authoring interpreter's realpath sha256 as the trust root (C9-06 P5-3,
    # owner round): the operator reviews this hash and the certification refuses to run
    # green under any other interpreter. The operator may override it with
    # --trusted-interpreter-sha256 at certification time.
    out["trusted_interpreter_sha256"] = interpreter_identity().get("sha256")
    # Host-specific dependency and Git hashes do not belong in portable inventory.
    # They are captured as run evidence and compared pre/post by certification.
    out.pop("trusted_pytest_package_sha256", None)
    out.pop("trusted_pytest_runtime_sha256", None)
    out.pop("trusted_python_runtime_sha256", None)
    out.pop("trusted_git", None)
    out["trusted_launch_policy"] = trusted_launch_policy()
    return out


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def render(checks: list[Check], head: str) -> bool:
    print("=" * 78)
    print("CANDIDATE RECEIPT — Export Track-1 recovery")
    print(f"CANDIDATE COMMIT: {head}")
    print("=" * 78)
    all_ok = True
    for c in checks:
        all_ok &= c.ok
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}\n         {c.detail}")
    print("=" * 78)
    print(f"RECEIPT: {'GREEN — all checks exact' if all_ok else 'RED — one or more checks failed'}")
    return all_ok


def evidence_payload(
    head: str,
    base_sha: str,
    checks: list[Check],
    green: bool,
    *,
    interpreter: dict[str, object] | None = None,
    python_runtime: dict[str, object] | None = None,
    git_identity: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "export-track1-recovery-receipt/v2",
        "candidate_sha": head,
        "base_sha": base_sha,
        "green": green,
        "interpreter": interpreter or {},
        "observed_launch_identity": {
            "interpreter": interpreter or {},
            "python_runtime": python_runtime or {},
            "git": git_identity or {},
            "policy": trusted_launch_policy(),
        },
        "pre_run_checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data}
            for c in checks
            if not c.name.startswith("post-run:")
        ],
        "post_run_checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data}
            for c in checks
            if c.name.startswith("post-run:")
        ],
        "checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data} for c in checks
        ],
    }
    return payload


def write_evidence_at(
    directory_fd: int,
    filename: str,
    payload: dict[str, object],
    *,
    repo: Path | None = None,
) -> None:
    """Atomically write evidence relative to the already-vetted pinned directory fd.

    When ``repo`` is supplied, descriptor ancestry is checked before creating the
    temporary file, immediately before publication, and immediately afterward. A
    failed post-publication check removes the output before raising.
    """
    if repo is not None and not _evidence_fd_is_outside_repo(directory_fd, repo):
        raise OSError("evidence directory moved inside checkout before write")
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temp_name = f".{filename}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        finally:
            os.close(fd)
        raise
    os.close(fd)
    published = False
    try:
        if repo is not None and not _evidence_fd_is_outside_repo(directory_fd, repo):
            raise OSError("evidence directory moved inside checkout before publication")
        os.replace(
            temp_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        published = True
        os.fsync(directory_fd)
        if repo is not None and not _evidence_fd_is_outside_repo(directory_fd, repo):
            raise OSError("evidence directory moved inside checkout during publication")
    except BaseException:
        try:
            os.unlink(filename if published else temp_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None, repo: Path | None = None) -> int:
    """``repo`` is injectable so the mutation suite can drive the REAL gate logic against
    a synthetic repository — no patching of this module's globals, no test-only branch."""
    injected_test_repo = repo is not None
    repo = repo or REPO
    if not injected_test_repo and not trusted_launch_isolated():
        print(
            "refusing certification: launch with python -I -S -P -B "
            "-X pycache_prefix=/dev/null; the invoked "
            "interpreter/stdlib/native host is an operator trust prerequisite",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(description="Export Track-1 recovery candidate receipt.")
    parser.add_argument(
        "--expect-head",
        required=True,
        help="REQUIRED: the exact 40-hex candidate commit the operator intends to certify. "
        "No SHA is hardcoded in this script; Git is compared against this value.",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="AUTHORING ONLY (with --emit-inventory): the base for git-derived scope. In "
        "the CERTIFICATION path the base is ALWAYS the inventory's frozen 'base_sha' and "
        "this flag is rejected — a candidate cannot re-scope itself.",
    )
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help="Write a structured JSON receipt here (MUST be an absolute path OUTSIDE the "
        "checkout).",
    )
    parser.add_argument(
        "--emit-inventory",
        action="store_true",
        help="AUTHORING: regenerate the inventory from the tree (never emits a receipt).",
    )
    parser.add_argument(
        "--trusted-interpreter-sha256",
        default=None,
        help="REQUIRED for a GREEN certification unless the inventory declares "
        "'trusted_interpreter_sha256': the sha256 of the trusted interpreter's realpath. "
        "The receipt refuses to certify green under any other interpreter (the recorded "
        "identity alone is not trust).",
    )
    parser.add_argument(
        "--trusted-python-runtime-sha256",
        default=None,
        help="OPTIONAL host-local pin for the raw derived site-packages tree digest; "
        "portable inventory never stores this host-specific value.",
    )
    args = parser.parse_args(argv)

    inventory = load_inventory(repo)

    if args.emit_inventory:
        # Authoring is VISIBLY separate from certification: it writes the inventory and
        # exits 0 WITHOUT ever running the gates, executing the proof suites, or writing a
        # certification receipt — it can never emit a green certification (C9-06).
        base_sha = args.base or str(inventory["base_sha"])
        fresh = emit_inventory(repo, base_sha, inventory)
        (repo / INVENTORY_REL).write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        files_map = fresh["files"]
        assert isinstance(files_map, dict)
        print(f"wrote {INVENTORY_REL} ({len(files_map)} files) [authoring — NOT a receipt]")
        return 0

    # Certification: the base is the inventory's frozen base — never operator-overridable.
    if args.base is not None:
        print(
            "--base is authoring-only; the certification path always uses the inventory's "
            "frozen base_sha (refusing to re-scope the candidate).",
            file=sys.stderr,
        )
        return 2
    base_sha = str(inventory["base_sha"])
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    interpreter = interpreter_identity()
    initial_git_identity: dict[str, object]
    try:
        initial_git_identity = git_runtime_identity()
    except OSError as exc:
        initial_git_identity = {"error": str(exc)}
    initial_repo_identity: dict[str, object]
    try:
        initial_repo_identity = git_repository_identity(repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        initial_repo_identity = {"error": str(exc)}
    # Pin the output directory once.  Every later write is fd-relative, so a pathname
    # race cannot retarget evidence into the checkout after the gate.
    evidence_fd: int | None = None
    evidence_location_check: Check | None = None
    if evidence_dir is not None:
        evidence_location_check, evidence_fd = secure_evidence_directory(evidence_dir, repo)

    # ---- Gates first: every one fails CLOSED, and none of them execute anything. ----
    checks = gate_checks(
        repo,
        args.expect_head,
        inventory,
        base_sha,
        evidence_dir,
        args.trusted_interpreter_sha256,
        evidence_location_check,
        initial_git_identity,
        initial_repo_identity,
    )
    head = git(repo, "rev-parse", "HEAD")

    if all(c.ok for c in checks):
        # Gates green -> the tree is the named commit, complete, and any declared
        # external evidence is byte-verified. Only now is it safe to execute, with the
        # ACTUAL running interpreter (no override).
        checks.append(
            run_proof_tests(
                repo,
                inventory,
                Path(sys.executable),
                args.trusted_python_runtime_sha256,
            )
        )
        # Post-run: prove nothing shifted underfoot during execution (C9-06).
        checks.extend(
            post_run_rechecks(
                repo,
                args.expect_head,
                inventory,
                base_sha,
                initial_git_identity,
                initial_repo_identity,
                interpreter,
            )
        )
    else:
        checks.append(
            Check(
                "execution step (proof suites)",
                False,
                "NOT RUN — a fail-closed gate above did not pass; the receipt refuses to "
                "execute (and thus cannot report a green result) on an unverified tree.",
                {"executed": False},
            )
        )

    if (
        evidence_fd is not None
        and evidence_location_check is not None
        and evidence_location_check.ok
    ):
        candidate_was_green = all(check.ok for check in checks)
        checks.append(
            evidence_write_boundary_check(
                repo,
                evidence_fd,
                args.expect_head,
                inventory,
                base_sha,
                require_candidate_integrity=candidate_was_green,
                initial_git_identity=initial_git_identity,
                initial_repo_identity=initial_repo_identity,
                initial_interpreter_identity=interpreter,
            )
        )
    green = render(checks, head)
    # Write evidence ONLY to the vetted resolved real path, and ONLY when the location
    # gate passed — never through a (possibly retargeted) symlink or into the checkout,
    # on the green OR the red path (C9-06 P5-2).
    if (
        evidence_fd is not None
        and evidence_location_check is not None
        and evidence_location_check.ok
        and checks[-1].name == "evidence write boundary remains outside repo with candidate intact"
        and checks[-1].ok
    ):
        try:
            recorded_runtime = next(
                (
                    check.data["python_runtime"]
                    for check in checks
                    if isinstance(check.data.get("python_runtime"), dict)
                ),
                None,
            )
            if not isinstance(recorded_runtime, dict):
                _, recorded_runtime = complete_python_runtime_identity(Path(sys.executable), repo)
            write_evidence_at(
                evidence_fd,
                "candidate-receipt.json",
                evidence_payload(
                    head,
                    base_sha,
                    checks,
                    green,
                    interpreter=interpreter,
                    python_runtime=recorded_runtime,
                    git_identity=initial_git_identity,
                ),
                repo=repo,
            )
            after_write = evidence_write_boundary_check(
                repo,
                evidence_fd,
                args.expect_head,
                inventory,
                base_sha,
                require_candidate_integrity=green,
                initial_git_identity=initial_git_identity,
                initial_repo_identity=initial_repo_identity,
                initial_interpreter_identity=interpreter,
            )
            if not after_write.ok:
                try:
                    os.unlink("candidate-receipt.json", dir_fd=evidence_fd)
                except FileNotFoundError:
                    pass
                print(
                    f"evidence removed: post-write integrity failed: {after_write.detail}",
                    file=sys.stderr,
                )
                green = False
        except OSError as exc:
            print(f"evidence NOT written: write-boundary check failed: {exc}", file=sys.stderr)
            green = False
    elif evidence_dir is not None:
        print(
            "evidence NOT written: the --evidence-dir location gate failed (symlink or "
            "inside the checkout); refusing to write a receipt there.",
            file=sys.stderr,
        )
    if evidence_fd is not None:
        os.close(evidence_fd)
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
