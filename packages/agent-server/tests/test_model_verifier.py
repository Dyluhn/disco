"""Model verifier bounded-context invocation."""

from __future__ import annotations

import json

import pytest
from disco.agent_server.verify.model_verifier import ModelVerifier, _SYSTEM_PROMPT
from disco.core.llm import (
    CompletionResponse,
    ModelRole,
    Requirement,
    TokenUsage,
)
from disco.core.loop import VerifierContextSeed, VerifierScreenshot
from disco.core.verify_medium import detect_html_medium


class _Router:
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests = []
        self.contexts = []

    async def complete(self, req, *, context=None):
        self.requests.append(req)
        self.contexts.append(context)
        return CompletionResponse(
            text=self._text,
            usage=TokenUsage(input_tokens=10, output_tokens=4),
            finish_reason="stop",
            model_used="verifier-fake",
        )


@pytest.mark.asyncio
async def test_model_verifier_uses_verifier_role_and_seed_only() -> None:
    router = _Router(
        json.dumps(
            {
                "verified": False,
                "verdict": "fail",
                "detail": "Hero copy does not match the brief.",
                "failures": [{"kind": "copy_quality", "message": "CTA is missing."}],
                "next_action": "Add the requested CTA.",
                "failure_fingerprint": "copy-cta",
            }
        )
    )
    seed = VerifierContextSeed(
        contract={"kind": "static.site"},
        deliverable_paths=["index.html"],
        check_results={"passed": True, "summary": "checks passed"},
        screenshot=VerifierScreenshot(
            path=".pmx/screenshots/0001.png",
            image_data_url="data:image/png;base64,abc123",
        ),
    )

    verdict = await ModelVerifier(router, conversation_id="conv").judge(seed)

    assert verdict.verified is False
    assert verdict.verdict == "fail"
    assert verdict.failures[0]["message"] == "CTA is missing."
    req = router.requests[0]
    assert req.profile.role is ModelRole.VERIFIER
    assert Requirement.JSON_MODE in req.profile.requirements
    assert req.response_format == "json"
    assert req.tools is None
    assert req.messages[0].role == "system"
    assert req.messages[1].role == "user"
    assert req.messages[1].images == ["data:image/png;base64,abc123"]

    payload = json.loads(req.messages[1].content)
    assert set(payload) == {"contract", "deliverable_paths", "check_results", "screenshot"}
    assert payload["contract"] == {"kind": "static.site"}
    assert payload["deliverable_paths"] == ["index.html"]
    assert payload["check_results"] == {"passed": True, "summary": "checks passed"}
    assert payload["screenshot"] == {
        "path": ".pmx/screenshots/0001.png",
        "image_attached": True,
        "image_data_url": "[attached as message image]",
    }


@pytest.mark.asyncio
async def test_model_verifier_deck_medium_prompt_variant() -> None:
    router = _Router(json.dumps({"verified": True, "verdict": "pass"}))
    medium = detect_html_medium(
        '<section class="slide" data-slide-id="slide-0" data-layout="title"></section>'
    )
    seed = VerifierContextSeed(
        contract={"kind": "static.site"},
        deliverable_paths=["deck.html"],
        check_results={"passed": True},
        medium=medium,
    )

    await ModelVerifier(router).judge(seed)

    req = router.requests[0]
    assert "slide deck" in req.messages[0].content
    assert "Bottom whitespace is correct" in req.messages[0].content
    assert "24px" in req.messages[0].content
    payload = json.loads(req.messages[1].content)
    assert payload["medium"]["kind"] == "deck"


@pytest.mark.asyncio
async def test_model_verifier_mobile_medium_prompt_variant() -> None:
    router = _Router(json.dumps({"verified": True, "verdict": "pass"}))
    medium = detect_html_medium(
        """
        <meta name="viewport" content="width=device-width,maximum-scale=1">
        <link rel="manifest" href="manifest.json">
        """,
        manifest_present=True,
    )
    seed = VerifierContextSeed(
        contract={"kind": "interactive.prototype"},
        deliverable_paths=["index.html", "manifest.json"],
        check_results={"passed": True},
        medium=medium,
    )

    await ModelVerifier(router).judge(seed)

    req = router.requests[0]
    assert "390x844" in req.messages[0].content
    assert "44px" in req.messages[0].content
    assert ":active" in req.messages[0].content
    payload = json.loads(req.messages[1].content)
    assert payload["medium"]["kind"] == "mobile"
    assert payload["medium"]["viewport_width"] == 390


@pytest.mark.asyncio
async def test_model_verifier_plain_web_prompt_unchanged() -> None:
    router = _Router(json.dumps({"verified": True, "verdict": "pass"}))
    seed = VerifierContextSeed(
        contract={"kind": "static.site"},
        deliverable_paths=["index.html"],
        check_results={"passed": True},
    )

    await ModelVerifier(router).judge(seed)

    req = router.requests[0]
    assert req.messages[0].content == _SYSTEM_PROMPT
    payload = json.loads(req.messages[1].content)
    assert "medium" not in payload
