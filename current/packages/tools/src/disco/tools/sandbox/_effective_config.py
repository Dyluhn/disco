"""Post-create verification that a container REALLY got the isolation it asked for.

Requesting an OCI runtime or a cgroup bound is not the same as receiving one, and
the difference is invisible from the request side. Podman's Docker-compatible API
advertises ``runsc`` in ``/info`` from a *static candidate path* whether or not the
binary exists, then drops ``HostConfig.Runtime`` on create without an error — so a
pre-flight "is this runtime registered?" check false-greens on exactly the
transport that lies, and the caller believes in a gVisor boundary it never got.

The only check that survives a lying transport is to ask the engine what it
actually assigned, after the fact. These helpers are engine-agnostic on purpose:
podman-py and docker-py both expose a container object with ``.reload()`` and
``.attrs``, so one implementation guards every backend that talks to a daemon.

Scope, stated honestly: ``HostConfig`` is the create request echoed back, so it
proves the engine *accepted and recorded* the request. It cannot prove a runtime
then honours it — a ``runsc`` registered with ``--runtime-flag ignore-cgroups``
records the caps and enforces none. :func:`cgroup_enforcement_warning` covers that
posture from the only signal that exposes it, the runtime's registered arguments.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import SandboxUnavailableError

# runc and crun ARE the engine defaults (Docker and Podman respectively) and promise
# no extra isolation, so asking for one is not a claim that needs enforcing. Anything
# else — runsc, kata, … — is a request for a stronger boundary, and silently not
# getting it is the failure these guards exist to prevent.
ENGINE_DEFAULT_RUNTIMES = frozenset({"", "runc", "crun"})

# Human labels for the HostConfig cgroup fields this module verifies.
_LIMIT_LABELS: Mapping[str, str] = {
    "Memory": "memory",
    "NanoCpus": "CPU",
    "CpuQuota": "CPU quota",
    "PidsLimit": "pids",
}

# Runtime arguments that switch cgroup enforcement off wholesale. `runsc` accepts
# the flag with either spelling and with any number of leading dashes.
_CGROUP_DISABLING_ARGS = ("ignore-cgroups", "ignore_cgroups")


def _inspect(container: Any) -> dict[str, Any]:
    """Re-read the container from the engine and return its attribute mapping."""
    container.reload()
    attrs = container.attrs
    return attrs if isinstance(attrs, dict) else {}


def assert_effective_runtime(container: Any, requested: str) -> None:
    """Fail CLOSED when the container did not actually get the configured runtime.

    Requesting a runtime and then verifying what was actually assigned is the only
    check that survives a transport which lies. If they disagree, the caller gets a
    typed refusal instead of a weaker isolation boundary it did not ask for. Any
    inspect failure is also fail-closed: an unverifiable boundary is not a boundary.
    """
    wanted = (requested or "").strip()
    if wanted in ENGINE_DEFAULT_RUNTIMES:
        return
    try:
        attrs = _inspect(container)
        effective = str(
            (attrs.get("HostConfig") or {}).get("Runtime") or attrs.get("OCIRuntime") or ""
        ).strip()
    except Exception as exc:  # noqa: BLE001 - any inspect failure is fail-closed
        raise SandboxUnavailableError(
            f"could not verify the {wanted!r} runtime took effect: {exc}"
        ) from exc
    if effective != wanted:
        raise SandboxUnavailableError(
            f"refusing to run unsandboxed: requested the {wanted!r} OCI runtime "
            f"but the container was created with {effective or 'the engine default'!r}. "
            "Podman's Docker-compatible API silently ignores the runtime field; use "
            "the rootful Docker or remote gvisor backend for a gVisor boundary."
        )


def assert_effective_limits(container: Any, requested: Mapping[str, int]) -> None:
    """Fail CLOSED when a requested cgroup bound is absent from, or looser than
    asked for in, the container the engine actually created.

    ``requested`` maps ``HostConfig`` field names (``Memory``, ``NanoCpus``,
    ``CpuQuota``, ``PidsLimit``) to the value the create call asked for. A field the
    caller did not request, or requested as ``0`` (the "unset" sentinel that
    ``resolve_bounds`` never emits), is not checked.

    Same reasoning as :func:`assert_effective_runtime`, applied to the bounds: the
    engine or a compat transport can DROP a create field it does not support,
    leaving the container unlimited while the caller believes a cap is in place —
    which is precisely the fork-bomb / memory-exhaustion host DoS the bounds
    validators exist to prevent. A recorded value that is *stricter* than requested
    is fine (the engine only tightened); absent, unlimited, or looser is refused.
    """
    wanted = {key: int(value) for key, value in requested.items() if int(value) > 0}
    if not wanted:
        return
    try:
        host_config = _inspect(container).get("HostConfig") or {}
        effective = {key: host_config.get(key) for key in wanted}
    except Exception as exc:  # noqa: BLE001 - any inspect failure is fail-closed
        raise SandboxUnavailableError(
            f"could not verify the sandbox resource limits took effect: {exc}"
        ) from exc
    problems = [
        problem
        for problem in (
            _limit_problem(key, want, effective.get(key)) for key, want in sorted(wanted.items())
        )
        if problem
    ]
    if problems:
        raise SandboxUnavailableError(
            "refusing to run without the requested resource bounds: "
            + "; ".join(problems)
            + ". An unbounded sandbox lets agent code exhaust host memory, CPU or PIDs."
        )


def _limit_problem(key: str, want: int, got: Any) -> str | None:
    """Describe how one recorded bound falls short of the requested one, or None."""
    label = _LIMIT_LABELS.get(key, key)
    value = got if isinstance(got, int) and not isinstance(got, bool) else None
    if value is None or value <= 0:
        return f"requested a {label} limit of {want} but the container has none"
    if value > want:
        return f"requested a {label} limit of {want} but the container was created with {value}"
    return None


def cgroup_enforcement_warning(runtimes: Mapping[str, Any], requested: str) -> str | None:
    """Describe a registered runtime whose own arguments disable cgroup enforcement.

    ``HostConfig`` cannot see this: a ``runsc`` registered with
    ``--runtime-flag ignore-cgroups`` records every requested cap and enforces none,
    so the create request and the inspect both look perfect. The engine's registered
    ``runtimeArgs`` is the only place the posture is visible.

    Returns a message when the requested runtime is registered with a
    cgroup-disabling argument, otherwise ``None``. ``None`` is NOT proof of
    enforcement — Podman's compat ``/info`` reports only a path per runtime and no
    arguments at all, so on that transport this signal is simply unavailable.
    """
    entry = runtimes.get(requested) if isinstance(runtimes, Mapping) else None
    args = entry.get("runtimeArgs") if isinstance(entry, Mapping) else None
    if not isinstance(args, (list, tuple)):
        return None
    disabling = [
        str(arg)
        for arg in args
        if any(flag in str(arg).lower().lstrip("-") for flag in _CGROUP_DISABLING_ARGS)
    ]
    if not disabling:
        return None
    return (
        f"the {requested!r} runtime is registered with {' '.join(disabling)}, which disables "
        "cgroup enforcement: the sandbox memory, CPU and pids limits will be recorded but "
        "NOT enforced, so agent code can exhaust host resources. Register the runtime "
        "without that flag, or run it under an engine that does not need the workaround."
    )


__all__ = [
    "ENGINE_DEFAULT_RUNTIMES",
    "assert_effective_limits",
    "assert_effective_runtime",
    "cgroup_enforcement_warning",
]
