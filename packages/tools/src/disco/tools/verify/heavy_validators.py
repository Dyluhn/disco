"""Heavy (VM-only / nightly-tier) artifact validators — compatibility facade.

These RENDER the artifact — LibreOffice for decks, poppler for PDFs, ffmpeg for audio —
rather than only reading metadata, so they catch corruption a mime/format check misses (a
.pptx that unzips fine but won't open, a PDF whose pages are blank). They need binaries
(`soffice`, `pdftoppm`, `ffprobe`) that live on the VM 201 evidence host, NOT the PR gate —
so each returns a single "tool unavailable" note when its binary is missing rather than
raising, and the suite is wired into the nightly (VM) tier in W17.

The cohesive implementation lives in the private ``verification_validators`` package
(OPC structural pre-check, pptx render, pdf render). This module is a state-free
compatibility facade that re-exports the public names so existing importers (the
agent-server verifier runner, tests) keep working unchanged.

Tools are evidence producers, not verdict authorities: the output here is immutable
evidence (a list of problem strings) bound for later host verification. It never
manufactures or upgrades a typed ``HostVerificationResult``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: F401 — re-exported (tests monkeypatch heavy_validators.ET)

from .verification_validators._opc_integrity import (  # noqa: F401 — re-exported
    _MAX_OPC_ARCHIVE_BYTES,
    _MAX_OPC_COMPRESSION_RATIO,
    _MAX_OPC_MEMBER_BYTES,
    _MAX_OPC_MEMBERS,
    _MAX_OPC_NAME_CHARS,
    _MAX_OPC_PROBLEMS,
    _MAX_OPC_RELATIONSHIP_BYTES,
    _MAX_OPC_RELATIONSHIPS,
    _MAX_OPC_TARGET_CHARS,
    _MAX_OPC_TOTAL_BYTES,
    _OPC_REL_NS,
    _ZIP_LOCAL_HEADER,
    _ZIP_LOCAL_SIGNATURE,
    _ForbiddenXmlDeclaration,
    _member_stream_problem,
    _OpcLimits,
    _relationship_xml_root,
)
from .verification_validators._opc_integrity import (
    _opc_integrity_problems as _scan_opc_integrity,
)
from .verification_validators._pdf_render import validate_pdf_renders
from .verification_validators._pptx_render import (
    validate_pptx_renders as _validate_pptx_renders,
)


def _current_opc_limits() -> _OpcLimits:
    return _OpcLimits(
        archive_bytes=_MAX_OPC_ARCHIVE_BYTES,
        members=_MAX_OPC_MEMBERS,
        member_bytes=_MAX_OPC_MEMBER_BYTES,
        total_bytes=_MAX_OPC_TOTAL_BYTES,
        compression_ratio=_MAX_OPC_COMPRESSION_RATIO,
        relationship_bytes=_MAX_OPC_RELATIONSHIP_BYTES,
        relationships=_MAX_OPC_RELATIONSHIPS,
        target_chars=_MAX_OPC_TARGET_CHARS,
        name_chars=_MAX_OPC_NAME_CHARS,
        problems=_MAX_OPC_PROBLEMS,
    )


def _opc_integrity_problems(path: str) -> list[str]:
    return _scan_opc_integrity(path, limits=_current_opc_limits())


def validate_pptx_renders(
    path: str, *, workdir: str | None = None
) -> list[str]:
    return _validate_pptx_renders(
        path,
        workdir=workdir,
        opc_integrity=_opc_integrity_problems,
    )

__all__ = [
    "validate_pdf_renders",
    "validate_pptx_renders",
]
