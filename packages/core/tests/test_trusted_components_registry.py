"""TC1 — TrustedComponentRegistry: loading, versions, D10 grammar, path safety,
and the pins tripwire over the REAL shipped registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.core.trusted_components import TrustedComponentManifest
from disco.core.trusted_components.registry import (
    LoadedComponent,
    TrustedComponentRegistry,
    parse_requirement,
    parse_version,
)
from disco.core.trusted_components.verify import pin

# ---- version / requirement grammar (D10) -----------------------------------


def test_parse_version_semantic_not_lexicographic() -> None:
    assert parse_version("1.10.0") > parse_version("1.9.9")


@pytest.mark.parametrize("bad", ["1.0", "1", "v1.0.0", "1.0.0-rc1", "1..0", ""])
def test_parse_version_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_version(bad)


def test_requirement_ge_compares_major_minor() -> None:
    req = parse_requirement("database-kit>=1.2")
    assert req.satisfied_by("database-kit", "1.2.0")
    assert req.satisfied_by("database-kit", "1.10.0")
    assert req.satisfied_by("database-kit", "2.0.0")
    assert not req.satisfied_by("database-kit", "1.1.9")
    assert not req.satisfied_by("other-kit", "9.9.9")


def test_requirement_eq_is_exact() -> None:
    req = parse_requirement("auth-kit==1.0.0")
    assert req.satisfied_by("auth-kit", "1.0.0")
    assert not req.satisfied_by("auth-kit", "1.0.1")


@pytest.mark.parametrize(
    "bad",
    ["auth-kit", "auth-kit>1.0", "auth-kit>=1", "auth-kit==1.0", "Auth-Kit>=1.0", "auth kit>=1.0"],
)
def test_requirement_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_requirement(bad)


# ---- registry loading over a synthetic tree ---------------------------------


def _write_component(
    root: Path,
    name: str = "demo-kit",
    version: str = "1.0.0",
    *,
    requires: list[str] | None = None,
) -> Path:
    comp = root / name / version
    (comp / "core").mkdir(parents=True)
    (comp / "config").mkdir()
    (comp / "probe").mkdir()
    core_file = comp / "core" / "demo.js"
    core_file.write_bytes(b"export const demo = 1;\n")
    (comp / "config" / "demo.config.js").write_bytes(b"export default {};\n")
    (comp / "probe" / "probe.py").write_bytes(b"print('{}')\n")
    (comp / "GUIDE.md").write_bytes(b"# demo\n")
    manifest = {
        "name": name,
        "version": version,
        "kind": "trusted_component",
        "summary": "demo",
        "when_to_use": "testing only",
        "files": {"core/demo.js": pin(core_file.read_bytes())},
        "config_surface": ["config/demo.config.js"],
        "requires": requires or [],
        "provides": ["demo"],
        "probe": "probe/probe.py",
        "guide": "GUIDE.md",
    }
    (comp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return comp


def test_get_defaults_to_highest_semantic_version(tmp_path: Path) -> None:
    _write_component(tmp_path, version="1.9.0")
    _write_component(tmp_path, version="1.10.0")
    reg = TrustedComponentRegistry(tmp_path)
    assert reg.versions("demo-kit") == ("1.9.0", "1.10.0")
    comp = reg.get("demo-kit")
    assert comp is not None and comp.manifest.version == "1.10.0"


def test_get_missing_returns_none_never_raises(tmp_path: Path) -> None:
    reg = TrustedComponentRegistry(tmp_path)
    assert reg.get("nope") is None
    _write_component(tmp_path)
    assert reg.get("demo-kit", "9.9.9") is None


def test_layout_mismatch_raises(tmp_path: Path) -> None:
    comp = _write_component(tmp_path)
    raw = json.loads((comp / "manifest.json").read_text())
    raw["version"] = "2.0.0"  # dir says 1.0.0
    (comp / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="layout mismatch"):
        TrustedComponentRegistry(tmp_path).get("demo-kit", "1.0.0")


def test_install_tree_excludes_probe_and_manifest(tmp_path: Path) -> None:
    _write_component(tmp_path)
    comp = TrustedComponentRegistry(tmp_path).get("demo-kit")
    assert comp is not None
    tree = comp.install_tree()
    assert set(tree) == {"core/demo.js", "config/demo.config.js", "GUIDE.md"}
    assert comp.probe_source() == b"print('{}')\n"


@pytest.mark.parametrize(
    "evil",
    [
        "../other-kit/1.0.0/core/demo.js",  # genuine escape
        "../../demo-kit/1.0.0/manifest.json",  # round-trips back inside — still rejected
        "/etc/passwd",
        "core/../../../etc/passwd",
    ],
)
def test_read_file_confined_to_component_dir(tmp_path: Path, evil: str) -> None:
    _write_component(tmp_path)
    comp = TrustedComponentRegistry(tmp_path).get("demo-kit")
    assert comp is not None
    with pytest.raises(ValueError, match="relative|traversal-free"):
        comp.read_file(evil)


def test_catalog_lines_render_when_to_use(tmp_path: Path) -> None:
    _write_component(tmp_path)
    line = TrustedComponentRegistry(tmp_path).catalog_lines()
    assert line == "- demo-kit 1.0.0 — testing only"


def test_junk_dirs_are_ignored(tmp_path: Path) -> None:
    _write_component(tmp_path)
    (tmp_path / "demo-kit" / "not-a-version").mkdir()
    (tmp_path / "stray-file").write_text("x")
    reg = TrustedComponentRegistry(tmp_path)
    assert reg.names() == frozenset({"demo-kit"})
    assert reg.versions("demo-kit") == ("1.0.0",)


# ---- the tripwire over the REAL shipped registry (D2) ------------------------


def _shipped_components() -> list[LoadedComponent]:
    reg = TrustedComponentRegistry.default()
    out: list[LoadedComponent] = []
    for name in sorted(reg.names()):
        for version in reg.versions(name):
            comp = reg.get(name, version)
            assert comp is not None
            out.append(comp)
    return out


def test_registry_pins_match_shipped_bytes() -> None:
    """Every shipped manifest pin equals the sha256 of the shipped bytes, every
    core file is pinned, and probe/guide exist. Pins cannot rot (spec §1.5)."""
    for comp in _shipped_components():
        m = comp.manifest
        for relpath, expected in m.files.items():
            assert pin(comp.read_file(relpath)) == expected, f"{m.name}: stale pin {relpath}"
        core_files = {
            p for p in comp.install_tree() if p.startswith("core/")
        }
        assert core_files == set(m.files), f"{m.name}: unpinned/extra core files"
        assert comp.probe_source() is not None or m.probe is None
        assert comp.read_file(m.guide), f"{m.name}: missing guide"


def test_shipped_requires_edges_resolve_in_registry() -> None:
    """Authoring sanity: every shipped requires edge names a component the
    registry can satisfy (spec §6 admissibility)."""
    reg = TrustedComponentRegistry.default()
    for comp in _shipped_components():
        for entry in comp.manifest.requires:
            req = parse_requirement(entry)
            versions = reg.versions(req.name)
            assert versions, f"{comp.manifest.name} requires unknown {req.name}"
            assert any(
                req.satisfied_by(req.name, v) for v in versions
            ), f"{comp.manifest.name}: no shipped version satisfies {entry}"


def test_manifest_requires_full_validation() -> None:
    with pytest.raises(ValueError):
        TrustedComponentManifest.model_validate(
            {
                "name": "x-kit",
                "version": "1.0.0",
                "kind": "trusted_component",
                "summary": "s",
                "when_to_use": "w",
                "files": {"core/a.js": "sha256:short"},
                "guide": "GUIDE.md",
            }
        )


def test_symlink_under_core_is_rejected_everywhere(tmp_path: Path) -> None:
    """Adversarial-review CRITICAL: a symlink under core/ used to flow through
    install_tree() unguarded, smuggling out-of-tree bytes into workspaces."""
    _write_component(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"HOST SECRET")
    comp_dir = tmp_path / "demo-kit" / "1.0.0"
    (comp_dir / "core" / "evil.js").symlink_to(secret)
    comp = TrustedComponentRegistry(tmp_path).get("demo-kit")
    assert comp is not None
    with pytest.raises(ValueError, match="symlink"):
        comp.install_tree()
    with pytest.raises(ValueError, match="symlink"):
        comp.read_file("core/evil.js")


def test_symlinked_guide_is_rejected(tmp_path: Path) -> None:
    _write_component(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"HOST SECRET")
    comp_dir = tmp_path / "demo-kit" / "1.0.0"
    (comp_dir / "GUIDE.md").unlink()
    (comp_dir / "GUIDE.md").symlink_to(secret)
    comp = TrustedComponentRegistry(tmp_path).get("demo-kit")
    assert comp is not None
    with pytest.raises(ValueError, match="symlink"):
        comp.install_tree()


def test_parse_version_rejects_leading_zeros() -> None:
    with pytest.raises(ValueError):
        parse_version("01.2.3")


def test_parse_requirement_rejects_three_segment_ge() -> None:
    """The drifted-grammar finding: >=X.Y.Z must be rejected, not truncated."""
    with pytest.raises(ValueError):
        parse_requirement("auth-kit>=1.0.5")


def test_registry_data_has_no_invisible_components() -> None:
    """Vacuous-tripwire guard (review finding 4): every directory under the
    REAL registry_data must be a loadable component with >=1 valid version —
    a typo'd version dir or missing manifest.json must fail HERE, not silently
    vanish from names() and every downstream tripwire."""
    reg = TrustedComponentRegistry.default()
    root = reg._root
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_file():
            assert entry.name == "README.md", f"stray file in registry_data: {entry.name}"
            continue
        versions = reg.versions(entry.name)
        assert versions, (
            f"registry_data/{entry.name} has NO loadable version "
            "(typo'd dir or missing manifest.json?)"
        )
        version_dirs = {p.name for p in entry.iterdir() if p.is_dir()}
        assert version_dirs == set(versions), (
            f"registry_data/{entry.name}: dirs {sorted(version_dirs)} != loadable {list(versions)}"
        )
        for q in entry.rglob("*"):
            assert not q.is_symlink(), f"symlink in shipped registry: {q}"


def test_shipped_config_surface_files_exist() -> None:
    """Review finding 5: a declared config_surface path that does not ship is a
    dangling affordance — the GUIDE will tell the model to edit a file that
    does not exist."""
    for comp in _shipped_components():
        tree = comp.install_tree()
        for rel in comp.manifest.config_surface:
            assert rel in tree, f"{comp.manifest.name}: config_surface {rel} not shipped"
