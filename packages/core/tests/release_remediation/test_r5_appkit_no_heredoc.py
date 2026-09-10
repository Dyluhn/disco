"""R5 (G12) — the AppKit dev_server entrypoint is materialized WITHOUT a heredoc.

Closeout remediation R5, at the PURE core boundary (no route): the AppKit interim
local-run overlay (`_emit_dev_server_overlay` -> `_dev_server_dockerfile`) must
materialize its secret-writing container entrypoint with instructions the LEGACY
Docker Engine builder accepts. A heredoc `COPY <<EOF` (or `RUN <<EOF`) is a
BuildKit/Buildx-only Dockerfile feature the legacy builder REJECTS, yet the exported
bundle's `SELFHOST.md` + live guard declare only "Docker Engine + Compose v2" (NOT
Buildx) as the prerequisite — so on a host that satisfies exactly the declared
prerequisite the AppKit image failed to build.

These live OUTSIDE the frozen closeout dirs (no `export_track1_closeout` marker) so
they never perturb the acceptance manifest. The frozen public-boundary proof is
`packages/agent-server/tests/export_track1_closeout/test_g12_appkit_no_heredoc.py`;
the live build/discrimination proof is
`packages/agent-server/tests/release_remediation/test_r5_live_docker.py`.
"""

from __future__ import annotations

import pytest
from disco.core.appkit import default_lead_gen_app_spec, generate, get_recipe
from disco.core.release import local_compose
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    RELEASE_JSON_PATH,
    SELFHOST_DOC_PATH,
    emit_local_compose,
)
from disco.core.release.spec import (
    DetectorProvenance,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    ServiceRole,
)

# The frozen single-service self-host overlay path set (mirrors WO-C6 §10.6). The R5
# fix must NOT add a 7th (discrete entrypoint) overlay file — that would break the
# frozen `test_c6_uncollided_overlay_is_exactly_frozen_set[appkit]` exact-set invariant.
_FROZEN_OVERLAY = frozenset(
    {
        COMPOSE_PATH,
        DOCKERFILE_PATH,
        DOCKERIGNORE_PATH,
        ENV_EXAMPLE_PATH,
        SELFHOST_DOC_PATH,
        RELEASE_JSON_PATH,
    }
)

_ENTRYPOINT_PATH = "/usr/local/bin/disco-entrypoint.sh"

# A real-looking admin token that is NEVER injected anywhere — the emitter takes only
# the NAMES-only spec — so its absence anywhere in the overlay is a genuine proof that
# no secret VALUE can leak into the build context / image.
_SECRET_SENTINEL = "adm1n-t0ken-VALUE-must-never-leak-r5-1a2b3c4d"


# ---- spec factories ------------------------------------------------------------


def _appkit_spec() -> ReleaseSpec:
    """A realistic AppKit dev_server spec built by driving the REAL generator +
    detector (a required ADMIN_TOKEN secret + a D1 sqlite resource), exactly the
    shape `test_release_appkit_local.py` proves is a self-hostable candidate."""
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    app = default_lead_gen_app_spec("Acme", recipe)
    tree = dict(generate(app, recipe.to_design_spec()))
    tree[".disco/appspec.json"] = app.model_dump_json(indent=2)
    result = detect_release(tree, intent=None, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.candidate
    assert result.ingress is not None and result.ingress.runtime is RuntimeStrategy.dev_server
    return ReleaseSpec(
        kind="appkit",
        name="Acme",
        version_seq=1,
        tree_digest="a" * 64,
        services=result.services,
        env=result.env,
        resources=result.resources,
        provenance=DetectorProvenance(
            detector="release-detect",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
            evidence=result.evidence,
        ),
    )


def _appkit_spec_no_required_secret() -> ReleaseSpec:
    """A dev_server spec variant with NO host-supplied required secret (env=()). The
    entrypoint then writes an empty dev-vars file — a distinct materialization the
    Dockerfile must still emit with only legacy-builder-compatible instructions."""
    return ReleaseSpec(
        kind="appkit",
        name="NoSecret",
        version_seq=1,
        tree_digest="b" * 64,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.dev_server,
                port_env="PORT",
                health_path="/",
            ),
        ),
        env=(),
        resources=(
            ResourceDecl(
                id="db",
                kind=ResourceKind.sqlite,
                persistent_path="/data/state",
                profiles=ResourceProfiles(
                    local=LocalResourceProfile(url="file:/data/state", volume="appkit-data")
                ),
                consumers=("web",),
                migrate_cmd=(
                    "npx",
                    "wrangler",
                    "d1",
                    "execute",
                    "appkit_prod",
                    "--local",
                    "--file=./schema.sql",
                ),
            ),
        ),
        provenance=DetectorProvenance(
            detector="r5", detector_version="1", assessment=ReleaseAssessment.candidate
        ),
    )


