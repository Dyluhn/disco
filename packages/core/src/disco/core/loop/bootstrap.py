"""F4 — GATED bootstrap detection (assist-tier project-type hint).

Extracted from `engine.py` (god-file decomposition, Wave 1). Pure, best-effort
manifest detectors: read a workspace's manifest files and emit a one-shot
observation listing the build/test/entry commands they *literally* define. No
loop state, no agent calls.

A weak model (assist=ON) on a fresh session can waste its first few turns
hunting for the build/test/entry commands instead of acting. Assist OFF
(capable-model default) → these are NEVER called, the engine is byte-identical.

The detectors are intentionally conservative: they only report commands that
are LITERALLY present in a manifest. No inference, no guessing, no hard-coded
command names beyond the well-known script keys. A model that sees this hint is
seeing the truth, not a guess.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

# Common build/test/entry script keys to surface from package.json. Kept
# narrow on purpose — listing every script invites the model to over-broaden
# its first move. Add new keys here only with a strong reason.
_F4_PKG_SCRIPT_KEYS = (
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
_F4_PYPROJECT_SCRIPT_TABLES = (
    ("project", "scripts"),
    ("tool", "poetry", "scripts"),
    ("tool", "pdm", "scripts"),
    ("tool", "pdm.scripts"),
)

# Makefile: pull lines of the form `target: ...` at column 0. Body parsing is
# out of scope (the model can `file_read` the Makefile if it needs the body);
# just surface the names so the model knows what targets exist.
_F4_MAKEFILE_TARGET_RE = re.compile(r"^([A-Za-z0-9_./-]+)\s*:")

# Cap the number of manifests / lines / chars we report so a noisy workspace
# doesn't drown the first turn in a wall of text. The model only needs
# ENOUGH to know what's available — over-reporting is worse than under.
_F4_MAX_SCRIPTS_PER_MANIFEST = 8
_F4_MAX_CHARS = 2000


def _safe_read_text(path: Path, *, max_bytes: int = 64 * 1024) -> str | None:
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


def _detect_package_json(workspace: Path) -> list[str]:
    """Return a sorted list of "name: command" lines from package.json scripts.
    Empty list if no scripts section or no recognizable keys."""
    text = _safe_read_text(workspace / "package.json")
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
    for key in _F4_PKG_SCRIPT_KEYS:
        cmd = scripts.get(key)
        if isinstance(cmd, str) and cmd.strip():
            out.append(f"  npm run {key}: {cmd.strip()}")
    return out


def _detect_pyproject_toml(workspace: Path) -> list[str]:
    """Return a list of "<runner> <name>: <cmd>" lines from pyproject.toml script
    tables. We surface the names so the model knows WHAT exists; the runner
    prefix (poetry/uv/pdm) is omitted because it's workspace-dependent and the
    model can `file_read` the manifest to confirm."""
    text = _safe_read_text(workspace / "pyproject.toml")
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for table in _F4_PYPROJECT_SCRIPT_TABLES:
        node: object = data
        for key in table:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)  # type: ignore[union-attr]
        if isinstance(node, dict) and node:
            for name, cmd in sorted(node.items()):
                if isinstance(cmd, str) and cmd.strip():
                    out.append(f"  {name}: {cmd.strip()}")
                elif isinstance(cmd, dict):
                    # tool.pdm.scripts uses {cmd = "..."} sub-tables; surface
                    # the inner string so the model still sees the command.
                    inner = cmd.get("cmd") if isinstance(cmd.get("cmd"), str) else None
                    if inner and inner.strip():
                        out.append(f"  {name}: {inner.strip()}")
    return out


def _detect_makefile(workspace: Path) -> list[str]:
    """Return a sorted list of Makefile target names. Body parsing is out of
    scope (model can read the file)."""
    text = _safe_read_text(workspace / "Makefile")
    if text is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _F4_MAKEFILE_TARGET_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(f"  make {name}")
    return out


def _detect_cargo_toml(workspace: Path) -> list[str]:
    """Surface Cargo [[bin]] names + the package name. Cargo's `cargo run` /
    `cargo test` / `cargo build` are universal — we report the BIN name so
    the model can call `cargo run --bin <name>` without guesswork."""
    text = _safe_read_text(workspace / "Cargo.toml")
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    pkg = data.get("package")
    if isinstance(pkg, dict):
        name = pkg.get("name")
        if isinstance(name, str) and name.strip():
            out.append(f"  cargo build/test/run: package = {name.strip()}")
    bins = data.get("bin")
    if isinstance(bins, list):
        for b in bins[: _F4_MAX_SCRIPTS_PER_MANIFEST]:
            if isinstance(b, dict):
                bn = b.get("name")
                if isinstance(bn, str) and bn.strip():
                    out.append(f"  cargo run --bin {bn.strip()}")
    return out


def _detect_go_mod(workspace: Path) -> list[str]:
    """Surface the module path from go.mod. `go test ./...` / `go run .` /
    `go build` are universal; the module path is the only piece the model
    can't infer."""
    text = _safe_read_text(workspace / "go.mod")
    if text is None:
        return []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("module "):
            mod = line[len("module "):].strip()
            if mod:
                return [f"  go test/build/run: module = {mod}"]
    return []


