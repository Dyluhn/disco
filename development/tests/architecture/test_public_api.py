"""Exact public-surface mutation tests for the architecture gate.

The fixtures are explicit Git roots.  They exercise the compiler/AST-derived
Python and frontend surfaces, contract bytes, transition metadata, and the
``check_public_api(root=...)`` boundary without redirecting the root loader.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import PUBLIC_API_MEMBER_NAMES, assert_problem_contains, git_add, write  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import public_api  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA = "disclaude-architecture-public-api-v1"
_ACCEPTED_DIAGRAM_SHA256 = "759993f1a3104700efe8f48395923117546bd0fd1ca371681bc2a09fc95b9f26"
_ACCEPTED_PARENT = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
_FIXTURE_SOURCE_IDENTITY = "f" * 40
_INIT_REL = "packages/demo/src/disco/demo/__init__.py"
_FRONTEND_PUBLIC_REL = "frontend/src/public.ts"
_DIAGRAM_REL = "docs/architecture.generated.md"
_CONTRACT_REL = "contracts/event-schema.json"
_FRONTEND_PUBLIC_SOURCE = """\
    export interface Envelope { id: string; }
    export type State = "ready" | "done";
    export const schema = { type: "object", required: ["id"] } as const;
    export function decode(input: string): Envelope {
      return { id: input };
    }
