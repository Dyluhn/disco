"""Signed operator approvals for host-side configured HTTP origins.

The main router config is model/agent-facing data, so it cannot be its own trust
root. This store is a separate operator artifact whose contents only count when
the HMAC verifies under the same master secret used by SecretStore.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .host_egress import origin_for_url

if TYPE_CHECKING:
    from .llm.secrets import SecretStore

_LOG = logging.getLogger(__name__)
# Warn at most once per ledger path per process about a non-empty-but-unverifiable
# ledger — verified() is called per-request, so an unconditional log would spam.
_WARNED_UNVERIFIABLE_PATHS: set[str] = set()

ApprovalChecker = Callable[[str, str, str | None], bool]

_ENV_APPROVALS = "DISCO_APPROVALS"
_ENV_APPROVALS_LEGACY = "PMX_APPROVALS"
_ENV_SECRET = "DISCO_SECRET_KEY"
_ENV_SECRET_LEGACY = "PMX_SECRET_KEY"
_VERSION = 1


@dataclass(frozen=True, order=True)
class OriginApproval:
    origin: str
    purpose: str
    secret_ref: str = ""


def default_approvals_path(config_path: str | os.PathLike[str] | None = None) -> Path:
    env_path = os.environ.get(_ENV_APPROVALS) or os.environ.get(_ENV_APPROVALS_LEGACY)
    if env_path:
        return Path(env_path)
    if config_path is not None:
        path = Path(config_path)
        return path.with_name("disco-approved-origins.json")
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(cfg) / "disco" / "approved-origins.json"


class OriginApprovalStore:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else default_approvals_path(config_path)
        self._secret_store = secret_store

    @property
    def path(self) -> Path:
        return self._path

    def verified(self) -> frozenset[OriginApproval]:
        raw = self._read()
        if not raw:
            return frozenset()
        entries = _normalize_entries(raw.get("approved_origins"))
        supplied = raw.get("hmac")
        if not isinstance(supplied, str):
            self._warn_unverifiable(entries, "ledger has no HMAC signature")
            return frozenset()
        expected = self._signature(entries)
        if expected is None or not hmac.compare_digest(supplied, expected):
            # SILENT-FAILURE GUARD: the ledger has operator approvals but they do NOT
            # verify under the current master secret (a DISCO_SECRET_KEY that differs
            # from the one that SIGNED the ledger — e.g. launched a different way, or a
            # rotated key). Failing closed is correct, but doing it SILENTLY disables
            # EVERY configured provider (image gen, OpenRouter routing, search keys)
            # with zero operator signal — a very expensive thing to diagnose. Surface it.
            reason = (
                "no master secret available"
                if expected is None
                else "HMAC does not verify under the current secret "
                "(the ledger was signed with a different DISCO_SECRET_KEY) — "
                "re-save Settings to re-approve these origins"
            )
            self._warn_unverifiable(entries, reason)
            return frozenset()
        return frozenset(entries)

    def _warn_unverifiable(self, entries: frozenset[OriginApproval], reason: str) -> None:
        """Warn ONCE per (path) per process when a NON-EMPTY approval ledger is being
        dropped — so silent 'all my providers stopped working' breakage is visible."""
        if not entries:
            return
        key = str(self._path)
        if key in _WARNED_UNVERIFIABLE_PATHS:
            return
        _WARNED_UNVERIFIABLE_PATHS.add(key)
        _LOG.warning(
            "origin-approval ledger %s has %d operator approval(s) but they are being "
            "IGNORED: %s. All configured provider origins (image gen / LLM routing / "
            "search) will read as unapproved until this is resolved.",
            key,
            len(entries),
            reason,
        )

    def is_approved(self, url: str, purpose: str, secret_ref: str | None = "") -> bool:
        origin = origin_for_url(url)
        if not origin:
            return False
        needle = OriginApproval(origin, _clean_purpose(purpose), _clean_ref(secret_ref))
        return needle in self.verified()

    def approved_origins(self) -> tuple[str, ...]:
        return tuple(sorted({entry.origin for entry in self.verified()}))

    def approve(self, url: str, purpose: str, secret_ref: str | None = "") -> None:
        origin = origin_for_url(url)
        if not origin:
            raise ValueError(f"cannot approve malformed HTTP origin for {url!r}")
        entries = set(self.verified())
        entries.add(OriginApproval(origin, _clean_purpose(purpose), _clean_ref(secret_ref)))
        self._write(entries)

    def approve_many(self, approvals: Iterable[OriginApproval]) -> None:
        entries = set(self.verified())
        for approval in approvals:
            origin = origin_for_url(approval.origin)
            if origin:
                entries.add(
                    OriginApproval(
                        origin,
                        _clean_purpose(approval.purpose),
                        _clean_ref(approval.secret_ref),
                    )
                )
        self._write(entries)

    def replace_purpose(
        self,
        url: str,
        purpose: str,
        secret_refs: Iterable[str | None] = ("",),
    ) -> None:
        """Replace every approval for one purpose with an exact current binding."""
        origin = origin_for_url(url)
        if not origin:
            raise ValueError(f"cannot approve malformed HTTP origin for {url!r}")
        clean_purpose = _clean_purpose(purpose)
        entries = {entry for entry in self.verified() if entry.purpose != clean_purpose}
        for secret_ref in secret_refs:
            entries.add(OriginApproval(origin, clean_purpose, _clean_ref(secret_ref)))
        self._write(entries)

    def revoke_purpose(self, purpose: str) -> int:
        """Remove every signed approval for one purpose and return the count."""
        clean_purpose = _clean_purpose(purpose)
        entries = set(self.verified())
        retained = {entry for entry in entries if entry.purpose != clean_purpose}
        removed = len(entries) - len(retained)
        if removed:
            self._write(retained)
        return removed

    def _signature(self, entries: Iterable[OriginApproval]) -> str | None:
        secret = _master_secret(self._secret_store)
        if not secret:
            return None
        payload = _payload(entries)
        digest = hmac.new(
            secret.encode("utf-8"),
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, entries: Iterable[OriginApproval]) -> None:
        signature = self._signature(entries)
        if signature is None:
            raise RuntimeError(f"{_ENV_SECRET} is not set - cannot sign origin approvals")
        data = _payload(entries)
        data["hmac"] = signature
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self._path.parent, 0o700)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)


def _payload(entries: Iterable[OriginApproval]) -> dict[str, Any]:
    approved = [
        {"origin": e.origin, "purpose": e.purpose, "secret_ref": e.secret_ref}
        for e in sorted(set(entries))
    ]
    return {"version": _VERSION, "approved_origins": approved}


def _normalize_entries(raw: object) -> frozenset[OriginApproval]:
    if not isinstance(raw, list):
        return frozenset()
    entries: set[OriginApproval] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        origin = origin_for_url(str(item.get("origin") or ""))
        purpose = _clean_purpose(item.get("purpose"))
        if origin and purpose:
            entries.add(OriginApproval(origin, purpose, _clean_ref(item.get("secret_ref"))))
    return frozenset(entries)


def _clean_purpose(value: object) -> str:
    return str(value or "").strip().lower()


def _clean_ref(value: object) -> str:
    return str(value or "").strip()


def _master_secret(secret_store: SecretStore | None) -> str | None:
    if secret_store is not None:
        secret = getattr(secret_store, "signing_secret", None)
        if isinstance(secret, str) and secret:
            return secret
    return os.environ.get(_ENV_SECRET) or os.environ.get(_ENV_SECRET_LEGACY)