def _detect_project_bootstrap(workspace_path: str | os.PathLike[str]) -> str | None:
    """F4: scan a workspace for known manifests and produce a one-shot
    bootstrap observation listing detected build/test/entry commands.

    Returns a `<system-reminder>`-formatted string the engine can hand to the
    model as a system message, or None if the workspace is empty / unreadable
    / has no recognized manifests. The function is pure: it reads files, it
    does not mutate state, it does not call the agent.

    The IRON RULE: this function MUST NOT report a command that isn't
    literally present in a manifest. No inference, no fallbacks, no
    "probably X". The whole point of F4 is to give a weak model a TRUE
    starting point, not a plausible-sounding lie.
    """
    if not workspace_path:
        return None
    try:
        root = Path(workspace_path)
    except (TypeError, ValueError):
        return None
    if not root.is_dir():
        return None

    sections: list[str] = []
    pkg = _detect_package_json(root)
    if pkg:
        sections.append("package.json scripts:\n" + "\n".join(pkg[: _F4_MAX_SCRIPTS_PER_MANIFEST]))
    pyp = _detect_pyproject_toml(root)
    if pyp:
        scripts_text = "\n".join(pyp[: _F4_MAX_SCRIPTS_PER_MANIFEST])
        sections.append("pyproject.toml scripts:\n" + scripts_text)
    mk = _detect_makefile(root)
    if mk:
        sections.append("Makefile targets:\n" + "\n".join(mk[: _F4_MAX_SCRIPTS_PER_MANIFEST]))
    cargo = _detect_cargo_toml(root)
    if cargo:
        sections.append("Cargo.toml:\n" + "\n".join(cargo[: _F4_MAX_SCRIPTS_PER_MANIFEST]))
    gomod = _detect_go_mod(root)
    if gomod:
        sections.append("go.mod:\n" + "\n".join(gomod[: _F4_MAX_SCRIPTS_PER_MANIFEST]))

    if not sections:
        return None

    body = "\n".join(sections)
    if len(body) > _F4_MAX_CHARS:
        body = (
            body[:_F4_MAX_CHARS]
            + "\n  … (truncated — file_read the manifest files directly for the full body)"
        )
    return (
        "<system-reminder>\n"
        "F4 bootstrap: detected project commands from workspace manifests. "
        "These are the build/test/entry commands the manifest literally defines "
        "— use them rather than guessing. Read the manifest directly if you need "
        "the full body.\n"
        "\n"
        f"{body}\n"
        "</system-reminder>"
    )
