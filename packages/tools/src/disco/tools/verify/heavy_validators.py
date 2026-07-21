"""Heavy (VM-only / nightly-tier) artifact validators.

These RENDER the artifact — LibreOffice for decks, poppler for PDFs, ffmpeg for audio —
rather than only reading metadata, so they catch corruption a mime/format check misses (a
.pptx that unzips fine but won't open, a PDF whose pages are blank). They need binaries
(`soffice`, `pdftoppm`, `ffprobe`) that live on the VM 201 evidence host, NOT the PR gate —
so each returns a single "tool unavailable" note when its binary is missing rather than
raising, and the suite is wired into the nightly (VM) tier in W17.
"""

from __future__ import annotations

import pathlib
import posixpath
import shutil
import struct
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
import zipfile
import zlib
from typing import IO

# The OPC package-relationships namespace (ECMA-376 Part 2).
_OPC_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"

# Resource ceilings for the structural pre-render pass.  These are intentionally far
# above ordinary presentation packages while ensuring that CRC validation and XML
# parsing never become an unbounded decompression step on attacker-controlled input.
_MAX_OPC_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_OPC_MEMBERS = 10_000
_MAX_OPC_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_OPC_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_OPC_COMPRESSION_RATIO = 1_000
_MAX_OPC_RELATIONSHIP_BYTES = 4 * 1024 * 1024
_MAX_OPC_RELATIONSHIPS = 10_000
_MAX_OPC_TARGET_CHARS = 2_048
_MAX_OPC_NAME_CHARS = 1_024
_MAX_OPC_PROBLEMS = 100
_ZIP_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_ZIP_LOCAL_SIGNATURE = b"PK\x03\x04"


def _missing(tool: str) -> bool:
    return shutil.which(tool) is None


class _ForbiddenXmlDeclaration(Exception):
    pass


def _relationship_xml_root(data: bytes) -> ET.Element:
    """Parse relationship XML only after an encoding-aware DTD/entity rejection.

    Byte searches miss UTF-16/UTF-32 declarations and can reject harmless comment text.
    Expat recognizes the XML encoding and reports declarations structurally; aborting its
    declaration handlers prevents expansion before ElementTree sees the same bytes.
    """
    guard = expat.ParserCreate()

    def reject(*_args: object) -> None:
        raise _ForbiddenXmlDeclaration

    def reject_external(*_args: object) -> int:
        raise _ForbiddenXmlDeclaration

    guard.StartDoctypeDeclHandler = reject
    guard.EntityDeclHandler = reject
    guard.ExternalEntityRefHandler = reject_external
    guard.Parse(data, True)
    return ET.fromstring(data)


