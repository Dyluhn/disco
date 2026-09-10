"""R2 (G03/G06) — the versioned intent schema + path validators + emit lowering.

Closeout remediation R2, at the PURE core boundary (no route): the typed
`ReleaseIntent` schema-v3 contract, its deterministic v1/v2->v3 read policy, its
workspace-relative path guards, and the detect->emit lowering of an interpreted
install layer and a static `output_dir`. These live OUTSIDE the frozen closeout dirs
(no `export_track1_closeout` marker) so they never perturb the acceptance manifest.
"""

from __future__ import annotations

import json

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import DOCKERFILE_PATH, emit_local_compose
from disco.core.release.spec import (
    RELEASE_INTENT_SCHEMA_VERSION,
    DetectorProvenance,
    IntentUpgradeError,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseSpec,
    RuntimeStrategy,
    parse_release_intent,
)
from pydantic import ValidationError


def _canonical(intent: ReleaseIntent) -> str:
    return json.dumps(
        intent.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


# ---- schema version + canonical byte identity ---------------------------------------


def test_intent_schema_version_is_three() -> None:
    assert RELEASE_INTENT_SCHEMA_VERSION == 3
    assert ReleaseIntent().schema_version == 3


def test_fully_typed_intent_canonical_bytes_are_serialize_parse_serialize_identical() -> None:
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.node,
        install_cmd=("npm", "ci"),
        build_cmd=("npm", "run", "build"),
        start_cmd=("node", "server.js"),
        package_manager="npm",
        lockfile="package-lock.json",
        output_dir="dist",
        required_env=("DATABASE_URL",),
    )
    first = _canonical(intent)
    reparsed = parse_release_intent(json.loads(first))
    second = _canonical(reparsed)
    assert first == second, "canonical serialize/parse/serialize is not byte-identical"
    assert '"output_dir":"dist"' in first and '"install_cmd":["npm","ci"]' in first


# ---- deterministic v1/v2 -> v3 migration; newer version fails closed ----------------


def test_v2_sidecar_migrates_deterministically_to_v3() -> None:
    stored_v2 = {
        "schema_version": 2,
        "start_cmd": ["node", "server.js"],
        "build_cmd": [],
        "port_env": "PORT",
        "required_env": ["SESSION_SECRET"],
        "resources": [],
    }
    once = parse_release_intent(json.loads(json.dumps(stored_v2)))
    twice = parse_release_intent(json.loads(json.dumps(stored_v2)))
    assert once.schema_version == 3, "a stored v2 sidecar must migrate to v3"
    assert once.start_cmd == ("node", "server.js")
    assert once.required_env == ("SESSION_SECRET",)
    assert _canonical(once) == _canonical(twice), "migration must be deterministic"


def test_v1_sidecar_migrates_to_v3() -> None:
    stored_v1 = {"start_cmd": ["node", "server.js"]}  # version-less legacy
    migrated = parse_release_intent(stored_v1)
    assert migrated.schema_version == 3 and migrated.start_cmd == ("node", "server.js")


def test_newer_than_current_sidecar_fails_closed() -> None:
    newer = {"schema_version": RELEASE_INTENT_SCHEMA_VERSION + 1, "start_cmd": ["node", "s.js"]}
    with pytest.raises(IntentUpgradeError):
        parse_release_intent(newer)


def test_unupgradeable_older_sidecar_fails_closed() -> None:
    # A v2-tagged shape carrying a field the v3 schema forbids cannot be upgraded.
    bad = {"schema_version": 2, "start_cmd": ["node", "s.js"], "deploy_target": "legacy"}
    with pytest.raises(IntentUpgradeError):
        parse_release_intent(bad)


# ---- workspace-relative path validators reject unsafe output_dir / lockfile ---------


@pytest.mark.parametrize(
    "bad",
    ["../etc", "/abs/dist", "a/../b", "dist\n", "di st", "-flag", "d;rm", "d$(x)"],
)
def test_unsafe_output_dir_is_rejected_at_construction(bad: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseIntent(output_dir=bad)


@pytest.mark.parametrize("bad", ["../lock", "/abs/lock", "a\nb"])
def test_unsafe_lockfile_is_rejected_at_construction(bad: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseIntent(lockfile=bad)


def test_install_cmd_token_hygiene_rejects_shell_metachars() -> None:
    with pytest.raises(ValidationError):
        ReleaseIntent(install_cmd=("npm", "ci", "&&", "rm"))


# ---- detect -> emit lowering (pure) --------------------------------------------------


def _spec_from(intent: ReleaseIntent, files: dict[str, bytes]) -> ReleaseSpec:
    detection = detect_release(files, intent=intent, provenance=Provenance())
    assert detection.assessment is ReleaseAssessment.candidate, detection.reasons
    ingress = detection.ingress
    assert ingress is not None
    return ReleaseSpec(
        kind=ingress.runtime.value,
        name="app",
        version_seq=1,
        tree_digest="0" * 64,
        services=detection.services,
        env=detection.env,
        resources=detection.resources,
        provenance=DetectorProvenance(
            detector="d", detector_version="1", assessment=detection.assessment
        ),
    )


def test_interpreted_node_intent_install_lowers_without_a_build() -> None:
    files = {
        "package.json": (
            b'{"name":"a","dependencies":{"express":"1"},"scripts":{"start":"node s.js"}}'
        ),
        "s.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    }
    spec = _spec_from(ReleaseIntent(start_cmd=("node", "s.js")), files)
    dockerfile = emit_local_compose(spec)[DOCKERFILE_PATH]
    assert '"npm", "install"' in dockerfile  # no lockfile -> install
    assert dockerfile.index('"npm", "install"') < dockerfile.index('"node", "s.js"')


def test_static_intent_output_dir_lowers_into_copy() -> None:
    files = {
        "index.html": b"<!doctype html><div id=a></div>\n",
        "package.json": (
            b'{"name":"s","scripts":{"build":"vite build"},"devDependencies":{"vite":"1"}}'
        ),
        "vite.config.js": b"export default { build: { outDir: 'out' } };\n",
        "src/m.js": b"1;\n",
    }
    intent = ReleaseIntent(build_cmd=("npm", "run", "build"), output_dir="out", health_path="/")
    spec = _spec_from(intent, files)
    dockerfile = emit_local_compose(spec)[DOCKERFILE_PATH]
    assert "COPY --from=build /app/out/ /site/" in dockerfile, dockerfile


def test_static_prebuilt_intent_without_build_serves_output_dir() -> None:
    files = {"dist/index.html": b"<!doctype html>ok\n"}
    intent = ReleaseIntent(runtime=RuntimeStrategy.static, output_dir="dist", health_path="/")
    spec = _spec_from(intent, files)
    dockerfile = emit_local_compose(spec)[DOCKERFILE_PATH]
    # A prebuilt static serve: single-stage, copies the declared output dir, no build.
    assert "COPY dist/ /site/" in dockerfile, dockerfile
    assert "AS build" not in dockerfile


@pytest.mark.parametrize("head", ["pnpm", "yarn", "bun", "poetry", "uv"])
def test_typed_unprovisioned_manager_start_is_needs_review(head: str) -> None:
    files = {
        "package.json": b'{"name":"s","scripts":{"start":"node s.js"}}',
        "s.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    }
    detection = detect_release(
        files, intent=ReleaseIntent(start_cmd=(head, "start")), provenance=Provenance()
    )
    assert detection.assessment is ReleaseAssessment.needs_review, head
    assert any(b.code == "toolchain_unsupported" for b in detection.blockers), head
    assert detection.services == ()  # no ingress -> no overlay
