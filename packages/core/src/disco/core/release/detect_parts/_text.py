"""Bounded, binary-safe text helpers shared by every content-scanning detector.

Every WHOLE-TREE content scan reads at most `_MAX_SCAN_BYTES` per file and skips
binary files (§7.11); these helpers are the single place that bound applies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ._constants import _APPKIT_FILES, _CONTAINER_NAMES, _MAX_SCAN_BYTES


def _as_text(value: str | bytes) -> str:
    """A best-effort, BOUNDED text view of a named-config file for parsing (binary
    bytes decode lossily; we only ever look for ascii markers). Capped at
    `_MAX_SCAN_BYTES` so even a pathological single config file can never force an
    unbounded decode (§7.11)."""
    if isinstance(value, bytes):
        return value[:_MAX_SCAN_BYTES].decode("utf-8", errors="ignore")
    return value[:_MAX_SCAN_BYTES]


def _scan_text(value: str | bytes) -> str:
    """A BOUNDED, binary-safe text view for WHOLE-TREE signal scanning (§7.11).

    Reads at most `_MAX_SCAN_BYTES` and returns an EMPTY view for a binary file (a
    NUL byte in the scanned head marks it binary). A marker that sits past the cap
    (an oversized text file) or inside a binary blob is therefore never read, so it
    can never reach the verdict via an unbounded whole-tree decode."""
    if isinstance(value, bytes):
        head = value[:_MAX_SCAN_BYTES]
        if b"\x00" in head:
            return ""
        return head.decode("utf-8", errors="ignore")
    return value[:_MAX_SCAN_BYTES]


def _file_text(files: Mapping[str, str | bytes], target: str) -> str | None:
    """The bounded text of the file whose normalized path is `target`, or `None`."""
    for path in files:
        if _norm(path) == target:
            return _scan_text(files[path])
    return None


def _strip_comments(text: str) -> str:
    """Best-effort removal of JS block/line comments (`/* */`, `//`) and Python line
    comments (`#`) for the fail-open-prone LITERAL checks (the `$PORT` bind, the Vite
    `outDir`, the health route). A decoy planted in a comment (`// outDir: 'dist'`,
    `# process.env.PORT`, `// '/healthz'`) then no longer satisfies the contract.

    Used ONLY for those literal scans — never for the database / framework / env-name
    scans. Comment lexing is approximate (it does not track string context), but that
    is safe HERE: a mistaken strip only removes a would-be literal, making the check
    MORE conservative (fail-closed), never fail-open."""
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    lines: list[str] = []
    for line in without_block.splitlines():
        cut = len(line)
        for marker in ("//", "#"):
            idx = line.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        lines.append(line[:cut])
    return "\n".join(lines)


def _norm(path: str) -> str:
    """Normalize a workspace path: trim, drop a leading `./` and trailing `/`."""
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def _paths(files: Mapping[str, str | bytes]) -> frozenset[str]:
    return frozenset(_norm(p) for p in files)


def _container_manifest(files: Mapping[str, str | bytes]) -> str | None:
    """The first (sorted) container manifest path, or `None`."""
    for path in sorted(files):
        if _basename(path) in _CONTAINER_NAMES:
            return _norm(path)
    return None


def _is_appkit(files: Mapping[str, str | bytes]) -> bool:
    present = _paths(files)
    return all(required in present for required in _APPKIT_FILES)