def _member_stream_problem(handle: IO[bytes], member: zipfile.ZipInfo) -> str | None:
    """Validate the real local compressed stream under strict output bounds.

    ``ZipInfo.file_size`` alone is not proof: a forged central/local header can claim
    one output byte for a deflate stream that actually expands to megabytes, and
    ``ZipExtFile`` then stops at the forged size.  Reading the raw stream through a
    bounded decompressor proves it reaches EOF after exactly the declared number of
    bytes.  PPTX/OPC uses stored or deflated members; other methods fail closed.
    """
    if member.flag_bits & 0x1:
        return f"pptx member {member.filename} is encrypted and cannot be safely inspected"
    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        return (
            f"pptx member {member.filename} uses unsupported compression method "
            f"{member.compress_type}"
        )
    handle.seek(member.header_offset)
    raw_header = handle.read(_ZIP_LOCAL_HEADER.size)
    if len(raw_header) != _ZIP_LOCAL_HEADER.size:
        return f"pptx member {member.filename} has a truncated local header"
    (
        signature,
        _version,
        local_flags,
        local_method,
        _mtime,
        _mdate,
        local_crc,
        local_compressed,
        local_size,
        name_size,
        extra_size,
    ) = _ZIP_LOCAL_HEADER.unpack(raw_header)
    if (
        signature != _ZIP_LOCAL_SIGNATURE
        or local_method != member.compress_type
        or local_flags != member.flag_bits
    ):
        return f"pptx member {member.filename} has inconsistent local metadata"
    local_name = handle.read(name_size)
    local_extra = handle.read(extra_size)
    if len(local_name) != name_size or len(local_extra) != extra_size:
        return f"pptx member {member.filename} has truncated local name/extra metadata"
    name_encoding = "utf-8" if local_flags & 0x0800 else "cp437"
    try:
        expected_local_name = member.orig_filename.encode(name_encoding)
    except UnicodeEncodeError:
        return f"pptx member {member.filename} has an invalid local filename encoding"
    if local_name != expected_local_name:
        return f"pptx member {member.filename} disagrees on its local filename"
    if not local_flags & 0x08:
        actual_local_size = local_size
        actual_local_compressed = local_compressed
        if local_size == 0xFFFFFFFF or local_compressed == 0xFFFFFFFF:
            offset = 0
            zip64: bytes | None = None
            while offset + 4 <= len(local_extra):
                field_id, field_size = struct.unpack_from("<HH", local_extra, offset)
                offset += 4
                field = local_extra[offset : offset + field_size]
                offset += field_size
                if field_id == 0x0001:
                    zip64 = field
                    break
            if zip64 is None:
                return f"pptx member {member.filename} has no ZIP64 size metadata"
            zip64_offset = 0
            if local_size == 0xFFFFFFFF:
                if len(zip64) < zip64_offset + 8:
                    return f"pptx member {member.filename} has truncated ZIP64 metadata"
                actual_local_size = struct.unpack_from("<Q", zip64, zip64_offset)[0]
                zip64_offset += 8
            if local_compressed == 0xFFFFFFFF:
                if len(zip64) < zip64_offset + 8:
                    return f"pptx member {member.filename} has truncated ZIP64 metadata"
                actual_local_compressed = struct.unpack_from("<Q", zip64, zip64_offset)[0]
        if (
            local_crc != member.CRC
            or actual_local_compressed != member.compress_size
            or actual_local_size != member.file_size
        ):
            return f"pptx member {member.filename} disagrees between local and central metadata"

    remaining = member.compress_size
    produced = 0
    crc = 0
    if member.compress_type == zipfile.ZIP_STORED and member.compress_size != member.file_size:
        return f"pptx stored member {member.filename} has inconsistent compressed size"
    decompressor = zlib.decompressobj(-15) if member.compress_type == zipfile.ZIP_DEFLATED else None
    while remaining:
        chunk = handle.read(min(64 * 1024, remaining))
        if not chunk:
            return f"pptx member {member.filename} has a truncated compressed stream"
        remaining -= len(chunk)
        if decompressor is None:
            produced += len(chunk)
            crc = zlib.crc32(chunk, crc)
        else:
            pending = chunk
            while pending:
                data = decompressor.decompress(pending, member.file_size + 1 - produced)
                produced += len(data)
                crc = zlib.crc32(data, crc)
                if produced > member.file_size:
                    return f"pptx member {member.filename} expands beyond its declared size"
                pending = decompressor.unconsumed_tail
                if pending and produced >= member.file_size + 1:
                    return f"pptx member {member.filename} expands beyond its declared size"
    if decompressor is not None:
        tail = decompressor.flush(member.file_size + 1 - produced)
        produced += len(tail)
        crc = zlib.crc32(tail, crc)
        if (
            produced != member.file_size
            or not decompressor.eof
            or bool(decompressor.unused_data)
            or bool(decompressor.unconsumed_tail)
        ):
            return f"pptx member {member.filename} has inconsistent deflate stream boundaries"
    elif produced != member.file_size:
        return f"pptx member {member.filename} has inconsistent stored stream boundaries"
    if (crc & 0xFFFFFFFF) != member.CRC:
        return f"pptx member {member.filename} failed its CRC check"
    return None


