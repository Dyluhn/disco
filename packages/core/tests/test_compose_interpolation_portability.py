"""Portability walls for the self-host container build.

Two rules, both learned from a stranger's clean Ubuntu 24.04 box.

1. The compose file must interpolate in ONE pass.

Compose implementations that ship with distributions substitute variables a
single time: podman-compose 1.0.6 (stock on Ubuntu 24.04) turns
``${A:-${B:-c}}`` into the literal text ``${B:-c}`` instead of ``c``. The stack
then boots with a garbage bind address, a garbage published port, or an
unparseable volume mount — and the failure surfaces as a YAML/mount error a
long way from the cause.

Nested defaults are therefore banned in ``compose.yaml``.

2. Every ``FROM`` must name a fully-qualified image. Podman has no implicit
Docker Hub: on a host whose ``unqualified-search-registries`` is empty (the
Debian/Ubuntu default) an unqualified ``FROM nginx:alpine`` fails outright,
and which short names happen to work depends on the distro's shortnames
aliases. Docker accepts the qualified form unchanged.

3. No healthcheck argument may contain a shell metacharacter. podman-compose
translates the exec-form ``test:`` list into ONE shell string, re-quoting each
argument, so an inline ``python -c "...urlopen('http://x')"`` becomes a broken
command and the container reports ``unhealthy`` forever while the service is
answering normally.

These tests are the wall: they fail on the files, at review time, instead of on
a stranger's machine.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# tests → core → packages → current → <repo>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE = _REPO_ROOT / "compose.yaml"

# A '${' that appears before the closing '}' of an enclosing '${' — i.e. nesting.
_NESTED = re.compile(r"\$\{[^{}]*\$\{")


def test_compose_file_exists() -> None:
    assert _COMPOSE.is_file(), f"expected the self-host compose file at {_COMPOSE}"


def test_no_nested_variable_interpolation() -> None:
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(_COMPOSE.read_text(encoding="utf-8").splitlines(), 1)
        # Comment lines are never interpolated; the ban is on live values only.
        if not line.lstrip().startswith("#") and _NESTED.search(line)
    ]
    assert not offenders, (
        "compose.yaml uses nested ${A:-${B:-c}} interpolation, which single-pass "
        "compose implementations (podman-compose 1.0.6) leave as literal text.\n"
        "Flatten each of these to one level:\n  " + "\n  ".join(offenders)
    )


# Dockerfiles reachable from `podman compose up --build`.
_DOCKERFILES = (
    "compose.yaml and the images it builds",
    "deploy/compose/Dockerfile.server",
    "deploy/sandbox/Dockerfile",
    "frontend/Dockerfile",
)
_FROM = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)


def test_compose_build_dockerfiles_use_qualified_base_images() -> None:
    offenders: list[str] = []
    for relative in _DOCKERFILES[1:]:
        path = _REPO_ROOT / relative
        assert path.is_file(), f"expected a Dockerfile at {path}"
        stages: set[str] = set()
        for image, stage in _FROM.findall(path.read_text(encoding="utf-8")):
            if stage:
                stages.add(stage.lower())
            # `scratch` and earlier build stages are not registry pulls.
            if image.lower() == "scratch" or image.lower() in stages:
                continue
            registry = image.split("/", 1)[0]
            if "/" not in image or ("." not in registry and ":" not in registry):
                offenders.append(f"{relative}: FROM {image}")
    assert not offenders, (
        "these base images are unqualified; Podman has no implicit Docker Hub and "
        "refuses them on a host with no unqualified-search-registries.\n"
        "Spell the registry (docker.io/library/...):\n  " + "\n  ".join(offenders)
    )


# ' " ; ( ) & | < > $ ` — anything a shell would not pass through untouched.
_SHELL_METACHARACTERS = set("'\";()&|<>$`*?[]{}~!#\n\t")


def test_healthcheck_arguments_have_no_shell_metacharacters() -> None:
    document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for name, service in (document.get("services") or {}).items():
        test = ((service or {}).get("healthcheck") or {}).get("test")
        if not isinstance(test, list):
            continue
        for argument in test:
            bad = sorted(_SHELL_METACHARACTERS.intersection(str(argument)))
            if bad:
                offenders.append(f"{name}: {argument!r} contains {bad}")
    assert not offenders, (
        "healthcheck arguments must survive being re-quoted into a single shell "
        "string by podman-compose. Move the logic into a script in the image and "
        "pass plain arguments:\n  " + "\n  ".join(offenders)
    )
