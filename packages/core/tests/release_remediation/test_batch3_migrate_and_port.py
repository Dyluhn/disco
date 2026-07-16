"""Batch-3 (adversarial non-bypass hardening) — the migrate-target proof + the
service port-contract schema guard.

Two detection defects the frozen adversarial harness pins (SPEC-02, MIGRATE-01..04):

* SPEC-02 — a directly-constructed ``ReleaseService`` whose ``start_cmd`` binds a public
  port through an env var that is NOT its ``port_env`` (or a hard-coded literal) is a
  port-contract bypass: the host publishes ingress on ``port_env`` while the process
  listens elsewhere, so the deployment is unreachable. The schema must REJECT it at model
  validation, before any emit.
* MIGRATE-01..04 — a resource ``migrate_cmd`` whose executable / file / script / package is
  not proven present in the SAME immutable tree/install is a false ``candidate`` (the
  one-shot migration exits before ingress). Each must fail closed to ``needs_review``; a
  migration whose target IS proven stays a ``candidate``.

These live OUTSIDE the frozen ``export_track1_closeout`` dirs (no marker), so they never
perturb the acceptance manifest.
"""

from __future__ import annotations

import json

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    ServiceRole,
)
from pydantic import ValidationError

# ---- shared helpers -----------------------------------------------------------

_NODE_SERVER = (
    "const http = require('http');\n"
    "const port = process.env.PORT;\n"
    "http.createServer((_req, res) => res.end('ok')).listen(port, '0.0.0.0');\n"
)


def _service(start_cmd: tuple[str, ...], *, port_env: str = "PORT", **kw: object) -> ReleaseService:
    return ReleaseService(
        id="web",
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.python,
        start_cmd=start_cmd,
        port_env=port_env,
        **kw,  # type: ignore[arg-type]
    )


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


def _node_intent(**updates: object) -> ReleaseIntent:
    data: dict[str, object] = {"runtime": RuntimeStrategy.node, "start_cmd": ("node", "server.js")}
    data.update(updates)
    return ReleaseIntent(**data)  # type: ignore[arg-type]


def _python_intent(requirements: str, **updates: object) -> tuple[dict[str, str], ReleaseIntent]:
    files = {
        "requirements.txt": requirements,
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    }
    data: dict[str, object] = {
        "runtime": RuntimeStrategy.python,
        "start_cmd": ("uvicorn", "main:app"),
    }
    data.update(updates)
    return files, ReleaseIntent(**data)  # type: ignore[arg-type]


def _assess(files: dict[str, str], intent: ReleaseIntent) -> ReleaseAssessment:
    return detect_release(files, intent=intent, provenance=Provenance()).assessment


# ===========================================================================
# SPEC-02 — the ReleaseService port-contract schema guard
# ===========================================================================


@pytest.mark.parametrize(
    ("start_cmd", "port_env"),
    [
        # the exact SPEC-02 attack: a bare --port whole-env-ref that is NOT port_env
        (("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${OTHER_PORT}"), "PORT"),
        # the =-joined form of the same foreign ref
        (("uvicorn", "main:app", "--port=${OTHER_PORT}"), "PORT"),
        # the combined gunicorn/hypercorn public-bind with a foreign env name
        (("gunicorn", "wsgi:app", "--bind", "0.0.0.0:${OTHER_PORT}"), "PORT"),
        # a hard-coded literal port (never the adapter-owned variable)
        (("uvicorn", "main:app", "--port", "9999"), "PORT"),
        # a hard-coded literal combined bind
        (("gunicorn", "wsgi:app", "--bind", "0.0.0.0:9999"), "PORT"),
    ],
)
def test_service_start_that_binds_a_foreign_or_literal_port_is_rejected(
    start_cmd: tuple[str, ...], port_env: str
) -> None:
    """SPEC-02: constructing a ``ReleaseService`` whose start_cmd binds a public port
    through an env var != port_env, or through a literal, must raise at model validation
    (before any emit) — the published ingress would otherwise be unreachable."""
    with pytest.raises(ValidationError):
        _service(start_cmd, port_env=port_env)


