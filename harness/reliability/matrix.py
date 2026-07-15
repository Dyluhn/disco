"""Typed loader and fail-closed validation for the reliability claim matrix."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode

PROOFS = frozenset({"hermetic", "live", "fresh_device"})
KINDS = frozenset({"build_soak", "fresh_device", "generic", "playwright", "vitest"})


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys.

    PyYAML's default loader silently keeps the last duplicate. In a promotion
    matrix that can change a suite kind, command, or proof without leaving any
    visible validation error, so duplicate keys are invalid evidence.
    """


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(
                f"duplicate YAML mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class Claim:
    id: str
    surface: str
    proof: str
    target: int
    description: str


@dataclass(frozen=True)
class Suite:
    id: str
    proof: str
    kind: str
    cwd: str
    timeout_s: float
    memory_gib: float
    units: int
    command: tuple[str, ...]
    claims: tuple[str, ...]
    requires_env: tuple[str, ...]
    environment: dict[str, str]
    provider_evidence: bool
    provider_conversation_manifest: bool


@dataclass(frozen=True)
class ReliabilityMatrix:
    schema_version: int
    claims: dict[str, Claim]
    suites: dict[str, Suite]

    def select_suites(
        self,
        *,
        proofs: set[str] | None = None,
        surfaces: set[str] | None = None,
        suite_ids: set[str] | None = None,
    ) -> list[Suite]:
        selected: list[Suite] = []
        for suite in self.suites.values():
            if proofs and suite.proof not in proofs:
                continue
            if suite_ids and suite.id not in suite_ids:
                continue
            if surfaces:
                suite_surfaces = {self.claims[claim_id].surface for claim_id in suite.claims}
                if not suite_surfaces.intersection(surfaces):
                    continue
            selected.append(suite)
        return selected


def _required_text(raw: dict[str, Any], key: str, owner: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner}.{key} must be a nonempty string")
    return value.strip()


def _positive_int(raw: dict[str, Any], key: str, owner: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{owner}.{key} must be a positive integer")
    return value


def _positive_float(raw: dict[str, Any], key: str, owner: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{owner}.{key} must be positive")
    return float(value)


def load_matrix(path: str | Path) -> ReliabilityMatrix:
    source = Path(path)
    raw = yaml.load(source.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader) or {}
    if raw.get("schema_version") != 1:
        raise ValueError("reliability matrix schema_version must be 1")

    claims: dict[str, Claim] = {}
    for item in raw.get("claims") or []:
        if not isinstance(item, dict):
            raise ValueError("each claim must be a mapping")
        claim_id = _required_text(item, "id", "claim")
        if claim_id in claims:
            raise ValueError(f"duplicate claim id: {claim_id}")
        proof = _required_text(item, "proof", claim_id)
        if proof not in PROOFS:
            raise ValueError(f"{claim_id}.proof must be one of {sorted(PROOFS)}")
        claims[claim_id] = Claim(
            id=claim_id,
            surface=_required_text(item, "surface", claim_id),
            proof=proof,
            target=_positive_int(item, "target", claim_id),
            description=_required_text(item, "description", claim_id),
        )

    suites: dict[str, Suite] = {}
    coverage: dict[str, int] = {claim_id: 0 for claim_id in claims}
    for item in raw.get("suites") or []:
        if not isinstance(item, dict):
            raise ValueError("each suite must be a mapping")
        suite_id = _required_text(item, "id", "suite")
        if suite_id in suites:
            raise ValueError(f"duplicate suite id: {suite_id}")
        proof = _required_text(item, "proof", suite_id)
        if proof not in PROOFS:
            raise ValueError(f"{suite_id}.proof must be one of {sorted(PROOFS)}")
        kind = _required_text(item, "kind", suite_id)
        if kind not in KINDS:
            raise ValueError(f"{suite_id}.kind must be one of {sorted(KINDS)}")

        command_raw = item.get("command")
        if (
            not isinstance(command_raw, list)
            or not command_raw
            or not all(isinstance(part, str) and part for part in command_raw)
        ):
            raise ValueError(f"{suite_id}.command must be a nonempty string list")
        claim_ids = item.get("claims")
        if not isinstance(claim_ids, list) or not claim_ids:
            raise ValueError(f"{suite_id}.claims must be a nonempty list")
        for claim_id in claim_ids:
            if claim_id not in claims:
                raise ValueError(f"{suite_id} references unknown claim {claim_id!r}")
            if claims[claim_id].proof != proof:
                raise ValueError(
                    f"{suite_id} proof {proof!r} does not match claim {claim_id} "
                    f"proof {claims[claim_id].proof!r}"
                )
            coverage[claim_id] += 1

        required_env = item.get("requires_env") or []
        environment = item.get("environment") or {}
        provider_evidence = item.get("provider_evidence", False)
        provider_conversation_manifest = item.get("provider_conversation_manifest", False)
        if not isinstance(required_env, list) or not all(
            isinstance(name, str) and name for name in required_env
        ):
            raise ValueError(f"{suite_id}.requires_env must be a string list")
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        ):
            raise ValueError(f"{suite_id}.environment must be a string mapping")
        if not isinstance(provider_evidence, bool):
            raise ValueError(f"{suite_id}.provider_evidence must be a boolean")
        if provider_evidence and proof != "live":
            raise ValueError(f"{suite_id}.provider_evidence is valid only for live proof")
        if not isinstance(provider_conversation_manifest, bool):
            raise ValueError(f"{suite_id}.provider_conversation_manifest must be a boolean")
        if provider_conversation_manifest and not provider_evidence:
            raise ValueError(
                f"{suite_id}.provider_conversation_manifest requires provider_evidence"
            )

        suites[suite_id] = Suite(
            id=suite_id,
            proof=proof,
            kind=kind,
            cwd=_required_text(item, "cwd", suite_id),
            timeout_s=_positive_float(item, "timeout_s", suite_id),
            memory_gib=_positive_float(item, "memory_gib", suite_id),
            units=_positive_int(item, "units", suite_id),
            command=tuple(command_raw),
            claims=tuple(str(claim_id) for claim_id in claim_ids),
            requires_env=tuple(required_env),
            environment=dict(environment),
            provider_evidence=provider_evidence,
            provider_conversation_manifest=provider_conversation_manifest,
        )

    if not claims:
        raise ValueError("reliability matrix contains no claims")
    if not suites:
        raise ValueError("reliability matrix contains no suites")
    uncovered = sorted(claim_id for claim_id, count in coverage.items() if count == 0)
    if uncovered:
        raise ValueError(f"claims have no evidence suite: {uncovered}")
    return ReliabilityMatrix(schema_version=1, claims=claims, suites=suites)
