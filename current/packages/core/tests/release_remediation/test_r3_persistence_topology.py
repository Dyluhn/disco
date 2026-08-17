"""R3 (G07) — a truthful persistence topology: every accepted persistent path is
backed by the emitted volume, and an unbackable filesystem-ROOT layout fails closed.

Closeout remediation R3, at the PURE core boundary (no route): the ``local_mount_target``
real-parent derivation, the detector's ``persistent_path_unbackable`` fail-closed
outcome, the ``ReleaseSpec`` mount-target belt, and the emitter's backing invariant.
These live OUTSIDE the frozen closeout dirs (no ``export_track1_closeout`` marker) so
they never perturb the acceptance manifest.

The headline is a MUTATION regression (``test_mutation_...``) proving the fix is
LOAD-BEARING: restoring the pre-R3 blind ``/data`` root-level fallback in
``local_mount_target`` makes the root-file persistence proof fail again — a self-host
candidate whose emitted volume does NOT contain the declared file.
"""

from __future__ import annotations

import pytest
import yaml
from disco.core.release import detect as detect_mod
from disco.core.release import local_compose as lc_mod
from disco.core.release import spec as spec_mod
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import COMPOSE_PATH, emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    EnvVarDecl,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    ServiceRole,
    local_mount_target,
)
from pydantic import ValidationError

_DIGEST = "0" * 64

# The FIXED, value-free ``persistent_path_unbackable`` message (must match the constant in
# ``detect._resource_topology_blocker`` byte-for-byte — it interpolates NO declared path).
_ROOT_UNBACKABLE_MESSAGE = (
    "a declared resource persists at a filesystem-root path whose parent "
    "directory is '/'; a named volume cannot back it without mounting at the "
    "container root, so its data would not survive a restart. Declare the "
    "persistent path under a subdirectory (for example '/data/app.db') so a "
    "volume can back it exactly."
)


# ---- helpers ------------------------------------------------------------------


def _resource(
    rid: str,
    *,
    path: str,
    url: str,
    volume: str,
    consumers: tuple[str, ...],
    migrate: tuple[str, ...] = (),
) -> ResourceDecl:
    return ResourceDecl(
        id=rid,
        kind=ResourceKind.sqlite,
        persistent_path=path,
        profiles=ResourceProfiles(local=LocalResourceProfile(url=url, volume=volume)),
        consumers=consumers,
        migrate_cmd=migrate,
    )


def _svc(sid: str, *, role: ServiceRole, start: tuple[str, ...]) -> ReleaseService:
    return ReleaseService(id=sid, role=role, runtime=RuntimeStrategy.node, start_cmd=start)


def _spec(
    *,
    services: tuple[ReleaseService, ...],
    resources: tuple[ResourceDecl, ...],
    env: tuple[EnvVarDecl, ...] = (),
) -> ReleaseSpec:
    return ReleaseSpec(
        kind="node",
        name="r3",
        version_seq=1,
        tree_digest=_DIGEST,
        services=services,
        env=env,
        resources=resources,
        provenance=DetectorProvenance(detector="r3", detector_version="1", assessment="candidate"),
    )


def _persistent_path_is_backed(mount_targets: set[str], persistent_path: str) -> bool:
    """Whether some mounted container directory actually PERSISTS ``persistent_path``
    (mirrors the frozen G07 predicate): a named volume mounted at directory ``D`` backs a
    file ``F`` iff ``D`` is ``/``, ``F`` IS ``D``, or ``F`` lives strictly under ``D``."""
    for target in mount_targets:
        directory = target.rstrip("/")
        if directory == "":
            return True
        if persistent_path == directory or persistent_path.startswith(directory + "/"):
            return True
    return False


def _web_mount_targets(compose: dict[str, object], sid: str) -> set[str]:
    services = compose["services"]
    assert isinstance(services, dict)
    block = services[sid]
    assert isinstance(block, dict)
    vols = block.get("volumes", [])
    assert isinstance(vols, list)
    out: set[str] = set()
    for entry in vols:
        assert isinstance(entry, str)
        _name, sep, target = entry.partition(":")
        assert sep, entry
        out.add(target)
    return out


def _root_intent(path: str) -> ReleaseIntent:
    return ReleaseIntent(
        start_cmd=("node", "server.js"),
        resources=(
            _resource("db", path=path, url=f"file:{path}", volume="app-data", consumers=("web",)),
        ),
    )


# The PRE-R3 local_mount_target: a filesystem-ROOT path fell back to a BLIND ``/data``
# that does not actually contain a root-level file. Kept here ONLY to drive the mutation
# regression that proves the R3 fix is load-bearing.
def _old_data_fallback_mount_target(resource: ResourceDecl) -> str:
    url = resource.profiles.local.url
    path = url[len("file:") :] if url.startswith("file:") else url
    path = (path or resource.persistent_path).rstrip("/")
    slash = path.rfind("/")
    if slash <= 0:
        return "/data"
    return path[:slash]


