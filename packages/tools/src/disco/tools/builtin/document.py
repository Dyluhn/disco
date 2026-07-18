"""Document/report section authoring and host-owned export.

The model authors schema-validated markdown parts via ``doc_set_section``. The
host owns assembly/export via ``doc_export``: it re-reads the persisted parts,
validates their part marker, bundles ``report.md``, renders a simple print-safe
PDF or HTML export, then stamps ``ExportRenderFacts`` using the same P10
read-back choke point as slide exports.
"""

from __future__ import annotations

import html
import json
import re
import textwrap
from typing import Literal

from disco.core.contract.export_render import EXPORT_RENDER_KEY, check_export_render
from disco.core.effects import EffectCapability
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ..sandbox.base import strip_redundant_workspace_prefix

_FS = frozenset({Capability.FILESYSTEM})
_PART_DIR = ".disco/parts"
_PART_KIND = "document/report"
_PART_MARKER_RE = re.compile(r"^<!--\s*disco-doc-part\s+(.+?)\s*-->\s*$")
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class DocumentPartMeta(BaseModel):
    """Metadata stamped into each part file and re-validated during export."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["document/report"] = _PART_KIND
    section: str = Field(description="Stable section id, used as .disco/parts/<section>.md.")
    title: str = Field(description="Human-readable section heading.")
    order: int = Field(default=100, ge=0, le=999, description="Assembly order.")

    @field_validator("section")
    @classmethod
    def _safe_section(cls, v: str) -> str:
        if not _SAFE_NAME_RE.match(v):
            raise ValueError("section must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return v

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        title = re.sub(r"\s+", " ", v).strip()
        if not title:
            raise ValueError("title must be non-empty")
        if len(title) > 120:
            raise ValueError("title must be 120 characters or fewer")
        return title


class DocumentPart(BaseModel):
    """A validated persisted document part."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    meta: DocumentPartMeta
    markdown: str

    @field_validator("markdown")
    @classmethod
    def _markdown_non_empty(cls, v: str) -> str:
        body = v.strip()
        if not body:
            raise ValueError("part markdown must be non-empty")
        return body + "\n"


