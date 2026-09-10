"""The GET `/api/leads` + `/admin` guard-first cluster that `worker_inspect.
inspect_worker` delegates to: whether each read route early-returns a denial via
the auth guard BEFORE any read, and whether `isAuthorized` actually validates the
bearer token / fails closed. Extracted verbatim (same regexes, same nested
`_admin_denial_ok` closure) so `inspect_worker` itself stays a thin orchestrator.
`reads_require_auth` (which also folds in `bearer_checked`) is left for the
caller to combine, since `inspect_worker` needs the three inputs individually for
its per-leg failure reasons too.

Calls back into a handful of pure ``worker_inspect`` helpers (`_isauthorized_body`,
`_read_block_gated`, `_returns_401`, `_admin_login_returns_401`). That import is
performed INSIDE the function body (never at this module's top level) so the
reference is re-resolved on every call — if a test ever monkeypatches one of
those names on `worker_inspect`, this caller observes the patch exactly as a
caller still living in `worker_inspect.py` would.
"""

from __future__ import annotations

import re


def _auth_guard_signals(
    src: str, get_block: str | None, admin_block: str | None
) -> tuple[bool, bool, bool, bool]:
    """The GET /api/leads + /admin guard-first checks plus the isAuthorized
    bearer-token + fail-closed checks. Returns ``(leads_get_guarded,
    admin_guarded, bearer_checked, fail_closed)``."""
    from disco.core.appkit.worker_inspect import (
        _admin_login_returns_401,
        _isauthorized_body,
        _read_block_gated,
        _returns_401,
    )

    auth_body = _isauthorized_body(src)
    bearer_checked = auth_body is not None and bool(
        re.search(r'"Bearer "', auth_body)
        and re.search(r"\.startsWith\s*\(", auth_body)
        and re.search(r"===\s*expected\b", auth_body)
    )
    # Fail-closed: the auth check denies when ADMIN_TOKEN (env → `expected`) is unset.
    fail_closed = auth_body is not None and bool(
        re.search(r"=\s*env\.ADMIN_TOKEN", auth_body)
        and re.search(r"if\s*\(\s*!\s*expected\s*\)\s*return\s+false", auth_body)
    )

    def _admin_denial_ok(guard_body: str) -> bool:
        if "adminLoginPage" in guard_body:
            return _admin_login_returns_401(src)
        return _returns_401(guard_body)

    leads_get_guarded = get_block is not None and _read_block_gated(get_block, _returns_401)
    admin_guarded = admin_block is not None and _read_block_gated(admin_block, _admin_denial_ok)
    return leads_get_guarded, admin_guarded, bearer_checked, fail_closed
