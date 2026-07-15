"""H195: report-audio provider attempts use the sanitized shared ledger."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import report_audio
from disco.core.llm import ConfigStore
from disco.tools.builtin import audio_overview


class _Response:
    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self._content = content
        self._finish_reason = finish_reason

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {"content": self._content},
                    "finish_reason": self._finish_reason,
                }
            ]
        }


Reply = Callable[[dict[str, Any], int], _Response]


class _HTTPFactory:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.posts: list[dict[str, Any]] = []

    def __call__(self, **_kwargs: Any) -> _HTTPClient:
        return _HTTPClient(self)


class _HTTPClient:
    def __init__(self, factory: _HTTPFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _HTTPClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> _Response:
        self._factory.posts.append({"url": url, "json": json, "headers": headers})
        return self._factory.reply(json, len(self._factory.posts))


def _valid_batch(payload: dict[str, Any], *, total: int = 12) -> _Response:
    content = "\n".join(str(message.get("content", "")) for message in payload["messages"])
    match = re.search(r"contiguous turn indexes (\d+) through (\d+)", content)
    assert match is not None
    start, end = (int(value) for value in match.groups())
    turns = [
        {
            "index": index,
            "speaker": "A" if index % 2 else "B",
            "text": f"Ledger test turn {index}.",
        }
        for index in range(start, end + 1)
    ]
    return _Response(json.dumps({"total_turns": total, "turns": turns}))


def _configure_real_adapter(monkeypatch: pytest.MonkeyPatch, factory: _HTTPFactory) -> None:
    monkeypatch.setattr(
        report_audio,
        "_resolve_report_llm",
        lambda: (
            "https://provider.invalid/private/provider/path",
            "audio-model",
            "provider.secret_ref",
            "model:test-provider",
        ),
    )
    monkeypatch.setattr(ConfigStore, "origin_approved", lambda *_args: True)

    from disco.core.llm import secret_refs

    monkeypatch.setattr(secret_refs, "secret_ref_allowed_for_origin", lambda *_args: True)
    monkeypatch.setattr(secret_refs, "resolve_provider_secret", lambda *_args: "API_KEY_SECRET")
    monkeypatch.setattr(report_audio.httpx, "AsyncClient", factory)


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _assert_audio_records(
    path: Path,
    *,
    expected_attempts: int,
    conversation_id: str,
    mode: str = "podcast",
) -> None:
    records = _read_records(path)
    assert len(records) == expected_attempts
    for record in records:
        assert set(record) == {
            "ts",
            "host",
            "model",
            "has_tools",
            "conversation_id",
            "purpose",
            "call_kind",
        }
        assert record["host"] == "provider.invalid"
        assert record["model"] == "audio-model"
        assert record["has_tools"] is False
        assert record["conversation_id"] == conversation_id
        assert record["purpose"] == f"report_audio.{mode}"
        assert record["call_kind"] == "chat_completion"
    retained = path.read_text()
    for forbidden in (
        "API_KEY_SECRET",
        "AUDIO_PROMPT_SECRET",
        "private/provider/path",
        "provider.secret_ref",
        "Authorization",
        "messages",
    ):
        assert forbidden not in retained


@pytest.mark.asyncio
async def test_audio_ledger_records_one_attempt_per_normal_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    factory = _HTTPFactory(lambda payload, _attempt: _valid_batch(payload))
    _configure_real_adapter(monkeypatch, factory)

    turns = await report_audio._generate_turn_script(
        "AUDIO_PROMPT_SECRET",
        "podcast",
        conversation_id="conv_audio_normal",
    )

    assert len(turns) == 12
    assert len(factory.posts) == 3
    _assert_audio_records(
        path,
        expected_attempts=len(factory.posts),
        conversation_id="conv_audio_normal",
    )


@pytest.mark.asyncio
async def test_audio_ledger_records_length_recovery_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))

    def reply(payload: dict[str, Any], attempt: int) -> _Response:
        if attempt == 1:
            return _Response("{", finish_reason="length")
        return _valid_batch(payload)

    factory = _HTTPFactory(reply)
    _configure_real_adapter(monkeypatch, factory)

    turns = await report_audio._generate_turn_script(
        "AUDIO_PROMPT_SECRET",
        "podcast",
        conversation_id="conv_audio_length",
    )

    assert len(turns) == 12
    assert len(factory.posts) == 7
    _assert_audio_records(
        path,
        expected_attempts=len(factory.posts),
        conversation_id="conv_audio_length",
    )


@pytest.mark.asyncio
async def test_audio_ledger_records_malformed_correction_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    factory = _HTTPFactory(
        lambda payload, attempt: (
            _Response("ordinary malformed output") if attempt == 1 else _valid_batch(payload)
        )
    )
    _configure_real_adapter(monkeypatch, factory)

    turns = await report_audio._generate_turn_script(
        "AUDIO_PROMPT_SECRET",
        "podcast",
        conversation_id="conv_audio_malformed",
    )

    assert len(turns) == 12
    assert len(factory.posts) == 4
    _assert_audio_records(
        path,
        expected_attempts=len(factory.posts),
        conversation_id="conv_audio_malformed",
    )


@pytest.mark.asyncio
async def test_audio_ledger_records_terminal_provider_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))
    factory = _HTTPFactory(lambda _payload, _attempt: _Response("", "content_filter"))
    _configure_real_adapter(monkeypatch, factory)

    with pytest.raises(report_audio.TurnScriptError, match="finish_reason='content_filter'"):
        await report_audio._generate_turn_script(
            "AUDIO_PROMPT_SECRET",
            "podcast",
            conversation_id="conv_audio_terminal",
        )

    assert len(factory.posts) == 1
    _assert_audio_records(
        path,
        expected_attempts=len(factory.posts),
        conversation_id="conv_audio_terminal",
    )


@pytest.mark.asyncio
async def test_fake_llm_short_circuit_never_emits_real_provider_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.jsonl"
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(path))

    async def _fake_llm(_payload: dict[str, Any], _url: str) -> str:
        return "fake result"

    monkeypatch.setattr(audio_overview, "_call_llm", _fake_llm)
    monkeypatch.setattr(
        report_audio.httpx,
        "AsyncClient",
        lambda **_kwargs: pytest.fail("fake LLM path attempted real HTTP"),
    )

    result = await report_audio._authenticated_call_llm(
        {"messages": [{"role": "user", "content": "AUDIO_PROMPT_SECRET"}]},
        "https://provider.invalid/v1",
        api_key_env=None,
        purpose="model:test-provider",
        conversation_id="conv_audio_fake",
        model="audio-model",
        ledger_purpose="report_audio.podcast",
    )

    assert result == "fake result"
    assert not path.exists()


@pytest.mark.asyncio
async def test_audio_ledger_unset_and_write_failure_are_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _HTTPFactory(lambda _payload, _attempt: _Response("ok"))
    monkeypatch.setattr(ConfigStore, "origin_approved", lambda *_args: True)
    monkeypatch.setattr(report_audio.httpx, "AsyncClient", factory)
    monkeypatch.delenv("DISCO_PROVIDER_LEDGER", raising=False)

    async def call() -> None:
        response = await report_audio._authenticated_call_llm(
            {"messages": []},
            "https://provider.invalid/v1",
            api_key_env=None,
            purpose="model:test-provider",
            conversation_id="conv_audio_best_effort",
            model="audio-model",
            ledger_purpose="report_audio.podcast",
        )
        assert isinstance(response, audio_overview.LLMResponse)

    await call()
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(tmp_path))
    await call()
    assert len(factory.posts) == 2
