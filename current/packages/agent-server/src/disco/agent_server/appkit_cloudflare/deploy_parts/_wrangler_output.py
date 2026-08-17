"""Strict parsing of ``wrangler`` JSON output (D1 list, deployments list), the
database-id substitution, and the plain (pre-record) abort-result constructors.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged.

``read_workspace_file``/``write_workspace_file`` are reached through the
``deploy`` facade (a lazy, function-local import) rather than a sibling module:
they are DEFINED on ``deploy.py`` itself (see ``_workspace.py``'s docstring for
why), and deploy.py imports FROM this module at its own top level, so a
module-level reverse import here would be circular.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import DeployExecutionResult, DeployPlan
from ..wrangler import BuildResult, CommandResult
from ._constants import _D1_ID_PLACEHOLDER, _UUID_RE, _WORKER_NOT_FOUND_RE


def _aborted(
    plan: DeployPlan, step: str, res: CommandResult | BuildResult, transcript: list[str]
) -> DeployExecutionResult:
    """A subprocess/build step failed — HALT the sequence BEFORE any further
    mutation. No deployment record is written (the deploy did not complete).
    Returns a clear failure result carrying the failed step + the (redacted)
    transcript so far."""
    return _aborted_msg(
        plan,
        step,
        transcript,
        f"step '{step}' failed (exit {res.returncode}); aborted before later steps.",
    )


def _aborted_msg(
    plan: DeployPlan, step: str, transcript: list[str], detail: str
) -> DeployExecutionResult:
    """An abort with an explicit detail message (e.g. an unresolved D1 id) rather
    than a subprocess exit code."""
    return DeployExecutionResult(
        executed=False,
        dry_run=False,
        plan=plan,
        transcript=transcript,
        succeeded=False,
        failed_step=step,
        error_detail=detail,
    )


def _validated_uuid(val: object) -> str | None:
    """Return *val* iff it is a string that is EXACTLY a UUID (fullmatch), else None.
    SEC-15: never accept a partial/garbage id as a database_id — a non-UUID is rejected
    so the wrangler.toml placeholder is never substituted with junk."""
    if not isinstance(val, str):
        return None
    s = val.strip()
    return s if _UUID_RE.fullmatch(s) else None


def _extract_database_id(create_stdout: str) -> str | None:
    """Pull the real D1 database_id (a UUID) out of ``wrangler d1 create`` output.
    Accepts ONLY a ``database_id``-labelled line whose value is a valid UUID — no bare
    first-UUID fallback (SEC-15: a stray UUID elsewhere in the output must not be
    mistaken for the database id)."""
    for line in create_stdout.splitlines():
        if "database_id" in line:
            m = _UUID_RE.search(line)
            if m and _validated_uuid(m.group(0)):
                return m.group(0)
    return None


class _ListParseError(Exception):
    """A SUCCESSFUL wrangler list command returned output that is NOT a JSON array
    (SEC-15/CORR-8). The caller must ABORT — never fall open to 'create a fresh DB' /
    'no existing Worker' off unparseable output."""


def _d1_entries_strict(list_stdout: str) -> list[dict]:
    """Parse ``wrangler d1 list --json`` to the list of entries, STRICTLY (SEC-15/CORR-8):
    a successful list whose stdout is not a JSON array raises :class:`_ListParseError` so
    the deploy ABORTS rather than falling open to creating a fresh DB. An empty array
    (``[]``) is a valid 'no databases' answer."""
    try:
        data = json.loads(list_stdout)
    except ValueError as exc:
        raise _ListParseError("d1 list output is not valid JSON") from exc
    if not isinstance(data, list):
        raise _ListParseError("d1 list output is not a JSON array")
    return [e for e in data if isinstance(e, dict)]


def _d1_entry_from_list(list_stdout: str, db_name: str) -> dict | None:
    """EXACT-parse ``wrangler d1 list --json`` and return the entry whose ``name``
    EQUALS *db_name* (SEC-15/CORR-8 — no substring match, no first-UUID fallback). Returns
    None when the database is absent. Raises :class:`_ListParseError` on a non-array body so
    the caller fails closed instead of treating malformed output as 'database absent'."""
    for entry in _d1_entries_strict(list_stdout):
        if entry.get("name") == db_name:
            return entry
    return None


def _worker_exists_from_deployments(stdout: str) -> bool:
    """SEC-10: from a SUCCESSFUL ``wrangler deployments list --name <worker> --json``,
    decide whether a Worker of that name already exists on the account. True iff the body
    is a NON-EMPTY JSON array of deployments. An empty array means 'no published
    deployments' (treat as absent → fresh deploy). A non-array body raises
    :class:`_ListParseError` so the caller ABORTS rather than overwriting a possibly
    unrelated Worker off unparseable output (fail closed)."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _ListParseError("deployments list output is not valid JSON") from exc
    if not isinstance(data, list):
        raise _ListParseError("deployments list output is not a JSON array")
    return len(data) > 0


def _preflight_signals_worker_absent(res: CommandResult) -> bool:
    """SEC-10-A: True ONLY when a NONZERO (errored) ``wrangler deployments list --name
    <w>`` carries a DOCUMENTED "script not found" signal (Cloudflare code 10007 /
    ``script_not_found`` / an explicit not-found phrase). That is the single nonzero
    outcome we trust to mean the Worker really does not exist (→ proceed to create). Any
    OTHER nonzero exit is ambiguous and must NOT be read as 'absent' — the caller fails
    closed (:class:`DeployRefused` ``WORKER_PREFLIGHT_FAILED``) instead of risking an
    overwrite."""
    blob = f"{res.stdout}\n{res.stderr}"
    return bool(_WORKER_NOT_FOUND_RE.search(blob))


def _database_id_from_list(list_stdout: str, db_name: str) -> str | None:
    """The validated (UUID) database_id for the EXACT-named *db_name* entry in
    ``wrangler d1 list --json``, or None if absent / not a valid UUID. No first-UUID
    fallback (SEC-15/CORR-8)."""
    entry = _d1_entry_from_list(list_stdout, db_name)
    if entry is None:
        return None
    return _validated_uuid(entry.get("uuid") or entry.get("database_id"))


def _substitute_database_id(workspace: Path, database_id: str) -> None:
    """Replace the ``REPLACE_WITH_D1_DATABASE_ID`` placeholder in wrangler.toml with
    the real *database_id* (P1-3), in the deploy working copy so ``wrangler deploy``
    binds D1. Idempotent: a re-deploy whose placeholder is already substituted is a
    no-op."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    workspace_root = workspace.resolve()
    # Guarded read (escaping symlink → WORKSPACE_SYMLINK_ESCAPE) BEFORE the write,
    # so we never dereference an escaping link nor write through it.
    current = _deploy.read_workspace_file(workspace / "wrangler.toml", workspace_root)
    if current is None or _D1_ID_PLACEHOLDER not in current:
        return
    # Guarded write: a wrangler.toml that is (or sits under) a symlinked component is
    # REFUSED here too — the write-back can never redirect through an in-workspace
    # symlink that resolve() would have masked.
    _deploy.write_workspace_file(
        "wrangler.toml", current.replace(_D1_ID_PLACEHOLDER, database_id), workspace
    )


def _extract_url(stdout: str) -> str | None:
    for token in stdout.split():
        if token.startswith("https://") and "workers.dev" in token:
            return token.rstrip(".,")
    return None
