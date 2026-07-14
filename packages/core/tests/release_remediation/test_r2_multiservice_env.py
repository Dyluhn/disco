"""R2 (design 1) — scoped env with requiredness + secret class across MULTIPLE services.

The typed `ReleaseIntent` is single-service (one app), so the scoped-env machinery it
feeds — `EnvVarDecl.scope` / `.required` / `.secret` / `.consumers` — is proven here at
the SPEC + emit + validate boundary a multi-service topology exercises (the C7
invariants R2's env decls rely on):

* a BUILD-scope SECRET env fails closed at `validate_release`
  (`secret_build_env_unsupported`) — it can never be lowered into a build arg;
* a RUNTIME SECRET renders a NAME-only `${NAME:?...}` guard in the emitted compose (no
  value, no secret material);
* per-consumer ISOLATION holds — a runtime var scoped to one service reaches ONLY that
  service's environment, never a co-resident service's.

Outside the frozen closeout dirs (no `export_track1_closeout` marker).
"""

from __future__ import annotations

import yaml
from disco.core.release.local_compose import COMPOSE_PATH, emit_local_compose
from disco.core.release.spec import ReleaseSpec
from disco.core.release.validate import BlockerCode, validate_release

_DIGEST = "b" * 64
_PROV = {"detector": "d", "detector_version": "1", "assessment": "candidate"}


def _svc(
    sid: str, *, role: str, start: tuple[str, ...] = ("node", "server.js")
) -> dict[str, object]:
    return {"id": sid, "role": role, "runtime": "node", "start_cmd": list(start)}


def _env(
    name: str,
    *,
    scope: str = "runtime",
    required: bool = True,
    secret: str = "public",
    consumers: tuple[str, ...] | None = None,
) -> dict[str, object]:
    block: dict[str, object] = {
        "name": name,
        "scope": scope,
        "required": required,
        "secret": secret,
    }
    if consumers is not None:
        block["consumers"] = list(consumers)
    return block


def _spec(services: list[dict[str, object]], env: list[dict[str, object]]) -> ReleaseSpec:
    return ReleaseSpec.model_validate(
        {
            "kind": "node",
            "name": "multi",
            "version_seq": 1,
            "tree_digest": _DIGEST,
            "services": services,
            "env": env,
            "resources": [],
            "provenance": dict(_PROV),
        }
    )


def _env_of(compose: dict[str, object], sid: str) -> dict[str, str]:
    services = compose["services"]
    assert isinstance(services, dict)
    block = services[sid]
    assert isinstance(block, dict)
    env = block.get("environment", {})
    assert isinstance(env, dict)
    return {str(k): str(v) for k, v in env.items()}


def test_per_consumer_runtime_secret_isolation_and_name_only_guard() -> None:
    """A multi-service spec with two runtime SECRET vars, each scoped to a different
    service, isolates them: `WEB_TOKEN` reaches only `web`, `WORKER_TOKEN` only
    `worker`, and each is a NAME-only `${NAME:?...}` guard (no value)."""
    spec = _spec(
        services=[_svc("web", role="ingress"), _svc("worker", role="worker")],
        env=[
            _env("WEB_TOKEN", secret="secret", consumers=("web",)),
            _env("WORKER_TOKEN", secret="secret", consumers=("worker",)),
        ],
    )
    # Sanity: this spec is releasable (no build secret, all consumers valid).
    assert validate_release(spec, {"server.js": 1}).ok

    compose = yaml.safe_load(emit_local_compose(spec)[COMPOSE_PATH])
    web_env = _env_of(compose, "web")
    worker_env = _env_of(compose, "worker")

    # Per-consumer isolation: each secret reaches ONLY its declared consumer.
    assert "WEB_TOKEN" in web_env and "WEB_TOKEN" not in worker_env, (web_env, worker_env)
    assert "WORKER_TOKEN" in worker_env and "WORKER_TOKEN" not in web_env, (web_env, worker_env)
    # NAME-only required guard — the value is a `${NAME:?...}` interpolation, never a literal.
    assert web_env["WEB_TOKEN"].startswith("${WEB_TOKEN:?"), web_env["WEB_TOKEN"]
    assert worker_env["WORKER_TOKEN"].startswith("${WORKER_TOKEN:?"), worker_env["WORKER_TOKEN"]
    # The guard message is name-only (no value / '=').
    assert "=" not in web_env["WEB_TOKEN"].split(":?", 1)[1].rstrip("}")


def test_build_scope_secret_env_fails_closed_at_validate() -> None:
    """A build-scope SECRET var can never lower into a build arg — validate_release
    refuses the spec with `secret_build_env_unsupported`, the authoritative emit-boundary
    gate independent of how the var was produced."""
    spec = _spec(
        services=[_svc("web", role="ingress")],
        env=[_env("BUILD_API_TOKEN", scope="build", secret="secret")],
    )
    result = validate_release(spec, {"server.js": 1})
    assert not result.ok
    assert any(b.code is BlockerCode.secret_build_env_unsupported for b in result.blockers), [
        b.code for b in result.blockers
    ]


def test_public_build_var_is_a_name_only_build_arg_not_a_runtime_env() -> None:
    """A build-scope PUBLIC var lowers to a Compose build arg + Dockerfile ARG (name-only
    guard), and is NOT injected into the runtime environment (it is compile-time only)."""
    spec = _spec(
        services=[_svc("web", role="ingress")],
        env=[_env("PUBLIC_BANNER", scope="build", secret="public")],
    )
    assert validate_release(spec, {"server.js": 1}).ok
    overlay = emit_local_compose(spec)
    compose = yaml.safe_load(overlay[COMPOSE_PATH])
    web = compose["services"]["web"]
    # It is a build arg (name-only guard), not a runtime environment entry.
    assert "PUBLIC_BANNER" in web["build"]["args"], web["build"]
    assert "PUBLIC_BANNER" not in _env_of(compose, "web")
    assert "ARG PUBLIC_BANNER" in overlay["Dockerfile"]
