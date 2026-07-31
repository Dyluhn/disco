"""Subprocess / git / tool-version primitives — extracted from
:mod:`verify_export_track1_closeout`. These are the lowest-level helpers: no
other ``closeout_verify_parts`` module is a dependency of this one.

Note: ``_pytest_env`` (and the ``_SCRIPTS_DIR`` it needs) stays physically
defined in the parent module rather than here — two release_remediation tests
assert on the parent file's own SOURCE TEXT (``"os.pathsep" in src`` among
them) as a wiring guard pinning the governed pytest-lane invocation to the
entry-point file. See ``verify_export_track1_closeout.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _run(
    cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a fixed-argv command (no shell), capturing output. Never raises on nonzero
    — the CALLER reads returncode (plan §1.2: exit code captured, not inferred). ``env``
    (when given) fully replaces the child environment; None inherits this process's."""
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _is_clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain") == ""


def _tool_versions(repo: Path) -> dict[str, str]:
    versions: dict[str, str] = {}

    def _probe(key: str, cmd: list[str]) -> None:
        if shutil.which(cmd[0]) is None:
            versions[key] = "unavailable"
            return
        proc = _run(cmd, cwd=repo)
        line = (proc.stdout or proc.stderr).strip().splitlines()
        versions[key] = line[0] if line else "unknown"

    _probe("python", [sys.executable, "--version"])
    _probe("pytest", [sys.executable, "-m", "pytest", "--version"])
    _probe("git", ["git", "--version"])
    _probe("node", ["node", "--version"])
    _probe("npm", ["npm", "--version"])
    _probe("docker", ["docker", "--version"])
    return versions
