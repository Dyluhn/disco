"""Bounded relationship-integrity walk for OPC packages."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
import zipfile
from dataclasses import dataclass, field

_OPC_REL_NS = (
    "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
)


class _ForbiddenXmlDeclaration(Exception):
    pass


@dataclass(frozen=True)
class _RelationshipLimits:
    part_bytes: int
    relationships: int
    target_chars: int
    problems: int


@dataclass
class _RelationshipState:
    problems: list[str] = field(default_factory=list)
    relationship_count: int = 0


def _relationship_xml_root(data: bytes) -> ET.Element:
    guard = expat.ParserCreate()

    def reject(*_args: object) -> None:
        raise _ForbiddenXmlDeclaration

    def reject_external(*_args: object) -> int:
        raise _ForbiddenXmlDeclaration

    guard.StartDoctypeDeclHandler = reject
    guard.EntityDeclHandler = reject
    guard.ExternalEntityRefHandler = reject_external
    guard.Parse(data, True)
    return ET.fromstring(data)


def _add_problem(
    state: _RelationshipState, message: str, *, limit: int
) -> bool:
    if len(state.problems) < limit - 1:
        state.problems.append(message)
        return False
    if len(state.problems) < limit:
        state.problems.append("pptx has additional structural problems")
    return True


def _read_relationship_root(
    archive: zipfile.ZipFile,
    rels_name: str,
    limits: _RelationshipLimits,
) -> tuple[ET.Element | None, str | None]:
    info = archive.getinfo(rels_name)
    if info.file_size > limits.part_bytes:
        return (
            None,
            f"pptx relationship part {rels_name} exceeds the XML size limit "
            f"({limits.part_bytes} bytes)",
        )
    try:
        return _relationship_xml_root(archive.read(rels_name)), None
    except _ForbiddenXmlDeclaration:
        return (
            None,
            f"pptx relationship part {rels_name} contains a forbidden DTD/entity",
        )
    except (ET.ParseError, expat.ExpatError):
        return None, f"pptx relationship part {rels_name} is not valid XML"


def _target_problem(
    rel: ET.Element,
    *,
    rels_name: str,
    base: str,
    names: set[str],
    target_limit: int,
) -> str | None:
    if (rel.get("TargetMode") or "Internal") == "External":
        return None
    target = rel.get("Target") or ""
    if len(target) > target_limit:
        return (
            f"pptx relationship part {rels_name} has a target longer than the "
            f"structural-validation limit ({target_limit} characters)"
        )
    resolved = (
        target.lstrip("/")
        if target.startswith("/")
        else posixpath.normpath(posixpath.join(base, target))
    )
    if resolved in names:
        return None
    return (
        f"pptx references a missing part: {rels_name} -> {target} "
        f"(resolved {resolved})"
    )


def _relationship_problems(
    archive: zipfile.ZipFile,
    names: set[str],
    limits: _RelationshipLimits,
) -> list[str]:
    state = _RelationshipState()
    relationship_parts = sorted(
        name for name in names if name.endswith(".rels") and "_rels/" in name
    )
    for rels_name in relationship_parts:
        base = rels_name.rsplit("_rels/", 1)[0].rstrip("/")
        root, read_problem = _read_relationship_root(
            archive, rels_name, limits
        )
        if read_problem is not None:
            if _add_problem(state, read_problem, limit=limits.problems):
                return state.problems
            continue
        if root is None:
            continue
        for rel in root.iter(_OPC_REL_NS):
            state.relationship_count += 1
            if state.relationship_count > limits.relationships:
                return [
                    "pptx archive has too many relationships for structural "
                    f"validation (>{limits.relationships})"
                ]
            problem = _target_problem(
                rel,
                rels_name=rels_name,
                base=base,
                names=names,
                target_limit=limits.target_chars,
            )
            if problem is not None and _add_problem(
                state, problem, limit=limits.problems
            ):
                return state.problems
    return state.problems
