"""Sheet generation tool — writes a real .xlsx workbook with LIVE formulas
(not pre-computed values) to the workspace. The model authors the sheet spec
(rows, columns, formulas) and this tool writes it via openpyxl.

Formula whitelist: only pure functions (SUM/AVERAGE/IF/VLOOKUP/INDEX/MATCH/DATE
and similar) are allowed. External-fetch / indirection functions like IMPORTXML,
INDIRECT, WEBSERVICE, HYPERLINK are REJECTED with a clear error.
"""

from __future__ import annotations

import io
import re
from typing import cast

import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

# ---- formula whitelist -------------------------------------------------------

# Allowed: pure functions (no external fetch, no indirection, no side effects).
# Deferred: financial (NPV/IRR), stats (STDEV/CORREL), text (LEFT/RIGHT/MID), etc.
# Those are not security risks — just not in the whitelist today.
_ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Aggregation
        "SUM",
        "SUMIF",
        "SUMIFS",
        "AVERAGE",
        "AVERAGEIF",
        "AVERAGEIFS",
        "COUNT",
        "COUNTA",
        "COUNTIF",
        "COUNTIFS",
        "MIN",
        "MAX",
        "MEDIAN",
        "MODE",
        "PRODUCT",
        "SUMPRODUCT",
        "SUBTOTAL",
        # Lookup
        "VLOOKUP",
        "HLOOKUP",
        "XLOOKUP",
        "INDEX",
        "MATCH",
        "CHOOSE",
        "OFFSET",
        # Logical
        "IF",
        "IFS",
        "IFERROR",
        "IFNA",
        "AND",
        "OR",
        "NOT",
        "XOR",
        "SWITCH",
        # Date/Time (pure — no NOW/TODAY? Actually those ARE volatile but safe)
        "DATE",
        "DATEDIF",
        "DATEVALUE",
        "DAY",
        "DAYS",
        "EDATE",
        "EOMONTH",
        "HOUR",
        "MINUTE",
        "MONTH",
        "NETWORKDAYS",
        "SECOND",
        "TIME",
        "TIMEVALUE",
        "TODAY",
        "WEEKDAY",
        "WEEKNUM",
        "WORKDAY",
        "YEAR",
        "YEARFRAC",
        # Math
        "ABS",
        "CEILING",
        "FLOOR",
        "INT",
        "ROUND",
        "ROUNDUP",
        "ROUNDDOWN",
        "MOD",
        "POWER",
        "SQRT",
        "EXP",
        "LN",
        "LOG",
        "LOG10",
        "RAND",
        "RANDBETWEEN",
        "PI",
        "RADIANS",
        "DEGREES",
        "SIN",
        "COS",
        "TAN",
        # Text (pure)
        "CONCAT",
        "CONCATENATE",
        "TEXT",
        "TEXTJOIN",
        "LEFT",
        "RIGHT",
        "MID",
        "LEN",
        "LOWER",
        "UPPER",
        "PROPER",
        "TRIM",
        "SUBSTITUTE",
        "REPLACE",
        "FIND",
        "SEARCH",
        "VALUE",
        # Reference (non-indirect)
        "ROW",
        "ROWS",
        "COLUMN",
        "COLUMNS",
        "ADDRESS",
    }
)

# Functions that are EXPLICITLY REJECTED with a clear error.
_REJECTED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "IMPORTXML",
        "IMPORTHTML",
        "IMPORTDATA",
        "IMPORTRANGE",
        "IMPORTFEED",
        "INDIRECT",
        "WEBSERVICE",
        "HYPERLINK",
        "GOOGLEFINANCE",
    }
)

# Regex to extract function names from a formula string.
# Matches an identifier followed by `(`, ALLOWING whitespace between the two
# (`INDIRECT ("A1")`): some spreadsheet engines tolerate that spacing, so the
# matcher must too — otherwise a spaced call would slip past the whitelist
# entirely (a deny-list bypass). This is deliberately simple: it doesn't parse
# nested parens or string literals; a string literal containing "IMPORTXML("
# would false-positive, but that's the safe direction for a deny-list (fail-safe).
_FORMULA_NAME_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)


def _validate_formula(formula: str, *, cell_ref: str) -> None:
    """Check every function name in `formula` against the whitelist.
    Raises ValueError with a clear message if a rejected function is found."""
    for m in _FORMULA_NAME_RE.finditer(formula):
        name = m.group(1).upper()
        if name in _REJECTED_FUNCTIONS:
            raise ValueError(
                f"Formula in cell {cell_ref} uses {name}() which is REJECTED "
                f"(external-fetch or indirection function). Allowed functions "
                f"include SUM, AVERAGE, IF, VLOOKUP, INDEX, MATCH, DATE, etc."
            )
        if name not in _ALLOWED_FUNCTIONS:
            raise ValueError(
                f"Formula in cell {cell_ref} uses {name}() which is not in the "
                f"allowed whitelist. Allowed functions include SUM, AVERAGE, IF, "
                f"VLOOKUP, INDEX, MATCH, DATE, etc. Rejected: IMPORTXML, INDIRECT, "
                f"WEBSERVICE, HYPERLINK, IMPORTRANGE."
            )


# ---- args model --------------------------------------------------------------


