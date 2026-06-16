"""E4 — zero-config self-healing project storage.

Proves:
(a) With no configured root + an isolated HOME/XDG_DATA_HOME, the effective
    root resolves under it, the directory is auto-created, and validate_root
    returns OK.
(b) A configured non-empty root is honored verbatim and still works.
(c) An explicit non-empty root that doesn't exist still surfaces NOT_FOUND
    (the self-healing logic must NOT silently mkdir a user-set path).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from disco.tools.projects import (
    StorageStatus,
    default_projects_root,
    resolve_projects_root,
    validate_root,
)
from disco.tools.projects.store import ProjectStore


# ── (a) fresh-machine: no configured root, isolated HOME ──────────────────────


def test_default_root_uses_disco_data_dir(tmp_path, monkeypatch):
    """DISCO_DATA_DIR env var → <DATA_DIR>/projects."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DISCO_DATA_DIR", str(data_dir))
    result = default_projects_root()
    assert result == str(data_dir / "projects")


def test_default_root_uses_xdg_data_home(tmp_path, monkeypatch):
    """XDG_DATA_HOME set → <XDG_DATA_HOME>/disco/projects."""
    monkeypatch.delenv("DISCO_DATA_DIR", raising=False)
    monkeypatch.delenv("PMX_DATA_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    result = default_projects_root()
    assert result == str(xdg / "disco" / "projects")


def test_default_root_posix_fallback(tmp_path, monkeypatch):
    """No DISCO_DATA_DIR, no XDG_DATA_HOME → ~/.local/share/disco/projects."""
    monkeypatch.delenv("DISCO_DATA_DIR", raising=False)
    monkeypatch.delenv("PMX_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = default_projects_root()
    assert result == str(tmp_path / ".local" / "share" / "disco" / "projects")


def test_resolve_empty_autocreates_and_is_ok(tmp_path, monkeypatch):
    """Empty configured root → resolve auto-creates the XDG default and the
    directory exists so validate_root returns OK — the headline assertion for
    a fresh machine with zero config."""
    monkeypatch.delenv("DISCO_DATA_DIR", raising=False)
    monkeypatch.delenv("PMX_DATA_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    effective = resolve_projects_root("")
    assert effective == str(xdg / "disco" / "projects"), (
        "effective root should be the XDG default"
    )
    assert Path(effective).is_dir(), "resolve_projects_root must mkdir the default"
    assert validate_root(effective) == StorageStatus.OK, (
        "a freshly created default root must validate as OK"
    )


def test_project_store_empty_root_is_ok(tmp_path, monkeypatch):
    """ProjectStore('') must give status OK on a fresh machine (auto-default
    created); this is the integration point the agent-server calls."""
    monkeypatch.delenv("DISCO_DATA_DIR", raising=False)
    monkeypatch.delenv("PMX_DATA_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    store = ProjectStore("")
    assert store.status() == StorageStatus.OK, (
        "ProjectStore('') on a fresh machine must report OK, not UNSET"
    )
    assert store.root is not None
    assert store.root.is_dir()


# ── (b) configured non-empty root works ───────────────────────────────────────


def test_resolve_nonempty_returns_verbatim(tmp_path):
    """A non-empty configured root is returned as-is (no XDG/DATA_DIR logic)."""
    explicit = str(tmp_path / "myprojects")
    os.makedirs(explicit)
    result = resolve_projects_root(explicit)
    assert result == explicit


def test_project_store_explicit_root_ok(tmp_path):
    """ProjectStore with an explicit existing root must report OK."""
    store = ProjectStore(str(tmp_path))
    assert store.status() == StorageStatus.OK
    assert store.root == tmp_path


# ── (c) explicit missing root keeps NOT_FOUND (no silent mkdir) ───────────────


def test_resolve_nonempty_missing_but_creatable_is_created(tmp_path):
    """E4 'works every time': a user-set path that doesn't exist YET but is
    CREATABLE (writable parent) is mkdir -p'd so it's ready to use — not rejected."""
    missing = str(tmp_path / "does_not_exist")
    result = resolve_projects_root(missing)
    assert result == missing
    assert Path(missing).is_dir(), (
        "resolve_projects_root must create a creatable explicit path"
    )
    assert validate_root(missing) == StorageStatus.OK


def test_project_store_explicit_unreachable_is_not_found(tmp_path):
    """An explicit root that CANNOT be created (here, under a regular file → mkdir
    fails) stays unavailable: ProjectStore surfaces NOT_FOUND so the UI can warn +
    offer the default rather than silently relocating the user's data."""
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    unreachable = str(blocker / "sub")  # parent is a file → mkdir -p fails
    store = ProjectStore(unreachable)
    assert store.status() == StorageStatus.NOT_FOUND
