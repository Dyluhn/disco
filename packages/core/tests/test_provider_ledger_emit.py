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
    metadata: dict[str, object] = {"driver_context_window": 65536}
    if cid:
        metadata["conversation_id"] = cid
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hi")],
        tools=[ToolSpec(name="t", description="d", parameters_schema={"type": "object"})],
        metadata=metadata,
        request_id="req_0123456789abcdef0123456789abcdef",
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
async def test_no_env_does_not_compute_request_shape(tmp_path, monkeypatch) -> None:
    import disco.core.llm.openai_provider as provider_module

    monkeypatch.delenv("DISCO_PROVIDER_LEDGER", raising=False)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("request accounting ran while the ledger was disabled")

    monkeypatch.setattr(provider_module, "_provider_request_shape", must_not_run)
    await _provider().complete(_req(), model="m")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_ledger_record_shape_and_harness_roundtrip(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    await _provider().complete(_req("conv_ledger"), model="deepseek-v4-flash")
    rec = json.loads(path.read_text().strip())
    assert set(rec) == {
        "ts",
        "host",
        "model",
        "has_tools",
        "conversation_id",
        "request_id",
        "driver_context_window",
        "model_repair_attempt",
        "stream",
        "max_output_tokens",
        "canonical_payload_bytes",
        "messages_json_bytes",
        "tools_json_bytes",
        "message_count",
        "tool_count",
        "system_message_count",
        "user_message_count",
        "assistant_message_count",
        "tool_message_count",
        "image_count",
        "image_url_chars",
    }
    assert isinstance(rec["ts"], float)
    assert rec["host"] == "opencode.ai"
    assert rec["model"] == "deepseek-v4-flash"
    assert rec["has_tools"] is True
    assert rec["conversation_id"] == "conv_ledger"
    assert rec["request_id"] == "req_0123456789abcdef0123456789abcdef"
    assert rec["driver_context_window"] == 65536
    assert rec["model_repair_attempt"] == 1
    assert rec["stream"] is False
    assert rec["max_output_tokens"] is None
    assert rec["canonical_payload_bytes"] > rec["messages_json_bytes"] > 0
    assert rec["tools_json_bytes"] > 0
    assert rec["message_count"] == rec["user_message_count"] == 1
    assert rec["tool_count"] == 1
    assert rec["system_message_count"] == 0
    assert rec["assistant_message_count"] == rec["tool_message_count"] == 0
    assert rec["image_count"] == rec["image_url_chars"] == 0

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
async def test_ledger_keeps_only_bounded_machine_route_labels(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    request = _req("conv_visual")
    assert request.metadata is not None
    request.metadata.update(
        {
            "provider_ledger_purpose": "visual_inspection",
            "provider_ledger_call_kind": "observer",
            "untrusted_note": "must never enter retained evidence",
        }
    )

    await _provider().complete(request, model="minimax-m3")

    record = json.loads(path.read_text())
    assert record["purpose"] == "visual_inspection"
    assert record["call_kind"] == "observer"
    assert "untrusted_note" not in record
    assert "must never enter retained evidence" not in path.read_text()


@pytest.mark.asyncio
async def test_ledger_failure_never_breaks_the_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(tmp_path))  # a DIRECTORY → open() fails
    resp = await _provider().complete(_req(), model="m")
    assert resp is not None  # request path unaffected by ledger failure


@pytest.mark.asyncio
async def test_shape_failure_keeps_provider_traffic_and_base_attempt_evidence(
    tmp_path, monkeypatch
) -> None:
    import disco.core.llm.openai_provider as provider_module

    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic accounting failure")

    monkeypatch.setattr(provider_module, "_provider_request_shape", explode)
    resp = await _provider().complete(_req(), model="m")

    assert resp.text == "ok"
    rec = json.loads(path.read_text())
    assert set(rec) == {"ts", "host", "model", "has_tools", "conversation_id"}


@pytest.mark.asyncio
async def test_request_shape_is_exact_utf8_and_never_retains_payload_data(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1},
            },
        )

    provider = OpenAIProvider(
        name="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="HEADER_SECRET_SENTINEL",
        transport=httpx.MockTransport(handler),
    )
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[
            LLMMessage(
                role="user",
                content="héllø PROMPT_SECRET_SENTINEL",
                images=["data:image/png;base64,IMAGE_SECRET_SENTINEL"],
            )
        ],
        tools=[
            ToolSpec(
                name="TOOL_NAME_SECRET_SENTINEL",
                description="TOOL_DESCRIPTION_SECRET_SENTINEL",
                parameters_schema={
                    "type": "object",
                    "properties": {"SCHEMA_SECRET_SENTINEL": {"type": "string"}},
                },
            )
        ],
        metadata={"conversation_id": "conv_private", "driver_context_window": 32768},
        request_id="req_abcdef0123456789abcdef0123456789",
        max_tokens=321,
    )

    await provider.complete(req, model="deepseek-v4-flash")

    assert len(seen_payloads) == 1
    payload = seen_payloads[0]
    canonical = lambda value: len(  # noqa: E731 - compact independent oracle
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    rec = json.loads(path.read_text())
    assert rec["canonical_payload_bytes"] == canonical(payload)
    assert rec["messages_json_bytes"] == canonical(payload["messages"])
    assert rec["tools_json_bytes"] == canonical(payload["tools"])
    assert rec["image_count"] == 1
    assert rec["image_url_chars"] == len("data:image/png;base64,IMAGE_SECRET_SENTINEL")
    assert rec["max_output_tokens"] == 321
    # UTF-8 accounting is bytes, not Python character count.
    assert rec["messages_json_bytes"] > len(
        json.dumps(payload["messages"], ensure_ascii=False, separators=(",", ":"))
    )
    retained = path.read_text()
    for forbidden in (
        "HEADER_SECRET_SENTINEL",
        "PROMPT_SECRET_SENTINEL",
        "IMAGE_SECRET_SENTINEL",
        "TOOL_NAME_SECRET_SENTINEL",
        "TOOL_DESCRIPTION_SECRET_SENTINEL",
        "SCHEMA_SECRET_SENTINEL",
    ):
        assert forbidden not in retained


@pytest.mark.asyncio
async def test_invalid_machine_identity_and_bool_context_are_not_retained(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    req = _req().model_copy(
        update={
            "request_id": "USER_CONTROLLED_SECRET_ID",
            "metadata": {
                "conversation_id": "conv_ledger",
                "driver_context_window": True,
            },
        }
    )

    await _provider().complete(req, model="m")

    rec = json.loads(path.read_text())
    assert rec["request_id"] is None
    assert rec["driver_context_window"] is None
    assert "USER_CONTROLLED_SECRET_ID" not in path.read_text()


@pytest.mark.asyncio
async def test_payload_is_built_once_and_each_stream_mode_emits_one_attempt(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["stream"]:
            body = (
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    class CountingProvider(OpenAIProvider):
        payload_calls = 0

        def _payload(self, req, model, *, stream):  # noqa: ANN001
            self.payload_calls += 1
            return super()._payload(req, model, stream=stream)

    provider = CountingProvider(
        name="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    await provider.complete(_req(), model="m")
    async for _ in provider.stream_complete(_req(), model="m"):
        pass

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert provider.payload_calls == 2
    assert len(records) == 2
    assert [record["stream"] for record in records] == [False, True]


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
