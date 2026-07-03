"""Tests for document/report section parts and host-owned export."""

from __future__ import annotations

import pytest
from disco.core.contract.export_render import EXPORT_RENDER_KEY, ExportRenderFacts
from disco.core.llm import ModelExecutionPolicy
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.document import (
    DocExportArgs,
    DocExportTool,
    DocSetSectionArgs,
    DocSetSectionTool,
)
from disco.tools.registry import agent_scope, artifact_scope
from pydantic import ValidationError
from tool_fakes import FakeSandboxInstance


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="doc-tests",
    )


def test_doc_set_section_rejects_path_escape_at_schema_validation() -> None:
    with pytest.raises(ValidationError):
        DocSetSectionArgs(section="../escape", title="Escape", body="Nope")


async def test_doc_set_section_writes_validated_part() -> None:
    sbx = FakeSandboxInstance()
    outcome = await DocSetSectionTool().run(
        DocSetSectionArgs(
            section="executive_summary",
            title="Executive Summary",
            body="Revenue increased by 18%.",
            order=10,
        ),
        _ctx(sbx),
    )

    assert outcome.success, outcome.content
    assert outcome.artifacts == [".disco/parts/executive_summary.md"]
    data = sbx._fs[".disco/parts/executive_summary.md"].decode("utf-8")
    assert data.startswith("<!-- disco-doc-part ")
    assert '"kind":"document/report"' in data
    assert "## Executive Summary" in data
    assert "Revenue increased by 18%." in data


async def test_doc_export_assembles_parts_and_stamps_render_facts() -> None:
    sbx = FakeSandboxInstance()
    ctx = _ctx(sbx)
    tool = DocSetSectionTool()
    await tool.run(
        DocSetSectionArgs(section="later", title="Later", body="Second section content.", order=20),
        ctx,
    )
    await tool.run(
        DocSetSectionArgs(section="first", title="First", body="First section content.", order=10),
        ctx,
    )

    outcome = await DocExportTool().run(
        DocExportArgs(filename="report", title="Quarterly Report"),
        ctx,
    )

    assert outcome.success, outcome.content
    assert outcome.artifacts == ["report.pdf", "report.md"]
    markdown = sbx._fs["report.md"].decode("utf-8")
    assert markdown.index("## First") < markdown.index("## Later")
    assert sbx._fs["report.pdf"].startswith(b"%PDF-1.4")
    assert outcome.structured is not None
    assert outcome.structured["pipeline_stages"] == ["preflight", "bundle", "validate", "deliver"]
    facts = ExportRenderFacts.model_validate(outcome.structured[EXPORT_RENDER_KEY])
    assert facts.ok is True
    assert facts.fmt == "pdf"
    assert facts.unit_count == facts.declared_units == 1
    assert facts.visible_text_len >= 12


async def test_doc_export_fails_without_parts() -> None:
    sbx = FakeSandboxInstance()
    outcome = await DocExportTool().run(DocExportArgs(), _ctx(sbx))

    assert outcome.success is False
    assert "doc_set_section" in outcome.content


def test_document_tools_registered_in_agent_and_artifact_scope() -> None:
    reg = build_default_registry()
    assert {"doc_set_section", "doc_export"} <= reg.names()
    agt = agent_scope(model_policy=ModelExecutionPolicy.standard())
    art = artifact_scope()
    assert {"doc_set_section", "doc_export"} <= agt.allowed_tools
    assert {"doc_set_section", "doc_export"} <= art.allowed_tools
