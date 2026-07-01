"""Unit tests for `disco verify` (disco.agent_server.verify).

We exercise the PASS/FAIL/SKIP decision logic of each capability check with a FAKE
router + config (no network), using the REAL CompletionResponse/ProposedToolCall
shapes the live router returns — so the assertions track the real contract. The live
grounding path (network + encoders) is covered by the manual `disco verify` run, not here.
"""

from __future__ import annotations

from types import SimpleNamespace

from disco.agent_server import verify
from disco.agent_server.verify import _setup_checks
from disco.core.llm.types import (
    CompletionResponse,
    ProposedToolCall,
    TokenUsage,
)


def _resp(*, text: str = "", tool_calls=None, model: str = "fake-model") -> CompletionResponse:
    return CompletionResponse(
        text=text,
        tool_calls=tool_calls or [],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
        finish_reason="tool_calls" if tool_calls else "stop",
        model_used=model,
    )


def _model(model_id: str = "m", base_url: str = "http://x/v1") -> SimpleNamespace:
    return SimpleNamespace(model_id=model_id, base_url=base_url)


def _rt(*, cfg=None, router=None) -> SimpleNamespace:
    """A stand-in ConversationRuntime exposing only what the checks touch."""
    return SimpleNamespace(
        _config_store=SimpleNamespace(load=lambda: cfg),
        _router_now=lambda *a, **k: router,
    )


def _cfg(*, assignments=None, default_model=None, models=None) -> SimpleNamespace:
    return SimpleNamespace(
        assignments=assignments or {},
        default_model=default_model,
        models=models or {},
    )


# ---- config ------------------------------------------------------------------


def test_config_pass_via_default_model():
    cfg = _cfg(default_model="d", models={"d": _model("Qwen-X", "http://lan/v1")})
    c = verify.check_config(_rt(cfg=cfg))
    assert c.status == "PASS"
    assert "Qwen-X" in c.detail and "http://lan/v1" in c.detail


def test_config_pass_via_explicit_assignment_over_default():
    # An explicit AGENT_DRIVER assignment is honored (string-keyed, as persisted).
    cfg = _cfg(
        assignments={"agent_driver": "a"},
        default_model="d",
        models={"a": _model("Assigned"), "d": _model("Default")},
    )
    c = verify.check_config(_rt(cfg=cfg))
    assert c.status == "PASS" and "Assigned" in c.detail


def test_config_fail_no_driver():
    c = verify.check_config(_rt(cfg=_cfg(default_model=None, assignments={})))
    assert c.status == "FAIL" and "no driver" in c.detail


def test_config_fail_driver_not_in_catalogue():
    c = verify.check_config(_rt(cfg=_cfg(default_model="ghost", models={})))
    assert c.status == "FAIL" and "ghost" in c.detail


# ---- completion --------------------------------------------------------------


async def test_completion_pass():
    class R:
        async def complete(self, req, **k):
            return _resp(text="ok")

    c = await verify.check_completion(_rt(router=R()))
    assert c.status == "PASS"


async def test_completion_fail_empty():
    class R:
        async def complete(self, req, **k):
            return _resp(text="   ")  # whitespace only, no tool calls

    c = await verify.check_completion(_rt(router=R()))
    assert c.status == "FAIL" and "empty" in c.detail


async def test_completion_fail_surfaces_verbatim_error():
    class R:
        async def complete(self, req, **k):
            raise RuntimeError("connection refused to 127.0.0.1:9999")

    c = await verify.check_completion(_rt(router=R()))
    assert c.status == "FAIL"
    assert "connection refused to 127.0.0.1:9999" in c.detail  # real error preserved


# ---- tool-calling ------------------------------------------------------------


async def test_tool_calling_pass():
    class R:
        async def complete(self, req, **k):
            return _resp(
                tool_calls=[ProposedToolCall(tool_name="get_weather", arguments={"city": "Paris"})]
            )

    c = await verify.check_tool_calling(_rt(router=R()))
    assert c.status == "PASS" and "get_weather" in c.detail


async def test_tool_calling_fail_prose_answer():
    class R:
        async def complete(self, req, **k):
            return _resp(text="It is sunny in Paris today.")

    c = await verify.check_tool_calling(_rt(router=R()))
    assert c.status == "FAIL" and "prose" in c.detail


async def test_tool_calling_fail_wrong_tool():
    class R:
        async def complete(self, req, **k):
            return _resp(tool_calls=[ProposedToolCall(tool_name="something_else", arguments={})])

    c = await verify.check_tool_calling(_rt(router=R()))
    assert c.status == "FAIL" and "something_else" in c.detail


# ---- orchestration / rendering ----------------------------------------------


async def test_run_checks_cascades_skip_when_config_fails(monkeypatch):
    # No driver → config FAIL → the live checks must SKIP, never crash on a missing model.
    monkeypatch.setattr(_setup_checks, "_build_runtime", lambda: _rt(cfg=_cfg()))
    results = await verify.run_checks(quick=True, network=False)
    by = {c.name: c.status for c in results}
    assert by["config"] == "FAIL"
    assert by["completion"] == "SKIP"
    assert by["tool-calling"] == "SKIP"
    assert by["grounding"] == "SKIP"


def test_render_exit_code_nonzero_on_failure():
    bad = [verify.Check("config", "FAIL", "x")]
    assert verify._render(bad) == 1


def test_render_exit_code_zero_when_no_failure():
    ok = [
        verify.Check("config", "PASS", "x"),
        verify.Check("grounding", "SKIP", "y"),
    ]
    assert verify._render(ok) == 0
