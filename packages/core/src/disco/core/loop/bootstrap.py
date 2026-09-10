"""F4 — GATED bootstrap detection (assist-tier project-type hint).

Compatibility facade: manifest detectors now live in :mod:`bootstrap_detection`.
This module re-exports the private names and keeps the public
``_detect_project_bootstrap`` entry point so existing import paths remain
unchanged.

A weak model (assist=ON) on a fresh session can waste its first few turns
hunting for the build/test/entry commands instead of acting. Assist OFF
(capable-model default) → these are NEVER called, the engine is byte-identical.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from .bootstrap_detection import (
    F4_MAX_CHARS as _F4_MAX_CHARS,
)
from .bootstrap_detection import (
    F4_MAX_SCRIPTS_PER_MANIFEST as _F4_MAX_SCRIPTS_PER_MANIFEST,
)
from .bootstrap_detection import (
    detect_package_json as _detect_package_json,
)
from .bootstrap_detection import (
    detect_pyproject_toml as _detect_pyproject_toml,
)
from .bootstrap_detection import (
    safe_read_text as _safe_read_text,
)

# Makefile: pull lines of the form `target: ...` at column 0. Body parsing is
# out of scope (the model can `file_read` the Makefile if it needs the body);
# just surface the names so the model knows what targets exist.
_F4_MAKEFILE_TARGET_RE = re.compile(r"^([A-Za-z0-9_./-]+)\s*:")


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
        for b in bins[:_F4_MAX_SCRIPTS_PER_MANIFEST]:
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
            mod = line[len("module ") :].strip()
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
        sections.append("package.json scripts:\n" + "\n".join(pkg[:_F4_MAX_SCRIPTS_PER_MANIFEST]))
    pyp = _detect_pyproject_toml(root)
    if pyp:
        scripts_text = "\n".join(pyp[:_F4_MAX_SCRIPTS_PER_MANIFEST])
        sections.append("pyproject.toml scripts:\n" + scripts_text)
    mk = _detect_makefile(root)
    if mk:
        sections.append("Makefile targets:\n" + "\n".join(mk[:_F4_MAX_SCRIPTS_PER_MANIFEST]))
    cargo = _detect_cargo_toml(root)
    if cargo:
        sections.append("Cargo.toml:\n" + "\n".join(cargo[:_F4_MAX_SCRIPTS_PER_MANIFEST]))
    gomod = _detect_go_mod(root)
    if gomod:
        sections.append("go.mod:\n" + "\n".join(gomod[:_F4_MAX_SCRIPTS_PER_MANIFEST]))

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