class SheetSpec(BaseModel):
    """One sheet in the workbook."""

    name: str = Field(description="Sheet name (unique within the workbook).")
    columns: list[str] = Field(
        description="Column header labels (e.g. ['Item', 'Price', 'Qty', 'Total'])."
    )
    rows: list[list[str | int | float]] = Field(
        description=(
            "Row data. Each row is a list of cell values. Values can be strings, "
            "numbers, or Excel formulas (strings starting with '='). Example: "
            "[['Widget', 10, 5, '=B2*C2'], ['Gadget', 15, 3, '=B3*C3']]"
        )
    )


class SheetGenerateArgs(BaseModel):
    """Arguments for the sheet_generate tool."""

    title: str = Field(description="Short human label for the workbook, e.g. 'Q4 Sales'.")
    filename: str = Field(
        default="workbook.xlsx",
        description="Filename to write (relative to workspace). Default: 'workbook.xlsx'.",
    )
    sheets: list[SheetSpec] = Field(description="One or more sheet definitions.")


# ---- tool implementation -----------------------------------------------------


class SheetsTool:
    definition = ToolDef(
        name="sheet_generate",
        description=(
            "Write a real .xlsx workbook with LIVE formulas (not pre-computed values) "
            "to the workspace. Provide one or more sheets with column headers and row "
            "data. Cells starting with '=' are written as Excel formulas (e.g. "
            "'=SUM(A2:A10)', '=VLOOKUP(D2, A:B, 2, FALSE)'). Formulas are validated "
            "against a whitelist — external-fetch functions like IMPORTXML/INDIRECT "
            "are rejected."
        ),
        args_model=SheetGenerateArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=None,  # file write — sandbox-jailed, no network needed
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: SheetGenerateArgs, ctx: ToolContext) -> ToolOutcome:
        # This tool declares runs_in="sandbox": all I/O MUST go through the
        # sandbox instance, which jails the path (rejects ../ and absolute
        # escapes) and lands the file in the conversation's workspace. Writing
        # via openpyxl to ctx.workspace_path ("." — the host process cwd) would
        # both escape the jail (model-controlled filename) AND drop the artifact
        # outside the workspace where the DeliverableEvent can't resolve it.
        assert ctx.sandbox is not None  # sandbox tools always receive an instance

        # 1. Validate all formulas before writing anything.
        errors: list[str] = []
        for _si, sheet_spec in enumerate(args.sheets):
            for ri, row in enumerate(sheet_spec.rows):
                for ci, cell_val in enumerate(row):
                    if isinstance(cell_val, str) and cell_val.startswith("="):
                        col_letter = get_column_letter(ci + 1)
                        cell_ref = f"'{sheet_spec.name}'!{col_letter}{ri + 2}"
                        try:
                            _validate_formula(cell_val, cell_ref=cell_ref)
                        except ValueError as e:
                            errors.append(str(e))

        if errors:
            return ToolOutcome(
                success=False,
                content="\n".join(errors),
                error="Formula validation failed. " + errors[0],
            )

        # 2. Build the workbook.
        wb = openpyxl.Workbook()
        # Remove the default sheet; we add our own.
        default_ws = wb.active
        assert default_ws is not None  # a fresh Workbook() always has an active sheet
        wb.remove(default_ws)

        for sheet_spec in args.sheets:
            ws = cast(Worksheet, wb.create_sheet(title=sheet_spec.name))

            # Write column headers (row 1).
            for ci, col_name in enumerate(sheet_spec.columns):
                ws.cell(row=1, column=ci + 1, value=col_name)

            # Style the header row.
            from openpyxl.styles import Font

            header_font = Font(bold=True)
            for ci in range(len(sheet_spec.columns)):
                ws.cell(row=1, column=ci + 1).font = header_font

            # Write data rows (row 2+).
            for ri, row in enumerate(sheet_spec.rows):
                for ci, cell_val in enumerate(row):
                    cell = ws.cell(row=ri + 2, column=ci + 1)
                    # ws.cell() returns Cell | MergedCell; we never merge cells
                    # in this tool, so a real Cell is provable here.
                    assert not isinstance(cell, MergedCell)
                    if isinstance(cell_val, str) and cell_val.startswith("="):
                        # Write as a formula — openpyxl stores it as the formula
                        # STRING, NOT the evaluated value.
                        cell.value = cell_val
                    else:
                        cell.value = cell_val

            # Auto-fit column widths (approximate).
            for ci in range(len(sheet_spec.columns)):
                max_width = len(sheet_spec.columns[ci])
                for ri in range(len(sheet_spec.rows)):
                    val = sheet_spec.rows[ri][ci] if ci < len(sheet_spec.rows[ri]) else ""
                    max_width = max(max_width, len(str(val)))
                ws.column_dimensions[get_column_letter(ci + 1)].width = min(max_width + 2, 40)

        # 3. Render to an in-memory buffer, then write THROUGH the sandbox so the
        #    path is jailed and the artifact lands in the conversation workspace.
        buf = io.BytesIO()
        wb.save(buf)
        await ctx.sandbox.write_file(args.filename, buf.getvalue())

        sheet_names = [s.name for s in args.sheets]

        return ToolOutcome(
            success=True,
            content=(
                f"Workbook '{args.title}' written to {args.filename}\n"
                f"Sheets: {', '.join(sheet_names)}\n"
                f"NOTE: formulas are NOT evaluated — open in a spreadsheet app to compute."
            ),
            artifacts=[args.filename],
            structured={
                "title": args.title,
                "filename": args.filename,
                "sheet_names": sheet_names,
                "formulas_evaluated": False,
            },
        )