# ---- the invariants under test -------------------------------------------------


def _heredoc_copy_lines(dockerfile: str) -> list[str]:
    """Every emitted `COPY` instruction that uses a heredoc redirect (`COPY <<EOF` /
    `COPY <<'EOF'` / `COPY <<-EOF`) — BuildKit/Buildx-only, rejected by the legacy
    builder. This mirrors the frozen G12 assertion's own predicate so a regression is
    caught the same way the public-boundary test catches it."""
    offending: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY") and "<<" in stripped:
            offending.append(stripped)
    return offending


def _buildkit_only_lines(dockerfile: str) -> list[str]:
    """Every instruction that requires the BuildKit/Buildx frontend and is REJECTED by
    the legacy Docker Engine builder: a heredoc redirect on COPY/RUN/ADD (`<<`), a
    `RUN --mount=`, a `--link` copy, or a `--chmod=` copy. (The `# syntax=` parser
    directive is NOT flagged — the legacy builder treats an unknown parser directive
    as a comment, proven by the R5 live legacy-builder build.)"""
    offending: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        head = stripped.split(" ", 1)[0] if stripped else ""
        if head in ("COPY", "RUN", "ADD") and "<<" in stripped:
            offending.append(stripped)
        elif "--mount=" in stripped or "--link" in stripped or "--chmod=" in stripped:
            offending.append(stripped)
    return offending


def test_emitted_appkit_dockerfile_has_no_heredoc_copy() -> None:
    dockerfile = emit_local_compose(_appkit_spec())[DOCKERFILE_PATH]
    assert _heredoc_copy_lines(dockerfile) == [], (
        "the AppKit dev_server Dockerfile still uses a heredoc COPY (BuildKit-only); "
        "the entrypoint must be materialized without a heredoc."
    )


def test_entrypoint_materialized_via_normal_run_then_chmod() -> None:
    """The entrypoint is written by a heredoc-free `RUN printf ... > <path> && chmod
    +x <path>` and run via a plain `ENTRYPOINT [<path>]` — the classic-builder path."""
    dockerfile = emit_local_compose(_appkit_spec())[DOCKERFILE_PATH]
    run_lines = [
        line
        for line in dockerfile.splitlines()
        if line.startswith("RUN printf ") and _ENTRYPOINT_PATH in line
    ]
    assert len(run_lines) == 1, (
        f"expected exactly one entrypoint-materializing RUN; got {run_lines}"
    )
    run_line = run_lines[0]
    assert f"> {_ENTRYPOINT_PATH}" in run_line, run_line
    assert f"chmod +x {_ENTRYPOINT_PATH}" in run_line, run_line
    assert f'ENTRYPOINT ["{_ENTRYPOINT_PATH}"]' in dockerfile


