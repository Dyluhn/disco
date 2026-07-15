"""DISCO_PROVIDER_LEDGER — the direct-driver provider-request ledger (2026-07-09).

The build-soak harness adjudicates provider-after-terminal from a relay-format
JSONL ledger; direct drivers (opencode/openrouter — no relay in the path) had
nothing to audit, so the soak runner fail-closed REFUSED to run. The provider
now emits one record per outbound completion request when the env names a file.
These tests pin: fail-closed off by default, the exact record shape, and that
the harness parser windows the record (numeric ts + conversation scoping)."""

from __future__ import annotations

import json

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.provider_ledger import emit_provider_attempt
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
    ToolSpec,
)


def _req(cid: str | None = "conv_ledger") -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hi")],
        tools=[ToolSpec(name="t", description="d", parameters_schema={"type": "object"})],
        metadata={"conversation_id": cid} if cid else {},
    )


def _provider() -> OpenAIProvider:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {},
            },
        )
    )
    return OpenAIProvider(
        name="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="k",
        transport=transport,
    )


@pytest.mark.asyncio
async def test_no_env_no_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DISCO_PROVIDER_LEDGER", raising=False)
    await _provider().complete(_req(), model="m")
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere in tmp


@pytest.mark.asyncio
async def test_ledger_record_shape_and_harness_roundtrip(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    await _provider().complete(_req("conv_ledger"), model="deepseek-v4-flash")
    rec = json.loads(path.read_text().strip())
    assert set(rec) == {"ts", "host", "model", "has_tools", "conversation_id"}
    assert isinstance(rec["ts"], float)
    assert rec["host"] == "opencode.ai"
    assert rec["model"] == "deepseek-v4-flash"
    assert rec["has_tools"] is True
    assert rec["conversation_id"] == "conv_ledger"

    # The HARNESS parser must window this record: numeric ts survives parsing and
    # the record scopes to its conversation (parallel lanes can't cross-flag).
    import importlib.util
    import pathlib
    import sys

    spec = importlib.util.spec_from_file_location(
        "provider_ledger",
        pathlib.Path(__file__).resolve().parents[3] / "harness/build_soak/provider_ledger.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provider_ledger"] = mod
    spec.loader.exec_module(mod)
    parsed = mod.parse_relay_log(path.read_text())
    assert len(parsed) == 1
    assert isinstance(parsed[0]["ts"], float)
    assert mod.record_applies_to_conversation(parsed[0], "conv_ledger") is True
    assert mod.record_applies_to_conversation(parsed[0], "conv_other") is False


@pytest.mark.asyncio
async def test_ledger_failure_never_breaks_the_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(tmp_path))  # a DIRECTORY → open() fails
    resp = await _provider().complete(_req(), model="m")
    assert resp is not None  # request path unaffected by ledger failure


def test_shared_ledger_keeps_only_sanitized_bounded_metadata(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))

    emit_provider_attempt(
        base_url="https://user:URL_SECRET@provider.invalid/private?token=URL_TOKEN",
        model="model-id",
        has_tools=False,
        conversation_id="conv_audio",
        purpose="REPORT_AUDIO.Podcast",
        call_kind="CHAT_COMPLETION",
    )
    emit_provider_attempt(
        base_url="https://provider.invalid/v1",
        model="model-id",
        has_tools=False,
        conversation_id="conv_audio",
        purpose="prompt text must never be retained",
        call_kind="x" * 65,
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0] == {
        "ts": records[0]["ts"],
        "host": "provider.invalid",
        "model": "model-id",
        "has_tools": False,
        "conversation_id": "conv_audio",
        "purpose": "report_audio.podcast",
        "call_kind": "chat_completion",
    }
    assert set(records[1]) == {"ts", "host", "model", "has_tools", "conversation_id"}
    retained = path.read_text()
    for forbidden in ("user", "URL_SECRET", "private", "URL_TOKEN", "prompt text"):
        assert forbidden not in retained
