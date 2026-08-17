"""The Cloudflare-export-readiness decision-cluster helpers that
`local_verify.cloudflare_export_ready` and `local_verify.cloudflare_export_ready_static`
delegate to. Both verifiers walk the same shape — deliverables present, wrangler.toml
parses, worker entry correct, [D1]/[assets] config correct, secret-safety contract
holds, OWNER_GUIDE.md documents the deploy steps — so the checks that are BYTE-IDENTICAL
between the two (deliverables-missing, TOML-parse, owner-guide-steps) are shared here as
a single function each; the checks that differ (main-entry match strictness, D1
presence-vs-absence, run_worker_first presence-vs-absence, the real-.dev.vars reason
text) are kept as separate functions per caller so each caller's exact reason strings
stay byte-unchanged.

Every function returns `None` for "this leg passed, continue" and a `CheckResult` for
"this leg failed, return it" — the caller (`cloudflare_export_ready(_static)`) chains
them in the exact original check order.

Calls back into a handful of pure ``local_verify`` names (`CheckResult`,
`main_points_at_worker_entry`, `_gitignore_ignores`, `_dev_vars_assignments`,
`_looks_like_real_secret`, and the private `_CF_*` constants). Those imports are
performed INSIDE each function body (never at this module's top level) so the
reference is re-resolved on every call — if a test ever monkeypatches one of those
names on `local_verify`, this caller observes the patch exactly as a caller still
living in `local_verify.py` would.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from disco.core.appkit.local_verify import CheckResult


def _missing_deliverables_result(
    files: Mapping[str, str | None], required: tuple[str, ...], name: str
) -> CheckResult | None:
    """Every deliverable in `required` must be present and non-empty."""
    from disco.core.appkit.local_verify import CheckResult

    missing = [p for p in required if not (files.get(p) or "").strip()]
    if missing:
        return CheckResult(
            name,
            False,
            "missing Cloudflare export deliverable(s): "
            f"{', '.join(missing)}. Re-run app_create to regenerate the export tree.",
        )
    return None


def _parse_wrangler_cfg(files: Mapping[str, str | None], name: str) -> dict[str, Any] | CheckResult:
    """Parse wrangler.toml. Returns the parsed config on success, or the failure
    CheckResult if the TOML does not parse."""
    import tomllib

    from disco.core.appkit.local_verify import CheckResult

    try:
        return tomllib.loads(files["wrangler.toml"] or "")
    except tomllib.TOMLDecodeError as exc:
        return CheckResult(name, False, f"wrangler.toml is not valid TOML: {exc}")


def _owner_guide_result(
    files: Mapping[str, str | None], required_steps: tuple[str, ...], name: str
) -> CheckResult | None:
    """OWNER_GUIDE.md must document every step in `required_steps`."""
    from disco.core.appkit.local_verify import CheckResult

    guide = files["OWNER_GUIDE.md"] or ""
    missing_steps = [s for s in required_steps if s not in guide]
    if missing_steps:
        return CheckResult(
            name,
            False,
            "OWNER_GUIDE.md does not document the required deploy step(s): "
            f"{', '.join(missing_steps)}.",
        )
    return None


# ---- lead-gen (cloudflare_export_ready) specific clusters ----------------------


def _wrangler_main_result(cfg: dict[str, Any], name: str) -> CheckResult | None:
    """The Worker entry `main` must point EXACTLY at the generated worker."""
    from disco.core.appkit.local_verify import (
        _CF_WORKER_ENTRY,
        CheckResult,
        main_points_at_worker_entry,
    )

    main = cfg.get("main")
    if not main_points_at_worker_entry(main):
        return CheckResult(
            name,
            False,
            "wrangler.toml Worker entry main does not EXACTLY name the worker "
            f'(main = "{_CF_WORKER_ENTRY}") — a non-canonical main (an absolute path, a '
            "'..' escape, or a look-alike dir like '....worker/index.ts') would deploy a "
            "DIFFERENT, unchecked Worker while /api/* + /admin never reach the generated "
            "one.",
        )
    return None


def _d1_binding_result(cfg: dict[str, Any], name: str) -> tuple[str | None, CheckResult | None]:
    """The [[d1_databases]] entry must bind "DB" and name a non-empty database_name.
    Returns (database_name, None) on success, or (None, failure)."""
    from disco.core.appkit.local_verify import CheckResult

    d1 = cfg.get("d1_databases")
    db_entry = (
        next(
            (b for b in d1 if isinstance(b, dict) and b.get("binding") == "DB"),
            None,
        )
        if isinstance(d1, list)
        else None
    )
    if db_entry is None:
        return None, CheckResult(
            name,
            False,
            'wrangler.toml has no [[d1_databases]] entry binding "DB" — the Worker '
            "cannot reach the leads database.",
        )
    db_database_name = db_entry.get("database_name")
    if not (isinstance(db_database_name, str) and db_database_name.strip()):
        return None, CheckResult(
            name,
            False,
            'wrangler.toml [[d1_databases]] binds "DB" but has no non-empty '
            "database_name — the Worker cannot target a database it can't name. "
            "Set database_name to the D1 database created with `wrangler d1 create`.",
        )
    return db_database_name, None


def _assets_result(cfg: dict[str, Any], name: str) -> CheckResult | None:
    """[assets] must bind ASSETS, declare the SPA not_found fallback, and route the
    lead API + admin worker-first."""
    from disco.core.appkit.local_verify import (
        _CF_SPA_NOT_FOUND,
        _CF_WORKER_FIRST_ROUTES,
        CheckResult,
    )

    assets = cfg.get("assets")
    if not (isinstance(assets, dict) and assets.get("binding") == "ASSETS"):
        return CheckResult(
            name,
            False,
            'wrangler.toml [assets] does not bind "ASSETS" for the built SPA.',
        )

    if assets.get("not_found_handling") != _CF_SPA_NOT_FOUND:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] does not set "
            f'not_found_handling = "{_CF_SPA_NOT_FOUND}" — client-side routes would '
            "404 instead of resolving to index.html.",
        )

    rwf = assets.get("run_worker_first")
    routes = set(rwf) if isinstance(rwf, list) else set()
    if rwf is not True and not set(_CF_WORKER_FIRST_ROUTES) <= routes:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] does not route "
            f"{', '.join(_CF_WORKER_FIRST_ROUTES)} worker-first "
            "(assets.run_worker_first) — the single-page-application asset layer would "
            "shadow the lead API + admin read-back with index.html.",
        )
    return None


def _secret_contract_result(files: Mapping[str, str | None], name: str) -> CheckResult | None:
    """The lead-gen secret-safety contract: no real .dev.vars in the export,
    .gitignore ignores it, .dev.vars.example declares ADMIN_TOKEN as a placeholder
    (never a real high-entropy secret, on ADMIN_TOKEN or any other key)."""
    from disco.core.appkit.local_verify import (
        _CF_GITIGNORE_SECRET_RULE,
        CheckResult,
        _dev_vars_assignments,
        _gitignore_ignores,
        _looks_like_real_secret,
    )

    real_secret = files.get(".dev.vars")
    if real_secret is not None and real_secret.strip():
        return CheckResult(
            name,
            False,
            "a real .dev.vars file is present in the export — secrets must never be "
            "generated or committed (only the .dev.vars.example template). Remove .dev.vars.",
        )

    gitignore = files.get(".gitignore") or ""
    if not _gitignore_ignores(gitignore, _CF_GITIGNORE_SECRET_RULE):
        return CheckResult(
            name,
            False,
            ".gitignore does not ignore the real `.dev.vars` secret file — a developer's "
            "local `.dev.vars` (with a real ADMIN_TOKEN) could be committed. Add a "
            "`.dev.vars` line to .gitignore.",
        )

    example = files[".dev.vars.example"] or ""
    assignments = _dev_vars_assignments(example)
    if not any(name == "ADMIN_TOKEN" for name, _ in assignments):
        return CheckResult(
            name,
            False,
            ".dev.vars.example does not declare ADMIN_TOKEN — the admin read-back "
            "secret has no documented local template.",
        )
    # Scan EVERY assignment's value (every occurrence of every key), not just the first
    # ADMIN_TOKEN: a real secret hidden after a placeholder line — or on any other KEY —
    # must never escape, regardless of position, order, or duplication.
    leaked_key = next((key for key, val in assignments if _looks_like_real_secret(val)), None)
    if leaked_key is not None:
        return CheckResult(
            name,
            False,
            f".dev.vars.example carries what looks like a REAL secret on {leaked_key}, not a "
            "placeholder — the template must ship only placeholders (e.g. "
            "ADMIN_TOKEN=replace-me). Replace it so no real secret is committed.",
        )
    return None


# ---- static (cloudflare_export_ready_static) specific clusters -----------------


def _static_main_result(cfg: dict[str, Any], name: str) -> CheckResult | None:
    """The static Worker entry `main` must point at the generated worker (the looser
    `lstrip("./")` match used for the static primitive — the lead-gen primitive requires
    the stricter exact-segment match, see `main_points_at_worker_entry`)."""
    from disco.core.appkit.local_verify import _CF_WORKER_ENTRY, CheckResult

    main = cfg.get("main")
    if not (isinstance(main, str) and main.lstrip("./") == _CF_WORKER_ENTRY):
        return CheckResult(
            name,
            False,
            "wrangler.toml Worker entry main does not point at the worker "
            f'(main = "{_CF_WORKER_ENTRY}") — the static-asset Worker would not serve.',
        )
    return None


def _static_d1_absence_result(cfg: dict[str, Any], name: str) -> CheckResult | None:
    """NEGATIVE assertion: a STATIC primitive has NO server data plane, so a D1 binding
    is a lead-gen leftover that must FAIL verify (not pass). A directory app that still
    carries `[[d1_databases]]` is a half-converted lead-gen tree."""
    from disco.core.appkit.local_verify import CheckResult

    if cfg.get("d1_databases"):
        return CheckResult(
            name,
            False,
            "wrangler.toml declares a [[d1_databases]] binding, but a STATIC directory "
            "site has no D1 data plane — this is a lead-gen leftover. Remove the "
            "[[d1_databases]] binding (a static primitive ships no database).",
        )
    return None


def _static_assets_result(cfg: dict[str, Any], name: str) -> CheckResult | None:
    """[assets] must bind ASSETS and declare the SPA not_found fallback (the static
    primitive has no worker-first routing to check — see
    `_static_worker_first_absence_result`)."""
    from disco.core.appkit.local_verify import _CF_SPA_NOT_FOUND, CheckResult

    assets = cfg.get("assets")
    if not (isinstance(assets, dict) and assets.get("binding") == "ASSETS"):
        return CheckResult(
            name,
            False,
            'wrangler.toml [assets] does not bind "ASSETS" for the built SPA.',
        )
    if assets.get("not_found_handling") != _CF_SPA_NOT_FOUND:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] does not set "
            f'not_found_handling = "{_CF_SPA_NOT_FOUND}" — client-side routes would '
            "404 instead of resolving to index.html.",
        )
    return None


def _static_worker_first_absence_result(cfg: dict[str, Any], name: str) -> CheckResult | None:
    """NEGATIVE assertion: a STATIC site serves everything from the asset layer — there
    is no Worker-first dynamic route. `assets.run_worker_first` is a lead-gen routing
    leftover (it shadows the asset layer with the Worker) and must FAIL verify."""
    from disco.core.appkit.local_verify import CheckResult

    assets = cfg.get("assets")
    # The caller's earlier [assets] gate already failed a non-dict; this restores
    # the narrowing that gate provided inline before the extraction.
    if not isinstance(assets, dict):
        return None
    if assets.get("run_worker_first") is not None:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] sets run_worker_first, but a STATIC directory site "
            "has no Worker-first dynamic route — this is a lead-gen routing leftover. "
            "Remove assets.run_worker_first so the asset layer serves everything.",
        )
    return None


def _static_schema_table_absence_result(
    files: Mapping[str, str | None], name: str
) -> CheckResult | None:
    """NEGATIVE assertion: the static schema.sql must be table-free. A `CREATE TABLE`
    DDL is a lead-gen leftover (the directory primitive persists nothing) and must FAIL
    verify — the placeholder schema.sql is comment-only."""
    from disco.core.appkit.local_verify import _CF_CREATE_TABLE_RE, CheckResult

    schema_sql = files.get("schema.sql") or ""
    if _CF_CREATE_TABLE_RE.search(schema_sql):
        return CheckResult(
            name,
            False,
            "schema.sql contains a CREATE TABLE statement, but a STATIC directory site "
            "persists nothing — this is a lead-gen schema leftover. The static "
            "schema.sql must be table-free (comment-only placeholder).",
        )
    return None


def _static_real_secret_result(files: Mapping[str, str | None], name: str) -> CheckResult | None:
    """Secret-safety still holds: a static site has no secret, so a real .dev.vars in
    the export is always a leak."""
    from disco.core.appkit.local_verify import CheckResult

    real_secret = files.get(".dev.vars")
    if real_secret is not None and real_secret.strip():
        return CheckResult(
            name,
            False,
            "a real .dev.vars file is present in the export — secrets must never be "
            "generated or committed. Remove .dev.vars.",
        )
    return None
