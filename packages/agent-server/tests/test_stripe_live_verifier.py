"""Focused tests for the host-owned ``stripe.security.v1`` live verifier.

The full integration test boots a real generated Worker + local D1 + authenticated
host-service bus and is marked ``integration``; the remaining tests are lightweight
unit/failure-path checks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from disco.agent_server.stripe_live_verifier import (
    StripeLiveVerifierError,
    _check_restricted_key_only,
    _find_wrangler,
    _StripeVerifyRun,
    _WorkerdApp,
    make_stripe_live_verifier,
)
from disco.core.appkit import get_recipe
from disco.core.appkit.generator import generate
from disco.core.appkit.primitives import VerifyCheck
from disco.core.appkit.records_primitive import default_records_auth_app_spec
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.appkit.stripe_primitive import StripeSpec, apply_stripe_spec
from disco.core.llm.secrets import SecretBox, SecretStore

_SPEC = StripeSpec(
    plan_name="Pro",
    price_display="$9/mo",
    entitlement_flag="pro_member",
    success_message="Welcome to Pro — your workspace is unlocked.",
    features=["Unlimited records", "Priority support"],
)


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


def _records_app() -> AppSpec:
    return default_records_auth_app_spec("Stripe live proof", _recipe())


def _filled_tree() -> tuple[AppSpec, dict[str, str]]:
    app = apply_stripe_spec(_records_app(), _SPEC)
    return app, generate(app, _design())


def test_make_stripe_live_verifier_handles_unknown_id() -> None:
    verifier = make_stripe_live_verifier()
    app, tree = _filled_tree()
    result = asyncio.run(verifier("not.stripe.security.v1", app, _design(), tree))
    assert result.ok is False
    assert [check.name for check in result.checks] == [
        "forged_signature_rejected",
        "replay_deduped",
        "secret_absence",
        "restricted_key_only",
        "price_injection_refused",
    ]
    assert all(check.passed is False for check in result.checks)
    assert "unknown live_verify_id" in result.checks[0].evidence


def test_make_stripe_live_verifier_fails_named_checks_when_wrangler_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr("disco.agent_server.stripe_live_verifier._find_wrangler", lambda _cwd: None)
    verifier = make_stripe_live_verifier()
    app, tree = _filled_tree()
    result = asyncio.run(verifier("stripe.security.v1", app, _design(), tree))
    assert result.ok is False
    for check in result.checks:
        assert check.passed is False
    assert "wrangler" in result.detail.lower() or any(
        "wrangler" in check.evidence.lower() for check in result.checks
    )


def test_restricted_key_only_refuses_sk() -> None:
    store = SecretStore(path=":memory:", box=SecretBox("a" * 32 + "b" * 32))
    check = asyncio.run(_check_restricted_key_only(store))
    assert check.passed is True
    assert "sk_" in check.evidence and "rk_" in check.evidence


def test_restricted_key_only_fails_when_sk_is_accepted(monkeypatch) -> None:
    store = SecretStore(path=":memory:", box=SecretBox("a" * 32 + "b" * 32))

    def accept_any(*_args, **_kwargs):
        pass

    monkeypatch.setattr(
        "disco.agent_server.stripe_live_verifier.configure_stripe_restricted_key",
        accept_any,
    )
    check = asyncio.run(_check_restricted_key_only(store))
    assert check.passed is False


async def _stub_price_check(_run, _token):
    return VerifyCheck(name="price_injection_refused", passed=True, evidence="stub")


def test_cleanup_runs_when_worker_boot_fails(monkeypatch) -> None:
    """If Worker boot fails, the verifier must still remove temp data and revoke tokens."""
    app, tree = _filled_tree()
    run = _StripeVerifyRun(app, _design(), tree)

    monkeypatch.setattr(
        "disco.agent_server.stripe_live_verifier._check_price_injection_refused",
        _stub_price_check,
    )

    def raise_on_enter(*_args, **_kwargs):
        raise StripeLiveVerifierError("simulated worker boot failure")

    monkeypatch.setattr(
        "disco.agent_server.stripe_live_verifier._WorkerdApp.__enter__",
        raise_on_enter,
    )

    result = asyncio.run(run.run())
    assert result.ok is False
    assert run.tmp_root is None
    assert run.bus is None
    assert not hasattr(run, "_host_bus_server")


def test_live_worker_refuses_a_tree_without_a_lockfile(tmp_path: Path) -> None:
    app, tree = _filled_tree()
    del tree["package-lock.json"]
    worker = _WorkerdApp(tree, {"ADMIN_TOKEN": "test"}, tmp_path, tmp_path / "ca.pem")
    with pytest.raises(StripeLiveVerifierError, match="package-lock"):
        worker.__enter__()


def test_cleanup_revokes_and_removes_state_after_worker_shutdown_failure(tmp_path: Path) -> None:
    class BrokenWorker:
        def __exit__(self, *_args: object) -> None:
            raise RuntimeError("worker stayed alive")

    class TokenStore:
        def __init__(self) -> None:
            self.revoked = False
            self.closed = False

        def revoke(self, _selector: str) -> bool:
            self.revoked = True
            return True

        def close(self) -> None:
            self.closed = True

    class Store:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    app, tree = _filled_tree()
    run = _StripeVerifyRun(app, _design(), tree)
    root = tmp_path / "private-run"
    root.mkdir()
    token_store = TokenStore()
    config_store = Store()
    event_store = Store()
    run.tmp_root = root
    run.worker = cast(Any, BrokenWorker())
    run.token_store = cast(Any, token_store)
    run.token_selector = "selector"
    run.stripe_config_store = cast(Any, config_store)
    run.event_store = cast(Any, event_store)

    with pytest.raises(StripeLiveVerifierError, match="cleanup was incomplete"):
        asyncio.run(run._cleanup())

    assert token_store.revoked and token_store.closed
    assert config_store.closed and event_store.closed
    assert not root.exists()


@pytest.mark.integration
def test_live_verifier_integration() -> None:
    if _find_wrangler(Path.cwd()) is None:
        pytest.skip("wrangler executable is required for the live Stripe verifier")
    verifier = make_stripe_live_verifier()
    app, tree = _filled_tree()
    result = asyncio.run(verifier("stripe.security.v1", app, _design(), tree))
    assert result.ok is True, result.detail
    for check in result.checks:
        assert check.passed is True, f"{check.name}: {check.evidence}"
