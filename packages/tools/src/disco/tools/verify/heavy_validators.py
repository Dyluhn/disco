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
    _opc_integrity_problems,
    _relationship_xml_root,
)
from .verification_validators._pdf_render import validate_pdf_renders
from .verification_validators._pptx_render import validate_pptx_renders

__all__ = [
    "validate_pdf_renders",
    "validate_pptx_renders",
]