@pytest.mark.parametrize(
    ("start_cmd", "port_env", "runtime"),
    [
        # --port ${PORT} where PORT == port_env
        (
            ("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"),
            "PORT",
            RuntimeStrategy.python,
        ),
        # a non-default port_env, bound exactly
        (
            ("gunicorn", "wsgi:app", "--bind", "0.0.0.0:${APP_PORT}"),
            "APP_PORT",
            RuntimeStrategy.python,
        ),
        # no explicit port token — the adapter injects port_env
        (("python3", "-m", "uvicorn", "main:app"), "PORT", RuntimeStrategy.python),
        # a direct node start binds its port through SOURCE, not an argv token
        (("node", "server.js"), "PORT", RuntimeStrategy.node),
    ],
)
def test_service_that_binds_its_own_port_env_or_omits_the_port_is_accepted(
    start_cmd: tuple[str, ...], port_env: str, runtime: RuntimeStrategy
) -> None:
    """SPEC-02 positive: a start_cmd that binds ${port_env} exactly, or names no explicit
    port, stays valid — the guard rejects only a proven port mismatch, never a legitimate
    contract-abiding start."""
    service = ReleaseService(
        id="web", role=ServiceRole.ingress, runtime=runtime, start_cmd=start_cmd, port_env=port_env
    )
    assert service.start_cmd == start_cmd


def test_spec02_foreign_port_service_is_never_emitted() -> None:
    """The SPEC-02 shape (uvicorn binding ${OTHER_PORT} while port_env=PORT, OTHER_PORT a
    declared spec env var) must be rejected at ReleaseService construction — so a
    ReleaseSpec that would emit an unreachable ingress can never even be assembled."""
    with pytest.raises(ValidationError):
        ReleaseService(
            id="web",
            role=ServiceRole.ingress,
            runtime=RuntimeStrategy.python,
            install_cmd=("pip", "install", "uvicorn"),
            start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${OTHER_PORT}"),
            port_env="PORT",
        )


# ===========================================================================
# MIGRATE-01..04 — the resource migrate-target proof (fail closed)
# ===========================================================================


@pytest.mark.parametrize(
    "command",
    [
        ("missing-migrator", "apply"),  # MIGRATE-01: executable not installed/provided (node)
        ("node", "missing-migration.js"),  # MIGRATE-02: the file is absent from the tree
        ("npm", "run", "migrate"),  # MIGRATE-03: no `migrate` script in package.json
    ],
)
def test_node_migrate_with_unprovable_target_fails_closed(command: tuple[str, ...]) -> None:
    """MIGRATE-01..03: a node-app resource migrate_cmd whose target (executable / file /
    npm script) is not proven present in the immutable tree fails closed to needs_review."""
    result = detect_release(
        {"server.js": _NODE_SERVER},
        intent=_node_intent(resources=(_migration_resource(command),)),
        provenance=Provenance(),
    )
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_python_migrate_tool_absent_from_pip_plan_fails_closed() -> None:
    """MIGRATE-04: a python-app migrate_cmd heading with a tool (alembic) that the exact pip
    install plan does NOT install fails closed to needs_review."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\n", resources=(_migration_resource(("alembic", "upgrade", "head")),)
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_wrangler_bare_tool_under_node_fails_closed() -> None:
    """A bare non-node/npm migration tool under a NODE runtime is not provably provided by
    the neutral base image / install plan, so it fails closed (defense beyond the harness)."""
    result = detect_release(
        {"server.js": _NODE_SERVER},
        intent=_node_intent(
            resources=(_migration_resource(("wrangler", "d1", "migrations", "apply")),)
        ),
        provenance=Provenance(),
    )
    assert result.assessment is ReleaseAssessment.needs_review


# ---- positive controls: a PROVEN migration stays a candidate ------------------


def test_python_migrate_tool_present_in_pip_plan_is_candidate() -> None:
    """A python migration whose tool IS in the exact pip plan (alembic in requirements)
    stays a self-host candidate — the proof rejects only unprovable targets."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nalembic\n",
        resources=(_migration_resource(("alembic", "upgrade", "head")),),
    )
    assert _assess(files, intent) is ReleaseAssessment.candidate


