"""Architecture policy loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCH_DIR = REPO_ROOT / "development" / "architecture"
POLICY_PATH = ARCH_DIR / "policy.json"
OWNERSHIP_PATH = ARCH_DIR / "ownership.json"


def _architecture_path(root: Path | None, name: str) -> Path:
    """Resolve one architecture authority without consulting another root."""
    repo_root = REPO_ROOT if root is None else Path(root)
    return repo_root / "development" / "architecture" / name


def load_policy(root: Path | None = None) -> dict[str, Any]:
    """Load and validate the policy from ``root`` (or stable ``REPO_ROOT``)."""
    path = _architecture_path(root, "policy.json")
    if not path.is_file():
        raise FileNotFoundError(f"policy file missing: {path}")
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema") != "disclaude-architecture-policy-v1":
        raise ValueError(f"invalid policy schema: {policy.get('schema')}")
    return policy


def load_json(path: Path) -> Any:
    """Load a JSON file, raising a clear error if missing or malformed."""
    if not path.is_file():
        raise FileNotFoundError(f"missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_ownership(root: Path | None = None) -> dict[str, Any]:
    """Load and validate ownership from ``root`` (or stable ``REPO_ROOT``)."""
    path = _architecture_path(root, "ownership.json")
    if not path.is_file():
        raise FileNotFoundError(f"ownership file missing: {path}")
    ownership = json.loads(path.read_text(encoding="utf-8"))
    if ownership.get("schema") != "disclaude-architecture-ownership-v1":
        raise ValueError(f"invalid ownership schema: {ownership.get('schema')}")
    return ownership


def logical_limits(policy: dict[str, Any]) -> dict[str, int]:
    """Return the logical limits dict from the policy."""
    return policy["logical_limits"]


def python_roots(policy: dict[str, Any]) -> list[str]:
    """Return the Python scan root prefixes."""
    return policy["roots"]["python"]


def typescript_roots(policy: dict[str, Any]) -> list[str]:
    """Return the TypeScript scan root prefixes."""
    return policy["roots"]["typescript"]


def protocol_non_violations(policy: dict[str, Any]) -> list[dict[str, str]]:
    """Return the typed Protocol non-violation classifications."""
    return policy["typed_classifications"]["protocol_non_violations"]


def is_protocol_non_violation(
    policy: dict[str, Any], path: str, symbol: str, rule: str
) -> bool:
    """Check whether a (path, symbol, rule) triple is a typed Protocol non-violation."""
    for entry in protocol_non_violations(policy):
        if (
            entry["path"] == path
            and entry["symbol"] == symbol
            and entry["rule"] == rule
        ):
            return True
    return False


def adjudicated_width_non_violations(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the owner-adjudicated public-width non-violation classifications.

    Distinct from ``protocol_non_violations``, which classifies a typed Protocol
    *declaration* with zero mutable state. These entries classify a concrete
    *implementation* whose public width is inherited from a contract it
    faithfully satisfies, or is intrinsic to a single authority the mission
    requires. Registration is owner-adjudicated per
    ``PROTOCOL-WIDTH-DECISION-2026-07-31.md``; ``budget`` proves every entry
    against the live scan and fails closed.
    """
    return policy["typed_classifications"].get("adjudicated_width_non_violations", [])


def is_adjudicated_width_non_violation(
    policy: dict[str, Any], path: str, symbol: str, rule: str
) -> bool:
    """Check whether a (path, symbol, rule) triple is an adjudicated width row."""
    for entry in adjudicated_width_non_violations(policy):
        if (
            entry["path"] == path
            and entry["symbol"] == symbol
            and entry["rule"] == rule
        ):
            return True
    return False


def collaborator_rules(policy: dict[str, Any]) -> dict[str, Any]:
    """Return the collaborator rules from the policy."""
    return policy["collaborator_rules"]


def composition_roots(ownership: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the composition roots from the ownership registry."""
    return ownership.get("composition_roots", [])


def composition_facades(ownership: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the composition facades from the ownership registry."""
    return ownership.get("composition_facades", [])


def dependency_aggregates(ownership: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the dependency aggregates from the ownership registry."""
    return ownership.get("dependency_aggregates", [])


def declarative_authorization_positives(
    ownership: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the declarative authorization positives from the ownership registry."""
    return ownership.get("declarative_authorization_positives", [])


def effect_owners(ownership: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the effect owners from the ownership registry."""
    return ownership.get("effect_owners", [])


def state_owners(ownership: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the state owners from the ownership registry."""
    return ownership.get("state_owners", [])


def evidence_owners(ownership: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the evidence owners from the ownership registry."""
    return ownership.get("evidence_owners", [])


def collaborator_classification(ownership: dict[str, Any]) -> dict[str, Any]:
    """Return the collaborator classification from the ownership registry."""
    return ownership.get("collaborator_classification", {})


def disguise_patterns_rejected(policy: dict[str, Any]) -> list[str]:
    """Return the disguise patterns rejected from the policy."""
    return policy["collaborator_rules"]["disguise_patterns_rejected"]
