"""Unit tests for the slides_generate tool.

Proves:
  - markdown → slides split (3 '---'-separated slides → 3 slides in output).
  - HTML path works end-to-end: markdown → HTML deck artifact written to
    jailed workspace; assert slide count.
  - marp-absent → PDF/PPTX return clean failure (success=False + message);
    HTML still succeeds via the fallback.
  - Format routing: invalid format → clean failure.
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
from conftest import FakeSandboxInstance, call
from disco.tools.anatomy import Capability, ToolContext
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


# ---- HTML fallback renderer --------------------------------------------------


async def test_html_fallback_writes_artifact(tmp_workspace):
    """When marp is absent, HTML is rendered via the fallback and lands in the
    workspace."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown=_THREE_SLIDE_MD,
                filename="test-deck",
                format="html",
            ),
            ctx,
        )

    assert outcome.success, f"Tool failed: {outcome.error}"
    assert outcome.artifacts == ["test-deck.html"]

    # File landed in workspace
    file_path = tmp_workspace / "test-deck.html"
    assert file_path.exists()
    content = file_path.read_text("utf-8")

    # Basic HTML structure
    assert "<!DOCTYPE html>" in content
    assert '<section class="slide"' in content
    # Three slides
    assert content.count('<section class="slide"') == 3
    # Slide content
    assert "Slide 1" in content
    assert "Bullet one" in content
    assert "Final slide" in content

    assert outcome.structured["renderer"] == "fallback"
    assert outcome.structured["slide_count"] == 3


async def test_html_fallback_with_theme(tmp_workspace):
    """Custom theme CSS is injected into the fallback HTML."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    custom_theme = "body { background: #f0f; }"

    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown="# One\n\n---\n\n# Two",
                filename="themed",
                format="html",
                theme=custom_theme,
            ),
            ctx,
        )

    assert outcome.success
    content = (tmp_workspace / "themed.html").read_text("utf-8")
    assert "background: #f0f" in content


async def test_html_fallback_slide_count_in_result(tmp_workspace):
    """The structured result reports the correct slide count."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown=_THREE_SLIDE_MD,
                filename="count",
                format="html",
            ),
            ctx,
        )

    assert outcome.success
    assert outcome.structured["slide_count"] == 3
    assert "Slides: 3" in outcome.content


# ---- format routing ----------------------------------------------------------


async def test_invalid_format_rejected(tmp_workspace):
    """Unknown format returns a clean failure."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    outcome = await tool.run(
        SlidesGenerateArgs(
            markdown="# Test",
            filename="bad",
            format="docx",
        ),
        ctx,
    )

    assert not outcome.success
    assert "Unsupported format" in outcome.content
    assert "docx" in outcome.content
    # No file was written
    assert not (tmp_workspace / "bad.docx").exists()


# ---- marp-absent clean failures ----------------------------------------------


async def test_pdf_fails_when_marp_absent(tmp_workspace):
    """PDF export returns a clean failure when marp is not on PATH."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown=_THREE_SLIDE_MD,
                filename="deck",
                format="pdf",
            ),
            ctx,
        )

    assert not outcome.success
    assert "PDF" in outcome.content
    # marp runs INSIDE the sandbox now; absence is surfaced as "not present in this
    # sandbox" + an HTML fallback hint — NOT the old false "after VM-201 rebuild".
    assert "sandbox" in outcome.content.lower() and "html" in outcome.content.lower()
    assert "marp CLI not found" in outcome.error
    # No file was written
    assert not (tmp_workspace / "deck.pdf").exists()


async def test_pptx_fails_when_marp_absent(tmp_workspace):
    """PPTX export returns a clean failure when marp is not on PATH."""
    tool = SlidesTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown=_THREE_SLIDE_MD,
                filename="deck",
                format="pptx",
            ),
            ctx,
        )

    assert not outcome.success
    assert "PPTX" in outcome.content
    assert "html" in outcome.content.lower()  # suggests HTML fallback
    assert "marp CLI not found" in outcome.error
    assert not (tmp_workspace / "deck.pptx").exists()


# ---- sandbox-jailed write ----------------------------------------------------


async def test_path_traversal_filename_rejected(tmp_workspace):
    """A model-controlled filename that escapes the workspace (../) must be
    rejected by the sandbox jail and surfaced as a clean failure — and NOTHING
    is written outside the workspace."""
    reg = build_default_registry()
    ex = DefaultToolExecutor(reg, agent_scope(), sandbox=_jailed_sandbox(tmp_workspace))

    escape_target = tmp_workspace.parent / "escape.html"
    assert not escape_target.exists()

    res = await ex.execute(
        call(
            "slides_generate",
            markdown="# Test",
            filename="../escape",
            format="html",
        )
    )

    # With marp absent, the fallback HTML path is taken — its sandbox.write_file
    # will reject the ../ path.
    assert res.success is False
    assert not escape_target.exists(), "no file may be written outside the workspace"


async def test_file_lands_in_workspace_not_host_cwd(tmp_workspace):
    """Prove the artifact lands INSIDE the jailed workspace, not the host's CWD."""
    tool = SlidesTool()
    sbx = _jailed_sandbox(tmp_workspace)
    ctx = _ctx(sbx)

    with patch("disco.tools.builtin.slides._marp_available", return_value=False):
        outcome = await tool.run(
            SlidesGenerateArgs(
                markdown="# Hello",
                filename="host-cwd-test",
                format="html",
            ),
            ctx,
        )

    assert outcome.success
    assert outcome.artifacts == ["host-cwd-test.html"]

    # File must exist in the TEMP workspace, not the current directory
    assert (tmp_workspace / "host-cwd-test.html").exists(), (
        "artifact must be written to jail workspace"
    )
    # The host CWD (the test's working dir) should NOT have this file
    cwd_file = Path.cwd() / "host-cwd-test.html"
    assert not cwd_file.exists(), (
        f"artifact leaked to host CWD: {cwd_file} — must only be in workspace"
    )


# ---- AGENT_TOOLS registration + scoping --------------------------------------


async def test_tool_registered_and_scoped():
    """slides_generate is in the default registry AND in agent scope."""
    reg = build_default_registry()
    agt_scope = agent_scope()
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
