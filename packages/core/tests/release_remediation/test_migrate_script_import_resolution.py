"""Correction Round 4 — the migrate-SCRIPT import/require proof, made SYMMETRIC with the
entrypoint proof.

The proven class-A defect: the migrate-target proof validated a ``python <script>`` /
``node <script>`` migration's PRESENCE + extension + (Python) ``ast.parse``, but never
resolved the script's OWN ``import`` / ``require()`` statements against the proven install
plan — although the ENTRYPOINT proof does exactly that. So a migration script importing
something that will NOT be installed graded ``candidate`` yet crashed at runtime; because the
emitted compose gates ``web`` on ``migrate: service_completed_successfully`` (migrate
``restart: no``), that crash makes the whole deployment unreachable.

The fix reuses the entrypoint machinery, not a second parser:

* Python — ``python_script_import_error`` -> ``_module_imports_are_proven`` over the same
  install plan ∪ shipped workspace ``.py`` ∪ stdlib. The script runs as top-level ``__main__``
  (``package == ()``), so relative imports fail closed exactly as CPython raises them.
* Node — ``prove_node_source`` (the entrypoint's node prover: builtins + shipped-local modules
  resolve, external require roots are returned) reconciled against the PRODUCTION
  ``dependencies`` only, with LOWERCASE-ONLY npm normalization (``socket.io`` != ``socket-io``).

Both blockers carry the shared ``migrate_target_unresolved`` code with a DISTINCTIVE, greppable
message so the orchestrator can histogram this defect among failures.

Lives OUTSIDE the frozen ``export_track1_closeout`` dirs (no marker), so it never perturbs the
acceptance manifest.
"""

from __future__ import annotations

import json

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
)

_PY_APP = "from fastapi import FastAPI\napp = FastAPI()\n"
_NODE_SERVER = (
    "const http = require('http');\n"
    "http.createServer((_q, r) => r.end('ok')).listen(process.env.PORT);\n"
)

# The distinctive, greppable message substrings the fix introduces (histogram keys).
_PY_IMPORT_MSG = "not backed by the install plan or a shipped workspace module"
_NODE_REQUIRE_MSG = "resolves to no installed dependency or shipped module"
_NODE_GRAPH_MSG = "require() graph is not statically provable"


def _migration_resource(command: tuple[str, ...]) -> ResourceDecl:
    return ResourceDecl(
        id="db",
        kind=ResourceKind.sqlite,
        persistent_path="/data/app.db",
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
        ),
        consumers=("web",),
        migrate_cmd=command,
    )


def _py(files: dict[str, str], command: tuple[str, ...]) -> ReleaseIntent:
    return ReleaseIntent(
        runtime=RuntimeStrategy.python,
        start_cmd=("uvicorn", "main:app"),
        resources=(_migration_resource(command),),
    )


def _node(command: tuple[str, ...]) -> ReleaseIntent:
    return ReleaseIntent(
        runtime=RuntimeStrategy.node,
        start_cmd=("node", "server.js"),
        resources=(_migration_resource(command),),
    )


def _run(files: dict[str, str], intent: ReleaseIntent):
    return detect_release(files, intent=intent, provenance=Provenance())


def _assert_migrate_blocked(
    files: dict[str, str], intent: ReleaseIntent, message_substr: str
) -> None:
    result = _run(files, intent)
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]
    assert any(message_substr in b.message for b in result.blockers), (
        f"expected {message_substr!r} in blocker messages: {[b.message for b in result.blockers]}"
    )


def _assert_candidate(files: dict[str, str], intent: ReleaseIntent) -> None:
    result = _run(files, intent)
    assert result.assessment is ReleaseAssessment.candidate, [b.message for b in result.blockers]


# ===========================================================================
# DEFECTS — the class-A false candidates (must fail closed to needs_review)
# ===========================================================================


def test_a1_python_migrate_imports_absent_package_fails_closed() -> None:
    """A1: ``python migrate.py`` where migrate.py imports a package absent from requirements
    is a ModuleNotFoundError at boot — a false candidate. It must fail closed."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": "import zzz_totally_absent_pkg_9999 as z\nz.run()\n",
    }
    _assert_migrate_blocked(files, _py(files, ("python", "migrate.py")), _PY_IMPORT_MSG)


def test_a1prime_python_migrate_imports_sqlalchemy_not_in_reqs_fails_closed() -> None:
    """A1': the single most common real developer mistake — a forgotten ``import sqlalchemy``
    that is not in requirements.txt — grades candidate under the old proof, yet crashes. It
    must fail closed (this is INSIDE the ordinary-app boundary)."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": "import sqlalchemy\nprint(sqlalchemy.__version__)\n",
    }
    _assert_migrate_blocked(files, _py(files, ("python", "migrate.py")), _PY_IMPORT_MSG)


