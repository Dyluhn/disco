"""SEC-10-B: server-controlled, SIGNED ownership records — the signing key, the
signature payload/computation, and the record-trust check.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged.

``read_workspace_file`` is reached through the ``deploy`` facade (a lazy,
function-local import) rather than a sibling module: it is DEFINED on
``deploy.py`` itself (see ``_workspace.py``'s docstring for why), and deploy.py
imports FROM this module at its own top level, so a module-level reverse import
here would be circular.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
from pathlib import Path
from typing import Literal

from disco.core.llm.secrets import SecretStore, WeakSecretError

from ..models import DeployPlan
from ._constants import _DEPLOY_RECORD_DIR, _OWNERSHIP_HMAC_KEY_SECRET


def _ownership_signing_key(store: SecretStore, *, create: bool) -> bytes | None:
    """The server-controlled HMAC key that signs/verifies deploy-ownership records
    (SEC-10-B). Persisted (encrypted) in the SecretStore, which lives OUTSIDE the
    workspace, so the untrusted build agent can neither READ it (to forge a signature)
    nor write it. Returns the raw key bytes, minting + persisting a fresh random key on
    first use when ``create`` is True (the WRITE path, where a real record is being
    signed). On the verify path (``create=False``) a missing key means no record can be
    trusted → returns None and every ownership check fails closed."""
    try:
        existing = store.get_secret(_OWNERSHIP_HMAC_KEY_SECRET)
    except WeakSecretError:
        return None
    if existing:
        try:
            return base64.urlsafe_b64decode(existing.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return None
    if not (create and store.can_store):
        return None
    key = _secrets.token_bytes(32)
    store.set_secret(_OWNERSHIP_HMAC_KEY_SECRET, base64.urlsafe_b64encode(key).decode("ascii"))
    return key


def _ownership_payload(
    *, account_id: str | None, worker_name: str, db_name: str, mutations: list[str]
) -> bytes:
    """The EXACT bytes the ownership signature covers: the account, BOTH target resource
    names, and the mutating legs that completed — so a valid signature proves a SPECIFIC
    (account, worker, db) tuple reached a real prior successful mutation and cannot be
    replayed against a different account/resource. ``mutations`` is sorted so the
    signature is order-independent."""
    return json.dumps(
        {
            "v": 1,
            "account_id": account_id or "",
            "worker_name": worker_name,
            "db_name": db_name,
            "mutations": sorted(mutations),
        },
        sort_keys=True,
    ).encode("utf-8")


def _ownership_signature(
    store: SecretStore,
    *,
    account_id: str | None,
    worker_name: str,
    db_name: str,
    mutations: list[str],
    create: bool,
) -> str | None:
    """HMAC-SHA256 over :func:`_ownership_payload`, keyed by the server-side signing key.
    Returns None when no signing key is available (the record is then written UNSIGNED and
    will authorize nothing on a later deploy — fail closed)."""
    key = _ownership_signing_key(store, create=create)
    if key is None:
        return None
    return hmac.new(
        key,
        _ownership_payload(
            account_id=account_id,
            worker_name=worker_name,
            db_name=db_name,
            mutations=mutations,
        ),
        hashlib.sha256,
    ).hexdigest()


def _proves_mutation(kind: Literal["worker", "d1"], mutations: list[str]) -> bool:
    """Whether *mutations* (from a candidate ownership record) reflects a real prior
    successful mutation of the target *kind* — the real deploy records a Worker
    publish as ``worker_deploy`` and a D1 creation as ``d1_create:<db_name>``
    (name-suffixed). A D1 we CREATED is a D1 we own."""
    if kind == "worker":
        return "worker_deploy" in mutations
    return any(m == "d1_create" or m.startswith("d1_create:") for m in mutations)


def _parse_ownership_fields(
    data: dict[str, object],
) -> tuple[str, list[str], str | None, str, str] | None:
    """Extract + type-validate the ownership-bearing fields from a candidate record's
    parsed JSON. Returns ``None`` when the record does not even have the right
    SHAPE (missing/mistyped signature, mutations list, worker/db name) — such a
    record can never verify and is skipped without a signature check."""
    sig = data.get("signature")
    mutations = data.get("mutations")
    rec_account = data.get("account_id")
    rec_worker = data.get("worker_name")
    rec_db = data.get("db_name")
    if not (isinstance(sig, str) and sig and isinstance(mutations, list)):
        return None
    if not (isinstance(rec_worker, str) and isinstance(rec_db, str)):
        return None
    str_mutations = [m for m in mutations if isinstance(m, str)]
    rec_account_str = rec_account if isinstance(rec_account, str) else None
    return sig, str_mutations, rec_account_str, rec_worker, rec_db


def _record_proves_ownership(
    child: Path,
    workspace_root: Path,
    key: bytes,
    plan: DeployPlan,
    kind: Literal["worker", "d1"],
    want_name: str,
) -> bool:
    """True iff *child* is a JSON record whose SIGNED (account, worker, db, mutations)
    fields verify against the server key AND bind to THIS deploy's account + target
    resource, AND reflect a real prior successful mutation of *kind*."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    if child.suffix != ".json":
        return False
    text = _deploy.read_workspace_file(child, workspace_root)
    if not text:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    parsed = _parse_ownership_fields(data)
    if parsed is None:
        return False
    sig, str_mutations, rec_account, rec_worker, rec_db = parsed
    expected = hmac.new(
        key,
        _ownership_payload(
            account_id=rec_account,
            worker_name=rec_worker,
            db_name=rec_db,
            mutations=str_mutations,
        ),
        hashlib.sha256,
    ).hexdigest()
    # SEC-10-B: a planted/forged/unsigned-by-us record fails here — only a record this
    # server actually signed survives the constant-time compare.
    if not hmac.compare_digest(sig, expected):
        return False
    # The signature is authentic; now the binding must match THIS deploy: same account,
    # same resource name, and the record must prove a REAL prior successful mutation.
    if rec_account != plan.account_id:
        return False
    rec_kind_name = rec_worker if kind == "worker" else rec_db
    if rec_kind_name != want_name:
        return False
    return _proves_mutation(kind, str_mutations)