"""


def _init_git(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(root)],
        capture_output=True,
        check=True,
    )


def _checkpoint(root: Path) -> str:
    git_add(root, ".")
    command = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "source checkpoint",
        "--allow-empty",
    ]
    subprocess.run(command, capture_output=True, check=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def _write_python_fixture(root: Path, initializer: str | None = None) -> Path:
    models = """\
        class PublicEvent:
            code: str

            def label(self, prefix: str = "event") -> str:
                return prefix

        class Helper:
            pass
    """
    core = """\
        class Tool:
            pass
    """
    initializer = (
        initializer
        or """\
        from .models import PublicEvent as Event
        from ..core import Tool

        def create_app(name: str, *, debug: bool = False) -> str:
            return name

        __all__ = ["Event", "Tool", "create_app"]
    """
    )
    write(
        root / "packages/demo/src/disco/demo/models.py",
        textwrap.dedent(models),
    )
    write(root / "packages/demo/src/disco/core.py", textwrap.dedent(core))
    return write(root / _INIT_REL, textwrap.dedent(initializer))


def _contexts_authority() -> dict[str, Any]:
    edge_fields = [
        "source",
        "source_context",
        "import",
        "target",
        "target_context",
    ]
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
                    "edge_fields": edge_fields,
                    "edges": [
                        [
                            "frontend/src/feature/source.ts",
                            "feature",
                            "../shared/types",
                            "frontend/src/shared/types.ts",
                            "shared",
                        ]
                    ],
                    "cycles": [],
                },
            }
        },
    }


def _write_frontend_fixture(root: Path) -> None:
    write(
        root / "frontend/src/feature/source.ts",
        textwrap.dedent(
            """\
            import type { SharedValue } from "../shared/types";

            export function readShared(value: SharedValue): string {
              return value.id;
            }
            """
        ),
    )
    write(
        root / "frontend/src/shared/types.ts",
        "export interface SharedValue { id: string; }\n",
    )
    write(
        root / _FRONTEND_PUBLIC_REL,
        textwrap.dedent(_FRONTEND_PUBLIC_SOURCE),
    )


def _contract_row(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    return {
        "path": rel,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def _bridge(
    public_name: str = "Event",
    *,
    old_origin: str = "disco.legacy.Event",
    new_origin: str = "disco.demo.models.PublicEvent",
) -> dict[str, str]:
    return {
        "path": _INIT_REL,
        "public_name": public_name,
        "old_origin": old_origin,
        "new_origin": new_origin,
        "owner_package": "PKG-03-HARNESS-ORACLES",
        "removal_package": "PKG-13-FACADES",
        "reason": "Preserve the accepted import until consumers migrate.",
    }


def _transition(
    root: Path,
    surface: str,
    path: str,
    public_name: str,
    owner_package: str,
    reason: str,
) -> dict[str, str]:
    targets = public_api._surface_targets(
        public_api.scan_python_public_surface(root),
        public_api.scan_frontend_public_surface(root),
    )
    matches = [key for key in targets if key[:3] == (surface, path, public_name)]
    assert len(matches) == 1
    return {
        "surface": surface,
        "path": path,
        "public_name": public_name,
        "target_sha256": matches[0][3],
        "owner_package": owner_package,
        "reason": reason,
    }


def _sorted_metadata(
    *rows: dict[str, str],
) -> list[dict[str, str]]:
    return sorted(rows, key=public_api._canonical)


def _write_authority(
    root: Path,
    source_identity: str,
    *,
    additive_transitions: list[dict[str, str]] | None = None,
    compatibility_bridges: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    diagram_sha256 = hashlib.sha256((root / _DIAGRAM_REL).read_bytes()).hexdigest()
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
                "to_sha256": diagram_sha256,
                "from_parent": _ACCEPTED_PARENT,
                "path": _DIAGRAM_REL,
            }
        ],
        "additive_transitions": sorted(additive_transitions or [], key=public_api._canonical),
        "compatibility_bridges": sorted(compatibility_bridges or [], key=public_api._canonical),
        "compatibility_rule": (
            "Any deleted or renamed public name, origin, signature, frontend "
            "export/type/schema, or contract byte fails. Additions require an "
            "owning-package baseline transition; moved implementations retain "
            "the old public name through an explicit bridge until PKG-13."
        ),
    }
    write(
        root / "architecture/public-api.json",
        json.dumps(authority, indent=2) + "\n",
    )
    git_add(root, "architecture/public-api.json")
    return authority


def _setup_repo(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_python: bool = True,
) -> dict[str, Any]:
    _init_git(root)
    write(
        root / "architecture/contexts.json",
        json.dumps(_contexts_authority(), indent=2) + "\n",
    )
    _write_frontend_fixture(root)
    if include_python:
        _write_python_fixture(root)
    write(root / _DIAGRAM_REL, "# Generated fixture architecture\n")
    write(root / _CONTRACT_REL, '{"event":"message","version":1}\n')
    git_add(root, ".")
    _write_authority(root, _checkpoint(root))
    authority_identity = _checkpoint(root)
    authority_bytes = (root / "architecture/public-api.json").read_bytes()
    monkeypatch.setattr(public_api, "_ACCEPTED_AUTHORITY_COMMIT", authority_identity)
    monkeypatch.setattr(public_api, "_PKG02_BASE_COMMIT", authority_identity)
    monkeypatch.setattr(
        public_api,
        "_ACCEPTED_AUTHORITY_SHA256",
        hashlib.sha256(authority_bytes).hexdigest(),
    )
    source_identity = _checkpoint(root)
    public_api.regenerate_public_api(root, source_identity)
    git_add(root, "architecture/public-api.json")
    tree = subprocess.check_output(["git", "-C", str(root), "write-tree"], text=True).strip()
    final_identity = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit-tree",
            tree,
            "-p",
            authority_identity,
        ],
        input="final candidate\n",
        text=True,
    ).strip()
    subprocess.run(["git", "-C", str(root), "update-ref", "HEAD", final_identity], check=True)
    return public_api.load_public_api(root)


def _initializer_surface(root: Path) -> dict[str, Any]:
    return public_api.extract_initializer(root, _INIT_REL)


def _replace_initializer(root: Path, content: str) -> None:
    write(root / _INIT_REL, textwrap.dedent(content))
    git_add(root, _INIT_REL)


def _assert_python_changed(result: dict[str, Any]) -> None:
    assert result["ok"] is False
    assert_problem_contains(
        result["problems"],
        "Python public surface",
        "changed",
        _INIT_REL,
    )


class TestExtractInitSurface:
    def test_extract_imports_and_definitions(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        path = _write_python_fixture(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool as RenamedTool

            class MyService(Base):
                endpoint: str

                def run(self, value: int = 1) -> str:
                    return str(value)

            async def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["RenamedTool", "Event", "MyService", "create_app"]
            """,
        )
        git_add(tmp_path, ".")

        surface = public_api._extract_init_surface(path)

        assert surface == {
            "has_all": True,
            "explicit_all": [
                "Event",
                "MyService",
                "RenamedTool",
                "create_app",
            ],
            "public_names": [
                "Event",
                "MyService",
                "RenamedTool",
                "create_app",
            ],
            "import_origins": [
                {
                    "public_name": "Event",
                    "name": "PublicEvent",
                    "module": "models",
                    "level": 1,
                    "alias": "Event",
                    "kind": "from",
                },
                {
                    "public_name": "RenamedTool",
                    "name": "Tool",
                    "module": "core",
                    "level": 2,
                    "alias": "RenamedTool",
                    "kind": "from",
                },
            ],
            "public_signatures": {
                "Event": {
                    "kind": "class",
                    "signature": "class PublicEvent()",
                    "members": ["def label(self, prefix: str='event') -> str"],
                    "fields": ["code: str"],
                },
                "MyService": {
                    "kind": "class",
                    "signature": "class MyService(Base)",
                    "members": ["def run(self, value: int=1) -> str"],
                    "fields": ["endpoint: str"],
                },
                "RenamedTool": {
                    "kind": "class",
                    "signature": "class Tool()",
                    "members": [],
                    "fields": [],
                },
                "create_app": {
                    "kind": "function",
                    "signature": ("async def create_app(name: str, *, debug: bool=False) -> str"),
                },
            },
        }

    def test_type_checking_class_declarations_preserve_static_surface(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        path = _write_python_fixture(
            tmp_path,
            """\
            from typing import TYPE_CHECKING

            class Public:
                if TYPE_CHECKING:
                    inherited_field: str

                    async def inherited(self, value: int = 1) -> str: ...

                if False:
                    def control_flow_only(self) -> None: ...

                def direct(self) -> None:
                    pass

            __all__ = ["Public"]
            """,
        )
        git_add(tmp_path, ".")

        surface = public_api._extract_init_surface(path)

        assert surface["public_signatures"]["Public"] == {
            "kind": "class",
            "signature": "class Public()",
            "members": [
                "async def inherited(self, value: int=1) -> str",
                "def direct(self) -> None",
            ],
            "fields": ["inherited_field: str"],
        }

    def test_no_explicit_all_returns_none(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        path = _write_python_fixture(
            tmp_path,
            """\
            from .models import PublicEvent as Event

            def local(value: int = 1) -> int:
                return value
            """,
        )
        git_add(tmp_path, ".")

        surface = public_api._extract_init_surface(path)

        assert surface["has_all"] is False
        assert surface["explicit_all"] is None
        assert surface["public_names"] == ["Event", "local"]
        assert surface["import_origins"] == [
            {
                "public_name": "Event",
                "name": "PublicEvent",
                "module": "models",
                "level": 1,
                "alias": "Event",
                "kind": "from",
            }
        ]
        assert surface["public_signatures"]["local"] == {
            "kind": "function",
            "signature": "def local(value: int=1) -> int",
        }

    def test_private_definitions_excluded(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        path = _write_python_fixture(
            tmp_path,
            """\
            class _Private:
                pass

            def _hidden() -> None:
                pass

            class Public:
                value: int
            """,
        )
        git_add(tmp_path, ".")

        surface = public_api._extract_init_surface(path)

        assert surface["public_names"] == ["Public"]
        assert surface["public_signatures"] == {
            "Public": {
                "kind": "class",
                "signature": "class Public()",
                "members": [],
                "fields": ["value: int"],
            }
        }

    def test_star_import_excluded(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        path = _write_python_fixture(
            tmp_path,
            "from .models import *\n",
        )
        git_add(tmp_path, ".")

        surface = public_api._extract_init_surface(path)

        assert surface == {
            "has_all": False,
            "explicit_all": None,
            "public_names": [],
            "import_origins": [],
            "public_signatures": {},
        }

    def test_empty_all_baseline(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        path = _write_python_fixture(
            tmp_path,
            """\
            from .models import PublicEvent as Event

            class Public:
                pass

            __all__ = []
            """,
        )
        git_add(tmp_path, ".")

        surface = public_api._extract_init_surface(path)

        assert surface["has_all"] is True
        assert surface["explicit_all"] == []
        assert surface["public_names"] == []
        assert surface["import_origins"] == []
        assert surface["public_signatures"] == {}


class TestInitializerDeletion:
    def test_deleted_initializer_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        subprocess.run(
            ["git", "-C", str(tmp_path), "rm", "-f", "-q", _INIT_REL],
            capture_output=True,
            check=True,
        )

        result = public_api.check_public_api(tmp_path)

        assert result["ok"] is False
        assert_problem_contains(
            result["problems"],
            "Python public surface",
            "deleted",
            _INIT_REL,
        )

    def test_deleted_import_detected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from disco.demo.models import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["Event", "Tool", "create_app"]
            """,
        )

        result = public_api.check_public_api(tmp_path)

        _assert_python_changed(result)
        surface = _initializer_surface(tmp_path)
        assert surface["public_names"] == ["Event", "Tool", "create_app"]
        event_origin = next(
            row for row in surface["import_origins"] if row["public_name"] == "Event"
        )
        assert event_origin == {
            "public_name": "Event",
            "name": "PublicEvent",
            "module": "disco.demo.models",
            "level": 0,
            "alias": "Event",
            "kind": "from",
        }

    def test_deleted_definition_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool

            __all__ = ["Event", "Tool"]
            """,
        )

        result = public_api.check_public_api(tmp_path)

        _assert_python_changed(result)
        assert "create_app" not in _initializer_surface(tmp_path)["public_signatures"]

        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(
                name: str,
                *,
                debug: bool = False,
                retries: int = 0,
            ) -> str:
                return name

            __all__ = ["Event", "Tool", "create_app"]
            """,
        )
        signature_result = public_api.check_public_api(tmp_path)
        _assert_python_changed(signature_result)
        assert _initializer_surface(tmp_path)["public_signatures"]["create_app"] == {
            "kind": "function",
            "signature": ("def create_app(name: str, *, debug: bool=False, retries: int=0) -> str"),
        }

    def test_removed_explicit_all_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name
            """,
        )

        result = public_api.check_public_api(tmp_path)

        _assert_python_changed(result)
        surface = _initializer_surface(tmp_path)
        assert surface["has_all"] is False
        assert surface["explicit_all"] is None

    def test_removed_all_entry_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["Event", "create_app"]
            """,
        )

        result = public_api.check_public_api(tmp_path)

        _assert_python_changed(result)
        assert _initializer_surface(tmp_path)["explicit_all"] == [
            "Event",
            "create_app",
        ]


class TestContractFileDrift:
    def test_missing_contract_file_detected(self, tmp_path: Path) -> None:
        baseline = {
            "path": "nonexistent.md",
            "sha256": "0" * 64,
            "bytes": 0,
        }
        problems: list[str] = []

        public_api._check_contract_file(baseline, tmp_path, problems)

        assert problems == ["contract file missing: nonexistent.md"]

    def test_contract_file_byte_drift_detected(self, tmp_path: Path) -> None:
        path = write(tmp_path / "contract.md", "content")
        baseline = {
            "path": "contract.md",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size + 1,
        }
        problems: list[str] = []

        public_api._check_contract_file(baseline, tmp_path, problems)

        assert len(problems) == 1
        assert_problem_contains(problems, "contract file drift", "contract.md")

    def test_matching_contract_file_passes(self, tmp_path: Path) -> None:
        path = write(tmp_path / "contract.md", "content")
        baseline = {
            "path": "contract.md",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        problems: list[str] = []

        public_api._check_contract_file(baseline, tmp_path, problems)

        assert problems == []


class TestCheckPublicApiBoundary:
    def test_check_public_api_with_empty_baseline_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = _setup_repo(tmp_path, monkeypatch, include_python=False)

        loaded = public_api.load_public_api(tmp_path)
        result = public_api.check_public_api(tmp_path)

        assert loaded == expected
        assert loaded["source_identity"] == expected["source_identity"]
        assert result == {
            "ok": True,
            "problems": [],
            "initializer_count": 0,
            "contract_file_count": 2,
            "frontend_module_count": 3,
        }
        with pytest.raises(RuntimeError, match="resolve to a Git commit"):
            public_api.regenerate_public_api(tmp_path, "0" * 40)
        loaded["source_identity"] = "0" * 40
        write(
            tmp_path / "architecture/public-api.json",
            json.dumps(loaded, indent=2) + "\n",
        )
        assert_problem_contains(
            public_api.check_public_api(tmp_path)["problems"],
            "source_identity",
            "Git commit",
        )

    def test_check_public_api_detects_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        subprocess.run(
            ["git", "-C", str(tmp_path), "rm", "-f", "-q", _INIT_REL],
            capture_output=True,
            check=True,
        )

        result = public_api.check_public_api(tmp_path)

        assert result["ok"] is False
        assert result["initializer_count"] == 0
        assert_problem_contains(result["problems"], "deleted", _INIT_REL)

    def test_check_public_api_detects_contract_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authority = _setup_repo(tmp_path, monkeypatch)
        authority_path = tmp_path / "architecture/public-api.json"
        accepted_bytes = authority_path.read_bytes()
        transition = dict(authority["diagram_transitions"][0])
        authority["diagram_transitions"][0]["unexpected"] = "field"
        write(
            tmp_path / "architecture/public-api.json",
            json.dumps(authority, indent=2) + "\n",
        )
        schema_result = public_api.check_public_api(tmp_path)
        assert_problem_contains(schema_result["problems"], "diagram transition schema mismatch")

        for field, bad_value in [
            ("owner", "PKG-03-HARNESS-ORACLES"),
            ("reason", ""),
            ("from_sha256", "0" * 64),
            ("from_parent", "0" * 40),
            ("path", "docs/other.md"),
        ]:
            mutated = {**transition, field: bad_value}
            problems: list[str] = []
            public_api._check_diagram_transition(
                mutated,
                tmp_path,
                {row["path"]: row for row in authority["contract_files"]},
                problems,
            )
            assert_problem_contains(problems, "diagram transition accepted authority drift")

        write(authority_path, accepted_bytes.decode())
        write(tmp_path / _CONTRACT_REL, '{"event":"changed","version":2}\n')
        with pytest.raises(RuntimeError, match="cannot be rebaselined"):
            public_api.regenerate_public_api(tmp_path, _checkpoint(tmp_path))
        assert authority_path.read_bytes() == accepted_bytes
        write(tmp_path / _CONTRACT_REL, '{"event":"message","version":1}\n')
        write(tmp_path / _DIAGRAM_REL, "# Drifted generated architecture\n")
        drift_result = public_api.check_public_api(tmp_path)

        assert drift_result["ok"] is False
        assert_problem_contains(drift_result["problems"], "contract file drift", _DIAGRAM_REL)
        assert_problem_contains(
            drift_result["problems"],
            "diagram transition target/contract bytes do not agree",
        )


class TestFrontendPublicApi:
    def test_contract_file_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        live = public_api.load_public_api(REPO_ROOT)
        authority = _setup_repo(tmp_path, monkeypatch)
        module = next(
            row for row in authority["frontend_modules"] if row["path"] == _FRONTEND_PUBLIC_REL
        )

        assert len(live["contract_files"]) == 15
        # 234 through Epic 12-A; 298 after Epic 12-B decomposed PKG-12-FE-BUILD
        # into 65 new modules and folded `buildTrace/activityTypes.ts` back into
        # its parent (net +64). Every one of them is additive surface — the 12-B
        # regeneration proves 0 deleted and 0 changed declarations.
        # 337 after Epic 12-C decomposed PKG-12-FE-RESEARCH + PKG-12-FE-PREVIEW
        # into 39 new modules (`previewPaneParts`, `needMoreCardParts`,
        # `deepResearchSurfaceParts`, `researchSurfaceParts`, `useDeepResearchParts`,
        # `deepResearchTraceParts`, plus `api/preview.ts`). Same shape and same
        # proof: the 12-C regeneration asserts 0 deleted and 0 changed identities,
        # because every decomposed module keeps DECLARING its own surface and the
        # frontend digest is computed withoutBody.
        # 399 after Epic 12-D decomposed PKG-12-FE-SETTINGS + PKG-12-FE-SHELL
        # (56 new part modules) and Amendment A3's allowlist closure added six
        # api-seam modules (`errors`, `liveness`, `session`, `schedules`,
        # `artifacts`, `deck`). Same shape, same proof: 0 deleted, 0 changed
        # identities. `api/preview.ts` gains ONE declaration and is the only
        # pre-existing module whose row moves — an addition, not a change to any
        # existing declaration's signature.
        # 401 after the Settings simplification extracted `SandboxRuntimeSection`
        # and `StoredCredentialField` as independently owned public components.
        # 404 after the mobile-first-class pass added three shared primitives
        # (tapTarget, useScrollFade, ScrollFade). 407 after the composer redesign
        # (SearchTypeSlider, DriverModelNotice, focusModelControl). 409 after the
        # production-readiness wave (McpImportBox, passageNormalize). All additive.
        # 432 after V51: the audio, preview and settings work added modules and
        # split three download components and the image-gen setup note out of
        # their oversized parents. All additive at the module level; the four
        # moved public targets carry frontend target relocations.
        assert len(live["frontend_modules"]) == 441
        assert len(authority["contract_files"]) == 2
        assert all(set(row) == {"path", "sha256", "bytes"} for row in authority["contract_files"])
        assert module == {
            "path": _FRONTEND_PUBLIC_REL,
            "public_declarations": [
                {
                    "kind": "function",
                    "name": "decode",
                    "signature": ("export function decode(input: string): Envelope"),
                },
                {
                    "kind": "InterfaceDeclaration",
                    "name": "Envelope",
                    "signature": "export interface Envelope { id: string; }",
                },
                {
                    "kind": "TypeAliasDeclaration",
                    "name": "State",
                    "signature": 'export type State = "ready" | "done";',
                },
                {
                    "kind": "variable",
                    "name": "schema",
                    "signature": (
                        'export const schema = { type: "object", required: ["id"] } as const;'
                    ),
                },
            ],
        }

    def test_initializer_count(self) -> None:
        baseline = public_api.load_public_api(REPO_ROOT)
        initializers = baseline["python_initializers"]

        # 34 at PKG-02 bootstrap; 74 after Epics 7-10 published 40 typed
        # *_parts packages; 85 after Epic 11-A; 87 after Epic 11-B; 93 after
        # Epic 11-C published six interior parts packages (0 added public
        # targets). Update at each accepted regeneration.
        assert len(initializers) == 93
        assert [row["path"] for row in initializers] == sorted(row["path"] for row in initializers)
        for row in initializers:
            assert set(row) == {
                "path",
                "has_all",
                "explicit_all",
                "public_names",
                "import_origins",
                "public_signatures",
            }
            assert set(row["public_names"]) == set(row["public_signatures"])
            assert all(
                set(origin)
                == {
                    "public_name",
                    "name",
                    "module",
                    "level",
                    "alias",
                    "kind",
                }
                and origin["public_name"] in row["public_names"]
                for origin in row["import_origins"]
            )

    def test_compatibility_rule_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = public_api.load_public_api(REPO_ROOT)
        valid = _bridge()

        assert baseline["compatibility_rule"] == (
            "Any deleted or renamed public name, origin, signature, frontend "
            "export/type/schema, or contract byte fails. Additions require an "
            "owning-package baseline transition; moved implementations retain "
            "the old public name through an explicit bridge until PKG-13. A "
            "member-level change at an unchanged origin requires an explicit "
            "member transition pinning both signature digests and the exact "
            "member delta. A frontend declaration change requires an explicit "
            "frontend declaration transition pinning both target digests and "
            "both declaration texts. A frontend public target that MOVES path "
            "or name requires an explicit frontend target relocation naming one "
            "live destination of the same declaration kind, one to one."
        )
        # No longer empty as of Epic 10-D: every added public target carries an
        # owning-package transition, and the two accepted member-level changes
        # carry member transitions. Bridges remain unused — no public name has
        # changed origin.
        assert baseline["additive_transitions"]
        assert baseline["compatibility_bridges"] == []
        # 2 through Epic 10-D; 5 after Epic 11-B moved authority off three
        # config classes onto collaborators at an UNCHANGED origin — the exact
        # case no compatibility bridge can express. 6 after Epic 11-D did the
        # same to BuildPlatformRegistry to clear PY-0431, whose public-method
        # width no parts extraction could reduce (a thin delegator still counts
        # toward service_public_methods, and a mixin is evasion). 7 after Epic
        # 13-B1 deleted 29 ConversationRuntime delegates at an unchanged origin
        # — the same case, on the class the facade was installed onto. 8 after
        # 13-C records FinishGate's explicit typed-service methods without
        # asserting a false relocation origin. 9 records AgentErrorEvent's
        # exact semantic fields; v31 adds delivery selection and loop collaborators;
        # v32 adds SectionContent's generated eyebrow slot; v36 adds the
        # bounded visual-inspection route on DefaultLLMRouter; the settings
        # correction adds the explicit capability fields on ProviderSettings
        # and ModelEntry; records policy adds Entity; MCP diagnostics add McpPool;
        # the launch closeout widens WSClientFrame's `type` Literal with the
        # explicit accept_finished stop-intent frame; V51 adds
        # SqliteEventStore.update_title_unique at both initializers that
        # re-export it and ProviderSettings' two key-status fields.
        assert (
            {row["public_name"] for row in baseline["member_transitions"]}
            == PUBLIC_API_MEMBER_NAMES
        )
        for row in baseline["member_transitions"]:
            # A member transition never changes origin — that is a bridge's job.
            assert row["removed_members"] or row["added_members"]
            assert row["old_signature_sha256"] != row["new_signature_sha256"]
        assert public_api._valid_bridge(valid) is True
        missing_reason = {key: value for key, value in valid.items() if key != "reason"}
        assert all(
            not public_api._valid_bridge(row)
            for row in [
                {**valid, "unexpected": "field"},
                missing_reason,
                {**valid, "owner_package": "not-a-package"},
                {**valid, "new_origin": valid["old_origin"]},
                {**valid, "removal_package": "PKG-12-FE-BUILD"},
            ]
        )

        _setup_repo(tmp_path, monkeypatch)
        write(
            tmp_path / "packages/demo/src/disco/core.py",
            textwrap.dedent(
                """\
                class PublicEvent:
                    code: str

                    def label(self, prefix: str = "event") -> str:
                        return prefix

                class Tool:
                    pass
                """
            ),
        )
        _replace_initializer(
            tmp_path,
            """\
            from ..core import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["Event", "Tool", "create_app"]
            """,
        )
        git_add(tmp_path, ".")
        transition = _transition(
            tmp_path,
            "python",
            _INIT_REL,
            "Event",
            "PKG-03-HARNESS-ORACLES",
            "Move Event behind its preserved facade.",
        )
        bridge = _bridge(
            old_origin="disco.demo.models.PublicEvent",
            new_origin="disco.core.PublicEvent",
        )
        next_identity = _checkpoint(tmp_path)
        with pytest.raises(RuntimeError, match="origins do not match"):
            public_api.regenerate_public_api(
                tmp_path,
                next_identity,
                additive_transitions=[transition],
                compatibility_bridges=[{**bridge, "old_origin": "forged.legacy.Event"}],
            )
        public_api.regenerate_public_api(
            tmp_path,
            next_identity,
            additive_transitions=[transition],
            compatibility_bridges=[bridge],
        )
        authority = public_api.load_public_api(tmp_path)
        authority["compatibility_bridges"][0]["new_origin"] = "forged.attacker.Event"
        write(
            tmp_path / "architecture/public-api.json",
            json.dumps(authority, indent=2) + "\n",
        )
        result = public_api.check_public_api(tmp_path)
        assert_problem_contains(result["problems"], "new_origin", "live origin")

    def test_frontend_contract_file_drift_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        mutations = [
            _FRONTEND_PUBLIC_SOURCE.replace(
                "export function decode",
                "function decode",
            ),
            _FRONTEND_PUBLIC_SOURCE.replace(
                "id: string",
                "id: number",
                1,
            ),
            _FRONTEND_PUBLIC_SOURCE.replace(
                '"ready" | "done"',
                '"ready" | "failed"',
            ),
            _FRONTEND_PUBLIC_SOURCE.replace(
                'required: ["id"]',
                'required: ["id", "kind"]',
            ),
        ]
        for mutation in mutations:
            write(
                tmp_path / _FRONTEND_PUBLIC_REL,
                textwrap.dedent(mutation),
            )
            result = public_api.check_public_api(tmp_path)
            assert result["ok"] is False
            assert_problem_contains(
                result["problems"],
                "frontend compiler surface",
                "changed",
                _FRONTEND_PUBLIC_REL,
            )

    def test_frontend_contract_file_deletion_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "rm",
                "-f",
                "-q",
                _FRONTEND_PUBLIC_REL,
            ],
            capture_output=True,
            check=True,
        )

        result = public_api.check_public_api(tmp_path)

        assert result["ok"] is False
        assert_problem_contains(
            result["problems"],
            "frontend compiler surface",
            "deleted",
            _FRONTEND_PUBLIC_REL,
        )


class TestPublicApiTransitions:
    def test_additive_import_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from .models import Helper
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["Event", "Helper", "Tool", "create_app"]
            """,
        )
        _assert_python_changed(public_api.check_public_api(tmp_path))

        transition = _transition(
            tmp_path,
            "python",
            _INIT_REL,
            "Helper",
            "PKG-03-HARNESS-ORACLES",
            "Accept the additive Helper import.",
        )
        next_identity = _checkpoint(tmp_path)
        public_api.regenerate_public_api(
            tmp_path,
            next_identity,
            additive_transitions=[transition],
        )
        authority = public_api.load_public_api(tmp_path)
        result = public_api.check_public_api(tmp_path)

        assert authority["additive_transitions"] == [transition]
        assert result["ok"] is True

    def test_additive_definition_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        authority_path = tmp_path / "architecture/public-api.json"
        accepted_authority = authority_path.read_bytes()
        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(
                name: str, *, debug: bool = False, trace: bool = False
            ) -> str:
                return name

            __all__ = ["Event", "Tool", "create_app"]
            """,
        )
        attack_identity = _checkpoint(tmp_path)
        forged = public_api.load_public_api(tmp_path)
        forged["python_initializers"] = public_api.scan_python_public_surface(tmp_path)
        write(authority_path, json.dumps(forged, indent=2) + "\n")
        forged_bytes = authority_path.read_bytes()

        attack_result = public_api.check_public_api(tmp_path)
        assert attack_result["ok"] is False
        assert_problem_contains(
            attack_result["problems"], "immutable public API authority", "sibling"
        )
        with pytest.raises(RuntimeError, match="working prewrite authority"):
            public_api.regenerate_public_api(tmp_path, attack_identity)
        assert authority_path.read_bytes() == forged_bytes

        write(authority_path, accepted_authority.decode())
        _write_python_fixture(tmp_path)
        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            def health() -> str:
                return "ok"

            __all__ = ["Event", "Tool", "create_app", "health"]
            """,
        )
        _assert_python_changed(public_api.check_public_api(tmp_path))

        transition = _transition(
            tmp_path,
            "python",
            _INIT_REL,
            "health",
            "PKG-04-SERVERS",
            "Accept the additive health facade.",
        )
        before = (tmp_path / "architecture/public-api.json").read_bytes()
        invalid_rows: list[list[dict[str, Any]]] = [
            [],
            [{}],
            [{"anything": "passes"}],
            [{**transition, "owner_package": "not-a-package"}],
            [{**transition, "public_name": "missing"}],
        ]
        next_identity = _checkpoint(tmp_path)
        for rows in invalid_rows:
            with pytest.raises(RuntimeError, match="regeneration rejected"):
                public_api.regenerate_public_api(
                    tmp_path,
                    next_identity,
                    additive_transitions=rows,
                )
            assert (tmp_path / "architecture/public-api.json").read_bytes() == before
        public_api.regenerate_public_api(
            tmp_path,
            next_identity,
            additive_transitions=[transition],
        )
        result = public_api.check_public_api(tmp_path)

        assert result["ok"] is True
        assert _initializer_surface(tmp_path)["public_signatures"]["health"] == {
            "kind": "function",
            "signature": "def health() -> str",
        }

        _checkpoint(tmp_path)
        write(
            tmp_path / _FRONTEND_PUBLIC_REL,
            textwrap.dedent(_FRONTEND_PUBLIC_SOURCE)
            + textwrap.dedent(
                """\

                export function encode(value: Envelope): string {
                  return value.id;
                }
                """
            ),
        )
        unapproved = public_api.check_public_api(tmp_path)
        assert unapproved["ok"] is False
        assert_problem_contains(
            unapproved["problems"],
            "frontend compiler surface",
            "changed",
            _FRONTEND_PUBLIC_REL,
        )

        frontend_transition = _transition(
            tmp_path,
            "frontend",
            _FRONTEND_PUBLIC_REL,
            "encode",
            "PKG-12-FE-SHELL",
            "Accept the additive encode export.",
        )
        transitions = _sorted_metadata(transition, frontend_transition)
        next_identity = _checkpoint(tmp_path)
        public_api.regenerate_public_api(
            tmp_path,
            next_identity,
            additive_transitions=transitions,
        )
        authority = public_api.load_public_api(tmp_path)
        accepted = public_api.check_public_api(tmp_path)
        public_module = next(
            row for row in authority["frontend_modules"] if row["path"] == _FRONTEND_PUBLIC_REL
        )

        assert authority["additive_transitions"] == transitions
        assert {
            "kind": "function",
            "name": "encode",
            "signature": "export function encode(value: Envelope): string",
        } in public_module["public_declarations"]
        assert accepted["ok"] is True

        _checkpoint(tmp_path)
        _write_python_fixture(tmp_path)
        deletion_identity = _checkpoint(tmp_path)
        forged = public_api.load_public_api(tmp_path)
        forged["source_identity"] = deletion_identity
        forged["python_initializers"] = public_api.scan_python_public_surface(tmp_path)
        forged["additive_transitions"] = [frontend_transition]
        write(authority_path, json.dumps(forged, indent=2) + "\n")
        forged_bytes = authority_path.read_bytes()
        deletion = public_api.check_public_api(tmp_path)
        assert deletion["ok"] is False
        assert_problem_contains(deletion["problems"], "deletes targets")
        with pytest.raises(RuntimeError, match="working prewrite authority"):
            public_api.regenerate_public_api(tmp_path, deletion_identity)
        assert authority_path.read_bytes() == forged_bytes

    def test_additive_all_entry_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from .models import Helper
            from .models import PublicEvent as Event
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["Event", "Helper", "Tool", "create_app"]
            """,
        )
        _assert_python_changed(public_api.check_public_api(tmp_path))

        transition = _transition(
            tmp_path,
            "python",
            _INIT_REL,
            "Helper",
            "PKG-13-FACADES",
            "Accept the explicit Helper export.",
        )
        public_api.regenerate_public_api(
            tmp_path,
            _checkpoint(tmp_path),
            additive_transitions=[transition],
        )
        result = public_api.check_public_api(tmp_path)

        assert result["ok"] is True
        assert _initializer_surface(tmp_path)["explicit_all"] == [
            "Event",
            "Helper",
            "Tool",
            "create_app",
        ]

    def test_renamed_import_detected_as_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_repo(tmp_path, monkeypatch)
        _replace_initializer(
            tmp_path,
            """\
            from .models import PublicEvent as RenamedEvent
            from ..core import Tool

            def create_app(name: str, *, debug: bool = False) -> str:
                return name

            __all__ = ["RenamedEvent", "Tool", "create_app"]
            """,
        )
        transition = _transition(
            tmp_path,
            "python",
            _INIT_REL,
            "RenamedEvent",
            "PKG-03-HARNESS-ORACLES",
            "A rename must not erase Event.",
        )
        with pytest.raises(RuntimeError, match="deletes targets"):
            public_api.regenerate_public_api(
                tmp_path,
                _checkpoint(tmp_path),
                additive_transitions=[transition],
            )
        _assert_python_changed(public_api.check_public_api(tmp_path))
        surface = _initializer_surface(tmp_path)
        assert surface["import_origins"][0]["alias"] == "RenamedEvent"
        assert "Event" not in surface["public_names"]
