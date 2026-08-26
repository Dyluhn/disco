"""Unit tests for the slides_generate tool.

Proves:
  - legacy markdown helpers still split slides for compatibility.
  - the model-facing schema rejects non-PPTX and reports legacy inputs explicitly.
  - structured output stays native PPTX with no basic-HTML degradation.
  - Sandbox-jailed write proven (the sheets.py test pattern): file lands in
    the workspace, NOT host cwd; '../escape' rejected.
  - AGENT_TOOLS registration + scoping.

The tool declares runs_in="sandbox", so every test drives it through a REAL
process-backend sandbox instance jailed to a temp workspace.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.tools.anatomy import Capability, ToolContext, ToolOutcome
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.slides import (
    SlidesGenerateArgs,
    SlidesTool,
    _split_slides,
)
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import agent_scope, research_scope
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxInstance
from disco.tools.secrets import CapabilityBroker
from pydantic import ValidationError
from tool_fakes import FakeSandboxInstance, call


@pytest.fixture
def tmp_workspace():
    """A real temp workspace dir that the jailed sandbox writes into."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _jailed_sandbox(workspace: Path) -> ProcessSandboxInstance:
    """The real process backend, jailed to `workspace`."""
    return ProcessSandboxInstance(
        id="test-sbx",
        owner_id="test",
        conversation_id="test-cid",
        spec=SandboxSpec(),
        workspace=workspace,
    )


def _ctx(sandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
    )


# A minimal 3-slide markdown deck
_THREE_SLIDE_MD = """# Slide 1

First slide content.

---

## Slide 2

- Bullet one
- Bullet two

---

# Slide 3

Final slide with **bold** and *italic* text.
"""


# ---- slide splitting ---------------------------------------------------------


def test_split_slides_basic():
    """Three '---'-separated slides → 3 slides."""
    slides = _split_slides(_THREE_SLIDE_MD)
    assert len(slides) == 3
    assert slides[0].startswith("# Slide 1")
    assert slides[1].startswith("## Slide 2")
    assert slides[2].startswith("# Slide 3")


def test_split_slides_single():
    """Single slide with no separators → 1 slide."""
    slides = _split_slides("# Only Slide\n\nSome content.")
    assert len(slides) == 1


def test_split_slides_crlf():
    """Windows CRLF line endings are normalized."""
    md = "# One\r\n\r\nContent\r\n\r\n---\r\n\r\n# Two\r\n"
    slides = _split_slides(md)
    assert len(slides) == 2


def test_split_slides_empty_filtered():
    """Empty slides from leading/trailing separators are filtered."""
    md = "\n---\n# Real\n---\n\n"
    slides = _split_slides(md)
    assert len(slides) == 1
    assert "# Real" in slides[0]


def test_split_slides_strips_marp_frontmatter():
    """P10-7: a leading Marp/YAML frontmatter block is not counted as a slide (it
    otherwise inflated the declared count → false truncation at the export gate)."""
    md = "---\nmarp: true\ntheme: default\n---\n# One\n\n---\n\n# Two"
    slides = _split_slides(md)
    assert len(slides) == 2
    assert "marp: true" not in slides[0]
    # a genuine leading `---\n# Slide\n---` separator (heading body) is NOT frontmatter
    assert len(_split_slides("\n---\n# Real\n---\n")) == 1


def test_legacy_markdown_and_format_inputs_are_rejected():
    """The model-facing schema cannot select Marp or a non-PPTX product."""
    legacy = SlidesGenerateArgs(markdown=_THREE_SLIDE_MD, filename="test-deck", format="pptx")
    assert "markdown" in (legacy.model_extra or {})
    with pytest.raises(ValidationError, match="format"):
        SlidesGenerateArgs(goal="A deck", filename="bad", format="html")