def test_entrypoint_is_secret_free_names_only() -> None:
    """The entrypoint materialization references the secret by NAME with a fail-closed
    `${NAME:?}` guard and reads the VALUE from the container env at runtime
    (`printf 'NAME=%s\\n' "$NAME"`) — never a baked value. No secret VALUE (which the
    emitter is never given) can appear anywhere in the whole overlay."""
    overlay = emit_local_compose(_appkit_spec())
    dockerfile = overlay[DOCKERFILE_PATH]
    # NAME-by-guard + runtime-read forms are present (secret written at container start):
    # a fail-closed `${NAME:?}` guard, the `NAME=%s` write format, and a `"$NAME"` env
    # read — the VALUE is resolved from the container env at runtime, never baked in.
    assert "${ADMIN_TOKEN:?" in dockerfile, dockerfile
    assert "ADMIN_TOKEN=%s" in dockerfile, dockerfile
    assert '"$ADMIN_TOKEN"' in dockerfile, dockerfile
    # The secret VALUE is never handed to the emitter, so it is absent everywhere.
    for path, content in overlay.items():
        assert _SECRET_SENTINEL not in content, path
    # The dev-vars secret file is referenced exactly once (the runtime write target),
    # and only inside the Dockerfile — never as a discrete emitted file.
    assert dockerfile.count(".dev.vars") == 1
    assert "> /app/.dev.vars" in dockerfile


def test_no_discrete_entrypoint_overlay_file_is_added() -> None:
    """The fix must NOT add a discrete entrypoint overlay file: the emitted set is
    EXACTLY the frozen 6-file overlay (WO-C6 §10.6). A 7th file would break the frozen
    `test_c6_uncollided_overlay_is_exactly_frozen_set[appkit]` exact-set invariant."""
    overlay = emit_local_compose(_appkit_spec())
    assert set(overlay) == set(_FROZEN_OVERLAY), (
        f"the R5 overlay is not exactly the frozen set: missing "
        f"{sorted(_FROZEN_OVERLAY - set(overlay))}, extra {sorted(set(overlay) - _FROZEN_OVERLAY)}."
    )
    assert "disco-entrypoint.sh" not in overlay


@pytest.mark.parametrize(
    "spec_factory",
    [_appkit_spec, _appkit_spec_no_required_secret],
    ids=["with_required_secret", "no_required_secret"],
)
def test_dockerfile_uses_only_legacy_builder_compatible_instructions(
    spec_factory: object,
) -> None:
    """Across dev_server variants (with/without a required secret, with the D1
    resource), the emitted Dockerfile uses NO BuildKit-only instruction — so it builds
    on exactly the declared prerequisite (Docker Engine + Compose v2, no Buildx)."""
    assert callable(spec_factory)
    dockerfile = emit_local_compose(spec_factory())[DOCKERFILE_PATH]
    assert _buildkit_only_lines(dockerfile) == [], (
        "the AppKit dev_server Dockerfile uses a BuildKit/Buildx-only instruction the "
        f"legacy builder rejects: {_buildkit_only_lines(dockerfile)}"
    )


def test_mutation_reintroducing_heredoc_copy_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix is ESSENTIAL: with the real materializer the Dockerfile is heredoc-
    free, but reintroducing the old heredoc `COPY <<EOF` form (mutating
    `_entrypoint_install_line`) makes the heredoc detector RED again — the same
    predicate the frozen G12 assertion uses."""
    clean = emit_local_compose(_appkit_spec())[DOCKERFILE_PATH]
    assert _heredoc_copy_lines(clean) == [], "precondition: the real emit is heredoc-free"

    def _old_heredoc_form(script: str) -> str:
        return (
            f"COPY <<'DISCO_ENTRYPOINT' {_ENTRYPOINT_PATH}\n"
            f"{script}\n"
            "DISCO_ENTRYPOINT\n"
            f"RUN chmod +x {_ENTRYPOINT_PATH}"
        )

    monkeypatch.setattr(local_compose, "_entrypoint_install_line", _old_heredoc_form)
    mutated = emit_local_compose(_appkit_spec())[DOCKERFILE_PATH]
    assert _heredoc_copy_lines(mutated), (
        "reintroducing the heredoc COPY entrypoint form must be caught by the heredoc "
        "detector — proving the R5 fix (the heredoc-free RUN) is what keeps G12 green."
    )
