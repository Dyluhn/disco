"""`reference_inspect`: only references/ paths; images through the visual observer
(dedicated answer, main-mode pixels attached, or truthfully asset-only); PDFs from
the text companion, optionally one page."""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from disco.tools.secrets import CapabilityBroker
from tool_fakes import FakeSandboxInstance, call

PDF_TEXT = "--- page 1 ---\nIntro text\n\n--- page 2 ---\nTerms and pricing"


def _executor(visual=None):
    sandbox = FakeSandboxInstance()
    sandbox._fs["references/brand/logo.png"] = b"\x89PNGpixels"
    sandbox._fs["references/brand/deck.pdf"] = b"%PDF"
    sandbox._fs["references/brand/deck.pdf.txt"] = PDF_TEXT.encode()
    sandbox._fs["references/brand/scan.pdf"] = b"%PDF"
    sandbox._fs["uploads/other.png"] = b"\x89PNG"
    broker = CapabilityBroker()
    if visual is not None:
        broker.register("visual_inspection", visual)
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=sandbox,
        broker=broker,
    )


async def test_only_reference_pack_paths():
    ex = _executor()
    out = await ex.execute(call("reference_inspect", path="uploads/other.png", question="what?"))
    assert not out.success and out.error and out.error.startswith("invalid_path")
    out = await ex.execute(call("reference_inspect", path="references/../x.png", question="what?"))
    assert not out.success


async def test_image_dedicated_answer_and_main_mode_pixels():
    async def dedicated(*, question, screenshot_b64, screenshot_path=""):
        return {
            "status": "answered",
            "mode": "dedicated",
            "answer": "A blue wordmark",
            "model": "v",
        }

    out = await _executor(dedicated).execute(
        call("reference_inspect", path="references/brand/logo.png", question="What is it?")
    )
    assert out.success and "A blue wordmark" in out.content

    async def main_mode(*, question, screenshot_b64, screenshot_path=""):
        return {"status": "pixels_attached", "mode": "main", "model": "driver"}

    out = await _executor(main_mode).execute(
        call("reference_inspect", path="references/brand/logo.png", question="What is it?")
    )
    assert out.success and out.structured is not None
    assert out.structured["visual_delivery"] == "main" and out.structured["screenshot_b64"]


async def test_image_without_a_vision_route_is_asset_only():
    out = await _executor(None).execute(
        call("reference_inspect", path="references/brand/logo.png", question="What is it?")
    )
    assert out.success and "asset only" in out.content
    assert out.structured == {
        "path": "references/brand/logo.png",
        "status": "unavailable",
        "reason": "no_vision_route",
    }


async def test_pdf_pages_and_scanned_pdf():
    ex = _executor()
    out = await ex.execute(
        call("reference_inspect", path="references/brand/deck.pdf", question="what does it say?")
    )
    assert out.success and "Intro text" in out.content and "Terms and pricing" in out.content
    out = await ex.execute(
        call(
            "reference_inspect",
            path="references/brand/deck.pdf",
            question="what does it say?",
            page=2,
        )
    )
    assert out.success and "Terms and pricing" in out.content and "Intro text" not in out.content
    out = await ex.execute(
        call(
            "reference_inspect",
            path="references/brand/deck.pdf",
            question="what does it say?",
            page=9,
        )
    )
    assert not out.success and out.error and out.error.startswith("page_not_found")
    out = await ex.execute(
        call("reference_inspect", path="references/brand/scan.pdf", question="what does it say?")
    )
    assert out.success and "asset only" in out.content
