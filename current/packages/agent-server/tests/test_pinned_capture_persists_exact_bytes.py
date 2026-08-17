"""Positive control: a pinned capture persists the user's exact bytes (F-21 proof 2/3).

Certified-lane seed 621005 lost EVERYTHING — no tree, no versions, no manifest —
because the capture re-resolved an executor teardown had already taken.
`c595d289` pins the session at the boundary. This proves what the pin is FOR:
workspace content and the browser screenshot reach the authoritative destination
byte-identically, and remain readable once the session is gone.

Exercises the real `snapshot_workspace` against its documented `_WorkspaceIO`
surface. The fake follows the walker's actual contract: `list_dir` returns PLAIN
names, and files are told from directories by probing `read_file` first and
falling back to `list_dir` (archive.py:565).
"""

from __future__ import annotations

import hashlib

import pytest
from disco.tools.projects import snapshot_workspace

_INDEX = b"<!doctype html><h1>Rollback recovered 900029</h1>\n"
_SHOT = b"\x89PNG\r\n\x1a\n" + b"pinned-capture-proof" * 8


class _NotAFile(Exception):
    """What a real sandbox raises when read_file targets a directory."""


class _Session:
    def __init__(self, tree: dict[str, bytes] | None = None) -> None:
        self.tree = {
            "index.html": _INDEX,
            ".pmx/screenshots/0001-navigate.png": _SHOT,
            "src/app.js": b"console.log('work');\n",
        } if tree is None else tree
        self.destroyed = False

    def _alive(self) -> None:
        if self.destroyed:
            raise AssertionError("read from a destroyed session")

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        prefix = "" if path in ("", ".", "/workspace") else path.rstrip("/") + "/"
        if prefix and not any(k.startswith(prefix) for k in self.tree):
            raise _NotAFile(f"not a directory: {path}")
        names: list[str] = []
        for key in self.tree:
            if not key.startswith(prefix):
                continue
            head = key[len(prefix):].split("/", 1)[0]
            if head and head not in names:
                names.append(head)          # PLAIN names — the walker's contract
        return names

    async def read_file(self, path: str) -> bytes:
        self._alive()
        rel = path.removeprefix("/workspace/").lstrip("/")
        if rel not in self.tree:
            raise _NotAFile(f"is a directory: {path}")
        return self.tree[rel]

    async def write_file(self, path: str, data: bytes) -> None:  # pragma: no cover
        self.tree[path] = data


@pytest.mark.asyncio
async def test_content_and_screenshot_persist_byte_identically(tmp_path):
    session = _Session()
    dest = tmp_path / "workspace"
    result = await snapshot_workspace(session, dest)
    session.destroyed = True  # the pin's purpose: capture ran while it was alive

    shot = (dest / ".pmx/screenshots/0001-navigate.png").read_bytes()
    assert hashlib.sha256(shot).hexdigest() == hashlib.sha256(_SHOT).hexdigest(), (
        "the screenshot the evidence collector reads must be the exact captured bytes"
    )
    assert (dest / "index.html").read_bytes() == _INDEX
    assert result.file_count >= 3


@pytest.mark.asyncio
async def test_the_tree_is_readable_after_the_session_is_destroyed(tmp_path):
    """Proof 3: evidence binds to persisted bytes, never to a live sandbox."""
    session = _Session()
    dest = tmp_path / "workspace"
    await snapshot_workspace(session, dest)
    session.destroyed = True  # teardown
    assert (dest / ".pmx/screenshots/0001-navigate.png").read_bytes() == _SHOT
    assert (dest / "src/app.js").read_bytes() == b"console.log('work');\n"


@pytest.mark.asyncio
async def test_an_empty_workspace_persists_no_false_evidence(tmp_path):
    """Proof 7 downstream: nothing captured must not invent a screenshot."""
    dest = tmp_path / "workspace"
    await snapshot_workspace(_Session(tree={}), dest)
    assert not (dest / ".pmx").exists()
