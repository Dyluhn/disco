"""WO-C4 §8.4 (closeout finding — build-secret gate at the emit/validate boundary).

Finding A was fixed ONLY at the DETECTOR (``_secret_build_env_blocker`` scans source):
a source-discovered secret-shaped ``import.meta.env.VITE_*`` build var fails closed. But
the "a secret never folds into ``build.args``" invariant must not rest solely on the
detector — a hand-built spec, or a future build-scope producer, could still carry a
build-scope ``SecretClass.secret`` var straight to the emit/validate boundary. Two gates
close it authoritatively, INDEPENDENT of discovery source:

* ``validate_release`` fails closed with the typed ``secret_build_env_unsupported`` blocker
  on ANY build-scope ``secret`` var (the required gate), so an offending spec is never
  emitted as an overlay carrying the secret; and
* ``emit_local_compose`` (the belt) filters build-scope ``secret`` vars OUT of the Compose
  ``build.args`` — symmetric with the Dockerfile ``ARG`` set (public-only), so a plain
  ``build.args`` guard can never smuggle the secret into image build history.

Boundary: these run at the ``disco.core.release`` leaf (``validate_release`` /
``emit_local_compose``) over a hand-built ``ReleaseSpec`` — the level this landmine lives
at. It is NOT observable through ``/release`` today (the detector is the only build-scope
producer and already gates a source secret build var, and a typed intent cannot express
build-scope env), so a route-level test could not isolate this gate; the core boundary is
where the invariant is proven. No seam is used — the spec is constructed directly.

RED vs GREEN on tip ``6c58f84e``:
  * RED — ``validate_release`` returns ``ok=True`` for a build-scope secret var (no
    blocker), and ``emit_local_compose`` folds it into ``build.args`` as
    ``${NAME:?...}`` — the secret lowers into a build arg (image-history leak).
  * GREEN (preservation) — a PUBLIC build var still folds into ``build.args`` unchanged.

Randomized (plan §4 crit 8): the spec ``name`` is drawn from the seeded ``closeout_name``
factory; the build-secret gate reads the env var's SCOPE and SECRET CLASS, never the spec
name, so the verdict cannot be recognized from a hard-coded name.
"""

from __future__ import annotations

import pytest
from disco.core.release.local_compose import COMPOSE_PATH, emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    ReleaseAssessment,
    ReleaseService,
    ReleaseSpec,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
)
from disco.core.release.validate import validate_release

pytestmark = pytest.mark.export_track1_closeout

_DIGEST = "a" * 64
_SECRET_BUILD_BLOCKER = "secret_build_env_unsupported"

# A secret-shaped build var (the ``TOKEN`` marker) and a public one — both build-scope.
_SECRET_BUILD_VAR = "VITE_API_TOKEN"
_PUBLIC_BUILD_VAR = "VITE_PUBLIC_BANNER"

# A minimal file tree for ``validate_release`` (path -> size); the service root ``.`` and
# the referenced lockfile must be present so no UNRELATED validation blocker fires and the
# build-secret gate is the only thing under test.
_TREE: dict[str, int] = {"package.json": 64, "package-lock.json": 32, "src/main.js": 48}


def _spec_with_build_env(name: str, env: tuple[EnvVarDecl, ...]) -> ReleaseSpec:
    """A single-service node candidate spec carrying the given build-scope env — the
    hand-built shape a build-scope producer could hand to the emit/validate boundary."""
    return ReleaseSpec(
        kind="web",
        name=name,
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                lockfile="package-lock.json",
                install_cmd=("npm", "ci"),
                build_cmd=("npm", "run", "build"),
                start_cmd=("npm", "start"),
                port_env="PORT",
                health_path="/",
            ),
        ),
        env=env,
        provenance=DetectorProvenance(
            detector="release-detect",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
            evidence=("package.json start script",),
        ),
    )


def test_secret_build_env_var_is_refused_by_validate_release(closeout_name: object) -> None:
    """WO-C4 §8.4 (validate gate) — RED on tip 6c58f84e.

    A spec carrying a build-scope ``SecretClass.secret`` var (``VITE_API_TOKEN``) must be
    refused by ``validate_release`` with the exact typed ``secret_build_env_unsupported``
    blocker — a secret build var can never be supplied to a secret-free bundle without
    leaking into image build history. On the tip ``validate_release`` has no build-secret
    check, so it returns ``ok=True`` (no blocker) and the spec is treated as releasable."""
    assert callable(closeout_name)
    spec = _spec_with_build_env(
        str(closeout_name("proj")),
        (
            EnvVarDecl(
                name=_SECRET_BUILD_VAR,
                scope=EnvScope.build,
                required=True,
                secret=SecretClass.secret,
            ),
        ),
    )

    result = validate_release(spec, _TREE)
    codes = {blocker.code.value for blocker in result.blockers}

    assert not result.ok, (
        "a build-scope secret var must make the spec unreleasable; the tip has no "
        f"build-secret validation check, so validate_release returned ok={result.ok!r}."
    )
    assert _SECRET_BUILD_BLOCKER in codes, (
        f"expected the exact typed blocker {_SECRET_BUILD_BLOCKER!r} for a secret build "
        f"var, saw {sorted(codes)}."
    )


def test_secret_build_env_var_never_folds_into_build_args(closeout_name: object) -> None:
    """WO-C4 §8.4 (emit belt) — RED on tip 6c58f84e.

    ``emit_local_compose`` must NEVER fold a build-scope ``secret`` var into the Compose
    ``build.args`` (that would bake the secret into image build history), while a PUBLIC
    build var still folds in normally. On the tip ``_scope_env(build)`` folds ANY
    build-scope var, so the secret lowers into ``build.args`` as ``${VITE_API_TOKEN:?...}``
    — the precise leak this test pins. The public var's presence proves the belt does not
    over-filter."""
    assert callable(closeout_name)
    spec = _spec_with_build_env(
        str(closeout_name("proj")),
        (
            EnvVarDecl(name=_PUBLIC_BUILD_VAR, scope=EnvScope.build, required=True),
            EnvVarDecl(
                name=_SECRET_BUILD_VAR,
                scope=EnvScope.build,
                required=True,
                secret=SecretClass.secret,
            ),
        ),
    )

    compose = emit_local_compose(spec)[COMPOSE_PATH]

    assert _PUBLIC_BUILD_VAR in compose, (
        f"a PUBLIC build var ({_PUBLIC_BUILD_VAR}) must still fold into build.args; the "
        "belt must not over-filter public vars."
    )
    assert _SECRET_BUILD_VAR not in compose, (
        f"a build-scope SECRET var ({_SECRET_BUILD_VAR}) folded into the emitted compose "
        "(build.args) — it would bake the secret into image build history. The tip folds "
        "ANY build-scope var; the fix must filter secret build vars out of build.args."
    )