# ---- local_mount_target: the real-parent derivation ---------------------------


@pytest.mark.parametrize(
    ("path", "url", "expected"),
    [
        ("/data/app.db", "file:/data/app.db", "/data"),
        ("/var/lib/app/db.sqlite", "file:/var/lib/app/db.sqlite", "/var/lib/app"),
        ("/data/a/b/c/deep.db", "file:/data/a/b/c/deep.db", "/data/a/b/c"),
    ],
)
def test_local_mount_target_is_the_files_real_parent(path: str, url: str, expected: str) -> None:
    """A NON-root file mounts at its ACTUAL containing directory, so the emitted volume
    backs the file exactly — never a blind ``/data``."""
    resource = _resource("db", path=path, url=url, volume="v", consumers=("web",))
    assert local_mount_target(resource) == expected
    assert _persistent_path_is_backed({expected}, path)


@pytest.mark.parametrize("path", ["/app.db", "/db.sqlite", "/state"])
def test_local_mount_target_root_file_resolves_to_the_honest_root(path: str) -> None:
    """A filesystem-ROOT file resolves to ``/`` — its TRUE parent — not a blind ``/data``.
    (Detection + the schema reject this layout up front; the value stays truthful.)"""
    resource = _resource("db", path=path, url=f"file:{path}", volume="v", consumers=("web",))
    assert local_mount_target(resource) == "/"


# ---- detection: root file fails closed, non-root is a candidate ---------------


@pytest.mark.parametrize("path", ["/app.db", "/db.sqlite"])
def test_detect_root_persistent_path_fails_closed_with_typed_blocker(path: str) -> None:
    """A typed intent declaring a filesystem-ROOT persistent path fails closed to
    ``needs_review`` with the typed ``persistent_path_unbackable`` blocker and NO ingress
    (so no self-host bundle is ever offered)."""
    detection = detect_release(
        {"package.json": b'{"name":"svc"}', "server.js": b"x\n"},
        intent=_root_intent(path),
        provenance=Provenance(),
    )
    assert detection.assessment is ReleaseAssessment.needs_review
    assert detection.ingress is None and detection.services == ()
    assert [b.code for b in detection.blockers] == ["persistent_path_unbackable"]
    # Value-free by construction: the blocker is a FIXED string with no interpolation of
    # the declared path (which could carry sensitive data). Assert it is the same constant
    # regardless of which root path was declared.
    assert detection.blockers[0].message == _ROOT_UNBACKABLE_MESSAGE


def test_detect_static_intent_root_persistent_path_also_fails_closed() -> None:
    """The static-intent lowering path guards the same invariant — a root persistent
    path on a served-static bundle also fails closed."""
    detection = detect_release(
        {"index.html": b"<!doctype html>hi\n"},
        intent=ReleaseIntent(
            output_dir=".",
            resources=(
                _resource("db", path="/app.db", url="file:/app.db", volume="v", consumers=("web",)),
            ),
        ),
        provenance=Provenance(),
    )
    assert detection.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in detection.blockers] == ["persistent_path_unbackable"]


@pytest.mark.parametrize("path", ["/data/app.db", "/var/lib/app/db.sqlite"])
def test_detect_nonroot_persistent_path_is_a_candidate(path: str) -> None:
    """A NON-root persistent path is a self-hostable candidate — the fix rejects ONLY the
    unbackable root layout, never a normal subdirectory path."""
    detection = detect_release(
        {"package.json": b'{"name":"svc"}', "server.js": b"x\n"},
        intent=ReleaseIntent(
            start_cmd=("node", "server.js"),
            resources=(
                _resource(
                    "db", path=path, url=f"file:{path}", volume="app-data", consumers=("web",)
                ),
            ),
        ),
        provenance=Provenance(),
    )
    assert detection.assessment is ReleaseAssessment.candidate
    assert detection.ingress is not None
    assert detection.resources[0].persistent_path == path


# ---- the ReleaseSpec emit-boundary belt ---------------------------------------


def test_spec_rejects_root_mount_target_defense_in_depth() -> None:
    """A hand-built ``ReleaseSpec`` (bypassing detection) carrying a root-level persistent
    path is REJECTED by the schema mount-target validator — the authoritative emit-boundary
    belt, so a spec built through a non-detecting path can never be lowered into a
    ``<volume>:/`` root-shadowing mount."""
    with pytest.raises(ValidationError):
        _spec(
            services=(_svc("web", role=ServiceRole.ingress, start=("node", "server.js")),),
            resources=(
                _resource("db", path="/app.db", url="file:/app.db", volume="v", consumers=("web",)),
            ),
        )


