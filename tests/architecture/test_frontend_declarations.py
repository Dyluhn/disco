"""Adversarial mutation battery for the frontend declaration authority.

Epic 12-A added ``frontend_declaration_transitions`` — the fourth public-API
authority — because ``_check_changed_identities`` rejected every changed
frontend identity unconditionally and binding Amendment A3 requires two such
changes.  A new authority over ``seal.PROTECTED`` bytes is only worth having if
every prong is proven to refuse, so this module mirrors the bar Epic 10-D set
for ``member_transitions``: each prong carries at least one mutation that must
be refused, and the refusals are asserted on their own message rather than on
"some problem occurred".

The fixtures are explicit Git roots with a real TypeScript surface, scanned by
the product's own compiler pass.  ``State`` is the declaration under test: it
widens from a two-member union to a three-member union, which is the same shape
as A3's ``AgentEvent`` change and, like it, is invisible to the three earlier
authorities.

The battery is deliberately self-contained.  An authority that decides which
governance bytes may be regenerated should be auditable in one file, and the
fixture scaffolding here is frontend-only, so it shares nothing meaningful with
``test_public_api.py``'s Python-first setup beyond the ``_helpers`` primitives.
The single authority implementation is not duplicated — only the scaffolding.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import assert_problem_contains, git_add, write  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from architecture import public_api  # noqa: E402

# Resolved through the ``public_api`` module object rather than imported
# directly, which is how this suite reaches every other interior part — and it
# keeps this module from adding a second sys.path-dependent import.
_frontend = public_api._frontend_authority

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = "disclaude-architecture-public-api-v1"
_ACCEPTED_DIAGRAM_SHA256 = "759993f1a3104700efe8f48395923117546bd0fd1ca371681bc2a09fc95b9f26"
_ACCEPTED_PARENT = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
_FIXTURE_SOURCE_IDENTITY = "f" * 40
_UNRESOLVABLE_COMMIT = "0" * 40
_FRONTEND_REL = "frontend/src/public.ts"
_DIAGRAM_REL = "docs/architecture.generated.md"
_CONTRACT_REL = "contracts/event-schema.json"
_OWNER = "PKG-12-FE-SHELL"
_RECEIPT = "2026-08-02/EPIC12A-BOUNDARY/EPIC-12A-ACCEPTANCE-RECEIPT.md"
_INVALID = "frontend_declaration_transitions has invalid explicit metadata"

_NARROW_STATE = 'export type State = "ready" | "done";'
_WIDE_STATE = 'export type State = "ready" | "done" | "failed";'
_WIDER_STATE = 'export type State = "ready" | "done" | "failed" | "stalled";'
_INTERFACE_STATE = "export interface State { phase: string; }"
_DELETED_STATE = ""


def _frontend_source(state: str) -> str:
    return (
        "export interface Envelope { id: string; }\n"
        f"{state}\n"
        "export const schema = { type: \"object\" } as const;\n"
        "export function decode(input: string): Envelope {\n"
        "  return { id: input };\n"
        "}\n"
    )


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=True)


def _checkpoint(root: Path) -> str:
    git_add(root, ".")
    subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "source checkpoint", "--allow-empty",
        ],
        capture_output=True,
        check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()


def _contexts_authority() -> dict[str, Any]:
    return {
        "schema": "disclaude-architecture-contexts-v1",
        "source_identity": _FIXTURE_SOURCE_IDENTITY,
        "bounded_contexts": {
            "frontend": {
                "root": "frontend/src",
                "cross_feature_imports_rejected": True,
                "contexts": {
                    "feature": {"modules": ["feature/source"]},
                    "shared": {"modules": ["shared/types"]},
                },
                "dm007_legacy": {
                    "observation_id": "DM-007",
                    "source_identity": _FIXTURE_SOURCE_IDENTITY,
                    "owner_package": "PKG-12-FE-BUILD",
                    "edge_fields": [
                        "source", "source_context", "import",
                        "target", "target_context",
                    ],
                    "edges": [
                        [
                            "frontend/src/feature/source.ts", "feature",
                            "../shared/types", "frontend/src/shared/types.ts",
                            "shared",
                        ]
                    ],
                    "cycles": [],
                },
            }
        },
    }


def _write_frontend(root: Path, state: str = _NARROW_STATE) -> None:
    write(
        root / "frontend/src/feature/source.ts",
        'import type { SharedValue } from "../shared/types";\n\n'
        "export function readShared(value: SharedValue): string {\n"
        "  return value.id;\n"
        "}\n",
    )
    write(root / "frontend/src/shared/types.ts", "export interface SharedValue { id: string; }\n")
    write(root / _FRONTEND_REL, _frontend_source(state))


def _contract_row(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    return {
        "path": rel,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def _write_authority(root: Path, source_identity: str) -> None:
    authority = {
        "schema": _SCHEMA,
        "source_identity": source_identity,
        "python_initializers": public_api.scan_python_public_surface(root),
        "frontend_modules": public_api.scan_frontend_public_surface(root),
        "contract_files": [
            _contract_row(root, rel) for rel in sorted([_CONTRACT_REL, _DIAGRAM_REL])
        ],
        "diagram_transitions": [
            {
                "owner": "PKG-02-GATE",
                "reason": "Fixture transition from accepted to generated bytes.",
                "from_sha256": _ACCEPTED_DIAGRAM_SHA256,
                "to_sha256": hashlib.sha256((root / _DIAGRAM_REL).read_bytes()).hexdigest(),
                "from_parent": _ACCEPTED_PARENT,
                "path": _DIAGRAM_REL,
            }
        ],
        "additive_transitions": [],
        "compatibility_bridges": [],
        "member_transitions": [],
        "frontend_declaration_transitions": [],
        "compatibility_rule": "Fixture rule.",
    }
    write(root / "architecture/public-api.json", json.dumps(authority, indent=2) + "\n")
    git_add(root, "architecture/public-api.json")


def _setup(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build an accepted frontend surface and leave HEAD on its sibling."""
    _init_git(root)
    write(root / "architecture/contexts.json", json.dumps(_contexts_authority(), indent=2) + "\n")
    _write_frontend(root)
    write(root / _DIAGRAM_REL, "# Generated fixture architecture\n")
    write(root / _CONTRACT_REL, '{"event":"message","version":1}\n')
    git_add(root, ".")
    _write_authority(root, _checkpoint(root))
    authority_identity = _checkpoint(root)
    monkeypatch.setattr(public_api, "_ACCEPTED_AUTHORITY_COMMIT", authority_identity)
    monkeypatch.setattr(public_api, "_PKG02_BASE_COMMIT", authority_identity)
    monkeypatch.setattr(
        public_api,
        "_ACCEPTED_AUTHORITY_SHA256",
        hashlib.sha256((root / "architecture/public-api.json").read_bytes()).hexdigest(),
    )
    public_api.regenerate_public_api(root, _checkpoint(root))
    git_add(root, "architecture/public-api.json")
    tree = subprocess.check_output(["git", "-C", str(root), "write-tree"], text=True).strip()
    final = subprocess.check_output(
        [
            "git", "-C", str(root),
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit-tree", tree, "-p", authority_identity,
        ],
        input="final candidate\n",
        text=True,
    ).strip()
    subprocess.run(["git", "-C", str(root), "update-ref", "HEAD", final], check=True)
    return public_api.load_public_api(root)