def test_a2_node_migrate_requires_package_absent_from_deps_fails_closed() -> None:
    """A2: ``node migrate.js`` where migrate.js ``require('knex')`` but knex is NOT in
    package.json dependencies is a ``Cannot find module 'knex'`` at boot. It must fail closed."""
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const knex = require('knex');\nknex();\n",
    }
    _assert_migrate_blocked(files, _node(("node", "migrate.js")), _NODE_REQUIRE_MSG)


# ---- transitive closure: a shipped-local module's OWN unbacked import fails closed ---------


def test_python_migrate_transitive_local_import_absent_fails_closed() -> None:
    """A migrate.py that imports a shipped helper whose OWN module body imports an absent
    distribution is still unrunnable — the reused proof recurses transitively (symmetry with
    the entrypoint's transitive import safety)."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "helpers.py": "import zzz_absent_transitive_pkg\n",
        "migrate.py": "import helpers\nhelpers.run()\n",
    }
    _assert_migrate_blocked(files, _py(files, ("python", "migrate.py")), _PY_IMPORT_MSG)


def test_node_migrate_transitive_local_require_absent_fails_closed() -> None:
    """A migrate.js requiring a shipped-local module that itself requires an undeclared
    external package fails closed — ``prove_node_source`` proves the whole local graph."""
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "server.js": _NODE_SERVER,
        "util.js": "const knex = require('knex');\nmodule.exports = knex;\n",
        "migrate.js": "const u = require('./util.js');\nu();\n",
    }
    _assert_migrate_blocked(files, _node(("node", "migrate.js")), _NODE_REQUIRE_MSG)


# ---- top-level relative import / missing-local graph fail closed (symmetry) ----------------


def test_python_migrate_top_level_relative_import_fails_closed() -> None:
    """A top-level ``python migrate.py`` has no parent package, so a relative import is an
    ``ImportError`` at boot — it must fail closed exactly as CPython raises it."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": "from . import helpers\n",
    }
    _assert_migrate_blocked(files, _py(files, ("python", "migrate.py")), _PY_IMPORT_MSG)


def test_node_migrate_missing_local_require_fails_closed() -> None:
    """A migrate.js requiring a local module that is NOT in the tree fails closed — the node
    prover cannot resolve the require graph."""
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const u = require('./missing.js');\nu();\n",
    }
    _assert_migrate_blocked(files, _node(("node", "migrate.js")), _NODE_GRAPH_MSG)


# ===========================================================================
# CONTROLS — provable migrations must STAY candidate (the over-rejection line)
# ===========================================================================


def test_c1_python_migrate_imports_installed_package_is_candidate() -> None:
    """C1: ``import fastapi`` where fastapi IS in requirements stays a candidate."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": "import fastapi\nprint(fastapi.__version__)\n",
    }
    _assert_candidate(files, _py(files, ("python", "migrate.py")))


def test_c2_python_migrate_imports_shipped_local_module_is_candidate() -> None:
    """C2: ``import helpers`` where helpers.py ships in the tree stays a candidate."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "helpers.py": "def run():\n    return 1\n",
        "migrate.py": "import helpers\nhelpers.run()\n",
    }
    _assert_candidate(files, _py(files, ("python", "migrate.py")))


def test_c3_python_migrate_stdlib_only_is_candidate() -> None:
    """C3: a stdlib-only migration (``import os, sqlite3``) stays a candidate."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": "import os, sqlite3\nprint(os.getcwd())\n",
    }
    _assert_candidate(files, _py(files, ("python", "migrate.py")))


def test_c4_node_migrate_requires_listed_dependency_is_candidate() -> None:
    """C4: ``require('knex')`` where knex IS a production dependency stays a candidate."""
    files = {
        "package.json": json.dumps({"dependencies": {"knex": "3.0.0"}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const knex = require('knex');\nknex();\n",
    }
    _assert_candidate(files, _node(("node", "migrate.js")))


def test_c5_node_migrate_requires_shipped_local_module_is_candidate() -> None:
    """C5: ``require('./util.js')`` where util.js ships stays a candidate."""
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "server.js": _NODE_SERVER,
        "util.js": "module.exports.run = () => 1;\n",
        "migrate.js": "const u = require('./util.js');\nu.run();\n",
    }
    _assert_candidate(files, _node(("node", "migrate.js")))


def test_c6_node_migrate_requires_node_builtin_is_candidate() -> None:
    """C6: ``require('fs')`` (a node builtin) stays a candidate."""
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const fs = require('fs');\nfs.readdirSync('.');\n",
    }
    _assert_candidate(files, _node(("node", "migrate.js")))


def test_django_manage_py_migrate_stays_candidate() -> None:
    """The canonical Django ``python manage.py migrate`` with a shipped, stdlib-only manage.py
    (and django installed) stays a candidate — the import proof accepts an ``import sys``
    manage.py exactly as before."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\ndjango\n",
        "main.py": _PY_APP,
        "manage.py": "import sys\n",
    }
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python,
        start_cmd=("uvicorn", "main:app"),
        resources=(_migration_resource(("python", "manage.py", "migrate")),),
    )
    _assert_candidate(files, intent)