class DocSetSectionArgs(BaseModel):
    """Arguments for doc_set_section."""

    model_config = ConfigDict(extra="forbid")

    section: str = Field(
        description=(
            "Stable section id for this report part. Lowercase slug only: letters, "
            "digits, underscores, and hyphens."
        )
    )
    title: str = Field(description="Section heading to render in the assembled document.")
    body: str = Field(description="Markdown body for this section, without the title heading.")
    order: int = Field(default=100, ge=0, le=999, description="Assembly order.")

    @field_validator("body")
    @classmethod
    def _body_non_empty(cls, v: str) -> str:
        body = v.strip()
        if not body:
            raise ValueError("body must be non-empty")
        return body

    @field_validator("section")
    @classmethod
    def _safe_section(cls, v: str) -> str:
        if not _SAFE_NAME_RE.match(v):
            raise ValueError("section must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return v

    @field_validator("title")
    @classmethod
    def _safe_title(cls, v: str) -> str:
        return DocumentPartMeta(section="section", title=v).title


class DocExportArgs(BaseModel):
    """Arguments for doc_export."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(
        default="report",
        description="Base output filename without extension. Default: report.",
    )
    title: str = Field(default="Report", description="Document title.")
    format: Literal["pdf", "html"] = Field(
        default="pdf",
        description="Export format. PDF is the default document/report deliverable.",
    )

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, v: str) -> str:
        name = strip_redundant_workspace_prefix(v.strip())
        if "." in name:
            name = name.rsplit(".", 1)[0]
        if "/" in name or not _SAFE_NAME_RE.match(name):
            raise ValueError("filename must be a safe basename slug without directories")
        return name

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        title = re.sub(r"\s+", " ", v).strip()
        if not title:
            raise ValueError("title must be non-empty")
        if len(title) > 120:
            raise ValueError("title must be 120 characters or fewer")
        return title


def _part_path(section: str) -> str:
    return f"{_PART_DIR}/{section}.md"


def _serialize_part(args: DocSetSectionArgs) -> tuple[str, DocumentPart]:
    meta = DocumentPartMeta(section=args.section, title=args.title, order=args.order)
    markdown = f"## {meta.title}\n\n{args.body.strip()}\n"
    part = DocumentPart(meta=meta, markdown=markdown)
    marker = json.dumps(meta.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return f"<!-- disco-doc-part {marker} -->\n\n{part.markdown}", part


def _parse_part(path: str, raw: bytes | str) -> DocumentPart:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    first, sep, rest = text.partition("\n")
    if not sep:
        raise ValueError(f"{path}: missing document part marker")
    match = _PART_MARKER_RE.match(first.strip())
    if match is None:
        raise ValueError(f"{path}: missing document part marker")
    try:
        meta = DocumentPartMeta.model_validate(json.loads(match.group(1)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{path}: invalid document part metadata: {exc}") from exc
    expected = _part_path(meta.section)
    if path != expected:
        raise ValueError(f"{path}: part section metadata expects {expected}")
    return DocumentPart(meta=meta, markdown=rest.strip())


async def _read_parts(ctx: ToolContext) -> list[DocumentPart]:
    if ctx.sandbox is None:
        raise ValueError("No sandbox available to read document parts.")
    try:
        names = await ctx.sandbox.list_dir(_PART_DIR)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("No document parts found. Call doc_set_section first.") from exc

    paths: list[str] = []
    for name in names:
        rel = strip_redundant_workspace_prefix(str(name))
        if rel.startswith(f"{_PART_DIR}/"):
            path = rel
        elif "/" not in rel:
            path = f"{_PART_DIR}/{rel}"
        else:
            continue
        if path.endswith(".md"):
            paths.append(path)
    if not paths:
        raise ValueError("No document parts found. Call doc_set_section first.")

    parts: list[DocumentPart] = []
    for path in sorted(set(paths)):
        raw = await ctx.sandbox.read_file(path)
        parts.append(_parse_part(path, raw))
    parts.sort(key=lambda p: (p.meta.order, p.meta.section))
    return parts


def _assemble_markdown(title: str, parts: list[DocumentPart]) -> str:
    chunks = [f"# {title.strip()}"]
    chunks.extend(part.markdown.strip() for part in parts)
    return "\n\n".join(chunks).rstrip() + "\n"


def _inline_markdown(md: str) -> str:
    text = html.escape(md)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def _markdown_to_html(title: str, md: str) -> str:
    out: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{_inline_markdown(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{_inline_markdown(line[3:].strip())}</h2>")
        elif line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{_inline_markdown(line[4:].strip())}</h3>")
        elif line.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_markdown(line[2:].strip())}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_inline_markdown(line)}</p>")
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    @page {{ margin: 0.75in; }}
    body {{ font: 12pt/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: #171717; }}
    h1 {{ font-size: 24pt; margin: 0 0 20pt; }}
    h2 {{ font-size: 16pt; margin: 18pt 0 8pt; page-break-after: avoid; }}
    h3 {{ font-size: 13pt; margin: 12pt 0 6pt; page-break-after: avoid; }}
    p, li {{ max-width: 72ch; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_lines_for_pdf(md: str) -> list[str]:
    lines: list[str] = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^[-*]\s+", "- ", line)
        line = re.sub(r"[*_`]", "", line)
        wrapped = textwrap.wrap(line, width=86) or [""]
        lines.extend(wrapped)
    return lines


def _render_pdf(md: str) -> tuple[bytes, int]:
    lines = _text_lines_for_pdf(md)
    lines_per_page = 48
    pages = [lines[i : i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[""]]
    font_id = 3 + (len(pages) * 2)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
    ]
    kids = " ".join(f"{3 + (i * 2)} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    for i, page_lines in enumerate(pages):
        page_id = 3 + (i * 2)
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        body_lines = ["BT", "/F1 11 Tf", "50 742 Td", "14 TL"]
        for line in page_lines:
            body_lines.append(f"({_pdf_escape(line)}) Tj")
            body_lines.append("T*")
        body_lines.append("ET")
        stream = "\n".join(body_lines).encode("latin-1", "replace")
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj_id, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode(
            "ascii"
        )
    )
    return bytes(out), len(pages)


class DocSetSectionTool:
    definition = ToolDef(
        name="doc_set_section",
        description=(
            "Create or replace one schema-validated section of a document/report. "
            "Writes only .disco/parts/<section>.md; the host assembles the final "
            "document during doc_export."
        ),
        args_model=DocSetSectionArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: DocSetSectionArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.sandbox is None:
            return ToolOutcome(success=False, content="No sandbox available.", error="NO_SANDBOX")
        try:
            payload, part = _serialize_part(args)
            path = _part_path(part.meta.section)
            await ctx.sandbox.write_file(path, payload.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(
                success=False,
                content=f"Failed to write document section: {exc}",
                error=str(exc),
            )
        return ToolOutcome(
            success=True,
            content=f"Document section '{part.meta.section}' written to {path}.",
            artifacts=[path],
            structured={
                "kind": _PART_KIND,
                "path": path,
                "section": part.meta.section,
                "title": part.meta.title,
                "order": part.meta.order,
            },
        )


class DocExportTool:
    definition = ToolDef(
        name="doc_export",
        description=(
            "Host-assemble document/report parts from .disco/parts, write report.md, "
            "render the requested export, and stamp ExportRenderFacts for the finish gate."
        ),
        args_model=DocExportArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: DocExportArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.sandbox is None:
            return ToolOutcome(success=False, content="No sandbox available.", error="NO_SANDBOX")
        try:
            # preflight: enumerate and schema-validate all authored parts.
            parts = await _read_parts(ctx)
            # bundle: assemble a canonical markdown source from validated parts.
            markdown = _assemble_markdown(args.title, parts)
            source_name = f"{args.filename}.md"
            await ctx.sandbox.write_file(source_name, markdown.encode("utf-8"))
            if args.format == "html":
                out_name = f"{args.filename}.html"
                rendered = _markdown_to_html(args.title, markdown).encode("utf-8")
                declared_units = 1
                fmt = "html"
            else:
                out_name = f"{args.filename}.pdf"
                rendered, declared_units = _render_pdf(markdown)
                fmt = "pdf"
            await ctx.sandbox.write_file(out_name, rendered)
            # validate: read back the actual persisted export and stamp P10 facts.
            data = await ctx.sandbox.read_file(out_name)
            if isinstance(data, str):
                data = data.encode("utf-8")
            facts = check_export_render(
                fmt,
                data,
                text=markdown,
                declared_units=declared_units,
                declared_exact=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(
                success=False,
                content=f"Document export failed: {exc}",
                error=str(exc),
            )

        artifacts = [out_name, source_name]
        return ToolOutcome(
            success=True,
            content=(
                f"Document exported to {out_name} from {len(parts)} validated part(s). "
                f"P10 render validation: {facts.detail}."
            ),
            artifacts=artifacts,
            structured={
                "kind": _PART_KIND,
                "filename": out_name,
                "source": source_name,
                "format": fmt,
                "section_count": len(parts),
                "pipeline_stages": ["preflight", "bundle", "validate", "deliver"],
                EXPORT_RENDER_KEY: facts.model_dump(mode="json"),
            },
        )
