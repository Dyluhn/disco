"""Provider-neutral regression: surrounding-space header values rejected early.

A static header value with leading/trailing spaces passes RequestPolicy
validation but ordinary HTTPX/h11 transport rejects it during serialization,
before the endpoint sees a request; the resulting transport failure then
surfaces as a retryable error that masks a static configuration mistake.
Validation must reject such values with the offending header named.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest
from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelRole
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_policy import RequestPolicy
from pydantic import ValidationError


def _request() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="Summarize this.")],
    )


@pytest.mark.parametrize(
    "value",
    [" leading", "trailing ", " "],
    ids=["leading-space", "trailing-space", "single-space"],
)
def test_surrounding_whitespace_header_value_rejected(value: str):
    with pytest.raises(ValidationError) as excinfo:
        RequestPolicy(headers={"X-Arbitrary-Transport": value})
    message = str(excinfo.value).lower()
    assert "x-arbitrary-transport" in message
    assert any(word in message for word in ("leading", "trailing", "surrounding"))


def test_empty_header_value_stays_valid():
    assert RequestPolicy(headers={"X-Arbitrary-Transport": ""}).headers == {
        "X-Arbitrary-Transport": ""
    }


def _loopback(responses: list, expected_name: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler seam
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            responses.append(self.headers.get(expected_name))
            body = json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


@pytest.mark.parametrize(
    "value",
    ["", "disco-wire/2.0", "value with internal space"],
    ids=["empty", "clean", "internal-space"],
)
async def test_loopback_carries_valid_values_over_real_transport(value: str):
    seen: list = []
    server = _loopback(seen, "X-Arbitrary-Transport")
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    worker.start()
    try:
        port = server.server_address[1]
        provider = OpenAIProvider(
            f"http://127.0.0.1:{port}/v1/responses",
            name="arbitrary-provider-7",
            timeout_s=10,
            request_policy=RequestPolicy(
                headers={"X-Arbitrary-Transport": value}
            ),
        )
        response = await provider.complete(_request(), model="arbitrary-model-7")
        assert response.text == "ok"
        assert seen == [value]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
