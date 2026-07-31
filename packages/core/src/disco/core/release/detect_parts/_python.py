"""Python runtime detection: the FastAPI/Flask signature check, the root-module
ASGI entrypoint resolver, and the supported ``uvicorn`` typed-intent start
contract (C9-04).

``_uvicorn_start_contract`` was originally one function covering head
recognition, option parsing, operand validation, and binding-completeness
checks; each phase is now a small helper below it returns through.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from disco.core.release.spec import ReleaseService, RuntimeStrategy, ServiceRole

from ._constants import _FASTAPI_APP_RE, _INGRESS_ID, _PY_ROOT_GET_RE, _UNSUPPORTED_PY_PM
from ._models import _DetectBlocker
from ._node import _unsupported_pm_blocker
from ._text import _file_text, _paths, _scan_text


def _has_web_framework(files: Mapping[str, str | bytes]) -> bool:
    for path in sorted(files):
        text = _scan_text(files[path]).lower()
        if "fastapi" in text or "from flask" in text or "import flask" in text:
            return True
    return False


def _python_root_module(files: Mapping[str, str | bytes]) -> str | None:
    """The root module (`main`/`app`) that defines a module-level FastAPI (ASGI)
    `app` — the only shape auto-startable with `uvicorn <module>:app`. Returns
    `None` for a nested entrypoint, a Flask (WSGI) app, or an absent root module —
    the detector must NOT invent a `main:app` that does not exist."""
    for module, filename in (("main", "main.py"), ("app", "app.py")):
        text = _file_text(files, filename)
        if text is not None and _FASTAPI_APP_RE.search(text):
            return module
    return None


def _python_detect(
    files: Mapping[str, str | bytes],
) -> ReleaseService | _DetectBlocker | None:
    """Detect a python ingress from a python manifest + a web-framework import.

    Returns `None` when there is no python-web signature; a `ReleaseService` for a
    resolvable FastAPI (ASGI) root app (`uvicorn main:app`, established `GET /`
    health when a root route exists); or a `_DetectBlocker`
    (`entrypoint_unresolved`) when the framework is present but no compatible root
    ASGI entrypoint can be resolved (a nested app, a Flask/WSGI app, or no root
    module) — never a fabricated `main:app`."""
    manifests = {"pyproject.toml", "requirements.txt"}
    tree = _paths(files)
    if not (manifests & tree) or not _has_web_framework(files):
        return None  # no python-web signature — not this runtime

    module = _python_root_module(files)
    if module is None:
        return _DetectBlocker(
            code="entrypoint_unresolved",
            message=(
                "a python web framework was detected but no compatible root ASGI "
                "entrypoint (a `main.py`/`app.py` defining `app = FastAPI(...)`) could "
                "be resolved — a nested module or a WSGI (Flask) app has no statically "
                "verifiable start command; declare the start command via a typed intent."
            ),
            field="start_cmd",
            evidence=("python entrypoint evidence: no root `app = FastAPI(...)` module",),
        )
    # A poetry/uv lockfile names a toolchain the neutral python base image does NOT
    # provision; fail closed rather than silently mapping it onto `pip install` (§8.8).
    toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_PY_PM)
    if toolchain is not None:
        return toolchain
    install: tuple[str, ...] = (
        ("pip", "install", "-r", "requirements.txt")
        if "requirements.txt" in tree
        else ("pip", "install", ".")
    )
    module_text = _file_text(files, f"{module}.py") or ""
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.python,
        install_cmd=install,
        start_cmd=("uvicorn", f"{module}:app", "--host", "0.0.0.0", "--port", "${PORT}"),
        port_env="PORT",
        health_path="/" if _PY_ROOT_GET_RE.search(module_text) else None,
    )


def _declared_python_deps(files: Mapping[str, str | bytes]) -> frozenset[str]:
    """The python package NAMES an owner has DECLARED (a `requirements.txt` line, or
    a `pyproject.toml` token), lowercased. A declared start executable that is a pip
    package (e.g. `gunicorn`) must appear here, or the base image cannot run it."""
    names: set[str] = set()
    req = _file_text(files, "requirements.txt")
    if req is not None:
        for line in req.splitlines():
            token = re.split(r"[\s<>=!~;\[#]", line.strip(), maxsplit=1)[0].strip().lower()
            if token:
                names.add(token)
    pyproject = _file_text(files, "pyproject.toml")
    if pyproject is not None:
        names.update(token.lower() for token in re.findall(r"[A-Za-z0-9_.-]+", pyproject))
    return frozenset(names)


# The DELIBERATELY SMALL supported uvicorn typed-intent contract (C9-04, 2026-07-17).
# One APP target of the exact `module[.module]*:attribute` shape; the ONLY accepted
# options are --host/--port (spaced or `=`-form, each exactly once, each with a value);
# either NEITHER is present (the platform appends `0.0.0.0` + `${port_env}`) or BOTH
# are (the owner's complete binding wins verbatim). Everything else — a bare `uvicorn`,
# flag-only invocations, dangling values, extra positionals, any other option, a
# path-qualified or case-variant executable, or the `python -m uvicorn` module form —
# fails CLOSED with a typed, actionable blocker: those commands predictably exit or
# bind the wrong interface at runtime, so accepting them ships a broken bundle. This is
# a recognized-shape/arity table, NOT a general command parser.
_UVICORN_APP_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*"
)


_UVICORN_VALUE_OPTIONS = frozenset({"--host", "--port"})


def _uvicorn_blocker(detail: str, evidence: str) -> _DetectBlocker:
    return _DetectBlocker(
        code="uvicorn_start_incoherent",
        message=(
            f"the declared uvicorn start command is outside the supported shape: {detail}. "
            "Supported: `uvicorn <module>:<app>` with either NO host/port binding (the "
            "platform then binds 0.0.0.0:${PORT}) or BOTH --host and --port declared with "
            "values. Anything else predictably fails or binds the wrong interface at "
            "runtime, so it cannot ship as a self-host bundle."
        ),
        field="start_cmd",
        evidence=(f"uvicorn contract evidence: {evidence}",),
    )


def _uvicorn_module_form(start_cmd: tuple[str, ...]) -> bool:
    """True for the `python -m uvicorn …` module form (any python-ish head)."""
    if len(start_cmd) < 3:
        return False
    head = start_cmd[0].rsplit("/", 1)[-1].lower()
    if not (head.startswith("python") or head in ("python", "python3")):
        return False
    return any(
        start_cmd[i] == "-m" and start_cmd[i + 1] == "uvicorn" for i in range(1, len(start_cmd) - 1)
    )


def _uvicorn_recognized_head(start_cmd: tuple[str, ...]) -> str | _DetectBlocker | None:
    """Resolve the head recognition state: `None` when `start_cmd` is not
    recognizably uvicorn at all (untouched for non-uvicorn paths), a blocker when
    it is recognizably uvicorn but outside the supported shape (the `python -m
    uvicorn` module form, or a path-qualified/case-variant executable), or the
    accepted bare `"uvicorn"` head."""
    if _uvicorn_module_form(start_cmd):
        return _uvicorn_blocker(
            "the `python -m uvicorn` module form is unsupported; declare the bare "
            "`uvicorn <module>:<app>` form",
            f"module form {' '.join(start_cmd[:4])!r}",
        )
    head = start_cmd[0]
    if head.rsplit("/", 1)[-1].lower() != "uvicorn":
        return None
    if head != "uvicorn":
        return _uvicorn_blocker(
            "a path-qualified or case-variant uvicorn executable is unsupported; declare "
            "the bare `uvicorn` token",
            f"unsupported executable form {head!r}",
        )
    return head


def _parse_uvicorn_options(
    start_cmd: tuple[str, ...],
) -> tuple[list[str], dict[str, str]] | _DetectBlocker:
    """Split `start_cmd[1:]` into (positional app operands, --host/--port values),
    or a blocker for an unsupported/duplicate/dangling option."""
    apps: list[str] = []
    options: dict[str, str] = {}
    i = 1
    while i < len(start_cmd):
        token = start_cmd[i]
        if not token.startswith("-"):
            apps.append(token)
            i += 1
            continue
        name, eq, inline = token.partition("=")
        if name not in _UVICORN_VALUE_OPTIONS:
            return _uvicorn_blocker(
                f"option {name!r} is outside the supported option set (--host/--port "
                "only); remove it or start the app through a committed script",
                f"unsupported option {token!r}",
            )
        if name in options:
            return _uvicorn_blocker(
                f"option {name!r} is declared more than once (ambiguous binding)",
                f"duplicate option {name!r}",
            )
        if eq:
            if not inline:
                return _uvicorn_blocker(
                    f"option {name!r} declares no value",
                    f"dangling `=`-form option {token!r}",
                )
            options[name] = inline
            i += 1
            continue
        nxt = start_cmd[i + 1] if i + 1 < len(start_cmd) else None
        if nxt is None or nxt.startswith("-"):
            return _uvicorn_blocker(
                f"option {name!r} declares no value (dangling argument)",
                f"dangling option {name!r}",
            )
        options[name] = nxt
        i += 2
    return apps, options


def _validate_uvicorn_app(apps: list[str]) -> str | _DetectBlocker:
    if not apps:
        return _uvicorn_blocker(
            "no application target is declared",
            "missing APP operand",
        )
    if len(apps) > 1:
        return _uvicorn_blocker(
            f"multiple positional operands {apps!r} — exactly one `module:app` target is supported",
            f"extra positional operands {apps[1:]!r}",
        )
    if _UVICORN_APP_RE.fullmatch(apps[0]) is None:
        return _uvicorn_blocker(
            f"the application target {apps[0]!r} is not a `module[.module]:attribute` reference",
            f"malformed APP operand {apps[0]!r}",
        )
    return apps[0]


def _uvicorn_binding_completeness(options: dict[str, str]) -> _DetectBlocker | None:
    """`None` when --host/--port are both present or both absent; a blocker when
    only one of the two is declared (an incomplete binding)."""
    has_host = "--host" in options
    has_port = "--port" in options
    if has_host == has_port:
        return None
    missing = "--port" if has_host else "--host"
    present = "--host" if has_host else "--port"
    return _uvicorn_blocker(
        f"an incomplete binding: {present} is declared without {missing}; declare "
        "BOTH or NEITHER (the platform then binds 0.0.0.0:${PORT})",
        f"incomplete binding: only {present}",
    )


def _validate_uvicorn_binding_values(
    options: dict[str, str], port_env: str
) -> _DetectBlocker | None:
    # A COMPLETE binding must bind the PLATFORM CONTRACT exactly (C9-04): the emitted
    # bundle publishes/injects/health-checks the container port via `${<port_env>}` on
    # all interfaces. `None` when the declared --host/--port already match it.
    port_ref = "${" + port_env + "}"
    host_val = options["--host"]
    port_val = options["--port"]
    if host_val != "0.0.0.0":
        return _uvicorn_blocker(
            f"the declared --host {host_val!r} does not bind all interfaces; the platform "
            "publishes the port on the container's external interface, so --host must be "
            "'0.0.0.0' (a loopback/hostname bind is unreachable through the port mapping)",
            f"incompatible host binding {host_val!r}",
        )
    if port_val != port_ref:
        return _uvicorn_blocker(
            f"the declared --port {port_val!r} does not bind the platform port contract "
            f"{port_ref!r}; the emitted bundle injects/publishes/health-checks that exact "
            "variable, so a literal, out-of-range, or different-variable port would bind "
            "the wrong port or fail at runtime",
            f"incompatible port binding {port_val!r} (want {port_ref!r})",
        )
    return None


def _uvicorn_start_contract(
    start_cmd: tuple[str, ...], port_env: str
) -> tuple[str, ...] | _DetectBlocker | None:
    """Apply the supported uvicorn intent contract (C9-04).

    Returns ``None`` when the command is not recognizably uvicorn (left untouched for
    the non-uvicorn paths), an argv tuple when accepted (normalized for the no-binding
    shape, verbatim for a complete binding), or a ``_DetectBlocker`` when the command
    is recognizably uvicorn but outside the contract."""
    if not start_cmd:
        return None
    head = _uvicorn_recognized_head(start_cmd)
    if head is None or isinstance(head, _DetectBlocker):
        return head
    parsed = _parse_uvicorn_options(start_cmd)
    if isinstance(parsed, _DetectBlocker):
        return parsed
    apps, options = parsed
    app = _validate_uvicorn_app(apps)
    if isinstance(app, _DetectBlocker):
        return app
    incomplete = _uvicorn_binding_completeness(options)
    if incomplete is not None:
        return incomplete
    if "--host" not in options:
        return start_cmd + ("--host", "0.0.0.0", "--port", "${" + port_env + "}")
    invalid = _validate_uvicorn_binding_values(options, port_env)
    if invalid is not None:
        return invalid
    return start_cmd
