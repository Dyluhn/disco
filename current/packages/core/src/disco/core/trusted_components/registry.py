"""TrustedComponentRegistry — host-side component storage (spec §2, WO-TC1).

Components are LITERAL FILES under ``registry_data/<name>/<version>/`` (D2 —
never string-builders, so hash pins are stable), each with a ``manifest.json``
validated by :class:`TrustedComponentManifest` at load. The registry is the
ONLY source of hash pins and manifests (D1): nothing verification-related is
ever read from a workspace copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import _VERSION_SHAPE, REQUIREMENT_RE, TrustedComponentManifest, _safe_relpath

# Directories copied into a workspace on install. probe/ is deliberately NOT
# here (D3 — host-run only), and manifest.json is host-side truth (D1).
_INSTALL_DIRS = ("core", "config")


def parse_version(version: str) -> tuple[int, int, int]:
    """Strict X.Y.Z → int tuple, leading zeros rejected ("01.2.3" would be a
    second identity for "1.2.3"). Registry authoring errors raise here (the
    tripwire runs this over every shipped manifest)."""
    m = _VERSION_SHAPE.match(version)
    if m is None:
        raise ValueError(f"not a X.Y.Z version (leading zeros rejected): {version!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


@dataclass(frozen=True)
class Requirement:
    """One parsed `requires` edge (D5: names a COMPONENT, not a capability)."""

    name: str
    op: str  # ">=" (major.minor floor) | "==" (exact X.Y.Z)
    version: tuple[int, ...]

    def satisfied_by(self, name: str, version: str) -> bool:
        if name != self.name:
            return False
        installed = parse_version(version)
        if self.op == "==":
            return installed == self.version
        # ">=X.Y" compares on the first two segments (D10).
        return installed[:2] >= self.version[:2]


def parse_requirement(entry: str) -> Requirement:
    """Parse a requires edge. SAME regex as the manifest validator (single
    source of truth): `>=` takes exactly X.Y, `==` exactly X.Y.Z — a drifted
    parser once accepted `>=X.Y.Z` and silently dropped the patch digit."""
    m = REQUIREMENT_RE.match(entry)
    if m is None:
        raise ValueError(f"invalid requires entry (D10 grammar): {entry!r}")
    if m.group("eq") is not None:
        ver = tuple(int(p) for p in m.group("eq").split("."))
        return Requirement(name=m.group("name"), op="==", version=ver)
    ver = tuple(int(p) for p in m.group("ge").split("."))
    return Requirement(name=m.group("name"), op=">=", version=ver)


@dataclass(frozen=True)
class LoadedComponent:
    """One (name, version) resolved from the registry, files readable."""

    manifest: TrustedComponentManifest
    root: Path

    def read_file(self, relpath: str) -> bytes:
        """Read a registry file, confined to the component dir. Rejects any
        '..'/absolute path outright (even ones that would round-trip back
        inside), rejects SYMLINKS anywhere on the relative chain (a symlink in
        OUR registry is an authoring error, and following one would copy
        out-of-tree bytes into workspaces under a benign name), and re-checks
        the resolved target — belt and braces."""
        _safe_relpath(relpath)
        probe = self.root
        for segment in relpath.split("/"):
            probe = probe / segment
            if probe.is_symlink():
                raise ValueError(f"registry path is a symlink (authoring error): {relpath!r}")
        target = probe.resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"manifest path must be normalized and traversal-free: {relpath!r}")
        return target.read_bytes()

    def install_tree(self) -> dict[str, bytes]:
        """Everything a workspace install copies: core/, config/, GUIDE.md.
        Excludes probe/ (D3) and manifest.json (D1). EVERY read goes through
        read_file's confinement + symlink rejection — the adversarial review
        proved a symlink under core/ used to smuggle out-of-tree bytes into
        the returned tree under a benign key."""
        tree: dict[str, bytes] = {}
        for dirname in _INSTALL_DIRS:
            base = self.root / dirname
            if base.is_symlink():
                raise ValueError(f"registry dir is a symlink (authoring error): {dirname!r}")
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_symlink():
                    rel = path.relative_to(self.root).as_posix()
                    raise ValueError(f"registry path is a symlink (authoring error): {rel!r}")
                if path.is_file():
                    rel = path.relative_to(self.root).as_posix()
                    tree[rel] = self.read_file(rel)
        guide = self.root / self.manifest.guide
        if guide.is_file() or guide.is_symlink():
            tree[self.manifest.guide] = self.read_file(self.manifest.guide)
        return tree

    def probe_source(self) -> bytes | None:
        """Host-side use only — never part of install_tree (D3)."""
        if self.manifest.probe is None:
            return None
        return self.read_file(self.manifest.probe)


class TrustedComponentRegistry:
    """Loads components from a registry_data directory tree."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else Path(__file__).parent / "registry_data"

    def names(self) -> frozenset[str]:
        if not self._root.is_dir():
            return frozenset()
        return frozenset(
            p.name for p in self._root.iterdir() if p.is_dir() and self.versions(p.name)
        )

    def versions(self, name: str) -> tuple[str, ...]:
        """Available versions for `name`, ascending by semantic order. Only
        directories that parse as X.Y.Z AND carry a manifest.json count."""
        base = self._root / name
        if not base.is_dir():
            return ()
        found: list[tuple[tuple[int, int, int], str]] = []
        for p in base.iterdir():
            if not p.is_dir() or not (p / "manifest.json").is_file():
                continue
            try:
                found.append((parse_version(p.name), p.name))
            except ValueError:
                continue
        return tuple(v for _, v in sorted(found))

    def get(self, name: str, version: str | None = None) -> LoadedComponent | None:
        """Resolve (name, version); version=None → highest available. Missing →
        None, never raises. A manifest that fails validation DOES raise — a
        corrupt registry is an authoring bug, not a lookup miss."""
        available = self.versions(name)
        if not available:
            return None
        if version is None:
            version = available[-1]
        elif version not in available:
            return None
        root = self._root / name / version
        manifest = TrustedComponentManifest.model_validate_json(
            (root / "manifest.json").read_bytes()
        )
        if manifest.name != name or manifest.version != version:
            raise ValueError(
                f"registry layout mismatch: dir {name}/{version} holds manifest "
                f"{manifest.name}/{manifest.version}"
            )
        return LoadedComponent(manifest=manifest, root=root)

    def catalog_lines(self) -> str:
        """'- <name> <version> — <when_to_use>' per component (latest version),
        for the tool schema (catalog-in-schema doctrine)."""
        lines = []
        for name in sorted(self.names()):
            comp = self.get(name)
            if comp is not None:
                lines.append(f"- {name} {comp.manifest.version} — {comp.manifest.when_to_use}")
        return "\n".join(lines)

    @classmethod
    def default(cls) -> TrustedComponentRegistry:
        return cls()