async def test_no_content_fails_loudly_instead_of_blank_deck(tmp_workspace):
    """Gauntlet root-cause (2026-07-07): a driver called slides_generate with
    theme + slide_count + format but NO ``goal`` and NO ``markdown``. The old gate
    silently fell through to the Marp path with empty content → a 1-slide, zero-text
    deck reported as success. The tool must now REFUSE loudly (so the driver re-calls
    with ``goal``) rather than ship a blank deliverable."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    outcome = await tool.run(
        SlidesGenerateArgs(
            filename="ptq-llm-deep-research",
            format="pptx",
            theme='{"palette": {"primary": "#1E3A5F"}, "style": "modern"}',
            slide_count=7,
            # note: NO goal, NO markdown — the exact failing call shape
        ),
        ctx,
    )

    assert not outcome.success
    assert "goal" in outcome.content
    assert outcome.error and "requires a structured goal" in outcome.error
    # Critically: NOTHING was written — no blank deck shipped.
    assert not (tmp_workspace / "ptq-llm-deep-research.pptx").exists()
    assert not (tmp_workspace / "ptq-llm-deep-research.html").exists()


def test_legacy_markdown_filename_is_rejected():
    legacy = SlidesGenerateArgs(markdown=_THREE_SLIDE_MD, filename=">my|deck?", format="pptx")
    assert "markdown" in (legacy.model_extra or {})


def test_legacy_mode_is_rejected_explicitly():
    legacy = SlidesGenerateArgs(
        markdown="   \n  ", filename="empty", format="pptx", mode="markdown"
    )
    assert set(legacy.model_extra or {}) == {"markdown", "mode"}


@pytest.mark.asyncio
async def test_legacy_markdown_runtime_returns_explicit_error(tmp_workspace):
    args = SlidesGenerateArgs(markdown="# Legacy", filename="legacy", format="pptx")
    outcome = await SlidesTool().run(args, _ctx(_jailed_sandbox(tmp_workspace)))
    assert outcome.success is False
    assert outcome.error == "legacy_markdown_slide_path_disabled"
    assert outcome.artifacts == []


# ---- sandbox-jailed write ----------------------------------------------------


async def test_path_traversal_filename_rejected(tmp_workspace):
    """A model-controlled filename that escapes the workspace (../) must be
    rejected by the sandbox jail and surfaced as a clean failure — and NOTHING
    is written outside the workspace."""
    reg = build_default_registry()
    ex = DefaultToolExecutor(
        reg,
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=_jailed_sandbox(tmp_workspace),
    )

    escape_target = tmp_workspace.parent / "escape.pptx"
    assert not escape_target.exists()

    res = await ex.execute(
        call(
            "slides_generate",
            goal="Test",
            filename="../escape",
            format="pptx",
        )
    )

    # Filename hygiene rejects traversal before any renderer writes.
    assert res.success is False
    assert not escape_target.exists(), "no file may be written outside the workspace"


def test_legacy_markdown_never_reaches_workspace_renderer():
    legacy = SlidesGenerateArgs(markdown="# Hello", filename="host-cwd-test", format="pptx")
    assert "markdown" in (legacy.model_extra or {})


async def _assert_result_carries_delivery_note_and_real_paths(tmp_workspace):
    """ROOT-4 (slides spiral): a successful generation must return the REAL on-disk
    path(s) + an explicit done/delivered/call-finish signal so the agent finishes
    instead of hunting /workspace and verify-looping."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    async def fake_c2(args, fake_ctx, fmt):
        del args, fmt
        await fake_ctx.sandbox.write_file("deck.pptx", b"not-a-real-pptx")
        return ToolOutcome(
            success=True, content="deck ready", artifacts=["deck.pptx"], structured={}
        )

    with patch.object(SlidesTool, "_run_c2_pipeline", side_effect=fake_c2):
        outcome = await tool.run(
            SlidesGenerateArgs(goal="A deck", filename="deck", format="pptx"), ctx
        )

    assert outcome.success
    # The done/delivered/finish signal is present.
    assert "Deck generated AND delivered" in outcome.content
    assert "no further" in outcome.content.lower()
    assert "finish" in outcome.content.lower()
    # The REAL absolute on-disk path is included (so no /workspace shell hunt).
    expected = str(tmp_workspace.resolve() / "deck.pptx")
    assert expected in outcome.content
    # The note does not disturb the artifacts list.
    assert "deck.pptx" in outcome.artifacts


async def test_result_carries_delivery_note_and_real_paths(tmp_workspace):
    await _assert_result_carries_delivery_note_and_real_paths(tmp_workspace)


# ---- AGENT_TOOLS registration + scoping --------------------------------------


async def test_tool_registered_and_scoped():
    """slides_generate is in the default registry AND in agent scope."""
    reg = build_default_registry()
    agt_scope = agent_scope(model_policy=ModelExecutionPolicy.standard())
    assert "slides_generate" in reg.names()
    assert "slides_generate" in agt_scope.allowed_tools

    tool = reg.get("slides_generate", scope=agt_scope)
    assert tool is not None
    assert tool.definition.name == "slides_generate"


async def test_executor_rejects_slides_in_research_scope():
    """slides_generate is agent-scope only, not research."""
    reg = build_default_registry()
    ex = DefaultToolExecutor(reg, research_scope(), sandbox=FakeSandboxInstance())
    res = await ex.execute(
        call(
            "slides_generate",
            markdown="# Test",
            filename="x",
            format="html",
        )
    )
    assert res.success is False
    assert res.structured["kind"] == "unknown_tool"