def _targets(root: Path) -> dict[Any, dict[str, Any]]:
    return public_api._surface_targets(
        public_api.scan_python_public_surface(root),
        public_api.scan_frontend_public_surface(root),
    )


def _stored_targets(authority: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return public_api._surface_targets(
        authority["python_initializers"], authority["frontend_modules"]
    )


def _one(targets: dict[Any, dict[str, Any]], name: str) -> tuple[Any, dict[str, Any]]:
    matches = [key for key in targets if key[:3] == ("frontend", _FRONTEND_REL, name)]
    assert len(matches) == 1, f"{name}: {matches}"
    return matches[0], targets[matches[0]]


def _set_state(root: Path, state: str) -> None:
    write(root / _FRONTEND_REL, _frontend_source(state))
    git_add(root, _FRONTEND_REL)


def _record(root: Path, accepted: dict[str, Any], name: str, commit: str) -> dict[str, str]:
    """Build the exactly-correct record for one accepted -> live transition."""
    old_key, old_target = _one(_stored_targets(accepted), name)
    new_key, new_target = _one(_targets(root), name)
    return {
        "surface": "frontend",
        "path": _FRONTEND_REL,
        "public_name": name,
        "declaration_kind": new_target["declaration"]["kind"],
        "old_signature": old_target["declaration"]["signature"],
        "new_signature": new_target["declaration"]["signature"],
        "old_target_sha256": old_key[3],
        "new_target_sha256": new_key[3],
        "owner_package": _OWNER,
        "accepting_commit": commit,
        "accepting_receipt": _RECEIPT,
    }


def _transition(root: Path, name: str) -> dict[str, str]:
    key, _ = _one(_targets(root), name)
    return {
        "surface": "frontend",
        "path": _FRONTEND_REL,
        "public_name": name,
        "target_sha256": key[3],
        "owner_package": _OWNER,
        "reason": "Epic 12-A: A3 contract parity widens the accepted declaration.",
    }


def _widen(
    root: Path, accepted: dict[str, Any], state: str = _WIDE_STATE,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Widen ``State``, commit, and return (identity, record, transition)."""
    _set_state(root, state)
    identity = _checkpoint(root)
    return identity, _record(root, accepted, "State", identity), _transition(root, "State")


def _regenerate(
    root: Path, identity: str, record: dict[str, Any] | None,
    transition: dict[str, Any], **kwargs: Any,
) -> dict[str, Any]:
    return public_api.regenerate_public_api(
        root,
        identity,
        additive_transitions=[transition],
        frontend_declaration_transitions=None if record is None else [record],
        **kwargs,
    )


def _accept_widening(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Land one accepted frontend declaration change and return its record.

    The regenerated authority is amended into a SIBLING of the source commit
    rather than committed as a child, because ``_source_prior`` requires a
    source commit to leave its parent's authority untouched.  That is the same
    sibling protocol the campaign's own seal follows, in miniature.
    """
    accepted = _setup(root, monkeypatch)
    identity, record, transition = _widen(root, accepted)
    _regenerate(root, identity, record, transition)
    parent = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", f"{identity}^"], text=True
    ).strip()
    _sibling_head(root, parent)
    return accepted, record


def _sibling_head(root: Path, parent: str) -> None:
    """Move HEAD to a sibling of ``parent`` carrying the working tree."""
    git_add(root, ".")
    tree = subprocess.check_output(["git", "-C", str(root), "write-tree"], text=True).strip()
    commit = subprocess.check_output(
        [
            "git", "-C", str(root),
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit-tree", tree, "-p", parent,
        ],
        input="sibling\n",
        text=True,
    ).strip()
    subprocess.run(["git", "-C", str(root), "update-ref", "HEAD", commit], check=True)


def _tamper(root: Path, rows: Any) -> None:
    """Write ``rows`` straight into the authority file, bypassing authoring."""
    path = root / "architecture/public-api.json"
    authority = json.loads(path.read_text())
    authority["frontend_declaration_transitions"] = rows
    path.write_text(json.dumps(authority, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Positive controls: the authority must actually authorize the real change.
# --------------------------------------------------------------------------


class TestFrontendDeclarationAuthorized:
    def test_widening_with_record_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        result = _regenerate(tmp_path, identity, record, transition)

        authority = public_api.load_public_api(tmp_path)
        assert authority["frontend_declaration_transitions"] == [record]
        assert result["frontend_declaration_transition_count"] == 1
        assert public_api.check_public_api(tmp_path)["ok"] is True

    def test_widening_without_the_authority_is_still_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-Epic-12-A refusal is preserved verbatim when no record claims it."""
        accepted = _setup(tmp_path, monkeypatch)
        identity, _, transition = _widen(tmp_path, accepted)

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, None, transition)
        assert "frontend incompatible change cannot be bridged" in str(error.value)
        assert "State" in str(error.value)

    def test_record_is_carried_across_a_later_regeneration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, record = _accept_widening(tmp_path, monkeypatch)
        identity = _checkpoint(tmp_path)

        public_api.regenerate_public_api(tmp_path, identity)

        assert public_api.load_public_api(tmp_path)["frontend_declaration_transitions"] == [record]
        assert public_api.check_public_api(tmp_path)["ok"] is True

    def test_python_member_authority_is_unaffected(self) -> None:
        """The live authority still carries its Python member transitions.

        Seven as of Epic 13-B1; the frontend declaration authority must stay
        disjoint from the Python member authority however many rows the latter
        carries.
        """
        baseline = public_api.load_public_api(REPO_ROOT)
        assert len(baseline["member_transitions"]) == 7
        assert all(row["surface"] == "python" for row in baseline["member_transitions"])
        for row in baseline.get("frontend_declaration_transitions", []):
            assert row["surface"] == "frontend"
            assert row["old_target_sha256"] != row["new_target_sha256"]


# --------------------------------------------------------------------------
# Prong 1 — schema, surface, package form, digest form, commit resolvability.
# --------------------------------------------------------------------------


def _drop_field(record: dict[str, str]) -> dict[str, Any]:
    mutated = dict(record)
    del mutated["accepting_receipt"]
    return mutated


def _extra_field(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "reason": "an unauthorized extra field"}


def _empty_value(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "accepting_receipt": ""}


def _non_string_value(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "declaration_kind": 7}


def _python_surface(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "surface": "python"}


def _bad_owner(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "owner_package": "PKG-BAD"}


def _malformed_old_digest(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "old_target_sha256": "not-a-digest"}


def _malformed_new_digest(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "new_target_sha256": record["new_target_sha256"][:63]}


def _identical_digests(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "old_target_sha256": record["new_target_sha256"]}


def _short_commit(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "accepting_commit": record["accepting_commit"][:12]}


def _unresolvable_commit(record: dict[str, str]) -> dict[str, Any]:
    return {**record, "accepting_commit": _UNRESOLVABLE_COMMIT}


_ROW_MUTATIONS = [
    pytest.param(_drop_field, id="missing-field"),
    pytest.param(_extra_field, id="extra-field"),
    pytest.param(_empty_value, id="empty-value"),
    pytest.param(_non_string_value, id="non-string-value"),
    pytest.param(_python_surface, id="python-surface"),
    pytest.param(_bad_owner, id="malformed-owner-package"),
    pytest.param(_malformed_old_digest, id="malformed-old-digest"),
    pytest.param(_malformed_new_digest, id="malformed-new-digest"),
    pytest.param(_identical_digests, id="identical-digests"),
    pytest.param(_short_commit, id="short-accepting-commit"),
    pytest.param(_unresolvable_commit, id="unresolvable-accepting-commit"),
]


class TestDeclarationRowValidity:
    @pytest.mark.parametrize("mutate", _ROW_MUTATIONS)
    def test_invalid_row_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Any
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, mutate(record), transition)
        assert _INVALID in str(error.value)

    def test_unsorted_rows_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        _set_state(tmp_path, _WIDE_STATE)
        write(
            tmp_path / _FRONTEND_REL,
            _frontend_source(_WIDE_STATE).replace(
                "export interface Envelope { id: string; }",
                "export interface Envelope { id: string; kind: string; }",
            ),
        )
        identity = _checkpoint(tmp_path)
        rows = [
            _record(tmp_path, accepted, "State", identity),
            _record(tmp_path, accepted, "Envelope", identity),
        ]
        unsorted_rows = sorted(rows, key=public_api._canonical, reverse=True)
        assert unsorted_rows != sorted(rows, key=public_api._canonical)

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path,
                identity,
                additive_transitions=[
                    _transition(tmp_path, "State"), _transition(tmp_path, "Envelope"),
                ],
                frontend_declaration_transitions=unsorted_rows,
            )
        assert "must be canonically sorted" in str(error.value)

    def test_duplicate_target_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path,
                identity,
                additive_transitions=[transition],
                frontend_declaration_transitions=[record, dict(record)],
            )
        assert "duplicate frontend declaration transition target" in str(error.value)

    def test_non_list_authority_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _accept_widening(tmp_path, monkeypatch)
        _tamper(tmp_path, {"frontend/src/public.ts": "State"})

        assert_problem_contains(public_api.check_public_api(tmp_path)["problems"], _INVALID)


# --------------------------------------------------------------------------
# Prongs 2, 3 and 4 — the record must pin the real transition, not a plausible
# one. Each of these rows is schema-valid and lies about exactly one thing.
# --------------------------------------------------------------------------


class TestDeclarationPins:
    def test_wrong_old_digest_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        record["old_target_sha256"] = hashlib.sha256(b"not the accepted target").hexdigest()

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "old_target_sha256 does not pin the accepted target" in str(error.value)

    def test_wrong_new_digest_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        record["new_target_sha256"] = hashlib.sha256(b"not the live target").hexdigest()

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "new_target_sha256 does not pin the live target" in str(error.value)

    def test_wrong_declaration_kind_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        record["declaration_kind"] = "InterfaceDeclaration"

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "requires one unchanged declaration kind" in str(error.value)

    def test_changed_declaration_kind_cannot_ride_the_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A type alias becoming an interface is a different change."""
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted, _INTERFACE_STATE)
        assert record["declaration_kind"] == "InterfaceDeclaration"

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "requires one unchanged declaration kind" in str(error.value)

    def test_wrong_old_signature_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        record["old_signature"] = _NARROW_STATE.replace("done", "finished")

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "old_signature does not equal the accepted declaration" in str(error.value)

    def test_wrong_new_signature_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        record["new_signature"] = record["new_signature"].replace("failed", "errored")

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "new_signature does not equal the live declaration" in str(error.value)

    def test_whitespace_drift_in_the_signature_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Byte-for-byte means byte-for-byte; a reformatted pin is not a pin."""
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        record["new_signature"] = record["new_signature"].replace(" | ", "|")

        with pytest.raises(RuntimeError) as error:
            _regenerate(tmp_path, identity, record, transition)
        assert "new_signature does not equal the live declaration" in str(error.value)


# --------------------------------------------------------------------------
# Prong 5 — the delta check: stale, extra and edited records all fail.
# --------------------------------------------------------------------------


class TestDeclarationDelta:
    def test_record_for_an_unchanged_identity_is_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        stowaway = dict(record)
        stowaway["public_name"] = "Envelope"

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path,
                identity,
                additive_transitions=[transition],
                frontend_declaration_transitions=sorted(
                    [record, stowaway], key=public_api._canonical
                ),
            )
        assert "do not exactly authorize regeneration" in str(error.value)
        assert "Envelope" in str(error.value)

    def test_dropping_a_carried_record_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare list REPLACES the carried set — proving it here, not assuming it."""
        _accept_widening(tmp_path, monkeypatch)
        identity = _checkpoint(tmp_path)

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path, identity, frontend_declaration_transitions=[]
            )
        assert "do not exactly authorize regeneration" in str(error.value)
        assert "missing=[('frontend'" in str(error.value)

    def test_editing_a_carried_record_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, record = _accept_widening(tmp_path, monkeypatch)
        identity = _checkpoint(tmp_path)
        edited = {**record, "accepting_receipt": "2026-08-02/A-DIFFERENT-RECEIPT.md"}

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path, identity, frontend_declaration_transitions=[edited]
            )
        assert "accepted frontend declaration transition changed" in str(error.value)

    def test_a_second_change_needs_a_second_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One record authorizes exactly one transition, not a standing licence."""
        _accept_widening(tmp_path, monkeypatch)
        accepted = public_api.load_public_api(tmp_path)
        _set_state(tmp_path, _WIDER_STATE)
        identity = _checkpoint(tmp_path)

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path, identity, additive_transitions=[
                    *accepted["additive_transitions"], _transition(tmp_path, "State"),
                ],
            )
        assert "old_target_sha256 does not pin the accepted target" in str(error.value)


# --------------------------------------------------------------------------
# Prong 6 and the live re-validation performed on every check_public_api call.
# --------------------------------------------------------------------------


class TestDeclarationDisjointAndLive:
    def test_bridge_claiming_the_same_target_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disjointness is asserted, not assumed, even though paths differ in practice."""
        accepted = _setup(tmp_path, monkeypatch)
        identity, record, transition = _widen(tmp_path, accepted)
        bridge = {
            "path": _FRONTEND_REL,
            "public_name": "State",
            "old_origin": "frontend.legacy.State",
            "new_origin": "frontend.public.State",
            "owner_package": _OWNER,
            "removal_package": "PKG-13-FACADES",
            "reason": "A bridge must not be able to double-claim this identity.",
        }

        with pytest.raises(RuntimeError) as error:
            public_api.regenerate_public_api(
                tmp_path,
                identity,
                additive_transitions=[transition],
                compatibility_bridges=[bridge],
                frontend_declaration_transitions=[record],
            )
        assert "frontend declaration transition and a Python authority claim" in str(error.value)

    def test_member_record_claiming_the_same_target_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, record = _accept_widening(tmp_path, monkeypatch)
        member = {
            "surface": "python",
            "path": _FRONTEND_REL,
            "public_name": "State",
            "origin": "frontend.public.State",
            "removed_members": [],
            "added_members": ["def widen(self) -> None"],
            "old_signature_sha256": hashlib.sha256(b"old").hexdigest(),
            "new_signature_sha256": hashlib.sha256(b"new").hexdigest(),
            "owner_package": _OWNER,
            "accepting_commit": record["accepting_commit"],
            "accepting_receipt": _RECEIPT,
        }
        path = tmp_path / "architecture/public-api.json"
        authority = json.loads(path.read_text())
        authority["member_transitions"] = [member]
        path.write_text(json.dumps(authority, indent=2) + "\n", encoding="utf-8")

        assert_problem_contains(
            public_api.check_public_api(tmp_path)["problems"],
            "frontend declaration transition and a Python authority claim",
        )

    def test_carried_record_must_still_match_the_live_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _ = _accept_widening(tmp_path, monkeypatch)
        parent = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
        ).strip()
        _set_state(tmp_path, _WIDER_STATE)
        _sibling_head(tmp_path, parent)

        assert_problem_contains(
            public_api.check_public_api(tmp_path)["problems"],
            "frontend declaration transition does not match the live declaration",
        )

    def test_carried_record_must_still_name_a_live_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _ = _accept_widening(tmp_path, monkeypatch)
        parent = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
        ).strip()
        _set_state(tmp_path, _DELETED_STATE)
        _sibling_head(tmp_path, parent)

        assert_problem_contains(
            public_api.check_public_api(tmp_path)["problems"],
            "frontend declaration transition no longer names one live public target",
        )

    def test_tampered_digest_in_a_carried_record_is_caught_on_every_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, record = _accept_widening(tmp_path, monkeypatch)
        _tamper(
            tmp_path,
            [{**record, "new_target_sha256": hashlib.sha256(b"tampered").hexdigest()}],
        )

        assert_problem_contains(
            public_api.check_public_api(tmp_path)["problems"],
            "new_target_sha256 does not match the live target",
        )