def test_node_migrate_file_present_is_candidate() -> None:
    """A `node <file>` migration whose file ships in the tree stays a candidate."""
    files = {"server.js": _NODE_SERVER, "migrate.js": "process.exit(0);\n"}
    assert (
        _assess(files, _node_intent(resources=(_migration_resource(("node", "migrate.js")),)))
        is ReleaseAssessment.candidate
    )


def test_npm_run_migrate_with_defined_script_is_candidate() -> None:
    """An `npm run migrate` migration whose package.json defines the `migrate` script stays
    a candidate."""
    files = {
        "server.js": _NODE_SERVER,
        "package.json": json.dumps({"scripts": {"migrate": "node migrate.js"}}),
        "migrate.js": "process.exit(0);\n",
    }
    assert (
        _assess(files, _node_intent(resources=(_migration_resource(("npm", "run", "migrate")),)))
        is ReleaseAssessment.candidate
    )


def test_empty_migrate_cmd_is_unaffected() -> None:
    """A resource with an EMPTY migrate_cmd imposes no migrate-target proof — it stays a
    candidate exactly as before."""
    assert (
        _assess({"server.js": _NODE_SERVER}, _node_intent(resources=(_migration_resource(()),)))
        is ReleaseAssessment.candidate
    )


def test_declared_credential_ref_on_proven_python_migrate_is_candidate() -> None:
    """A migration credential passed as a whole DECLARED ${NAME} reference, on a migration
    whose tool IS installed, stays a candidate — the migrate-target proof and the
    inline-secret rail both pass a benign, provable migration."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nalembic\n",
        required_env=("DB_TOKEN",),
        resources=(_migration_resource(("alembic", "upgrade", "head", "--token", "${DB_TOKEN}")),),
    )
    assert _assess(files, intent) is ReleaseAssessment.candidate


# ===========================================================================
# CORRECTION ROUND 2 — the residual holes a fresh adversary CONSTRUCTED
# ===========================================================================
#
# Round 1 fixed the enumerated harness cases; a fresh adversary then found REAL
# residual holes of the SAME class that the round-1 proofs were UNSOUND against, in
# both directions. Each finding below carries a NEGATIVE + POSITIVE regression.


# ---- Defense 2 FALSE CANDIDATES (must fail closed to needs_review) ------------


def test_fc1_python_bare_tool_without_same_named_console_script_fails_closed() -> None:
    """D2-FC1: a bare python migrate tool whose DISTRIBUTION is installed but ships NO
    same-named CONSOLE SCRIPT (``requests`` in requirements, migrate ``requests``) is NOT
    provably runnable — a distribution's NAME does not certify a console script. It must fail
    closed, not ride the unsound distribution-name==runnable rule to a false candidate."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nrequests\n",
        resources=(_migration_resource(("requests", "migrate")),),
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_fc1_positive_bare_tool_present_console_script_stays_candidate() -> None:
    """D2-FC1 positive: the SAME bare-tool shape whose tool IS a known migration CLI present
    in the plan (alembic) stays a candidate — the fix rejects only the unprovable console
    script, never a legitimately-installed migration CLI."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nalembic\n",
        resources=(_migration_resource(("alembic", "upgrade", "head")),),
    )
    assert _assess(files, intent) is ReleaseAssessment.candidate


def test_fc2_npm_run_script_body_invoking_missing_tool_fails_closed() -> None:
    """D2-FC2: an ``npm run migrate`` whose script is DEFINED but whose BODY invokes a tool
    the image cannot run (``alembic upgrade head`` in a Node image) must fail closed — the
    proof recurses into the body, not merely the script NAME."""
    files = {
        "server.js": _NODE_SERVER,
        "package.json": json.dumps(
            {"name": "s", "scripts": {"start": "node server.js", "migrate": "alembic upgrade head"}}
        ),
    }
    result = detect_release(
        files,
        intent=_node_intent(resources=(_migration_resource(("npm", "run", "migrate")),)),
        provenance=Provenance(),
    )
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_fc2_positive_npm_run_script_body_resolving_stays_candidate() -> None:
    """D2-FC2 positive: an ``npm run migrate`` whose body IS provable (``node migrate.js`` and
    migrate.js ships) stays a candidate — the body recursion accepts a resolving target."""
    files = {
        "server.js": _NODE_SERVER,
        "package.json": json.dumps(
            {"name": "s", "scripts": {"start": "node server.js", "migrate": "node migrate.js"}}
        ),
        "migrate.js": "process.exit(0);\n",
    }
    assert (
        _assess(files, _node_intent(resources=(_migration_resource(("npm", "run", "migrate")),)))
        is ReleaseAssessment.candidate
    )


def test_fc3_node_migrate_on_non_js_file_fails_closed() -> None:
    """D2-FC3: ``node schema.sql`` (the .sql SHIPS) is a runtime SyntaxError, not a runnable
    migration — a ``node <file>`` target must have a Node-executable extension."""
    files = {"server.js": _NODE_SERVER, "schema.sql": "CREATE TABLE t(x);\n"}
    result = detect_release(
        files,
        intent=_node_intent(resources=(_migration_resource(("node", "schema.sql")),)),
        provenance=Provenance(),
    )
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_fc3_positive_node_migrate_on_js_file_stays_candidate() -> None:
    """D2-FC3 positive: the SAME ``node <file>`` shape on a .js file that ships stays a
    candidate — the fix rejects only the non-executable extension."""
    files = {"server.js": _NODE_SERVER, "migrate.js": "process.exit(0);\n"}
    assert (
        _assess(files, _node_intent(resources=(_migration_resource(("node", "migrate.js")),)))
        is ReleaseAssessment.candidate
    )


# ---- Defense 2 OVER-REJECTIONS (must be provable candidates) ------------------


def test_or1_python_dash_m_module_in_install_plan_is_candidate() -> None:
    """D2-OR1: ``python -m alembic upgrade head`` with alembic in the pip plan is a canonical,
    provable migration — the module's providing package IS installed, so it is a candidate."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nalembic\n",
        resources=(_migration_resource(("python", "-m", "alembic", "upgrade", "head")),),
    )
    assert _assess(files, intent) is ReleaseAssessment.candidate


