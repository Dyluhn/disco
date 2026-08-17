"""Route-specific evidence for bounded visual-observer provider calls."""

from __future__ import annotations

import json
from pathlib import Path

from harness.reliability._runner.provider_evidence import _provider_evidence_result
from harness.reliability.state import FAIL, INVALID, PASS


def _write_ledger(path: Path, records: list[dict[str, object]]) -> None:
    complete = [
        {"ts": 1_700_000_000.0 + index, "has_tools": True, **record}
        for index, record in enumerate(records)
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in complete), encoding="utf-8")
    path.chmod(0o600)


def _visual_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "host": "ollama.com",
        "model": "minimax-m3",
        "conversation_id": "conv_1",
        "has_tools": False,
        "purpose": "visual_inspection",
        "call_kind": "observer",
        "request_id": None,
        "driver_context_window": None,
        "model_repair_attempt": 1,
        "stream": False,
        "max_output_tokens": 900,
        "canonical_payload_bytes": 500,
        "messages_json_bytes": 400,
        "tools_json_bytes": 0,
        "message_count": 2,
        "tool_count": 0,
        "system_message_count": 1,
        "user_message_count": 1,
        "assistant_message_count": 0,
        "tool_message_count": 0,
        "image_count": 1,
        "image_url_chars": 100,
    }
    record.update(overrides)
    return record


def _main_record(**overrides: object) -> dict[str, object]:
    record = _visual_record(
        model="deepseek-v4-flash",
        has_tools=True,
        stream=True,
        tools_json_bytes=50,
        tool_count=1,
        image_count=0,
        image_url_chars=0,
    )
    record.pop("purpose")
    record.pop("call_kind")
    record.update(overrides)
    return record


def _adjudicate(path: Path, *, dedicated: bool = True) -> tuple[str, int, str]:
    return _provider_evidence_result(
        path,
        expected_host="ollama.com",
        expected_model="deepseek-v4-flash",
        expected_vision_host="ollama.com" if dedicated else "",
        expected_vision_model="minimax-m3" if dedicated else "",
        units=1,
    )


def test_accepts_exact_tagged_visual_observer_route(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_ledger(path, [_main_record(), _visual_record()])

    status, units, reason = _adjudicate(path)

    assert (status, units) == (PASS, 1)
    assert "1 auxiliary" in reason


def test_rejects_wrong_tagged_visual_model(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_ledger(path, [_visual_record(model="unexpected-model")])

    status, units, reason = _adjudicate(path)

    assert (status, units) == (FAIL, 0)
    assert "model 'unexpected-model' != 'minimax-m3'" in reason


def test_rejects_untagged_main_model_image_call(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_ledger(
        path,
        [
            _main_record(
                has_tools=False,
                stream=False,
                tools_json_bytes=0,
                tool_count=0,
                image_count=1,
                image_url_chars=100,
            )
        ],
    )

    status, units, reason = _adjudicate(path)

    assert (status, units) == (INVALID, 0)
    assert "does not identify its image-bearing route" in reason


def test_tagged_visual_observer_uses_main_policy_without_override(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_ledger(path, [_visual_record(model="deepseek-v4-flash")])

    assert _adjudicate(path, dedicated=False)[:2] == (PASS, 1)


def test_rejects_unbounded_visual_observer(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_ledger(path, [_visual_record(has_tools=True, tool_count=1)])

    status, units, reason = _adjudicate(path)

    assert (status, units) == (INVALID, 0)
    assert "not a bounded visual observer" in reason