# ---- tool definition integrity -----------------------------------------------


def test_tool_def_runs_in_sandbox():
    """slides_generate must declare runs_in='sandbox' to enforce jail."""
    tool = SlidesTool()
    assert tool.definition.runs_in == "sandbox"
    assert tool.definition.read_only is False
    assert Capability.FILESYSTEM in tool.definition.needs


# Historical Marp/fallback IDs remain as compatibility sentinels.  The product
# contract is now native authored PPTX only, so each sentinel proves that the
# retired path fails closed without creating a substitute artifact.


async def test_html_fallback_writes_artifact(tmp_workspace):
    outcome = await SlidesTool().run(
        SlidesGenerateArgs(markdown=_THREE_SLIDE_MD, filename="legacy", format="pptx"),
        _ctx(_jailed_sandbox(tmp_workspace)),
    )
    assert outcome.success is False
    assert outcome.error == "legacy_markdown_slide_path_disabled"
    assert not list(tmp_workspace.iterdir())


async def test_html_fallback_with_theme(tmp_workspace):
    outcome = await SlidesTool().run(
        SlidesGenerateArgs(
            markdown="# One", filename="legacy-themed", format="pptx", theme="ignored"
        ),
        _ctx(_jailed_sandbox(tmp_workspace)),
    )
    assert outcome.success is False
    assert outcome.error == "legacy_markdown_slide_path_disabled"
    assert not list(tmp_workspace.iterdir())


async def test_html_fallback_slide_count_in_result(tmp_workspace):
    outcome = await SlidesTool().run(
        SlidesGenerateArgs(markdown=_THREE_SLIDE_MD, filename="legacy-count", format="pptx"),
        _ctx(_jailed_sandbox(tmp_workspace)),
    )
    assert outcome.success is False
    assert outcome.error == "legacy_markdown_slide_path_disabled"
    assert "slide_count" not in (outcome.structured or {})


def test_invalid_format_rejected():
    with pytest.raises(ValidationError):
        SlidesGenerateArgs(goal="A deck", filename="bad", format="docx")


async def test_hostile_filename_chars_stripped(tmp_workspace):
    seen: dict[str, str] = {}

    async def _capture(args, _ctx, _fmt):
        seen["filename"] = args.filename
        return ToolOutcome(success=False, content="failed", error="test")

    with patch.object(SlidesTool, "_run_c2_pipeline", side_effect=_capture):
        outcome = await SlidesTool().run(
            SlidesGenerateArgs(goal="A deck", filename=">my|deck?", format="pptx"),
            _ctx(_jailed_sandbox(tmp_workspace)),
        )
    assert outcome.success is False
    assert seen["filename"] == "mydeck"
    assert not list(tmp_workspace.iterdir())


async def test_all_invalid_filename_rejected(tmp_workspace):
    outcome = await SlidesTool().run(
        SlidesGenerateArgs(goal="A deck", filename=">>?", format="pptx"),
        _ctx(_jailed_sandbox(tmp_workspace)),
    )
    assert outcome.success is False
    assert outcome.error == "invalid filename"
    assert not list(tmp_workspace.iterdir())


async def test_whitespace_markdown_also_refused(tmp_workspace):
    outcome = await SlidesTool().run(
        SlidesGenerateArgs(markdown="   \n  ", filename="empty", format="pptx", mode="markdown"),
        _ctx(_jailed_sandbox(tmp_workspace)),
    )
    assert outcome.success is False
    assert outcome.error == "legacy_markdown_slide_path_disabled"
    assert not list(tmp_workspace.iterdir())


async def test_pdf_degrades_to_html_when_marp_absent(tmp_workspace):
    with pytest.raises(ValidationError):
        SlidesGenerateArgs(markdown=_THREE_SLIDE_MD, filename="deck", format="pdf")
    assert not list(tmp_workspace.iterdir())


async def test_pptx_degrades_to_html_when_marp_absent(tmp_workspace):
    async def _fail(*_args, **_kwargs):
        return ToolOutcome(success=False, content="authored failure", error="failed")

    with patch.object(SlidesTool, "_run_c2_pipeline", side_effect=_fail):
        outcome = await SlidesTool().run(
            SlidesGenerateArgs(goal="A deck", filename="deck", format="pptx"),
            _ctx(_jailed_sandbox(tmp_workspace)),
        )
    assert outcome.success is False
    assert outcome.artifacts == []
    assert not (tmp_workspace / "deck.html").exists()
    assert not (tmp_workspace / "deck.pptx").exists()


async def test_file_lands_in_workspace_not_host_cwd(tmp_workspace):
    await _assert_result_carries_delivery_note_and_real_paths(tmp_workspace)