def test_or1_negative_python_dash_m_module_absent_from_plan_fails_closed() -> None:
    """D2-OR1 negative: ``python -m alembic`` with alembic ABSENT from the pip plan fails
    closed — the module's providing package must be proven installed."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\n",
        resources=(_migration_resource(("python", "-m", "alembic", "upgrade", "head")),),
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_or2_python_manage_py_migrate_when_script_ships_is_candidate() -> None:
    """D2-OR2: the canonical Django ``python manage.py migrate`` with manage.py SHIPPED is a
    candidate — the script file is proven present in the immutable tree."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\ndjango\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "manage.py": "import sys\n",
    }
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python,
        start_cmd=("uvicorn", "main:app"),
        resources=(_migration_resource(("python", "manage.py", "migrate")),),
    )
    assert _assess(files, intent) is ReleaseAssessment.candidate


def test_or2_negative_python_script_absent_from_tree_fails_closed() -> None:
    """D2-OR2 negative: ``python manage.py migrate`` when manage.py does NOT ship fails
    closed — the script file must be present in the immutable tree."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\ndjango\n",
        resources=(_migration_resource(("python", "manage.py", "migrate")),),
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


# ===========================================================================
# Defense 1 — the port-contract BELT must FAIL CLOSED at ReleaseService
# ===========================================================================


@pytest.mark.parametrize(
    ("start_cmd", "port_env", "runtime"),
    [
        # D1-1 duplicate --port: click last-wins binds the literal 9999 (parse aborts on the
        # duplicate, so the round-1 token extractor failed open) — the scan still catches it.
        (
            ("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}", "--port", "9999"),
            "PORT",
            RuntimeStrategy.python,
        ),
        # D1-2 loopback host: unreachable from the published ingress even with the right port.
        (
            ("uvicorn", "main:app", "--host", "127.0.0.1", "--port", "${PORT}"),
            "PORT",
            RuntimeStrategy.python,
        ),
        # D1-3 unix socket bind: no TCP listener the published ingress can reach.
        (("hypercorn", "main:app", "--bind", "unix:/tmp/x.sock"), "PORT", RuntimeStrategy.python),
        # D1-4 literal port amid an unknown trailing flag (the parse-abort fail-open).
        (("uvicorn", "main:app", "--port", "9999", "--frobnicate"), "PORT", RuntimeStrategy.python),
        # a loopback COMBINED bind (gunicorn -b 127.0.0.1:PORT) is equally unreachable.
        (("gunicorn", "wsgi:app", "-b", "127.0.0.1:8000"), "PORT", RuntimeStrategy.python),
        # a foreign ${VAR} port riding an unknown trailing flag is still positively wrong.
        (
            ("uvicorn", "main:app", "--port", "${OTHER_PORT}", "--frobnicate"),
            "PORT",
            RuntimeStrategy.python,
        ),
    ],
)
def test_belt_rejects_provably_wrong_public_bind(
    start_cmd: tuple[str, ...], port_env: str, runtime: RuntimeStrategy
) -> None:
    """The port-contract belt FAILS CLOSED by positive proof at ReleaseService construction:
    a recognized dedicated-server start whose ANALYZABLE bind is a literal/foreign port, a
    loopback interface, or a non-TCP socket — even amid a duplicate or unknown-flag parse
    abort — raises before any emit."""
    with pytest.raises(ValidationError):
        ReleaseService(
            id="web",
            role=ServiceRole.ingress,
            runtime=runtime,
            start_cmd=start_cmd,
            port_env=port_env,
        )


@pytest.mark.parametrize(
    ("start_cmd", "port_env", "runtime"),
    [
        # binds ${port_env} on the public wildcard — the contract-abiding shape.
        (
            ("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"),
            "PORT",
            RuntimeStrategy.python,
        ),
        # the sanctioned combined public bind.
        (("gunicorn", "wsgi:app", "--bind", "0.0.0.0:${PORT}"), "PORT", RuntimeStrategy.python),
        # NO explicit port token: the adapter injects port_env.
        (("uvicorn", "main:app"), "PORT", RuntimeStrategy.python),
        # a direct node start binds its port through SOURCE, not an argv token — belt defers.
        (("node", "server.js"), "PORT", RuntimeStrategy.node),
        # a --config-only bind lives inside the config file: un-analyzable at the schema layer,
        # so the belt DEFERS (detection is the primary guard) rather than fail closed.
        (("gunicorn", "wsgi:app", "--config", "gunicorn.conf.py"), "PORT", RuntimeStrategy.python),
    ],
)
def test_belt_preserves_contract_abiding_or_unanalyzable_starts(
    start_cmd: tuple[str, ...], port_env: str, runtime: RuntimeStrategy
) -> None:
    """The belt constructs (never over-rejects) a start that binds ${port_env} exactly, names
    no explicit port (the adapter injects it), is source/adapter-bound (``node server.js``),
    or carries a bind the schema layer genuinely cannot analyze (``--config`` only)."""
    service = ReleaseService(
        id="web", role=ServiceRole.ingress, runtime=runtime, start_cmd=start_cmd, port_env=port_env
    )
    assert service.start_cmd == start_cmd


# ===========================================================================
# CORRECTION ROUND 3 — a second fresh adversary's residual holes
# ===========================================================================
#
# The round-2 proofs still admitted these (all reproduced by the orchestrator):
#   D1 — a loopback SUBNET host (127.0.0.0/8 beyond the single 127.0.0.1 literal) and an
#        EMPTY-host literal bind (`:9999`) slipping the port-contract belt;
#   D2 — migrate-target proofs that trusted the wrong providing DISTRIBUTION (`flask db`
#        needs Flask-Migrate, not Flask), accepted a `python -m <module>` that is not
#        `-m`-runnable, never validated a `.py` migrate body, or over-rejected the common
#        `npm run <node-cli>` case. Each finding below carries BOTH directions.


# ---- D1: loopback-subnet + empty-host literal binds (belt, fail closed) --------


@pytest.mark.parametrize(
    "start_cmd",
    [
        # 1a: a 127.0.0.0/8 loopback host OTHER than the bare 127.0.0.1 literal — still
        # unreachable from the published ingress, but the round-2 4-element set missed it.
        ("uvicorn", "main:app", "--host", "127.0.0.2", "--port", "${PORT}"),
        ("uvicorn", "main:app", "--host", "127.0.1.1", "--port", "${PORT}"),
        # 127.0.0.1 itself STILL rejected (the round-2 loopback win is preserved).
        ("uvicorn", "main:app", "--host", "127.0.0.1", "--port", "${PORT}"),
        # a loopback-SUBNET COMBINED bind is equally unreachable.
        ("gunicorn", "wsgi:app", "--bind", "127.0.0.9:8000"),
        # 1b: an EMPTY/omitted host with a hard-coded literal port binds all interfaces on the
        # WRONG port — as positively wrong as `0.0.0.0:9999`, but the literal regex (which
        # requires a non-empty host) let it defer.
        ("gunicorn", "wsgi:app", "--bind", ":9999"),
    ],
)
def test_d1_belt_rejects_loopback_subnet_and_empty_host_literal_binds(
    start_cmd: tuple[str, ...],
) -> None:
    """D1: the port-contract belt must fail closed at ReleaseService construction for a
    loopback host anywhere in 127.0.0.0/8 (not just the single 127.0.0.1 literal) and for an
    empty-host literal bind (`:9999`) — both leave the published ingress unreachable."""
    with pytest.raises(ValidationError):
        _service(start_cmd)


@pytest.mark.parametrize(
    ("start_cmd", "port_env"),
    [
        # a PUBLIC (non-loopback) host that is not the wildcard still constructs — the belt
        # positively rejects only loopback, deferring otherwise (never over-rejecting).
        (("uvicorn", "main:app", "--host", "10.0.0.1", "--port", "${PORT}"), "PORT"),
        # the sanctioned combined public bind — the 1b empty-host LITERAL fix must not catch
        # the env-REFERENCED adapter bind (the constructible empty-host counterpart is exactly
        # this `0.0.0.0:${PORT}` form; `:${PORT}` is rejected earlier by token hygiene — see
        # test_d1_belt_defers_on_empty_host_adapter_ref_bind below).
        (("gunicorn", "wsgi:app", "--bind", "0.0.0.0:${PORT}"), "PORT"),
    ],
)
def test_d1_belt_preserves_public_host_and_adapter_ref_bind(
    start_cmd: tuple[str, ...], port_env: str
) -> None:
    """D1 positive: a public (non-loopback) `--host`, and the sanctioned `0.0.0.0:${port_env}`
    combined bind, still construct — the loopback-subnet + empty-host-literal fixes reject only
    positively-wrong binds."""
    service = _service(start_cmd, port_env=port_env)
    assert service.start_cmd == start_cmd


def test_d1_belt_defers_on_empty_host_adapter_ref_bind() -> None:
    """D1 (1b) unit regression: the empty-host LITERAL reject must not catch the env-REFERENCED
    empty-host bind. At the belt, `:${PORT}` DEFERS (returns None — not a positively-wrong
    literal) while `:9999` is rejected. (At ReleaseService construction `:${PORT}` never even
    reaches the belt — a token bearing a non-whole `$` is rejected upstream by
    check_token_hygiene, so the sanctioned constructible adapter bind is `0.0.0.0:${PORT}`,
    covered above.)"""
    from disco.core.release.command_grammar import _bind_value_reason

    assert _bind_value_reason(":${PORT}", "PORT") is None
    assert _bind_value_reason(":9999", "PORT") is not None
    # And it is genuinely rejected at construction (by token hygiene, before the belt).
    with pytest.raises(ValidationError):
        _service(("gunicorn", "wsgi:app", "--bind", ":${PORT}"))


# ---- D2 (2a): flask db needs the Flask-Migrate distribution, not Flask ----------


def _flask_db_case(requirements: str) -> tuple[dict[str, str], ReleaseIntent]:
    """A gunicorn-served Flask app whose sole migration is `flask db upgrade` (the `db`
    subcommand group is provided by the SEPARATE Flask-Migrate distribution)."""
    files = {
        "requirements.txt": requirements,
        "wsgi.py": "from flask import Flask\napp = Flask(__name__)\n",
    }
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python,
        start_cmd=("gunicorn", "wsgi:app"),
        resources=(_migration_resource(("flask", "db", "upgrade")),),
    )
    return files, intent


def test_2a_flask_db_without_flask_migrate_fails_closed() -> None:
    """D2-2a: `flask db upgrade` with Flask but NO Flask-Migrate is a FALSE candidate — the
    `db` command group is registered by Flask-Migrate, so without it `flask db` errors. It must
    fail closed; proving only the `flask` console-script dist is present is unsound."""
    files, intent = _flask_db_case("flask\ngunicorn\n")
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_2a_flask_db_with_flask_migrate_is_candidate() -> None:
    """D2-2a positive: the SAME `flask db upgrade` with Flask-Migrate in the pip plan is a
    candidate — the distribution that provides the migration capability is proven installed."""
    files, intent = _flask_db_case("flask\nflask-migrate\ngunicorn\n")
    assert _assess(files, intent) is ReleaseAssessment.candidate


# ---- D2 (2b): `python -m <module>` must be a known -m-runnable migration module -


def test_2b_python_dash_m_non_runnable_module_fails_closed() -> None:
    """D2-2b: `python -m requests` (requests in the plan) proves only that the dist is
    installed, never that the module is `-m`-runnable — `requests` has no `__main__`, so the
    command is a runtime error. It must fail closed, not ride the dist-installed check."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nrequests\n",
        resources=(_migration_resource(("python", "-m", "requests")),),
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_2b_python_dash_m_alembic_stays_candidate() -> None:
    """D2-2b positive (OR1 preserved): `python -m alembic` with alembic installed stays a
    candidate — alembic is a curated, genuinely `-m`-runnable migration module (it ships
    `alembic/__main__.py`)."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\nalembic\n",
        resources=(_migration_resource(("python", "-m", "alembic", "upgrade", "head")),),
    )
    assert _assess(files, intent) is ReleaseAssessment.candidate


# ---- D2 (2c-py): a `.py` migrate file must parse as valid Python -----------------


def test_2c_py_garbage_python_migrate_file_fails_closed() -> None:
    """D2-2c-py: a shipped `migrate.py` whose body is shell (`#!/bin/sh ... apt-get moo`) is a
    runtime SyntaxError, not a runnable migration — a `.py` EXTENSION is not proof of valid
    Python, so ast.parse catches it and it fails closed."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\n",
        resources=(_migration_resource(("python", "migrate.py")),),
    )
    files["migrate.py"] = "#!/bin/sh\necho hi\napt-get moo\n"
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]