# ===========================================================================
# SOUNDNESS — reconciliation & sys.path[0] base (the fix's deliberate choices)
# ===========================================================================


def test_node_reconciliation_uses_lowercase_npm_semantics_not_pep503() -> None:
    """A dotted npm package (``socket.io``) that IS declared must stay a candidate: npm treats
    ``socket.io`` and ``socket-io`` as DISTINCT packages, so the reconciliation is lowercase-only
    (NOT the PEP503 dash-collapse ``_node_dependency_names`` applies to the curated migration-CLI
    allowlist). PEP503 here would over-reject this ordinary app."""
    files = {
        "package.json": json.dumps({"dependencies": {"socket.io": "4.7.0"}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const io = require('socket.io');\nio();\n",
    }
    _assert_candidate(files, _node(("node", "migrate.js")))


def test_node_reconciliation_dotted_package_undeclared_fails_closed() -> None:
    """The same dotted require with NO matching dependency still fails closed — the lowercase
    reconciliation is sound in BOTH directions."""
    files = {
        "package.json": json.dumps({"dependencies": {}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const io = require('socket.io');\nio();\n",
    }
    _assert_migrate_blocked(files, _node(("node", "migrate.js")), _NODE_REQUIRE_MSG)


def test_node_devdependency_only_migrate_require_fails_closed() -> None:
    """A require whose package is ONLY in devDependencies (pruned from the production image)
    fails closed — the migration one-shot runs in production."""
    files = {
        "package.json": json.dumps({"devDependencies": {"knex": "3.0.0"}}),
        "server.js": _NODE_SERVER,
        "migrate.js": "const knex = require('knex');\nknex();\n",
    }
    _assert_migrate_blocked(files, _node(("node", "migrate.js")), _NODE_REQUIRE_MSG)


def test_python_migrate_in_subdir_resolves_local_against_script_dir() -> None:
    """``python db/migrate.py`` runs from db/ (CPython puts the script's dir on sys.path[0]),
    so a sibling ``db/schema.py`` resolves — the base is the SCRIPT's directory, not the repo
    root. This must stay a candidate (soundness of the base computation)."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "db/schema.py": "def apply():\n    return 1\n",
        "db/migrate.py": "import schema\nschema.apply()\n",
    }
    _assert_candidate(files, _py(files, ("python", "db/migrate.py")))


def test_python_migrate_in_subdir_does_not_resolve_repo_root_sibling() -> None:
    """The mirror of the above: ``python db/migrate.py`` importing a module that exists only at
    the REPO ROOT (``helpers.py``), not beside the script, is a ModuleNotFoundError — sys.path[0]
    is db/, so root-level modules are NOT importable. It must fail closed."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "helpers.py": "def run():\n    return 1\n",
        "db/migrate.py": "import helpers\nhelpers.run()\n",
    }
    _assert_migrate_blocked(files, _py(files, ("python", "db/migrate.py")), _PY_IMPORT_MSG)


@pytest.mark.parametrize(
    "body",
    [
        "import sys\n\nprint('migrating')\nsys.exit(0)\n",  # a legitimate top-level sys.exit(0)
        "import os\nif os.environ.get('X'):\n    raise SystemExit(1)\n",  # a conditional abort
    ],
)
def test_python_migrate_is_not_required_to_avoid_module_scope_exit(body: str) -> None:
    """A migration SCRIPT legitimately runs to completion and may ``sys.exit(0)`` — the reused
    proof checks ONLY import resolution, never the entrypoint's no-unconditional-abort rule, so a
    stdlib-only script that exits stays a candidate (no over-rejection beyond imports)."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": body,
    }
    _assert_candidate(files, _py(files, ("python", "migrate.py")))
