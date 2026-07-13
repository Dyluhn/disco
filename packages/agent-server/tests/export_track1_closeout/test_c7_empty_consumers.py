"""WO-C7 (§4.7 frozen ADDITION) — an EXPLICIT empty env `consumers` scope fails closed.

Closes a false-affordance / silent-broken-bundle gap in the WO-C7 landing: an
``EnvVarDecl`` declaring ``consumers=[]`` — an EXPLICIT empty scope, DISTINCT from
an absent ``consumers=None`` — was ACCEPTED by ``ReleaseSpec`` validation, and the
emitter then routed that var to NO service (``service.id in ()`` is always False).
A REQUIRED secret / runtime var declared ``consumers=[]`` therefore vanished from
every service's resolved environment, so the app booted MISSING a required var — a
broken bundle dressed as valid config. The ``EnvVarDecl`` docstring already claimed
an empty tuple is "rejected"; the code did not enforce it. This pins the fix.

The contract (the None-vs-``[]`` distinction is load-bearing):

  * ``consumers=None``   (no explicit scope) → a SINGLE-service spec defaults the
    var to its sole ingress; a MULTI-service unbound consumer-less var is rejected
    by the separate implicit-fan-out guard (``test_c7_topology_matrix.py``). ACCEPTED
    here (single-service).
  * ``consumers=[]``     (EXPLICIT empty)    → REJECTED with a typed cross-field
    error, in BOTH a single- AND a multi-service spec (this file).
  * ``consumers=["web"]``(named)             → ACCEPTED.

Boundary (plan §1.2): these are PURE schema-validation criteria, so they call the
REAL ``ReleaseSpec.model_validate`` assembler / ``load_release_spec`` directly —
the code under test, never a mock. No ``monkeypatch`` at all.

RED on ``7ae564b5`` (the WO-C7 landing): ``ReleaseSpec.model_validate`` ACCEPTS a
``consumers=[]`` env var (no raise), so ``test_explicit_empty_env_consumers_is_rejected``
trips with ``DID NOT RAISE``. GREEN once the cross-field validator rejects an
explicit-empty declared consumer scope.

This is a §4.7 frozen-harness ADDITION — a NEW file; the existing frozen
``test_c7_topology_matrix.py`` is UNCHANGED (byte-identical).
"""

from __future__ import annotations

import json

import pytest
from disco.core.release.spec import ReleaseSpec, load_release_spec
from pydantic import ValidationError

pytestmark = pytest.mark.export_track1_closeout

# A valid `store.VersionRecord`-shaped digest (a bare 64-char lowercase sha256).
_DIGEST = "a" * 64
_PROVENANCE: dict[str, object] = {
    "detector": "disco.core.release.detect",
    "detector_version": "1",
    "assessment": "candidate",
}


def _svc(
    sid: str, *, role: str = "ingress", start: tuple[str, ...] = ("node", "server.js")
) -> dict[str, object]:
    return {"id": sid, "role": role, "runtime": "node", "start_cmd": list(start)}


def _envvar(name: str, *, consumers: tuple[str, ...] | None = None) -> dict[str, object]:
    block: dict[str, object] = {"name": name, "scope": "runtime", "secret": "secret"}
    if consumers is not None:
        # The EXPLICIT scope. `consumers=()` is a distinct, DECLARED empty scope
        # (not the same as omitting the key, which yields `None`).
        block["consumers"] = list(consumers)
    return block


def _payload(
    *, services: list[dict[str, object]], env: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "kind": "node",
        "name": "app",
        "version_seq": 1,
        "tree_digest": _DIGEST,
        "services": services,
        "env": env,
        "resources": [],
        "provenance": dict(_PROVENANCE),
    }


_WORKER = _svc("worker", role="worker", start=("node", "worker.js"))


@pytest.mark.parametrize(
    "services",
    [[_svc("web")], [_svc("web"), dict(_WORKER)]],
    ids=["single_service", "multi_service"],
)
def test_explicit_empty_env_consumers_is_rejected(services: list[dict[str, object]]) -> None:
    """§11 model invariant — RED on 7ae564b5.

    A runtime env var declaring an EXPLICIT empty ``consumers=[]`` scope must FAIL
    schema validation in BOTH a single- and a multi-service spec: a declared
    consumer scope that names no service would route the var to no container at all,
    silently dropping a REQUIRED secret. Baseline (7ae564b5) accepts it (the field
    parses to an empty tuple and no cross-field validator rejects it), so this
    ``pytest.raises`` trips with DID NOT RAISE."""
    payload = _payload(services=services, env=[_envvar("SESSION_SECRET", consumers=())])
    with pytest.raises(ValidationError, match="at least one service"):
        ReleaseSpec.model_validate(payload)


def test_explicit_empty_env_consumers_is_rejected_via_load() -> None:
    """The same fail-closed rule holds through the on-disk ``load_release_spec`` path
    (a persisted spec carrying an explicit empty consumer scope is refused, never
    read as a var that reaches no service)."""
    payload = _payload(services=[_svc("web")], env=[_envvar("SESSION_SECRET", consumers=())])
    with pytest.raises(ValidationError, match="at least one service"):
        _ = load_release_spec(json.dumps(payload))


def test_absent_env_consumers_defaults_to_sole_ingress() -> None:
    """GREEN preservation — the None case is DISTINCT from the empty case.

    An UNBOUND var that OMITS ``consumers`` (parses to ``None``, no explicit scope)
    in a single-service spec is ACCEPTED and defaults to the sole ingress; the fix
    must not conflate ``None`` (no scope) with ``[]`` (explicit empty)."""
    spec = ReleaseSpec.model_validate(
        _payload(services=[_svc("web")], env=[_envvar("SESSION_SECRET")])
    )
    assert {var.name for var in spec.env} == {"SESSION_SECRET"}
    assert spec.env[0].consumers is None


def test_named_env_consumers_is_accepted() -> None:
    """GREEN preservation — a DECLARED non-empty consumer scope validates and is
    preserved, in a multi-service spec."""
    spec = ReleaseSpec.model_validate(
        _payload(
            services=[_svc("web"), dict(_WORKER)],
            env=[_envvar("SESSION_SECRET", consumers=("web",))],
        )
    )
    assert spec.env[0].consumers == ("web",)
