"""Settings Test must prove completion success, not infer auth from rejection."""

import json

import httpx
import pytest
from disco.app_server import probe_clients


async def run_probe(monkeypatch, response, *, api_key="test-secret", model="arbitrary-model"):
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return response

    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(probe_clients.httpx, "AsyncClient", client)
    result = await probe_clients.probe_openai_auth("https://generic.invalid/v1", api_key, model)
    assert len(requests) == 1  # no speculative compatibility retry
    request = requests[0]
    assert request.url.path == "/v1/chat/completions"
    assert json.loads(request.content)["model"] == model
    assert request.headers.get("authorization") == (f"Bearer {api_key}" if api_key else None)
    assert "test-secret" not in result[2]
    return result


@pytest.mark.parametrize("code", [400, 404, 405, 422, 429, 500, 503])
async def test_rejections_never_claim_valid_auth_or_success(monkeypatch, code):
    ok, status, detail = await run_probe(monkeypatch, httpx.Response(code))
    assert not ok and status == "error"
    assert str(code) in detail
    assert "key works" not in detail and "key authenticated" not in detail
    assert "authenticated the key" not in detail


@pytest.mark.parametrize("code", [401, 403])
async def test_access_denied_is_distinct_from_protocol_failure(monkeypatch, code):
    ok, status, _ = await run_probe(monkeypatch, httpx.Response(code))
    assert not ok and status == "unauthorized"


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"error": "bad key"},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": "bad"}]},
    ],
)
async def test_200_without_completion_is_not_green(monkeypatch, body):
    response = (
        httpx.Response(200, json=body)
        if body is not None
        else httpx.Response(200, text="<html>login</html>")
    )
    ok, status, _ = await run_probe(monkeypatch, response)
    assert not ok and status == "error"


@pytest.mark.parametrize("model", ["arbitrary-model", "gpt-5", "DeepSeek-v4.1", "Qwen3.8"])
@pytest.mark.parametrize("api_key", ["test-secret", None])
async def test_completion_success_is_name_neutral_and_not_auth_proof(monkeypatch, model, api_key):
    response = httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    ok, status, detail = await run_probe(monkeypatch, response, api_key=api_key, model=model)
    assert ok and status == "ok"
    assert model in detail
    assert "key works" not in detail and "authenticated" not in detail


async def test_tiny_probe_can_return_reasoning_only_truncation(monkeypatch):
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": None, "reasoning_content": "start"},
                    "finish_reason": "length",
                }
            ]
        },
    )
    ok, status, _ = await run_probe(monkeypatch, response)
    assert ok and status == "ok"  # transport test, not an answer-quality claim


def test_search_extraction_rate_limit_is_not_success():
    ok, status, detail = probe_clients._classify_vendor_probe(
        httpx.Response(429), "https://source.invalid", "a real query"
    )
    assert not ok and status == "error" and "429" in detail
    assert "authenticated" not in detail


@pytest.mark.parametrize("message", [{}, {"content": ""}, {"content": None}])
async def test_one_token_empty_completion_envelopes_remain_transport_success(monkeypatch, message):
    response = httpx.Response(
        200, json={"choices": [{"message": message, "finish_reason": "length"}]}
    )
    ok, status, _ = await run_probe(monkeypatch, response)
    assert ok and status == "ok"
