"""Bounded browser visual-inspection capability."""

from __future__ import annotations

from disco.agent_server.vision_inspection import VisionInspectionCapability
from disco.core.llm import CompletionResponse, ModelRole, Requirement, TokenUsage

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Router:
    def __init__(self, route, *, text: str = "The header is visible.", error=None) -> None:
        self.route = route
        self.text = text
        self.error = error
        self.requests = []
        self.contexts = []

    def visual_inspection_route(self):
        return self.route

    async def complete(self, request, *, context=None):
        self.requests.append(request)
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return CompletionResponse(
            text=self.text,
            usage=TokenUsage(input_tokens=10, output_tokens=8),
            finish_reason="stop",
            model_used="visual-model",
        )


async def test_main_vision_route_attaches_pixels_without_a_secondary_call():
    router = _Router(("main", "main-model", "main_model_has_vision"))
    result = await VisionInspectionCapability(router, conversation_id="conv").inspect(
        question="Is the header visible?",
        screenshot_b64=_PNG_B64,
        screenshot_path=".pmx/screenshots/shot.png",
    )

    assert result["status"] == "pixels_attached"
    assert result["mode"] == "main"
    assert result["screenshot_sha256"]
    assert router.requests == []


async def test_dedicated_vision_route_gets_one_image_question_and_no_agent_authority():
    router = _Router(("dedicated", "visual-model", "configured_vision_model"))
    result = await VisionInspectionCapability(router, conversation_id="conv").inspect(
        question="Is the header visible?",
        screenshot_b64=_PNG_B64,
        screenshot_path=".pmx/screenshots/shot.png",
    )

    assert result["status"] == "answered"
    assert result["answer"] == "The header is visible."
    assert result["authority"] == "advisory"
    assert len(router.requests) == 1
    request = router.requests[0]
    assert request.profile.role is ModelRole.AGENT_DRIVER
    assert request.profile.requirements == frozenset({Requirement.VISION})
    assert request.tools is None
    assert len(request.messages) == 2
    assert request.messages[1].images == [f"data:image/png;base64,{_PNG_B64}"]
    assert "entire user payload" in request.messages[0].content
    assert request.messages[1].content == (
        '<untrusted-visual-question-json>\n{"visual_question": "Is the header visible?"}'
        "\n</untrusted-visual-question-json>"
    )
    assert router.contexts[0].conversation_id == "conv"
    assert router.contexts[0].model_override == "visual-model"


async def test_unavailable_route_and_provider_errors_never_fabricate_an_answer():
    unavailable = _Router(("unavailable", "text-model", "vision_model_text_only"))
    first = await VisionInspectionCapability(unavailable, conversation_id="conv").inspect(
        question="Is the header visible?",
        screenshot_b64=_PNG_B64,
    )
    assert first["status"] == "unavailable"
    assert first["answer"] == ""
    assert unavailable.requests == []

    failing = _Router(
        ("dedicated", "visual-model", "configured_vision_model"),
        error=RuntimeError("secret provider response"),
    )
    second = await VisionInspectionCapability(failing, conversation_id="conv").inspect(
        question="Is the header visible?",
        screenshot_b64=_PNG_B64,
    )
    assert second["status"] == "error"
    assert second["reason"] == "vision_provider_RuntimeError"
    assert "secret provider response" not in str(second)
