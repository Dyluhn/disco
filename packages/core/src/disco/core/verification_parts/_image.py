"""Bounded image-data-URL validation — private helper for verification.

A magic prefix is not pixel evidence.  Pillow must parse and verify the
complete bounded image before host verification is allowed to retain it.
Extracted from ``verification.py`` to keep the public facade small; the public
entry point :func:`disco.core.verification.validated_image_data_url` re-exports
:func:`validated_image_data_url` from here unchanged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from io import BytesIO

from PIL import Image, UnidentifiedImageError

_MAX_VERIFIER_IMAGE_BYTES = 20_000_000
_MAX_VERIFIER_IMAGE_PIXELS = 50_000_000
_IMAGE_FORMAT_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def validated_image_data_url(value: str) -> tuple[str, str] | None:
    """Return the decoded image's authoritative media type and digest.

    A magic prefix is not pixel evidence.  Pillow must parse and verify the
    complete bounded image before host verification is allowed to retain it.
    """

    header, separator, payload = value.partition(",")
    if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
        return None
    declared_media_type = header[5:-7].lower()
    if declared_media_type not in set(_IMAGE_FORMAT_MEDIA_TYPES.values()):
        return None
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > _MAX_VERIFIER_IMAGE_BYTES:
        return None
    try:
        with Image.open(BytesIO(raw)) as image:
            actual_media_type = _IMAGE_FORMAT_MEDIA_TYPES.get(str(image.format or "").upper())
            width, height = image.size
            if (
                actual_media_type != declared_media_type
                or width < 1
                or height < 1
                or width * height > _MAX_VERIFIER_IMAGE_PIXELS
            ):
                return None
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return None
    return declared_media_type, hashlib.sha256(raw).hexdigest()