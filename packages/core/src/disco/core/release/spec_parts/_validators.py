"""Shared field/cross-field validation helpers for the release spec.

Extracted from ``spec.py`` to reduce module size and (for the ``ReleaseSpec``
reference-resolution helpers) to reduce cyclomatic complexity; the public facade
re-imports the field-validator helpers and calls the reference-resolution
helpers from its ``_references_resolve`` model validator unchanged.

The reference-resolution helpers accept plain tuples of the spec's model
objects. They only need attribute access (duck typing), never a runtime
class/isinstance check, so the models themselves (``ReleaseService`` /
``ResourceDecl`` / ``EnvVarDecl``, which stay defined in ``spec.py``) are
imported here ONLY under ``TYPE_CHECKING`` — this module never imports its
parent module at runtime, so there is no import cycle.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from typing import TYPE_CHECKING

from disco.core.release.command_grammar import check_token_hygiene

if TYPE_CHECKING:
    from disco.core.release.spec import EnvVarDecl, ReleaseService, ResourceDecl

from ._constants import _ENV_NAME_RE, _ID_RE

# ---- shared validation helpers ------------------------------------------------


def _validate_env_var_name(value: str, *, field: str) -> str:
    if not _ENV_NAME_RE.match(value):
        raise ValueError(
            f"{field} must be an UPPERCASE env-var name matching {_ENV_NAME_RE.pattern!r} "
            "(a leading letter/underscore, then letters/digits/underscores; no '=', "
            f"whitespace, lower-case or other punctuation), got {value!r}"
        )
    return value


def _validate_id(value: str, *, field: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(
            f"{field} must match {_ID_RE.pattern!r} (a lowercase letter, then "
            f"letters/digits/underscore/hyphen), got {value!r}"
        )
    return value


def _validate_argv(value: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    # An argv-list is a real exec vector (`["node", "server.js"]`), NOT a shell string.
    # Each element must be a non-empty token (an empty token would exec an empty arg)
    # AND must pass token hygiene (WO-C5): the ONLY expandable form is an ENTIRE typed
    # env reference (`${NAME}`); a `$` outside a whole reference, command substitution,
    # backticks, separators, redirections, quotes, backslashes, control characters, NUL,
    # Unicode separators, an inline `NAME=value` assignment, or URL userinfo are all
    # rejected — so a token can never smuggle a separator, substitution, partial
    # interpolation, or inline credential. Every message is value-free (it could carry
    # a secret), so a `str(ValidationError)` names the FIELD/rule, never the input.
    for index, arg in enumerate(value):
        if not arg.strip():
            raise ValueError(f"{field} argv element {index} must be a non-empty token")
        check_token_hygiene(arg, field=f"{field} argv element {index}")
    return value


def _reject_traversal(value: str, *, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL byte")
    if ".." in value.split("/"):
        raise ValueError(f"{field} must not contain a '..' path segment, got {value!r}")
    return value


def _require_unique(values: Iterable[str], *, what: str) -> None:
    seen: set[str] = set()
    for item in values:
        if item in seen:
            raise ValueError(f"duplicate {what}: {item!r}")
        seen.add(item)


def _require_absolute_normalized_posix(value: str, *, field: str) -> None:
    """A resource `persistent_path` (WO-C7 model invariant) must be an ABSOLUTE,
    NORMALIZED POSIX path: it starts at the root (`/`), carries no `//` run, no
    `/./` single-dot segment, no `..` traversal, and no trailing slash — i.e. it is
    already in canonical form. A relative path, a double slash (including a leading
    `//`, which POSIX/`posixpath.normpath` treats specially and would otherwise
    survive normalization), or a `.`/`..` segment is rejected. Value-free: the
    message names the RULE, never the path (a path could carry sensitive data)."""
    if not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute POSIX path (a leading '/')")
    if value.startswith("//"):
        raise ValueError(f"{field} must not begin with a '//' run")
    if value != posixpath.normpath(value):
        raise ValueError(
            f"{field} must be a NORMALIZED absolute POSIX path "
            "(no '//' run, no '/./' segment, no '..' segment, no trailing slash)"
        )


def _strip_file_scheme(url: str) -> str:
    return url[len("file:") :] if url.startswith("file:") else url


# ---- ReleaseSpec._references_resolve helpers -----------------------------------
#
# Split out of the single ``_references_resolve`` model validator (mccabe 17) into
# one helper per reference class it resolves, so each stays well under the
# complexity limit. Messages and control flow are IDENTICAL to the original body;
# only the decomposition is new.


def _resolve_service_depends_on(
    services: tuple[ReleaseService, ...], service_ids: set[str]
) -> None:
    for service in services:
        for dep in service.depends_on:
            if dep not in service_ids:
                raise ValueError(f"service {service.id!r} depends_on unknown service {dep!r}")


def _resolve_resource_consumers(resources: tuple[ResourceDecl, ...], service_ids: set[str]) -> None:
    for resource in resources:
        # WO-C7: every resource must have at least one (valid) consumer — an
        # orphan resource that no service uses is an ill-formed topology.
        if not resource.consumers:
            raise ValueError(
                f"resource {resource.id!r} declares no consumers; every resource must "
                "have at least one consumer service"
            )
        for consumer in resource.consumers:
            if consumer not in service_ids:
                raise ValueError(
                    f"resource {resource.id!r} names unknown consumer service {consumer!r}"
                )


def _resolve_env_references(
    env: tuple[EnvVarDecl, ...], service_ids: set[str], resource_ids: set[str]
) -> None:
    for var in env:
        if var.binding is not None and var.binding not in resource_ids:
            raise ValueError(f"env var {var.name!r} binds unknown resource {var.binding!r}")
        # WO-C7: a DECLARED per-env consumer scope must name at least one real
        # service. `None` (no scope) is handled elsewhere (sole-ingress default /
        # implicit-fan-out rejection); an EXPLICIT empty tuple `[]` is a distinct,
        # fail-closed error — it is NOT "reaches every service", it would reach
        # NONE, silently dropping a required var from every container (a
        # broken-bundle false affordance). Reject it, and reject any named
        # consumer that is not a real service.
        if var.consumers is not None:
            if not var.consumers:
                raise ValueError(
                    f"env var {var.name!r} declares an empty consumer scope; a "
                    "declared env consumer scope must name at least one service"
                )
            for consumer in var.consumers:
                if consumer not in service_ids:
                    raise ValueError(
                        f"env var {var.name!r} names unknown consumer service {consumer!r}"
                    )
