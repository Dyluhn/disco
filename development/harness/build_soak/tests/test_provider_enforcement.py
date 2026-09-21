from __future__ import annotations

import pytest
from harness.build_soak.run import _require_exact_provider


def test_live_provider_requirement_injects_fail_closed_assertion() -> None:
    source = {"s": {"prompt": "build", "assertions": {"terminal_status_in": ["FINISHED"]}}}

    secured = _require_exact_provider(
        source, expected_host="opencode.ai", expected_model="deepseek-v4-flash"
    )

    provider = secured["s"]["assertions"]["provider"]
    assert provider == {
        "require_ledger": True,
        "require_host_substr": "opencode.ai",
        "model": "deepseek-v4-flash",
    }
    assert "provider" not in source["s"]["assertions"]


@pytest.mark.parametrize("host,model", [("", "deepseek-v4-flash"), ("opencode.ai", "")])
def test_live_provider_requirement_rejects_missing_expectation(host: str, model: str) -> None:
    with pytest.raises(ValueError, match="nonempty expected host and model"):
        _require_exact_provider({"s": {"assertions": {}}}, expected_host=host, expected_model=model)


def test_live_provider_requirement_rejects_conflicting_scenario() -> None:
    source = {
        "s": {
            "assertions": {
                "provider": {"require_host_substr": "fallback.example", "model": "other"}
            }
        }
    }

    with pytest.raises(ValueError, match="conflicts with required"):
        _require_exact_provider(
            source, expected_host="opencode.ai", expected_model="deepseek-v4-flash"
        )


def test_live_provider_requirement_injects_exact_visual_observer_route() -> None:
    secured = _require_exact_provider(
        {"s": {"assertions": {}}},
        expected_host="ollama.com",
        expected_model="deepseek-v4-flash",
        expected_vision_host="ollama.com",
        expected_vision_model="minimax-m3",
    )

    assert secured["s"]["assertions"]["provider"]["visual_observer"] == {
        "require_host_substr": "ollama.com",
        "model": "minimax-m3",
    }


@pytest.mark.parametrize("vision_host,vision_model", [("", "minimax-m3"), ("ollama.com", "")])
def test_live_provider_requirement_rejects_partial_visual_route(
    vision_host: str, vision_model: str
) -> None:
    with pytest.raises(ValueError, match="requires both expected vision host and model"):
        _require_exact_provider(
            {"s": {"assertions": {}}},
            expected_host="ollama.com",
            expected_model="deepseek-v4-flash",
            expected_vision_host=vision_host,
            expected_vision_model=vision_model,
        )