def _opc_integrity_problems(path: str) -> list[str]:
    """Deterministic OOXML/OPC structural pre-check (C9-01 defect-1 correction): a
    ``.pptx`` must be a valid ZIP whose internal relationship targets all resolve to
    parts that actually exist in the package.

    This catches two corruption classes the RENDER step alone cannot, because
    LibreOffice silently RECOVERS them: ZIP-invalid bytes, and a valid ZIP whose slide
    (or any part) references a missing part via a ``_rels/*.rels`` entry. It is a
    bounded relationship-integrity walk over the package's own manifest — NOT a general
    OOXML parser and NOT a render. External relationships (``TargetMode="External"``)
    are skipped; every internal target is resolved (POSIX, ``..``-aware, root-relative
    when absolute) and required to be present in the zip namelist."""
    try:
        archive_size = pathlib.Path(path).stat().st_size
        if archive_size > _MAX_OPC_ARCHIVE_BYTES:
            return [
                "pptx archive exceeds the structural-validation size limit "
                f"({_MAX_OPC_ARCHIVE_BYTES} bytes)"
            ]
        with zipfile.ZipFile(path) as zf:
            members = zf.infolist()
            if len(members) > _MAX_OPC_MEMBERS:
                return [
                    "pptx archive has too many members for structural validation "
                    f"({len(members)} > {_MAX_OPC_MEMBERS})"
                ]
            member_names = [member.filename for member in members]
            if any(len(name) > _MAX_OPC_NAME_CHARS for name in member_names):
                return [
                    "pptx archive contains a member name longer than the structural-validation "
                    f"limit ({_MAX_OPC_NAME_CHARS} characters)"
                ]
            if len(set(member_names)) != len(member_names):
                return ["pptx archive contains duplicate part names"]
            total_size = 0
            for member in members:
                if member.file_size > _MAX_OPC_MEMBER_BYTES:
                    return [
                        f"pptx member {member.filename} exceeds the uncompressed-size "
                        f"limit ({member.file_size} > {_MAX_OPC_MEMBER_BYTES})"
                    ]
                total_size += member.file_size
                if total_size > _MAX_OPC_TOTAL_BYTES:
                    return [
                        "pptx archive exceeds the total uncompressed-size limit "
                        f"({_MAX_OPC_TOTAL_BYTES} bytes)"
                    ]
                if (
                    member.file_size > 0
                    and member.file_size / max(member.compress_size, 1) > _MAX_OPC_COMPRESSION_RATIO
                ):
                    return [
                        f"pptx member {member.filename} exceeds the compression-ratio "
                        f"limit ({_MAX_OPC_COMPRESSION_RATIO}:1)"
                    ]
            if zf.fp is None:
                return ["pptx archive closed before structural validation"]
            for member in members:
                stream_problem = _member_stream_problem(zf.fp, member)
                if stream_problem is not None:
                    return [stream_problem]
            names = set(member_names)
            problems: list[str] = []

            def add_problem(message: str) -> bool:
                """Append under the global diagnostic cap; return true when full.

                Reserve the last slot for a stable truncation marker so hostile packages
                cannot produce an unbounded result while operators still learn that more
                defects were intentionally omitted.
                """
                if len(problems) < _MAX_OPC_PROBLEMS - 1:
                    problems.append(message)
                    return False
                if len(problems) < _MAX_OPC_PROBLEMS:
                    problems.append("pptx has additional structural problems")
                return True

            relationship_count = 0
            for rels_name in sorted(n for n in names if n.endswith(".rels") and "_rels/" in n):
                base = rels_name.rsplit("_rels/", 1)[0].rstrip("/")
                rel_info = zf.getinfo(rels_name)
                if rel_info.file_size > _MAX_OPC_RELATIONSHIP_BYTES:
                    if add_problem(
                        f"pptx relationship part {rels_name} exceeds the XML size limit "
                        f"({_MAX_OPC_RELATIONSHIP_BYTES} bytes)"
                    ):
                        return problems
                    continue
                try:
                    relationship_xml = zf.read(rels_name)
                    root = _relationship_xml_root(relationship_xml)
                except _ForbiddenXmlDeclaration:
                    if add_problem(
                        f"pptx relationship part {rels_name} contains a forbidden DTD/entity"
                    ):
                        return problems
                    continue
                except (ET.ParseError, expat.ExpatError):
                    if add_problem(f"pptx relationship part {rels_name} is not valid XML"):
                        return problems
                    continue
                for rel in root.iter(_OPC_REL_NS):
                    relationship_count += 1
                    if relationship_count > _MAX_OPC_RELATIONSHIPS:
                        return [
                            "pptx archive has too many relationships for structural validation "
                            f"(>{_MAX_OPC_RELATIONSHIPS})"
                        ]
                    if (rel.get("TargetMode") or "Internal") == "External":
                        continue
                    target = rel.get("Target") or ""
                    if len(target) > _MAX_OPC_TARGET_CHARS:
                        if add_problem(
                            f"pptx relationship part {rels_name} has a target longer than the "
                            f"structural-validation limit ({_MAX_OPC_TARGET_CHARS} characters)"
                        ):
                            return problems
                        continue
                    resolved = (
                        target.lstrip("/")
                        if target.startswith("/")
                        else posixpath.normpath(posixpath.join(base, target))
                    )
                    if resolved not in names:
                        if add_problem(
                            f"pptx references a missing part: {rels_name} -> {target} "
                            f"(resolved {resolved})"
                        ):
                            return problems
            return problems
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        return ["pptx is not a valid zip archive (not an OOXML package)"]
    except (NotImplementedError, RuntimeError, ValueError, zlib.error) as exc:
        return [f"pptx archive could not be safely inspected: {exc}"]
    except OSError as exc:
        return [f"pptx could not be read: {exc}"]


