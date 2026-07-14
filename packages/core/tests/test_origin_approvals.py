"""Regression proofs for the signed origin-approval ledger."""

from __future__ import annotations

import logging

from disco.core.origin_approvals import OriginApprovalStore


def test_nonempty_ledger_with_wrong_secret_fails_closed_and_warns_once(
    tmp_path, monkeypatch, caplog
) -> None:
    path = tmp_path / "approved-origins.json"
    monkeypatch.setenv("DISCO_SECRET_KEY", "original-high-entropy-test-secret")
    store = OriginApprovalStore(path)
    store.approve("https://api.example.test/v1", "model:test", "provider_ref")
    assert store.is_approved("https://api.example.test/other", "model:test", "provider_ref")

    monkeypatch.setenv("DISCO_SECRET_KEY", "rotated-high-entropy-test-secret")
    caplog.set_level(logging.WARNING, logger="disco.core.origin_approvals")

    assert store.verified() == frozenset()
    assert store.verified() == frozenset()
    warnings = [
        record.message for record in caplog.records if "origin-approval ledger" in record.message
    ]
    assert len(warnings) == 1
    assert "HMAC does not verify" in warnings[0]
    assert "IGNORED" in warnings[0]


def test_empty_or_missing_ledger_is_quiet(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="disco.core.origin_approvals")
    assert OriginApprovalStore(tmp_path / "missing.json").verified() == frozenset()
    assert not [record for record in caplog.records if "origin-approval ledger" in record.message]


def test_replace_and_revoke_purpose_remove_stale_authorization(tmp_path, monkeypatch) -> None:
    path = tmp_path / "approved-origins.json"
    monkeypatch.setenv("DISCO_SECRET_KEY", "purpose-scoped-high-entropy-test-secret")
    store = OriginApprovalStore(path)
    store.approve("https://models.example.test/v1", "model:stable", "model_key")
    store.approve("https://old-mcp.example.test/mcp", "mcp:docs", "old_key")

    store.replace_purpose(
        "https://new-mcp.example.test/mcp",
        "MCP:Docs",
        ("new_key", "secondary_key"),
    )

    assert not store.is_approved("https://old-mcp.example.test/other", "mcp:docs", "old_key")
    assert store.is_approved("https://new-mcp.example.test/other", "mcp:docs", "new_key")
    assert store.is_approved("https://new-mcp.example.test/other", "mcp:docs", "secondary_key")
    assert store.is_approved("https://models.example.test/other", "model:stable", "model_key")

    assert store.revoke_purpose("mcp:docs") == 2
    assert store.revoke_purpose("mcp:docs") == 0
    assert not store.is_approved("https://new-mcp.example.test/other", "mcp:docs", "new_key")
    assert store.is_approved("https://models.example.test/other", "model:stable", "model_key")