def test_emitted_compose_backs_a_nonroot_file_exactly() -> None:
    """The emitted compose mounts the file's real parent so the mount BACKS the file, and
    the mount target, the resource url, and release.json all identify the SAME location."""
    spec = _spec(
        services=(_svc("web", role=ServiceRole.ingress, start=("node", "server.js")),),
        resources=(
            _resource(
                "db",
                path="/var/lib/app/db.sqlite",
                url="file:/var/lib/app/db.sqlite",
                volume="app-data",
                consumers=("web",),
            ),
        ),
    )
    overlay = emit_local_compose(spec)
    compose = yaml.safe_load(overlay[COMPOSE_PATH])
    targets = _web_mount_targets(compose, "web")
    assert targets == {"/var/lib/app"}
    assert _persistent_path_is_backed(targets, "/var/lib/app/db.sqlite")
    assert "release.json" in overlay
    parsed = yaml.safe_load(overlay["release.json"])
    resource = parsed["resources"][0]
    assert resource["persistent_path"] == "/var/lib/app/db.sqlite"
    assert resource["profiles"]["local"]["url"] == "file:/var/lib/app/db.sqlite"


def test_multi_consumer_deep_path_with_migration_preserves_isolation() -> None:
    """C7 invariants hold under the real-parent mount: a deep-path resource consumed by
    BOTH services mounts at its real parent on each, the one-shot migrate service mounts
    ONLY that resource, and the mount backs the file."""
    spec = _spec(
        services=(
            _svc("web", role=ServiceRole.ingress, start=("node", "server.js")),
            _svc("worker", role=ServiceRole.worker, start=("node", "worker.js")),
        ),
        resources=(
            _resource(
                "db",
                path="/var/lib/app/db.sqlite",
                url="file:/var/lib/app/db.sqlite",
                volume="app-data",
                consumers=("web", "worker"),
                migrate=("node", "migrate.js"),
            ),
        ),
    )
    overlay = emit_local_compose(spec)
    compose = yaml.safe_load(overlay[COMPOSE_PATH])
    assert _web_mount_targets(compose, "web") == {"/var/lib/app"}
    assert _web_mount_targets(compose, "worker") == {"/var/lib/app"}
    # The one-shot migrate service mounts ONLY the resource it migrates, at its real parent.
    assert _web_mount_targets(compose, "migrate") == {"/var/lib/app"}
    for sid in ("web", "worker", "migrate"):
        assert _persistent_path_is_backed(
            _web_mount_targets(compose, sid), "/var/lib/app/db.sqlite"
        )


# ---- the load-bearing MUTATION regression -------------------------------------


def test_mutation_restoring_data_fallback_breaks_the_persistence_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix is LOAD-BEARING: restoring the pre-R3 blind ``/data`` root-level fallback in
    ``local_mount_target`` makes the root-file persistence proof FAIL again.

    First (unmutated) the root layout fails closed. Then the OLD ``/data`` fallback is
    restored across the three modules that consume ``local_mount_target`` (detect gate,
    spec belt, emitter). With it restored, ``/app.db`` is no longer recognized as
    unbackable: detection returns a self-host CANDIDATE, the spec validates, and the
    emitted compose mounts the volume at ``/data`` — which does NOT contain ``/app.db``, so
    the file is on the ephemeral layer while the candidate promises it persists. That is
    exactly the G07 defect; the real fix (real-parent ``/`` derivation) is what prevents
    it, so this mutation regression pins it."""
    files = {"package.json": b'{"name":"svc"}', "server.js": b"x\n"}

    # Control — the REAL fix rejects the root layout.
    fixed = detect_release(files, intent=_root_intent("/app.db"), provenance=Provenance())
    assert fixed.assessment is ReleaseAssessment.needs_review
    assert [b.code for b in fixed.blockers] == ["persistent_path_unbackable"]

    # Mutation — restore the blind /data fallback in every module that mounts by it.
    monkeypatch.setattr(detect_mod, "local_mount_target", _old_data_fallback_mount_target)
    monkeypatch.setattr(spec_mod, "local_mount_target", _old_data_fallback_mount_target)
    monkeypatch.setattr(lc_mod, "local_mount_target", _old_data_fallback_mount_target)

    mutated = detect_release(files, intent=_root_intent("/app.db"), provenance=Provenance())
    assert mutated.assessment is ReleaseAssessment.candidate, (
        "restoring the /data fallback should re-open the gap (no unbackable-path blocker)"
    )
    ingress = mutated.ingress
    assert ingress is not None
    spec = _spec(services=(ingress,), resources=mutated.resources)
    overlay = emit_local_compose(spec)
    compose = yaml.safe_load(overlay[COMPOSE_PATH])
    targets = _web_mount_targets(compose, "web")
    assert targets == {"/data"}, targets
    assert not _persistent_path_is_backed(targets, "/app.db"), (
        "the mutation must reproduce the defect: /app.db is NOT backed by the /data mount "
        "— the persistence proof fails, proving the real-parent fix is load-bearing."
    )