# --------------------------------------------------------------------------
# Unit-level refusals for the two helpers that have no reachable failure path
# through the public entry points.
# --------------------------------------------------------------------------


class TestDeclarationHelpers:
    def test_declaration_of_rejects_a_malformed_descriptor(self) -> None:
        problems: list[str] = []
        record = {
            "surface": "frontend", "path": _FRONTEND_REL, "public_name": "State",
            "declaration_kind": "TypeAliasDeclaration",
            "old_signature": _NARROW_STATE, "new_signature": _WIDE_STATE,
            "old_target_sha256": "a" * 64, "new_target_sha256": "b" * 64,
            "owner_package": _OWNER, "accepting_commit": "c" * 40,
            "accepting_receipt": _RECEIPT,
        }
        key = ("frontend", _FRONTEND_REL, "State", "b" * 64)
        _frontend.check_one_declaration(record, key[:3], {key: {"declaration": None}}, problems)

        assert_problem_contains(problems, "does not match the live declaration")

    def test_change_check_requires_exactly_one_target_on_each_side(self) -> None:
        problems: list[str] = []
        _frontend.check_declaration_change(
            ("frontend", _FRONTEND_REL, "State"), [], [], {}, {}, {}, problems
        )

        assert_problem_contains(problems, "requires one accepted and one live target")

    def test_describe_is_stable_and_names_the_record(self) -> None:
        described = _frontend.describe(
            {
                "path": _FRONTEND_REL, "public_name": "State",
                "declaration_kind": "TypeAliasDeclaration", "owner_package": _OWNER,
                "accepting_commit": "c" * 40,
            }
        )

        assert json.loads(described)["public_name"] == "State"
        assert described == _frontend.describe(json.loads(described))
