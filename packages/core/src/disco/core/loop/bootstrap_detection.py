"""F4 — GATED bootstrap detection helpers (split from ``bootstrap.py``).

Pure, best-effort manifest detectors: read a workspace's manifest files and
emit a one-shot observation listing the build/test/entry commands they
*literally* define. No loop state, no agent calls.

The detectors are intentionally conservative: they only report commands that
are LITERALLY present in a manifest. No inference, no guessing, no hard-coded
command names beyond the well-known script keys. A model that sees this hint is
seeing the truth, not a guess.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

# Common build/test/entry script keys to surface from package.json. Kept
# narrow on purpose — listing every script invites the model to over-broaden
# its first move. Add new keys here only with a strong reason.
F4_PKG_SCRIPT_KEYS = (
    "build",
    "test",
    "start",
    "dev",
    "lint",
    "typecheck",
    "format",
    "check",
    "verify",
)

# Same idea for pyproject.toml: surface project scripts, poetry scripts, and
# the well-known tool script tables. The script names ARE the commands
# (poetry run <name>, uv run <name>, pdm run <name>, etc.).
F4_PYPROJECT_SCRIPT_TABLES = (
    ("project", "scripts"),
    ("tool", "poetry", "scripts"),
    ("tool", "pdm", "scripts"),
    ("tool", "pdm.scripts"),
)

# Cap the number of manifests / lines / chars we report so a noisy workspace
# doesn't drown the first turn in a wall of text.
F4_MAX_SCRIPTS_PER_MANIFEST = 8
F4_MAX_CHARS = 2000


def safe_read_text(path: Path, *, max_bytes: int = 64 * 1024) -> str | None:
    """Read a small text file defensively. Returns None on any failure — the
    detector is best-effort; a missing or unreadable manifest is not an error
    worth surfacing (the loop will fall through to the no-bootstrap path)."""
    try:
        data = path.read_bytes()[:max_bytes]
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def detect_package_json(workspace: Path) -> list[str]:
    """Return a sorted list of "name: command" lines from package.json scripts.
    Empty list if no scripts section or no recognizable keys."""
    text = safe_read_text(workspace / "package.json")
    if text is None:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return []
    out: list[str] = []
    for key in F4_PKG_SCRIPT_KEYS:
        cmd = scripts.get(key)
        if isinstance(cmd, str) and cmd.strip():
            out.append(f"  npm run {key}: {cmd.strip()}")
    return out


def _resolve_script_table(data: dict, table: tuple[str, ...]) -> dict | None:
    """Walk one nested table path in ``data``; return the leaf dict or None."""
    node: object = data
    for key in table:
        if not isinstance(node, dict):
            return None
        node = node.get(key)  # type: ignore[union-attr]
    return node if isinstance(node, dict) and node else None


def _format_script_entry(name: str, cmd: object) -> str | None:
    """Format one pyproject script entry; return None if it is not usable."""
    if isinstance(cmd, str) and cmd.strip():
        return f"  {name}: {cmd.strip()}"
    if isinstance(cmd, dict):
        # tool.pdm.scripts uses {cmd = "..."} sub-tables; surface the inner
        # string so the model still sees the command.
        inner = cmd.get("cmd") if isinstance(cmd.get("cmd"), str) else None
        if inner and inner.strip():
            return f"  {name}: {inner.strip()}"
    return None


def detect_pyproject_toml(workspace: Path) -> list[str]:
    """Return a list of "<runner> <name>: <cmd>" lines from pyproject.toml script
    tables. We surface the names so the model knows WHAT exists; the runner
    prefix (poetry/uv/pdm) is omitted because it's workspace-dependent and the
    model can `file_read` the manifest to confirm."""
    text = safe_read_text(workspace / "pyproject.toml")
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for table in F4_PYPROJECT_SCRIPT_TABLES:
        node = _resolve_script_table(data, table)
        if node is None:
            continue
        for name, cmd in sorted(node.items()):
            entry = _format_script_entry(name, cmd)
            if entry is not None:
                out.append(entry)
    return out