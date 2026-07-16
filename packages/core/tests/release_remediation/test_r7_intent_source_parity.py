"""R7/Round-5: exact effective-plan proof for every interpreted release candidate.

These tests live outside the frozen closeout manifest. They attack the public parser,
raw-sidecar detection, neutral spec, and emitter boundaries without retaining a second
argv scanner in the detector.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from disco.core.release.command_grammar import parse_effective_start_argv
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseSpec,
    SecretClass,
)
from pydantic import ValidationError

_FASTAPI: dict[str, bytes] = {
    "requirements.txt": b"fastapi==0.111\nuvicorn==0.30\n",
    "main.py": b"from fastapi import FastAPI\napp = FastAPI()\n",
}
_GUNICORN: dict[str, bytes] = {
    "requirements.txt": b"gunicorn==22\n",
    "wsgi.py": b"def app(environ, start_response):\n    return []\n",
}
_HYPERCORN: dict[str, bytes] = {
    "requirements.txt": b"hypercorn==0.17\n",
    "asgi.py": b"async def app(scope, receive, send):\n    pass\n",
}
_NODE_SOURCE = (
    b"const http = require('http');\n"
    b"http.createServer((req,res)=>res.end('ok')).listen(process.env.PORT);\n"
)


def _detect(files: Mapping[str, bytes], intent: ReleaseIntent | None):
    return detect_release(dict(files), intent=intent, provenance=Provenance())


def _blocker(result: object, code: str) -> None:
    assert result.assessment is ReleaseAssessment.needs_review
    assert result.ingress is None and result.services == ()
    assert any(item.code == code for item in result.blockers), [
        item.code for item in result.blockers
    ]


def _spec(result: object) -> ReleaseSpec:
    assert result.assessment is ReleaseAssessment.candidate, result.reasons
    assert result.ingress is not None
    return ReleaseSpec(
        kind=result.ingress.runtime.value,
        name="r7-round5",
        version_seq=1,
        tree_digest="0" * 64,
        services=result.services,
        env=result.env,
        resources=result.resources,
        provenance=DetectorProvenance(
            detector="r7-round5",
            detector_version="1",
            assessment=result.assessment,
        ),
    )


# One parser API: positive shapes and exact executable semantics.


@pytest.mark.parametrize(
    ("argv", "runtime", "server"),
    [
        (("node", "server.js"), "node", None),
        (("npm", "start"), "node", None),
        (("npm", "run", "start"), "node", None),
        (("uvicorn", "main:app"), "python", "uvicorn"),
        (("gunicorn", "wsgi:app"), "python", "gunicorn"),
        (("hypercorn", "asgi:app"), "python", "hypercorn"),
        (("python", "-m", "uvicorn", "main:app"), "python", "uvicorn"),
        (("python3", "-u", "-m", "gunicorn", "wsgi:app"), "python", "gunicorn"),
    ],
)
def test_effective_start_parser_accepts_only_closed_shapes(
    argv: tuple[str, ...], runtime: str, server: str | None
) -> None:
    parsed = parse_effective_start_argv(argv, declared_names=frozenset({"PORT"}), field="start_cmd")
    assert parsed.runtime == runtime and parsed.server == server


@pytest.mark.parametrize(
    "argv",
    [
        ("/usr/bin/uvicorn", "main:app"),
        ("UVICORN", "main:app"),
        ("python3.13", "-m", "uvicorn", "main:app"),
        ("python-wrapper", "-m", "uvicorn", "main:app"),
        ("python", "main.py"),
        ("python", "-c", "pass"),
        ("python", "-m", "flask", "run"),
        ("npx", "serve"),
        ("node", "--require", "hook.js", "server.js"),
        ("node", "server.js", "extra"),
        ("npm", "run", "serve"),
        ("uvicorn",),
        ("uvicorn", "main-app"),
        ("uvicorn", "main:app", "other:app"),
        ("uvicorn", "main:app", "--workers"),
        ("uvicorn", "main:app", "--mystery", "${PORT}"),
        ("uvicorn", "main:app", "--host", "0.0.0.0", "--host", "0.0.0.0"),
        ("gunicorn", "wsgi:app", "-b", "0.0.0.0:${PORT}", "--bind", "0.0.0.0:${PORT}"),
    ],
)
def test_effective_start_parser_rejects_paths_versions_opaque_and_ambiguous_argv(
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        parse_effective_start_argv(argv, declared_names=frozenset({"PORT"}), field="start_cmd")


@pytest.mark.parametrize(
    "argv",
    [
        ("/usr/bin/uvicorn", "main:app"),
        ("python3.13", "-m", "uvicorn", "main:app"),
        ("python", "main.py"),
        ("npx", "serve"),
        ("uvicorn", "main:app", "--mystery", "${PORT}"),
    ],
)
def test_raw_sidecar_path_uses_the_same_effective_start_parser(argv: tuple[str, ...]) -> None:
    _blocker(_detect(_FASTAPI, ReleaseIntent(start_cmd=argv)), "toolchain_unsupported")


# Effective install is authoritative; unrelated manifests never satisfy a server.


def test_explicit_pip_plan_wins_and_ignored_root_manifest_is_not_unioned() -> None:
    files = dict(_FASTAPI)
    result = _detect(
        files,
        ReleaseIntent(
            install_cmd=("pip", "install", "fastapi"),
            start_cmd=("uvicorn", "main:app"),
        ),
    )
    _blocker(result, "toolchain_unsupported")


def test_explicit_requirements_path_is_the_only_dependency_source() -> None:
    files = {
        **_FASTAPI,
        "prod.txt": b"fastapi==0.111\nuvicorn==0.30\n",
        "wrong.txt": b"gunicorn==22\n",
    }
    good = _detect(
        files,
        ReleaseIntent(
            install_cmd=("pip", "install", "-r", "prod.txt"),
            start_cmd=("uvicorn", "main:app"),
        ),
    )
    assert good.assessment is ReleaseAssessment.candidate
    bad = _detect(
        files,
        ReleaseIntent(
            install_cmd=("pip", "install", "-r", "wrong.txt"),
            start_cmd=("uvicorn", "main:app"),
        ),
    )
    _blocker(bad, "toolchain_unsupported")


@pytest.mark.parametrize("head", [("pip3",), ("python", "-m", "pip"), ("python3", "-m", "pip")])
def test_exact_pip_launcher_forms_install_direct_server_package(head: tuple[str, ...]) -> None:
    files = {"main.py": b"async def app(scope, receive, send):\n    pass\n"}
    result = _detect(
        files,
        ReleaseIntent(
            install_cmd=head + ("install", "uvicorn"),
            start_cmd=("uvicorn", "main:app"),
        ),
    )
    assert result.assessment is ReleaseAssessment.candidate, result.reasons


def test_pip_install_dot_reads_only_pep621_project_dependencies() -> None:
    files = {
        "main.py": b"app = object()\n",
        "pyproject.toml": (
            b"[project]\nname='x'\nversion='1'\ndependencies=['fastapi']\n"
            b"[project.optional-dependencies]\ndev=['uvicorn']\n"
            b"[tool.poetry.dependencies]\ngunicorn='22'\n"
        ),
    }
    for server in ("uvicorn", "gunicorn"):
        result = _detect(
            files,
            ReleaseIntent(
                install_cmd=("pip", "install", "."),
                start_cmd=(server, "main:app"),
            ),
        )
        _blocker(result, "toolchain_unsupported")


def test_python_target_markers_are_evaluated_for_the_emitted_image() -> None:
    files = {
        # A real WSGI callable so the gunicorn branch proves a reachable app (the uvicorn
        # branch fails at the false marker, before the target is reached).
        "main.py": b"def app(environ, start_response):\n    return []\n",
        "requirements.txt": (
            b"uvicorn; python_version < '3.0'\n"
            b"gunicorn; python_version >= '3.13' and sys_platform == 'linux'\n"
        ),
    }
    _blocker(
        _detect(files, ReleaseIntent(start_cmd=("uvicorn", "main:app"))),
        "toolchain_unsupported",
    )
    assert (
        _detect(files, ReleaseIntent(start_cmd=("gunicorn", "main:app"))).assessment
        is ReleaseAssessment.candidate
    )


@pytest.mark.parametrize(
    "install_cmd",
    [
        ("pip", "install", "--dry-run", "uvicorn"),
        ("pip", "install", "--target", "vendor", "uvicorn"),
        ("pip", "install", "--user", "uvicorn"),
        ("pip", "install", "--prefix", "x", "uvicorn"),
        ("pip", "install", "--root", "x", "uvicorn"),
        ("pip", "install", "-r", "missing.txt"),
        ("npm", "install"),
    ],
)
def test_unproved_python_install_semantics_fail_closed(install_cmd: tuple[str, ...]) -> None:
    result = _detect(
        {"main.py": b"app = object()\n"},
        ReleaseIntent(install_cmd=install_cmd, start_cmd=("uvicorn", "main:app")),
    )
    _blocker(result, "toolchain_unsupported")


# Runtime matrix, immutable Python target/config proof, and provider-neutral binds.


@pytest.mark.parametrize(
    ("runtime", "start"),
    [
        ("node", ("uvicorn", "main:app")),
        ("python", ("node", "server.js")),
        ("static", ("node", "server.js")),
        ("container", ("node", "server.js")),
        ("dev_server", ("node", "server.js")),
    ],
)
def test_explicit_runtime_must_match_effective_start(runtime: str, start: tuple[str, ...]) -> None:
    files = _FASTAPI if start[0] == "uvicorn" else {"server.js": _NODE_SOURCE}
    result = _detect(files, ReleaseIntent(runtime=runtime, start_cmd=start))
    _blocker(result, "runtime_contract_unresolved")


def test_output_dir_cannot_coexist_with_process_start() -> None:
    _blocker(
        _detect(
            {"server.js": _NODE_SOURCE},
            ReleaseIntent(start_cmd=("node", "server.js"), output_dir="dist"),
        ),
        "runtime_contract_unresolved",
    )


@pytest.mark.parametrize(
    ("files", "start"),
    [
        ({"requirements.txt": b"uvicorn\n"}, ("uvicorn", "main:app")),
        (
            {"requirements.txt": b"uvicorn\n", "main.py": b"other = object()\n"},
            ("uvicorn", "main:app"),
        ),
        (
            {
                "requirements.txt": b"uvicorn\n",
                "main.py": b"app=object()\n",
                "main/__init__.py": b"app=object()\n",
            },
            ("uvicorn", "main:app"),
        ),
        (
            {"requirements.txt": b"gunicorn\n", "wsgi.py": b"app=object()\n"},
            ("gunicorn", "wsgi:app", "--config", "missing.py"),
        ),
        (
            {"requirements.txt": b"uvicorn\n", "main.py": b"app=object()\n"},
            ("uvicorn", "main:app", "--app-dir", "missing"),
        ),
    ],
)
def test_python_target_and_file_inputs_must_exist_exactly(
    files: dict[str, bytes], start: tuple[str, ...]
) -> None:
    _blocker(_detect(files, ReleaseIntent(start_cmd=start)), "entrypoint_unresolved")


def test_python_app_dir_chdir_and_config_existing_files_are_proven() -> None:
    uvicorn = _detect(
        {
            "requirements.txt": b"uvicorn\n",
            "src/main.py": b"async def app(scope, receive, send):\n    pass\n",
        },
        ReleaseIntent(start_cmd=("uvicorn", "main:app", "--app-dir", "src")),
    )
    assert uvicorn.assessment is ReleaseAssessment.candidate
    gunicorn = _detect(
        {
            "requirements.txt": b"gunicorn\n",
            "src/wsgi.py": b"def app(environ, start_response):\n    return []\n",
            "gunicorn.conf.py": b"workers=1\n",
        },
        ReleaseIntent(
            start_cmd=(
                "gunicorn",
                "wsgi:app",
                "--chdir",
                "src",
                "--config",
                "gunicorn.conf.py",
            )
        ),
    )
    assert gunicorn.assessment is ReleaseAssessment.candidate


@pytest.mark.parametrize("head", ["uvicorn", "python"])
def test_uvicorn_bind_is_normalized_for_direct_and_python_module(head: str) -> None:
    start = (
        ("uvicorn", "main:app") if head == "uvicorn" else ("python", "-m", "uvicorn", "main:app")
    )
    result = _detect(_FASTAPI, ReleaseIntent(start_cmd=start))
    assert result.assessment is ReleaseAssessment.candidate, result.reasons
    assert result.ingress.start_cmd[-4:] == (
        "--host",
        "0.0.0.0",
        "--port",
        "${PORT}",
    )


def test_uvicorn_partial_correct_bind_is_idempotently_completed() -> None:
    host = _detect(
        _FASTAPI,
        ReleaseIntent(start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0")),
    )
    assert host.ingress.start_cmd[-2:] == ("--port", "${PORT}")
    port = _detect(
        _FASTAPI,
        ReleaseIntent(start_cmd=("uvicorn", "main:app", "--port", "${PORT}")),
    )
    assert port.ingress.start_cmd[-2:] == ("--host", "0.0.0.0")


@pytest.mark.parametrize(
    "start",
    [
        ("uvicorn", "main:app", "--host", "127.0.0.1"),
        ("uvicorn", "main:app", "--host", "::"),
        ("uvicorn", "main:app", "--port", "8080"),
        ("uvicorn", "main:app", "--port", "${OTHER}"),
    ],
)
def test_uvicorn_wrong_host_literal_port_or_foreign_port_env_fails_closed(
    start: tuple[str, ...],
) -> None:
    has_other = any("${OTHER}" in token for token in start)
    intent = ReleaseIntent(start_cmd=start, required_env=("OTHER",) if has_other else ())
    _blocker(_detect(_FASTAPI, intent), "port_contract_unresolved")


@pytest.mark.parametrize(
    ("files", "start", "expected"),
    [
        (_GUNICORN, ("gunicorn", "wsgi:app"), "0.0.0.0:${PORT}"),
        (_HYPERCORN, ("hypercorn", "asgi:app"), "0.0.0.0:${PORT}"),
        (_GUNICORN, ("gunicorn", "wsgi:app"), "0.0.0.0:${APP_PORT}"),
    ],
)
def test_combined_bind_servers_use_provider_neutral_port_name(
    files: dict[str, bytes], start: tuple[str, ...], expected: str
) -> None:
    port_env = "APP_PORT" if "APP_PORT" in expected else "PORT"
    result = _detect(files, ReleaseIntent(start_cmd=start, port_env=port_env))
    assert result.assessment is ReleaseAssessment.candidate, result.reasons
    assert result.ingress.start_cmd[-2:] == ("--bind", expected)
    assert all("8080" not in token for token in result.ingress.start_cmd)


@pytest.mark.parametrize(
    ("files", "start"),
    [
        (_GUNICORN, ("gunicorn", "wsgi:app", "--bind", "0.0.0.0:8080")),
        (_GUNICORN, ("gunicorn", "wsgi:app", "-b", "127.0.0.1:8080")),
        (_HYPERCORN, ("hypercorn", "asgi:app", "--bind", "0.0.0.0:${OTHER}")),
        (_HYPERCORN, ("hypercorn", "asgi:app", "--bind", "unix:socket")),
    ],
)
def test_combined_bind_literal_wrong_host_foreign_env_and_non_tcp_fail_closed(
    files: dict[str, bytes], start: tuple[str, ...]
) -> None:
    has_other = any("${OTHER}" in token for token in start)
    intent = ReleaseIntent(start_cmd=start, required_env=("OTHER",) if has_other else ())
    _blocker(_detect(files, intent), "port_contract_unresolved")


def test_emitter_expands_provider_neutral_combined_bind_as_one_safe_argument() -> None:
    result = _detect(_GUNICORN, ReleaseIntent(start_cmd=("gunicorn", "wsgi:app")))
    overlay = emit_local_compose(_spec(result))
    assert "0.0.0.0:${PORT}" in overlay["release.json"]
    cmd_line = next(line for line in overlay["Dockerfile"].splitlines() if line.startswith("CMD "))
    command = json.loads(cmd_line.removeprefix("CMD "))
    assert "'0.0.0.0:'\"${PORT}\"" in command[2]


# Port ownership, direct Node parity, and build-env filtering/conflicts.


@pytest.mark.parametrize(
    "intent",
    [
        ReleaseIntent(start_cmd=("uvicorn", "main:app"), required_env=("PORT",)),
        ReleaseIntent(
            start_cmd=("uvicorn", "main:app"),
            env=(EnvVarDecl(name="PORT", scope=EnvScope.runtime),),
        ),
        ReleaseIntent(
            start_cmd=("uvicorn", "main:app"),
            port_env="APP_PORT",
            env=(EnvVarDecl(name="APP_PORT", scope=EnvScope.build),),
        ),
    ],
)
def test_adapter_port_env_cannot_be_application_env_at_detection(intent: ReleaseIntent) -> None:
    _blocker(_detect(_FASTAPI, intent), "port_env_contract_conflict")


def test_release_spec_and_emitter_reject_port_env_collision_even_after_model_bypass() -> None:
    result = _detect(_FASTAPI, ReleaseIntent(start_cmd=("uvicorn", "main:app")))
    clean = _spec(result)
    with pytest.raises(ValidationError):
        ReleaseSpec.model_validate(
            {**clean.model_dump(mode="python"), "env": [{"name": "PORT", "scope": "build"}]}
        )
    bypass = clean.model_copy(update={"env": (EnvVarDecl(name="PORT", scope=EnvScope.runtime),)})
    with pytest.raises(ValidationError):
        emit_local_compose(bypass)


@pytest.mark.parametrize(
    "source",
    [
        _NODE_SOURCE,
        b"const PORT=process.env.PORT || 3000; app.listen(PORT, '0.0.0.0');\n",
        b"const port=process.env.PORT || 3000; app.listen(port, () => {});\n",
    ],
)
def test_typed_direct_node_proves_exact_entry_and_public_port(source: bytes) -> None:
    result = _detect({"server.js": source}, ReleaseIntent(start_cmd=("node", "server.js")))
    assert result.assessment is ReleaseAssessment.candidate, result.reasons


@pytest.mark.parametrize(
    ("files", "start"),
    [
        ({"other.js": _NODE_SOURCE}, ("node", "server.js")),
        ({"server.js": b"app.listen(3000)\n", "decoy.js": _NODE_SOURCE}, ("node", "server.js")),
        ({"server.js": b"app.listen(process.env.PORT, '127.0.0.1')\n"}, ("node", "server.js")),
        ({"server.js": b"app.listen(process.env.APP_PORT)\n"}, ("node", "server.js")),
    ],
)
def test_typed_node_missing_target_decoy_literal_localhost_or_wrong_env_fails_closed(
    files: dict[str, bytes], start: tuple[str, ...]
) -> None:
    result = _detect(files, ReleaseIntent(start_cmd=start))
    assert result.assessment is ReleaseAssessment.needs_review and result.ingress is None


def test_typed_npm_start_resolves_only_shell_inert_direct_node_script() -> None:
    good = {
        "package.json": b'{"scripts":{"start":"node server.js"}}',
        "server.js": _NODE_SOURCE,
    }
    assert _detect(good, ReleaseIntent(start_cmd=("npm", "start"))).assessment is (
        ReleaseAssessment.candidate
    )
    for script in (
        "node server.js && echo bad",
        "cross-env X=1 node server.js",
        "npx tsx server.ts",
    ):
        files = dict(good)
        files["package.json"] = ('{"scripts":{"start":"' + script + '"}}').encode()
        _blocker(
            _detect(files, ReleaseIntent(start_cmd=("npm", "run", "start"))),
            "entrypoint_unresolved",
        )


_FROZEN_C4_HEAD_CASES: tuple[tuple[str, str, str | None], ...] = (
    ("start", "NODE_ENV=production node server.js", None),
    ("build", "cd app && npm run build", "build_cmd"),
    ("build", "vite build && node post.js", "build_cmd"),
    ("start", "npx serve", "start_cmd"),
    ("start", 'echo "use bun" && node server.js', "start_cmd"),
    ("postinstall", "node scripts/patch.js", "install_cmd"),
    ("postinstall", "patch-package", "install_cmd"),
    ("prestart", "node warmup.js", "start_cmd"),
    ("prepare", "husky install", "install_cmd"),
    ("prepare", "node scripts/x.js", "install_cmd"),
    ("prepare", "patch-package", "install_cmd"),
    ("start", "cross-env NODE_ENV=production node server.js", "start_cmd"),
    ("start", "env node x", "start_cmd"),
    ("start", "exec node server.js", None),
    ("start", "dotenv -- npm run start", "start_cmd"),
)


@pytest.mark.parametrize(("script_key", "script", "failed_field"), _FROZEN_C4_HEAD_CASES)
def test_frozen_c4_script_shapes_follow_the_closed_effective_plan(
    script_key: str, script: str, failed_field: str | None
) -> None:
    """Only the two source-only prefixes with a fully proven Node target survive.

    The other exact fixtures do not prove the files, dependencies, directories, wrappers,
    or shell-chain npm executes, and fail in the phase that would have run them.
    """
    scripts = {"start": "node server.js", script_key: script}
    files = {
        "package.json": json.dumps({"scripts": scripts}).encode(),
        "package-lock.json": b"{}",
        "server.js": _NODE_SOURCE,
    }
    result = _detect(files, None)
    if failed_field is None:
        assert result.assessment is ReleaseAssessment.candidate, result.reasons
        assert result.ingress is not None
        return
    assert result.assessment is ReleaseAssessment.needs_review
    assert result.ingress is None and result.services == ()
    assert len(result.blockers) == 1
    assert result.blockers[0].code == "entrypoint_unresolved"
    assert result.blockers[0].field == failed_field


@pytest.mark.parametrize(
    "script",
    [
        "NODE_ENV=production node server.js",
        "exec node server.js",
        "NODE_ENV=production LOG_LEVEL=info exec node server.js",
    ],
)
def test_source_only_safe_prefixes_are_not_admitted_by_typed_npm_start(script: str) -> None:
    files = {
        "package.json": json.dumps({"scripts": {"start": script}}).encode(),
        "server.js": _NODE_SOURCE,
    }
    _blocker(
        _detect(files, ReleaseIntent(start_cmd=("npm", "start"))),
        "entrypoint_unresolved",
    )


@pytest.mark.parametrize(
    "script",
    [
        "API_TOKEN=public node server.js",
        "NODE_ENV=sk-ant-abcdefghijklmnopqrstuvwxyz node server.js",
        "NODE_ENV=$OTHER node server.js",
        'NODE_ENV="production" node server.js',
        "A=1 B=2 C=3 D=4 E=5 node server.js",
        "exec node missing.js",
    ],
)
def test_source_prefix_rejects_secret_dynamic_quoted_unbounded_or_missing_target(
    script: str,
) -> None:
    files = {
        "package.json": json.dumps({"scripts": {"start": script}}).encode(),
        "server.js": _NODE_SOURCE,
    }
    result = _detect(files, None)
    assert result.assessment is ReleaseAssessment.needs_review
    assert result.ingress is None and result.services == ()
    assert len(result.blockers) == 1
    assert (result.blockers[0].code, result.blockers[0].field) == (
        "entrypoint_unresolved",
        "start_cmd",
    )


@pytest.mark.parametrize(
    ("script_key", "script", "extra", "dependencies"),
    [
        ("postinstall", "node scripts/patch.js", {"scripts/patch.js": b""}, {}),
        ("prepare", "node scripts/x.js", {"scripts/x.js": b""}, {}),
        ("prestart", "node warmup.js", {"warmup.js": b""}, {}),
        ("build", "node post.js", {"post.js": b""}, {}),
        ("build", "vite build", {"index.html": b"<main>ok</main>"}, {"vite": "5"}),
    ],
)
def test_exact_existing_node_hooks_and_declared_vite_build_remain_candidates(
    script_key: str,
    script: str,
    extra: dict[str, bytes],
    dependencies: dict[str, str],
) -> None:
    scripts = {"start": "node server.js", script_key: script}
    files = {
        "package.json": json.dumps({"scripts": scripts, "devDependencies": dependencies}).encode(),
        "server.js": _NODE_SOURCE,
        **extra,
    }
    result = _detect(files, None)
    assert result.assessment is ReleaseAssessment.candidate, result.reasons


def test_typed_node_runs_only_the_lifecycle_hooks_in_its_effective_plan() -> None:
    package = {
        "scripts": {"postinstall": "missing-wrapper", "prestart": "missing-wrapper"},
    }
    files = {
        "package.json": json.dumps(package).encode(),
        "server.js": _NODE_SOURCE,
    }
    # Direct node with no dependency-derived install executes neither npm install hooks
    # nor npm start hooks, so an inactive package script does not create a false block.
    assert _detect(files, ReleaseIntent(start_cmd=("node", "server.js"))).assessment is (
        ReleaseAssessment.candidate
    )

    package["dependencies"] = {"express": "4"}
    files["package.json"] = json.dumps(package).encode()
    _blocker(
        _detect(files, ReleaseIntent(start_cmd=("node", "server.js"))),
        "entrypoint_unresolved",
    )

    del package["dependencies"]
    package["scripts"]["start"] = "node server.js"
    files["package.json"] = json.dumps(package).encode()
    _blocker(
        _detect(files, ReleaseIntent(start_cmd=("npm", "start"))),
        "entrypoint_unresolved",
    )


def test_typed_static_build_rejects_opaque_direct_npx_plan() -> None:
    files = _vite(b"const x='ok';\n")
    _blocker(
        _detect(
            files,
            ReleaseIntent(build_cmd=("npx", "vite", "build"), output_dir="dist"),
        ),
        "entrypoint_unresolved",
    )


def _vite(source: bytes, *, extra: Mapping[str, bytes] | None = None) -> dict[str, bytes]:
    return {
        "index.html": b"<div id='app'></div>",
        "package.json": b'{"scripts":{"build":"vite build"},"devDependencies":{"vite":"5"}}',
        "src/main.js": source,
        **dict(extra or {}),
    }


def test_build_env_discovery_ignores_comments_docs_tests_fixtures_vendor_and_output() -> None:
    decoy = b"const x = import.meta.env.VITE_ADMIN_SECRET;\n"
    files = _vite(
        b"// import.meta.env.VITE_COMMENT_SECRET\nconst x='ok';\n",
        extra={
            "README.md": decoy,
            "tests/a.js": decoy,
            "fixtures/a.ts": decoy,
            "vendor/a.jsx": decoy,
            "dist/a.js": decoy,
            "node_modules/x/a.js": decoy,
        },
    )
    result = _detect(
        files,
        ReleaseIntent(build_cmd=("npm", "run", "build"), output_dir="dist"),
    )
    assert result.assessment is ReleaseAssessment.candidate, result.reasons
    assert result.env == ()


def test_real_secret_build_source_still_fails_and_public_source_is_merged() -> None:
    secret = _detect(
        _vite(b"const x=import.meta.env.VITE_ADMIN_SECRET;\n"),
        ReleaseIntent(build_cmd=("npm", "run", "build"), output_dir="dist"),
    )
    _blocker(secret, "secret_build_env_unsupported")
    public = _detect(
        _vite(b"const x=import.meta.env.VITE_PUBLIC_BANNER;\n"),
        ReleaseIntent(build_cmd=("npm", "run", "build"), output_dir="dist"),
    )
    assert [(item.name, item.scope) for item in public.env] == [
        ("VITE_PUBLIC_BANNER", EnvScope.build)
    ]


def test_compatible_optional_public_build_declaration_is_preserved_exactly() -> None:
    declaration = EnvVarDecl(
        name="VITE_PUBLIC_BANNER",
        scope=EnvScope.build,
        required=False,
        secret=SecretClass.public,
    )
    result = _detect(
        _vite(b"const x=import.meta.env.VITE_PUBLIC_BANNER;\n"),
        ReleaseIntent(
            build_cmd=("npm", "run", "build"),
            output_dir="dist",
            env=(declaration,),
        ),
    )
    assert result.assessment is ReleaseAssessment.candidate
    assert result.env == (declaration,)


@pytest.mark.parametrize(
    "declaration",
    [
        EnvVarDecl(name="VITE_PUBLIC_BANNER", scope=EnvScope.runtime),
        EnvVarDecl(name="VITE_PUBLIC_BANNER", scope=EnvScope.build, secret=SecretClass.secret),
        EnvVarDecl(name="VITE_PUBLIC_BANNER", scope=EnvScope.build, consumers=("web",)),
    ],
)
def test_incompatible_build_env_metadata_fails_closed(declaration: EnvVarDecl) -> None:
    result = _detect(
        _vite(b"const x=import.meta.env.VITE_PUBLIC_BANNER;\n"),
        ReleaseIntent(
            build_cmd=("npm", "run", "build"),
            output_dir="dist",
            env=(declaration,),
        ),
    )
    assert result.assessment is ReleaseAssessment.needs_review
    assert any(
        item.code in {"build_env_contract_conflict", "secret_build_env_unsupported"}
        for item in result.blockers
    )


def test_source_build_env_cannot_collide_with_port_env() -> None:
    result = _detect(
        _vite(b"const x=import.meta.env.VITE_PORT;\n"),
        ReleaseIntent(
            build_cmd=("npm", "run", "build"),
            output_dir="dist",
            port_env="VITE_PORT",
        ),
    )
    _blocker(result, "port_env_contract_conflict")


def test_secret_npmrc_is_checked_even_when_install_is_explicit() -> None:
    files = {
        "server.js": _NODE_SOURCE,
        "package.json": b'{"dependencies":{"express":"4"}}',
        ".npmrc": b"//registry.example/:_authToken=${NPM_TOKEN}\n",
    }
    result = _detect(
        files,
        ReleaseIntent(
            start_cmd=("node", "server.js"),
            install_cmd=("npm", "ci"),
        ),
    )
    _blocker(result, "secret_build_env_unsupported")


def test_source_fastapi_and_express_rungs_use_the_same_effective_proofs() -> None:
    python = _detect(_FASTAPI, None)
    assert python.assessment is ReleaseAssessment.candidate
    assert python.ingress.start_cmd[-4:] == (
        "--host",
        "0.0.0.0",
        "--port",
        "${PORT}",
    )
    node_files = {
        "package.json": b'{"scripts":{"start":"node server.js"}}',
        "server.js": _NODE_SOURCE,
    }
    node = _detect(node_files, None)
    assert node.assessment is ReleaseAssessment.candidate
    assert node.ingress.start_cmd == ("npm", "start")


def test_detection_is_deterministic_after_all_effective_derivations() -> None:
    intent = ReleaseIntent(start_cmd=("python", "-m", "uvicorn", "main:app"))
    assert _detect(_FASTAPI, intent) == _detect(dict(reversed(_FASTAPI.items())), intent)