def _has_ownership_record(
    workspace: Path, plan: DeployPlan, store: SecretStore, *, kind: Literal["worker", "d1"]
) -> bool:
    """SEC-10 / SEC-10-B: True iff a TRUSTWORTHY prior Disco deploy record under this
    workspace proves we own the target *kind* (``worker`` or ``d1``) — i.e. a previous
    deploy of THIS app actually created/published the SAME resource on the SAME account,
    and the record carries a VALID server-side HMAC signature.

    The record dir (``.disco``) is workspace-local and digest-SKIPPED, so the untrusted
    build agent can PLANT a JSON file there. A planted / unsigned / forged record is
    REJECTED (SEC-10-B): the signature is recomputed (with the server-controlled key the
    agent cannot read) over the record's OWN (account_id, worker_name, db_name, mutations)
    and must match via a constant-time compare. Then the record must additionally (a) bind
    the SAME account as this deploy, (b) name the SAME resource, and (c) reflect a real
    prior successful MUTATION of that resource — ``worker_deploy`` for a Worker, a
    ``d1_create``/``d1_create:<name>`` leg for a D1. Without ALL of that, a name collision
    is treated as an
    UNRELATED resource and adoption/overwrite is refused (never clobber someone else's).
    Reads route through the guarded reader so a planted escaping symlink fails closed."""
    key = _ownership_signing_key(store, create=False)
    if key is None:
        # No server signing key has ever been established → no record can be trusted.
        return False
    rec_dir = workspace / _DEPLOY_RECORD_DIR
    if not rec_dir.is_dir():
        return False
    workspace_root = workspace.resolve()
    try:
        children = sorted(rec_dir.iterdir())
    except OSError:
        return False
    want_name = plan.worker_name if kind == "worker" else plan.db_name
    return any(
        _record_proves_ownership(child, workspace_root, key, plan, kind, want_name)
        for child in children
    )
