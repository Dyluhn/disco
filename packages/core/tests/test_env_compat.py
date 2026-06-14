"""Tests for the DISCO_/PMX_ env compat helper (disco.core.env)."""

from __future__ import annotations

import disco.core.env as env_mod
from disco.core.env import disco_env, disco_env_set


def test_prefers_disco_over_pmx(monkeypatch):
    monkeypatch.setenv("DISCO_FOO", "new")
    monkeypatch.setenv("PMX_FOO", "old")
    assert disco_env("FOO") == "new"


def test_falls_back_to_pmx(monkeypatch):
    monkeypatch.delenv("DISCO_FOO", raising=False)
    monkeypatch.setenv("PMX_FOO", "legacy")
    assert disco_env("FOO") == "legacy"


def test_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("DISCO_BAR", raising=False)
    monkeypatch.delenv("PMX_BAR", raising=False)
    assert disco_env("BAR", "fallback") == "fallback"
    assert disco_env("BAR") is None


def test_disco_only(monkeypatch):
    monkeypatch.setenv("DISCO_ONLY", "v")
    monkeypatch.delenv("PMX_ONLY", raising=False)
    assert disco_env("ONLY") == "v"


def test_empty_string_is_honored_not_treated_as_unset(monkeypatch):
    # An explicit empty value must win over the default (distinct from unset).
    monkeypatch.setenv("DISCO_EMPTY", "")
    assert disco_env("EMPTY", "default") == ""


def test_pmx_empty_string_honored(monkeypatch):
    monkeypatch.delenv("DISCO_PE", raising=False)
    monkeypatch.setenv("PMX_PE", "")
    assert disco_env("PE", "default") == ""


def test_deprecation_logged_once(monkeypatch, caplog):
    env_mod._DEPRECATION_LOGGED.discard("DEPR")
    monkeypatch.delenv("DISCO_DEPR", raising=False)
    monkeypatch.setenv("PMX_DEPR", "x")
    import logging

    with caplog.at_level(logging.WARNING, logger="disco.env"):
        disco_env("DEPR")
        disco_env("DEPR")
    warnings = [r for r in caplog.records if "PMX_DEPR" in r.getMessage()]
    assert len(warnings) == 1  # one-time only


def test_disco_env_set(monkeypatch):
    monkeypatch.delenv("DISCO_S", raising=False)
    monkeypatch.delenv("PMX_S", raising=False)
    assert disco_env_set("S") is False
    monkeypatch.setenv("PMX_S", "")
    assert disco_env_set("S") is True  # presence, not truthiness
