"""Selector/verifier byte generation and digesting for host-service tokens."""

from __future__ import annotations

import base64
import hashlib
import secrets


def _random_component(byte_count: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _digest(verifier: str) -> bytes:
    return hashlib.sha256(verifier.encode("ascii")).digest()
