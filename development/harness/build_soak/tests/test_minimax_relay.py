"""P1B-LIVE-3a: the MiniMax relay's PURE request transform (model-map + max_tokens clamp +
host parse) — fastapi/httpx/secret-free, so it tests in isolation. The network proxy is the
live run (3b)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.product_build.minimax_relay import (
    MAX_TOKENS_CAP,
    conversation_id_from_headers,
    map_model,
    relay_log_record,
    transform_request,
    upstream_host,
)


def test_model_map_known_and_default() -> None:
    assert map_model("minimax-m3") == "MiniMax-M3"
    assert map_model("minimax/minimax-m3") == "MiniMax-M3"
    assert map_model("minimax-m2") == "MiniMax-M2"
    assert map_model("something-else") == "MiniMax-M3"  # unknown → default (M3)


def test_max_tokens_clamp_matrix() -> None:
    assert transform_request({})["max_tokens"] == MAX_TOKENS_CAP  # absent → cap
    for bad in (0, -5, True, 9_999_999, "8", 1.5):
        assert transform_request({"max_tokens": bad})["max_tokens"] == MAX_TOKENS_CAP, bad
    assert transform_request({"max_tokens": 2048})["max_tokens"] == 2048  # a valid int is kept


def test_no_mutation_and_passthrough() -> None:
    src = {
        "model": "minimax-m3",
        "max_tokens": 9_999_999,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "x"}],
        "stream": True,
    }
    out = transform_request(src)
    assert out["model"] == "MiniMax-M3" and out["max_tokens"] == MAX_TOKENS_CAP
    assert (
        out["messages"] == src["messages"]
        and out["tools"] == src["tools"]
        and out["stream"] is True
    )
    # the input dict is NOT mutated
    assert src["model"] == "minimax-m3" and src["max_tokens"] == 9_999_999


def test_non_dict_body_passes_through() -> None:
    assert transform_request(["not", "a", "dict"]) == ["not", "a", "dict"]


def test_upstream_host_for_provider_ledger() -> None:
    assert upstream_host("https://api.minimaxi.chat/v1/chat/completions") == "api.minimaxi.chat"
    rec = relay_log_record("https://api.minimaxi.chat/v1/chat/completions", "MiniMax-M3")
    assert (
        rec["host"] == "api.minimaxi.chat"
        and rec["model"] == "MiniMax-M3"
        and "minimax" in rec["host"]
    )
    assert rec["conversation_id"] is None


def test_relay_log_record_maps_conversation_header_field() -> None:
    conversation_id = conversation_id_from_headers({"X-Disco-Conversation": "conv_relay"})
    rec = relay_log_record(
        "https://api.minimaxi.chat/v1/chat/completions",
        "MiniMax-M3",
        conversation_id=conversation_id,
    )
    assert rec["conversation_id"] == "conv_relay"
    assert conversation_id_from_headers({}) is None


def test_create_app_fails_fast_without_key(monkeypatch) -> None:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    from harness.product_build.minimax_relay import create_app

    with pytest.raises(RuntimeError, match="MINIMAX_API_KEY"):
        create_app()


def test_relay_importable_without_disco_or_fastapi() -> None:
    # the relay must import dependency-light: only the repo root on PYTHONPATH (NOT packages/*/src),
    # so a successful import proves it pulls neither disco nor fastapi.
    root = str(Path(__file__).resolve().parents[4])
    code = (
        "import sys, harness.product_build.minimax_relay as m;"
        "assert 'disco' not in sys.modules, 'disco leaked';"
        "assert 'fastapi' not in sys.modules, 'fastapi leaked';"
        "print(m.map_model('minimax-m3'))"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": root}
    )
    assert r.returncode == 0 and "MiniMax-M3" in r.stdout, (r.stdout, r.stderr)