def test_2c_py_valid_python_migrate_file_is_candidate() -> None:
    """D2-2c-py positive: the SAME `python migrate.py` on a file that ships AND parses as valid
    Python stays a candidate — the ast.parse gate rejects only an unparseable body."""
    files, intent = _python_intent(
        "fastapi\nuvicorn\n",
        resources=(_migration_resource(("python", "migrate.py")),),
    )
    files["migrate.py"] = "import sys\n\nprint('migrating')\nsys.exit(0)\n"
    assert _assess(files, intent) is ReleaseAssessment.candidate


# ---- D2 (2d): a Node migration-CLI allowlist (node_modules/.bin) -----------------


def _node_migrate_files(pkg: dict[str, object]) -> dict[str, str]:
    return {"server.js": _NODE_SERVER, "package.json": json.dumps(pkg)}


@pytest.mark.parametrize(
    ("pkg", "command"),
    [
        # `npm run migrate` whose body is a known Node CLI, its package in dependencies — npm
        # prepends node_modules/.bin to PATH, so the bin provably exists.
        (
            {
                "dependencies": {"prisma": "5.0.0"},
                "scripts": {"start": "node server.js", "migrate": "prisma migrate deploy"},
            },
            ("npm", "run", "migrate"),
        ),
        (
            {
                "dependencies": {"knex": "3.0.0"},
                "scripts": {"start": "node server.js", "migrate": "knex migrate:latest"},
            },
            ("npm", "run", "migrate"),
        ),
        # the `sequelize` CLI is provided by the SEPARATE `sequelize-cli` package.
        (
            {
                "dependencies": {"sequelize-cli": "6.0.0"},
                "scripts": {"start": "node server.js", "migrate": "sequelize db:migrate"},
            },
            ("npm", "run", "migrate"),
        ),
        # a DIRECT bare-tool migrate_cmd (not via npm run) resolves through the same allowlist.
        (
            {"dependencies": {"prisma": "5.0.0"}, "scripts": {"start": "node server.js"}},
            ("prisma", "migrate", "deploy"),
        ),
    ],
)
def test_2d_node_migration_cli_in_dependencies_is_candidate(
    pkg: dict[str, object], command: tuple[str, ...]
) -> None:
    """D2-2d positive: a known Node migration CLI whose providing npm package is in the
    package.json dependencies is a candidate — both as an `npm run <script>` body and as a
    direct bare-tool migrate_cmd."""
    files = _node_migrate_files(pkg)
    assert (
        _assess(files, _node_intent(resources=(_migration_resource(command),)))
        is ReleaseAssessment.candidate
    )


@pytest.mark.parametrize(
    ("pkg", "command"),
    [
        # a known CLI whose package is ONLY in devDependencies (a production install may prune
        # them) — fail closed, never a false candidate.
        (
            {
                "devDependencies": {"prisma": "5.0.0"},
                "scripts": {"start": "node server.js", "migrate": "prisma migrate deploy"},
            },
            ("npm", "run", "migrate"),
        ),
        # an UNLISTED tool is never provably provided — fail closed (safe over-rejection).
        (
            {
                "dependencies": {"whatever": "1.0.0"},
                "scripts": {"start": "node server.js", "migrate": "whatever-migrate up"},
            },
            ("npm", "run", "migrate"),
        ),
    ],
)
def test_2d_unlisted_or_absent_node_cli_fails_closed(
    pkg: dict[str, object], command: tuple[str, ...]
) -> None:
    """D2-2d negative: an unlisted Node tool, or a known CLI whose package is absent from
    (production) dependencies, is not provably runnable and fails closed."""
    files = _node_migrate_files(pkg)
    result = detect_release(
        files,
        intent=_node_intent(resources=(_migration_resource(command),)),
        provenance=Provenance(),
    )
    assert result.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in result.blockers] == ["migrate_target_unresolved"]