def validate_pptx_renders(path: str, *, workdir: str | None = None) -> list[str]:
    """Render a ``.pptx`` to PDF with headless LibreOffice — proves the deck actually opens
    and lays out AS A PRESENTATION, then reuses the cheap PDF checks on the result.

    Hardened for the C9-01 findings (2026-07-17):

    * A deterministic OPC relationship-integrity pre-check (``_opc_integrity_problems``)
      runs FIRST: ZIP-invalid bytes and a valid ZIP whose parts reference a missing part
      are rejected up front, because LibreOffice silently RECOVERS both and renders them
      (independent verification demonstrated a slide pointing at a deleted slideLayout
      rendering clean).
    * Beyond OPC integrity, the package must contain the core presentation parts
      (``[Content_Types].xml``, ``ppt/presentation.xml``) — a valid zip that is not a
      presentation package is rejected before, and independently of, the renderer.
    * The IMPORT filter is pinned to Impress's pptx filter and the EXPORT filter to
      ``impress_pdf_Export`` — corrupt bytes can no longer succeed through LibreOffice's
      generic import fallback (which happily renders garbage as a text document).
    * Every invocation gets an ISOLATED LibreOffice user profile
      (``-env:UserInstallation``), so a concurrently running soffice instance can never
      absorb the request and return without producing our output.
    * The output directory is FRESH and unique per invocation, and the expected PDF must
      exist there afterwards. LibreOffice exits 0 on "source file could not be loaded",
      so the exit code alone is NEVER trusted as success.
    """
    # The structural OPC pre-check needs no renderer, so it runs FIRST — corruption is
    # detected identically on the PR gate and the VM host, and a corrupt package never
    # depends on LibreOffice being present to be rejected.
    structural = _opc_integrity_problems(path)
    if structural:
        return structural
    # LibreOffice is deliberately permissive: corrupt/plaintext bytes carrying a .pptx
    # suffix can be opened via its generic import fallback (e.g. as a Writer document)
    # and exported with exit code 0. Beyond zip/OPC integrity, the package must
    # actually BE a presentation — and that rejection must not depend on the renderer
    # either, so it also precedes the soffice guard (callers rightly filter the
    # unavailable-tool note, which must never hide a corrupt package). The CRC/
    # decompression proof this check once forced by reading the required parts is
    # subsumed by the bounded per-member stream validation above.
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return [f"corrupt pptx: {exc}"]
    if not {"[Content_Types].xml", "ppt/presentation.xml"} <= names:
        return ["corrupt pptx: required presentation package parts are missing"]
    if _missing("soffice"):
        return ["soffice unavailable — run this heavy validator on the VM 201 evidence host"]
    # `workdir` (documented optional) may be relative; resolve it before building the
    # profile file URI, which requires an absolute path (C9-01 defect-2 correction).
    work = (
        pathlib.Path(workdir).resolve()
        if workdir
        else pathlib.Path(tempfile.mkdtemp(prefix="deckrender-"))
    )
    invocation = work / f"render-{uuid.uuid4().hex}"
    outdir = invocation / "out"
    profile = invocation / "profile"
    outdir.mkdir(parents=True)
    profile.mkdir(parents=True)
    proc = subprocess.run(
        [
            "soffice",
            "--headless",
            f"-env:UserInstallation={profile.as_uri()}",
            "--infilter=Impress MS PowerPoint 2007 XML",
            "--convert-to",
            "pdf:impress_pdf_Export",
            "--outdir",
            str(outdir),
            path,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return [f"soffice convert failed: {proc.stderr.strip()[:200]}"]
    pdf = outdir / (pathlib.Path(path).stem + ".pdf")
    if not pdf.exists() or pdf.stat().st_size == 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:200]
        return [
            "pptx did not render to a pdf (deck does not open as a presentation"
            + (f": {detail}" if detail else "")
            + ")"
        ]
    from disco.tools.verify.artifact_validators import validate_pdf

    # A deck made entirely of images/shapes (and even a deliberately blank
    # template) may have no extractable PDF text. That is valid for PPTX; retain
    # the structural page/open checks while leaving visual non-blank assertions
    # to deck-specific fixture tests.
    return [
        problem for problem in validate_pdf(str(pdf)) if problem != "pdf has no extractable text"
    ]


def validate_pdf_renders(path: str, *, workdir: str | None = None) -> list[str]:
    """Rasterize page 1 of a PDF with poppler — proves the page is non-blank (real content,
    not an empty/black page)."""
    if _missing("pdftoppm"):
        return ["pdftoppm unavailable — run this heavy validator on the VM 201 evidence host"]
    work = workdir or tempfile.mkdtemp(prefix="pdfrender-")
    out = pathlib.Path(work) / "page"
    proc = subprocess.run(
        ["pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "50", path, str(out)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return [f"pdftoppm failed: {proc.stderr.strip()[:200]}"]
    pngs = list(pathlib.Path(work).glob("page*.png"))
    if not pngs:
        return ["pdf did not rasterize to an image"]
    try:
        from PIL import Image

        img = Image.open(pngs[0]).convert("L")
        extrema = img.getextrema()  # (min, max) luminance
        if extrema[0] == extrema[1]:
            return ["pdf page 1 is uniformly blank"]
    except ImportError:
        pass  # Pillow optional; the render itself succeeding is already signal
    return []
