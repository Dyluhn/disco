"""Shared agent-server test support."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_projects_root(monkeypatch, tmp_path):
    """[REL-RC-J fallout] Isolate the durable ProjectStore per test.

    Without this, every scripted test that shares CID "c1" reads/writes ONE
    persistent workspace at ~/.local/share/disco/projects/c1/ — files written by
    any test in any PAST run leak into every future run (a file_write to a path
    that "already exists" is refused read_before_write, silently zeroing the
    test's productive work). The old attempt-counting finish gate masked this for
    weeks; the outcome-counting gate (REL-RC-J) surfaced it. DISCO_DATA_DIR is
    the highest-priority root override (default_projects_root, projects/store.py).
    """
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path / "disco-data"))
