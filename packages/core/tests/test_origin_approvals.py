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
    assert store.is_approved(
        "https://api.example.test/other", "model:test", "provider_ref"
    )

    monkeypatch.setenv("DISCO_SECRET_KEY", "rotated-high-entropy-test-secret")
    caplog.set_level(logging.WARNING, logger="disco.core.origin_approvals")

    assert store.verified() == frozenset()
    assert store.verified() == frozenset()
    warnings = [
        record.message
        for record in caplog.records
        if "origin-approval ledger" in record.message
    ]
    assert len(warnings) == 1
    assert "HMAC does not verify" in warnings[0]
    assert "IGNORED" in warnings[0]


def test_empty_or_missing_ledger_is_quiet(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="disco.core.origin_approvals")
    assert OriginApprovalStore(tmp_path / "missing.json").verified() == frozenset()
    assert not [
        record for record in caplog.records if "origin-approval ledger" in record.message
    ]
