"""WO-F3.3 Cloudflare webhook deployment bindings and token composition."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.agent_server.appkit_cloudflare import deploy as cf
from disco.agent_server.appkit_cloudflare.stripe_deploy import (
    StripeDeployContext,
    StripeDeployDependencies,
)
from disco.agent_server.appkit_cloudflare.webhook_deploy import (
    WebhookDeployContext,
    WebhookDeployDependencies,
    WebhookDeployError,
    WebhookDeploymentLifecycle,
    webhook_lifecycle_for,
)
from disco.agent_server.appkit_cloudflare.wrangler import BuildResult, CommandResult
from disco.agent_server.host_token_store import HostTokenStore
from disco.core.appkit import generate, get_recipe, save_app_spec, save_design_spec
from disco.core.appkit.records_primitive import default_records_auth_app_spec
from disco.core.appkit.spec import AppSpec
from disco.core.appkit.stripe_primitive import StripeSpec, apply_stripe_spec
from disco.core.appkit.webhook_primitive import WebhookSpec, apply_webhook_spec
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.origin_approvals import OriginApprovalStore
from disco.core.stripe_host_service import (
    StripeAppConfigStore,
    configure_stripe_restricted_key,
    configure_stripe_webhook_secret,
    ensure_stripe_binding_secret,
)
from disco.core.webhook_host_service import (
    WEBHOOK_EMIT_SERVICE_NAME,
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
    configure_webhook_inbound_secret,
)

_OWNER = "owner-1"
_CID = "11111111-1111-1111-1111-111111111111"
_ORIGIN = "https://stripe-test.workers.dev"
_ADMIN = "admin-token-with-enough-entropy-123"
_CF_TOKEN = "cfut_realCloudflareToken1234567890abcdefABCDEF"
_ACCOUNT = "abc123account456"
_DB_ID = "11111111-2222-3333-4444-555555555555"
_INBOUND = "inbound-signing-secret-never-persisted-123456"
_OUTBOUND = "outbound-signing-secret-never-persisted-12345"


class _Probe:
    async def payments_ready(self, _deployed_origin: str, _admin_token: str) -> bool:
        return True


class _Runner:
    def __init__(self, fail_binding: tuple[str, str] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_binding = fail_binding

    async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
        self.calls.append({"argv": list(argv), "cwd": str(cwd), "env": dict(env), "stdin": stdin})
        sub = list(argv[1:])
        if sub[:2] == ["secret", "put"] and self.fail_binding == (
            str(sub[-1]),
            str(stdin),
        ):
            return CommandResult(1, "", "simulated secret failure")
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
        del install_cmd, build_cmd
        dest = workspace / asset_dir
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text("<html></html>", encoding="utf-8")
        return BuildResult(0, "built", "", True)


def _webhook_specs() -> tuple[WebhookSpec, WebhookSpec]:
    return (
        WebhookSpec(
            endpoint_id="orders_in",
            direction="inbound",
            event_types=["order.created"],
        ),
        WebhookSpec(
            endpoint_id="orders_out",
            direction="outbound",
            event_types=["order.shipped"],
        ),
    )


def _write_export(workspace: Path, *, stripe: bool) -> AppSpec:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    app = default_records_auth_app_spec("Webhook Records", recipe)
    stripe_spec: StripeSpec | None = None
    if stripe:
        stripe_spec = StripeSpec(
            plan_name="Pro",
            price_display="$9/mo",
            entitlement_flag="pro_member",
            success_message="Welcome to Pro.",
        )
        app = apply_stripe_spec(app, stripe_spec)
    webhook_specs = _webhook_specs()
    for spec in webhook_specs:
        app = apply_webhook_spec(app, spec)
    design = recipe.to_design_spec()
    for rel, body in generate(app, design).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    save_app_spec(workspace, app)
    save_design_spec(workspace, design)
    primitive_dir = workspace / ".disco" / "primitives"
    primitive_dir.mkdir(parents=True, exist_ok=True)
    (primitive_dir / "webhook.json").write_text(
        json.dumps(
            {
                "primitive_id": "webhook",
                "tier": "template_only",
                "applied_at": "2026-07-11T00:00:00Z",
                "specs": [spec.model_dump(mode="json") for spec in webhook_specs],
            }
        ),
        encoding="utf-8",
    )
    if stripe_spec is not None:
        (primitive_dir / "stripe.json").write_text(
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


def _runtime(tmp_path: Path, app: AppSpec):
    assert app.webhooks is not None
    secrets = SecretStore(
        tmp_path / "secrets.json", box=SecretBox("strong-app-key-0123456789abcdef")
    )
    cf.connect_account(secrets, token=_CF_TOKEN, account_id=_ACCOUNT)
    configure_webhook_inbound_secret(secrets, _OWNER, app.webhooks.app_binding, _INBOUND)
    webhook_configs = WebhookAppConfigStore(tmp_path / "webhook.db")
    webhook_configs.configure(
        owner_id=_OWNER,
        audience=app.webhooks.app_binding,
        endpoint_id="orders_out",
        target_url="https://hooks.example.test/delivery",
        signing_secret=_OUTBOUND,
        event_types=frozenset({"order.shipped"}),
        enabled=True,
        secret_store=secrets,
    )
    config = webhook_configs.get(_OWNER, app.webhooks.app_binding, "orders_out")
    assert config is not None
    approvals = OriginApprovalStore(tmp_path / "approvals.json", secret_store=secrets)
    approvals.approve(config.target_url, WEBHOOK_PURPOSE, config.secret_ref)
    tokens = HostTokenStore(tmp_path / "tokens.db")
    return secrets, webhook_configs, tokens, approvals


def _deploy_environment(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("DISCO_DEPLOY_STAGE_DIR", str(tmp_path / "staging"))
    wrangler = tmp_path / "trusted" / "wrangler"
    wrangler.parent.mkdir()
    wrangler.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DISCO_WRANGLER_BIN", str(wrangler))


async def test_directional_secrets_are_installed_only_when_required(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=False)
    assert app.webhooks is not None
    secrets, configs, tokens, approvals = _runtime(tmp_path, app)

    class Sink:
        def __init__(self) -> None:
            self.names: list[str] = []

        async def __call__(self, name: str, _value: str) -> bool:
            self.names.append(name)
            return True

    inbound_meta = app.webhooks.model_copy(update={"endpoints": (app.webhooks.endpoints[0],)})
    outbound_meta = app.webhooks.model_copy(update={"endpoints": (app.webhooks.endpoints[1],)})
    inbound = WebhookDeploymentLifecycle(
        app_spec=app.model_copy(update={"webhooks": inbound_meta}),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        approvals=approvals,
    )
    outbound = WebhookDeploymentLifecycle(
        app_spec=app.model_copy(update={"webhooks": outbound_meta}),
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        approvals=approvals,
    )
    inbound_sink, outbound_sink = Sink(), Sink()
    await inbound.install_fixed_bindings(inbound_sink)
    await outbound.install_fixed_bindings(outbound_sink)
    assert inbound_sink.names == ["WEBHOOK_SIGNING_SECRET"]
    assert outbound_sink.names == ["DISCO_SVC_BUS"]
    assert inbound.required_services == frozenset()
    assert outbound.required_services == frozenset({WEBHOOK_EMIT_SERVICE_NAME})
    tokens.close()
    configs.close()


def test_webhook_deploy_fails_closed_without_host_dependencies_or_outbound_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=False)
    assert app.webhooks is not None
    secrets = SecretStore(
        tmp_path / "secrets.json", box=SecretBox("strong-app-key-0123456789abcdef")
    )
    configure_webhook_inbound_secret(secrets, _OWNER, app.webhooks.app_binding, _INBOUND)
    with pytest.raises(WebhookDeployError, match="not wired"):
        webhook_lifecycle_for(
            app,
            owner_id=_OWNER,
            conversation_id=_CID,
            secret_store=secrets,
            dependencies=None,
        )
    configs = WebhookAppConfigStore(tmp_path / "webhook.db")
    tokens = HostTokenStore(tmp_path / "tokens.db")
    with pytest.raises(WebhookDeployError, match="missing or disabled"):
        WebhookDeploymentLifecycle(
            app_spec=app,
            owner_id=_OWNER,
            conversation_id=_CID,
            secret_store=secrets,
            config_store=configs,
            token_store=tokens,
        )
    tokens.close()
    configs.close()


async def test_webhook_quiesce_failure_retries_ready_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=False)
    secrets, configs, tokens, approvals = _runtime(tmp_path, app)
    lifecycle = WebhookDeploymentLifecycle(
        app_spec=app,
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        approvals=approvals,
    )

    class Sink:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def __call__(self, name: str, value: str) -> bool:
            self.calls.append((name, value))
            return len(self.calls) > 1

    sink = Sink()
    with pytest.raises(WebhookDeployError):
        await lifecycle.establish_disabled(sink)
    assert await lifecycle.fail_closed(sink)
    assert sink.calls == [
        ("WEBHOOK_RUNTIME_READY", "0"),
        ("WEBHOOK_RUNTIME_READY", "0"),
    ]
    tokens.close()
    configs.close()


async def test_webhook_only_deploy_installs_directional_bindings_and_rotated_scope(
    tmp_path: Path, monkeypatch
) -> None:
    _deploy_environment(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=False)
    secrets, configs, tokens, approvals = _runtime(tmp_path, app)
    runner = _Runner()
    context = WebhookDeployContext(
        WebhookDeployDependencies(tokens, configs, approvals), _OWNER, _CID
    )
    plan = cf.build_plan(workspace, secrets)
    result = await cf.execute_deploy(
        workspace,
        secrets,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=_Build(),
        admin_token=_ADMIN,
        webhook_context=context,
    )
    assert result.succeeded and result.executed
    puts = [
        list(call["argv"])[-1]
        for call in runner.calls
        if list(call["argv"])[1:3] == ["secret", "put"]
    ]
    assert puts == [
        "WEBHOOK_RUNTIME_READY",
        "ADMIN_TOKEN",
        "WEBHOOK_SIGNING_SECRET",
        "DISCO_SVC_BUS",
        "DISCO_SVC_TOKEN",
        "WEBHOOK_RUNTIME_READY",
    ]
    ready_values = [
        call["stdin"] for call in runner.calls if list(call["argv"])[-1] == "WEBHOOK_RUNTIME_READY"
    ]
    assert ready_values == ["0", "1"]
    active = [record for record in tokens.list_for_conversation(_CID) if record.is_active]
    assert len(active) == 1
    assert active[0].allowed_services == frozenset({WEBHOOK_EMIT_SERVICE_NAME})
    assert active[0].allowed_origins == frozenset({_ORIGIN})
    persisted = Path(result.record_path or "").read_text(encoding="utf-8")
    rendered = json.dumps(result.transcript) + persisted
    tree = "\n".join(
        path.read_text(encoding="utf-8") for path in workspace.rglob("*") if path.is_file()
    )
    for secret in (_INBOUND, _OUTBOUND, _ADMIN):
        assert secret not in rendered and secret not in tree
        assert all(secret not in list(call["argv"]) for call in runner.calls)
    assert "a2v0." not in rendered
    tokens.close()
    configs.close()


async def test_webhook_enable_failure_restores_ready_zero(tmp_path: Path, monkeypatch) -> None:
    _deploy_environment(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=False)
    secrets, configs, tokens, approvals = _runtime(tmp_path, app)
    runner = _Runner(("WEBHOOK_RUNTIME_READY", "1"))
    result = await cf.execute_deploy(
        workspace,
        secrets,
        dry_run=False,
        confirmation=cf.build_plan(workspace, secrets).confirmation_phrase,
        runner=runner,
        build_backend=_Build(),
        admin_token=_ADMIN,
        webhook_context=WebhookDeployContext(
            WebhookDeployDependencies(tokens, configs, approvals), _OWNER, _CID
        ),
    )
    assert not result.succeeded and not result.executed
    assert result.failed_step == "wrangler secret put WEBHOOK_RUNTIME_READY"
    ready_values = [
        call["stdin"] for call in runner.calls if list(call["argv"])[-1] == "WEBHOOK_RUNTIME_READY"
    ]
    assert ready_values == ["0", "1", "0"]
    tokens.close()
    configs.close()


async def test_combined_stripe_webhook_deploy_mints_one_union_token(
    tmp_path: Path, monkeypatch
) -> None:
    _deploy_environment(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=True)
    assert app.stripe is not None and app.webhooks is not None
    assert app.stripe.app_binding == app.webhooks.app_binding
    secrets, webhook_configs, tokens, approvals = _runtime(tmp_path, app)
    configure_stripe_restricted_key(secrets, "rk_test_restricted_checkout_key")
    ensure_stripe_binding_secret(secrets, _OWNER, app.stripe.app_binding)
    configure_stripe_webhook_secret(
        secrets, _OWNER, app.stripe.app_binding, "whsec_test_deployment_signer"
    )
    stripe_configs = StripeAppConfigStore(tmp_path / "stripe.db")
    stripe_configs.configure(
        owner_id=_OWNER,
        audience=app.stripe.app_binding,
        plan_selector=app.stripe.plan_selector,
        stripe_price_id="price_123456789",
        allowed_return_origins=frozenset({_ORIGIN}),
        enabled=True,
        secret_store=secrets,
    )
    runner = _Runner()
    result = await cf.execute_deploy(
        workspace,
        secrets,
        dry_run=False,
        confirmation=cf.build_plan(workspace, secrets).confirmation_phrase,
        runner=runner,
        build_backend=_Build(),
        admin_token=_ADMIN,
        stripe_context=StripeDeployContext(
            StripeDeployDependencies(tokens, stripe_configs, _Probe()), _OWNER, _CID
        ),
        webhook_context=WebhookDeployContext(
            WebhookDeployDependencies(tokens, webhook_configs, approvals), _OWNER, _CID
        ),
    )
    assert result.succeeded
    puts = [
        list(call["argv"])[-1]
        for call in runner.calls
        if list(call["argv"])[1:3] == ["secret", "put"]
    ]
    assert puts.count("DISCO_SVC_BUS") == 1
    assert puts.count("DISCO_SVC_TOKEN") == 1
    assert "WEBHOOK_SIGNING_SECRET" in puts
    assert puts == [
        "WEBHOOK_RUNTIME_READY",
        "WEBHOOK_SIGNING_SECRET",
        "STRIPE_RUNTIME_READY",
        "ADMIN_TOKEN",
        "DISCO_SVC_BUS",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_APP_BINDING_SECRET",
        "DISCO_SVC_TOKEN",
        "STRIPE_RUNTIME_READY",
        "WEBHOOK_RUNTIME_READY",
    ]
    active = [record for record in tokens.list_for_conversation(_CID) if record.is_active]
    assert len(active) == 1
    assert active[0].allowed_services == frozenset(
        {"payments.ready", "payments.checkout", WEBHOOK_EMIT_SERVICE_NAME}
    )
    tokens.close()
    webhook_configs.close()
    stripe_configs.close()


def test_post_build_webhook_config_drift_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_SVC_BUS_PUBLIC_URL", "https://bus.example.test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _write_export(workspace, stripe=False)
    secrets, configs, tokens, approvals = _runtime(tmp_path, app)
    lifecycle = WebhookDeploymentLifecycle(
        app_spec=app,
        owner_id=_OWNER,
        conversation_id=_CID,
        secret_store=secrets,
        config_store=configs,
        token_store=tokens,
        approvals=approvals,
    )
    configs.configure(
        owner_id=_OWNER,
        audience=app.webhooks.app_binding if app.webhooks is not None else "",
        endpoint_id="orders_out",
        target_url="https://hooks.example.test/delivery",
        signing_secret="rotated-outbound-secret-never-accepted-12345",
        event_types=frozenset({"order.shipped"}),
        enabled=True,
        secret_store=secrets,
    )
    with pytest.raises(WebhookDeployError, match="changed"):
        lifecycle.recheck_after_build(app)
    tokens.close()
    configs.close()
