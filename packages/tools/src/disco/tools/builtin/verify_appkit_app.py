"""AppKit EPIC G — `verify_appkit_app`, the STRICT app verifier.

Where `verify_web_app` (W-45) answers "does the running web app render without
errors", `verify_appkit_app` answers the stronger AppKit question: "is this a
design-clean lead-gen app whose lead+admin contract is STRUCTURALLY correct —
presence + ordering + parameterization + guard-first". This is STRUCTURAL
verification of the generated source, NOT a proof that the app works at runtime:
runtime reachability / behavioural execution of the Worker is proved separately by
`packages/core/tests/test_workerd_persistence.py`, which runs the generated Worker
under local workerd/wrangler with real local D1 and a cold restart. Hosted
Cloudflare deploy remains an owner-gated action. It runs NINE checks against the
generated app in the workspace, each returning a PASS/FAIL with concrete evidence:

* ``design_lint_clean``   — `lint_design(workspace_tree, design_spec)` == 0 findings.
* ``schema_sql_valid``    — run the generated `schema.sql` in in-memory sqlite,
                            insert + read back a representative lead row, and prove
                            required columns reject NULL (pure, no deploy).
* ``drizzle_schema_valid`` — parse `src/db/schema.ts` and `schema.sql` and prove the
                            generated Drizzle table has the same column names +
                            notNull flags, and package.json declares drizzle-orm +
                            drizzle-kit.
* ``worker_contract``     — STRUCTURALLY inspect `worker/index.ts` (presence +
                            ordering + parameterization + guard-first; NOT live
                            execution — the local runtime proof is
                            packages/core/tests/test_workerd_persistence.py): the
                            public POST /api/leads
                            region CONTAINS a Drizzle insert in a
                            non-dead position; GET /api/leads AND /admin each
                            early-return 401 via the auth guard as the FIRST statement
                            BEFORE any read; isAuthorized Bearer-checks + fails closed
                            when ADMIN_TOKEN unset. A guard whose result is ignored, a
                            200-regardless block, a string-built insert, or an insert
                            after an early return / inside a dead branch FAILS (no
                            false-PASS on a broken/insecure worker).
* ``lead_form_posts``     — control-flow-inspect the form component(s): a REAL (non-
                            comment) `useSubmit("/api/leads")` call, generated
                            client/hook files with the single POST JSON fetch
                            chokepoint, optimistic add/remove state, and every lead
                            field actually BOUND to form state (value + onChange).
* ``local_api_roundtrip`` — a MODEL (driven by the structural flags above, NOT a
                            runtime execution of the worker) exercised against
                            in-memory sqlite: confirms that structure is internally
                            consistent (modelled POST inserts; unauth GET/admin → 401;
                            an authed read returns the row; ADMIN_TOKEN unset fails
                            closed). The local runtime proof is
                            packages/core/tests/test_workerd_persistence.py; hosted
                            Cloudflare deploy remains owner-gated.
* ``cloudflare_export_ready`` — Epic I deploy-export completeness: the export tree
                            carries the owner deliverables (OWNER_GUIDE.md + the
                            .dev.vars.example secret template), wrangler.toml binds DB +
                            ASSETS and routes /api/* + /admin worker-first (so the SPA
                            asset layer can't shadow them), no real .dev.vars secret
                            ships, and OWNER_GUIDE documents the deploy steps. Config +
                            doc completeness, NOT a runtime deploy proof.
* ``route_coverage``      — navigate every AppSpec page route (the W-45 browser path)
                            and require 2xx/3xx with no console/network errors.
* ``section_coverage``    — assert every AppSpec section's `data-appkit-section`
                            marker is present in the RENDERED DOM (same browser path).

The verdict is W-45-COMPATIBLE — top-level {ok/verdict/passed, summary,
next_action, url, failure_fingerprint, console/network} + an embedded
`verify_web_app` verdict for the structural part + a per-check breakdown — so the
finish gate's existing verdict-consumer (`_gate_verify_web_app`) drives it with no
new plumbing.

WO-A3 (primitive verify dispatch): the primitive-specific check bundles are no
longer hard-coded here — they live in core as the primitives' own
`PrimitiveDefinition.verify` hooks (`disco.core.appkit.primitive_verify` for the
lead-gen/directory ports, `hello_primitive.hello_verify` for hello), and `run()`
DISPATCHES on the resolved primitive. A primitive WITHOUT a verify hook (records —
on purpose) and a missing/unreadable app both fall back to `lead_gen_verify`,
preserving the pre-dispatch verdicts byte-for-byte. Applied-primitive provenance
records (`.disco/primitives/*.json`) are additionally ENFORCED fail-closed: each
record lands a `primitive_verify:<id>` check in the same verdict (a template_only
primitive with no verify harness FAILS), so the existing finish gate consumes them
with zero loop changes. The pure text/TS inspectors this tool used to define moved
VERBATIM to `disco.core.appkit.worker_inspect` (re-exported here for
compatibility).

Layering: tools → core is allowed. The PURE sqlite/spec logic (the local Worker/D1
shim) lives in `disco.core.appkit.local_verify` and the static TS inspectors in
`disco.core.appkit.worker_inspect` so core stays a leaf; the
browser/sandbox-dependent route+section coverage stays here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    APPSPEC_RELPATH,
    CF_EXPORT_FILES,
    DESIGNSPEC_RELPATH,
    STATIC_CF_EXPORT_FILES,
    generate,
    get_primitive,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
    resolve_primitive,
)
from disco.core.appkit.primitive_verify import lead_gen_verify
from disco.core.appkit.primitives import (
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    required_security_primitives,
)
from disco.core.appkit.spec import AppSpec, DesignSpec

# Compatibility re-exports (WO-A3): the pure static inspectors moved VERBATIM to
# core (`disco.core.appkit.worker_inspect`) so the primitives' verify hooks can run
# them; existing importers (tests, the deploy gate's lazy import) keep working
# through this module's public names.
from disco.core.appkit.worker_inspect import (
    WorkerAuthVerdict,
    inspect_directory_listing,
    inspect_lead_form,
    inspect_static_worker,
    inspect_submit_support,
    inspect_worker,
)
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..appkit_scope import APPKIT_CANONICAL_ENTRY_RELPATH
from ..behavior import declares
from ._outcomes import fail_outcome
from .browser import BrowserArgs, BrowserTool
from .design_lint import DesignLintArgs, DesignLintTool
from .verify_app import VerifyWebAppArgs, VerifyWebAppTool

_SCHEMA_RELPATH = "schema.sql"
_DRIZZLE_SCHEMA_RELPATH = "src/db/schema.ts"
_PACKAGE_RELPATH = "package.json"
_WORKER_RELPATH = "worker/index.ts"
_COMPONENTS_DIR = "src/components"
_VITE_CONFIG_RELPATH = "vite.config.ts"
APPKIT_VITE_PACKAGE_SHA_RELPATH = ".disco/appkit-vite-package.sha256"
_VITE_BUILD_TIMEOUT_S = 300
_SEALED_OUTPUT_MAX_FILES = 1024
_SEALED_OUTPUT_MAX_FILE_BYTES = 16 * 1024 * 1024
_SEALED_OUTPUT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
APPKIT_LIVE_PREVIEW_NAME = "appkit-live-vite"
_BUILT_PREVIEW_NAME = "appkit-built-vite"
_VITE_PREVIEW_COMMAND = "npx vite preview --host 0.0.0.0 --port {port} --strictPort"
# Where app_add_primitive persists applied-primitive provenance records (keep in
# sync with app_kit._PRIMITIVES_RELDIR) — the A3.2 enforcement input.
_PRIMITIVES_RELDIR = ".disco/primitives"
# The fixed path set the pre-dispatch verifier read straight from the sandbox for
# its lead-gen/directory check bundles. ALWAYS folded into the verify tree so the
# ported (now-pure) checks see exactly what those direct reads saw — even when the
# app/design specs are missing/unreadable (the run-app_create-first failures) or a
# file exists on disk without appearing in the current projection.
_LEGACY_TREE_PATHS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            _SCHEMA_RELPATH,
            _DRIZZLE_SCHEMA_RELPATH,
            _PACKAGE_RELPATH,
            _WORKER_RELPATH,
            "src/api/client.ts",
            "src/hooks/useSubmit.ts",
            *CF_EXPORT_FILES,
            *STATIC_CF_EXPORT_FILES,
            ".dev.vars",
        )
    )
)


# ---- verdict assembly ----------------------------------------------------------


def _check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "evidence": evidence}


@dataclass(frozen=True)
class _PreviewProbe:
    status: int
    content_type: str
    body: str
    error: str = ""


@dataclass(frozen=True)
class _BuildResult:
    ok: bool
    evidence: str = ""


@dataclass(frozen=True)
class _PreparedPreview:
    url: str
    stop_name: str | None = None
    failure_evidence: str | None = None
    runtime: dict[str, Any] | None = None


async def _sealed_appkit_output_identity(
    ctx: ToolContext,
) -> tuple[dict[str, str] | None, str]:
    """Hash the exact bounded ``dist`` closure produced by the strict verifier."""

    assert ctx.sandbox is not None
    sandbox = ctx.sandbox
    pending = ["dist"]
    manifest: list[dict[str, object]] = []
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(await sandbox.list_dir(directory))
        except Exception as exc:  # noqa: BLE001 — an unreadable output is unsealable
            return None, f"cannot enumerate verifier output {directory!r}: {exc}"
        for name in entries:
            child = f"{directory}/{name}"
            try:
                regular_file = bool(await sandbox.file_exists(child))
            except Exception as exc:  # noqa: BLE001 — an unclassifiable entry is unsealable
                return None, f"cannot classify verifier output {child!r}: {exc}"
            if regular_file:
                size_probe = await sandbox.exec_shell(
                    f"wc -c < {shlex.quote(child)}",
                    timeout_s=10,
                )
                try:
                    size = int(str(getattr(size_probe, "stdout", "") or "").strip())
                except ValueError:
                    return None, f"cannot bound verifier output file {child!r}"
                if (
                    size < 0
                    or size > _SEALED_OUTPUT_MAX_FILE_BYTES
                    or total_bytes + size > _SEALED_OUTPUT_MAX_TOTAL_BYTES
                ):
                    return None, f"verifier output exceeds the bounded seal budget at {child!r}"
                try:
                    data = await sandbox.read_file(child)
                except Exception as exc:  # noqa: BLE001 — incomplete output fails closed
                    return None, f"cannot read verifier output file {child!r}: {exc}"
                if len(data) != size:
                    return None, f"verifier output changed while sealing {child!r}"
                total_bytes += size
                manifest.append(
                    {
                        "path": child,
                        "size": size,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                if len(manifest) > _SEALED_OUTPUT_MAX_FILES:
                    return None, "verifier output exceeds the bounded file-count seal budget"
                continue
            try:
                await sandbox.list_dir(child)
            except Exception as exc:  # noqa: BLE001 — symlinks/special entries fail closed
                return None, f"verifier output contains an unsupported entry {child!r}: {exc}"
            else:
                pending.append(child)
    if not manifest or not any(item["path"] == APPKIT_CANONICAL_ENTRY_RELPATH for item in manifest):
        return None, "verifier output has no canonical AppKit entry"
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return (
        {
            "scheme": "sha256-tree-manifest-v1",
            "digest": f"sha256:{digest}",
            "entry_reference": APPKIT_CANONICAL_ENTRY_RELPATH,
            "producer_id": "disco.appkit_strict_verifier@1",
        },
        f"sealed {len(manifest)} files ({total_bytes} bytes) as sha256:{digest}",
    )


def _is_vite_app_tree(package_json: str | None, has_vite_config: bool) -> bool:
    """True for the generated React/Vite app shape the browser verifier must build.

    The trigger is intentionally narrow: package.json must be valid JSON with a
    ``devDependencies.vite`` entry and the workspace must carry ``vite.config.ts``.
    """
    if not package_json or not has_vite_config:
        return False
    try:
        pkg = json.loads(package_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(pkg, dict):
        return False
    dev_deps = pkg.get("devDependencies")
    return isinstance(dev_deps, dict) and "vite" in dev_deps


_UNBUILT_VITE_ENTRY_RE = re.compile(
    r"""<script\b(?=[^>]*\btype=["']module["'])(?=[^>]*\bsrc=["']/src/[^"']+)""",
    re.IGNORECASE,
)
_BUILT_VITE_BUNDLE_RE = re.compile(
    r"""<script\b(?=[^>]*\btype=["']module["'])(?=[^>]*\bsrc=["'][^"']*/assets/[^"']+\.js["'])""",
    re.IGNORECASE,
)


def _served_preview_uses_built_bundle(index_html: str) -> bool | None:
    """Classify a served Vite index page.

    ``False`` means the preview is definitely the source tree (for example,
    ``/src/main.tsx``). ``True`` means it is definitely built Vite output
    (hashed ``/assets/*.js`` bundle). ``None`` means the body is not recognizable
    enough to force a platform rebuild.
    """
    if _UNBUILT_VITE_ENTRY_RE.search(index_html) or "/src/main.tsx" in index_html:
        return False
    if _BUILT_VITE_BUNDLE_RE.search(index_html):
        return True
    return None


def _served_preview_is_vite_dev(index_html: str) -> bool:
    """Recognize Vite's real development transform, not a raw source server."""

    return "/@vite/client" in index_html and _served_preview_uses_built_bundle(index_html) is False


def _tail(text: str, *, limit: int = 2000) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return "..." + stripped[-limit:]


def _exec_failure_evidence(step: str, res: Any) -> str:
    reason = (
        "timed out"
        if bool(getattr(res, "timed_out", False))
        else (f"exit {getattr(res, 'exit_code', 'unknown')}")
    )
    stderr = _tail(str(getattr(res, "stderr", "") or ""))
    stdout = _tail(str(getattr(res, "stdout", "") or ""))
    tail = stderr or stdout or "(no stderr/stdout captured)"
    return f"platform Vite build failed during `{step}` ({reason}); stderr tail: {tail}"


def _composite_fingerprint(checks: list[dict[str, Any]], embedded_fp: str) -> str:
    """Stable hash over the FAILING check names + the embedded verify_web_app
    fingerprint. Identical failures → identical fingerprint (the finish-gate
    loop-breaker key); all-clean → a fixed 'clean' hash."""
    failing = sorted(c["name"] for c in checks if not c["passed"])
    raw = "|".join(failing) + "#" + (embedded_fp or "")
    if not failing:
        raw = "CLEAN#" + (embedded_fp or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_verdict(checks: list[dict[str, Any]], embedded: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble the W-45-compatible verdict from the per-check results + the
    embedded `verify_web_app` (structural) verdict."""
    emb = embedded or {}
    first_fail = next((c for c in checks if not c["passed"]), None)
    passed = first_fail is None
    n = len(checks)
    if first_fail is None:
        summary = (
            f"verify_appkit_app: all {n} STRUCTURAL checks passed — design-clean; "
            "schema+worker+form structure intact (presence/ordering/parameterization/"
            "guard-first); local lead/admin model round-trip consistent; routes + "
            "sections covered. STRUCTURE verified — local runtime proof lives in "
            "packages/core/tests/test_workerd_persistence.py."
        )
        # Counted-promotion failure 2026-07-27 (`p4_appkit_semantic_edit` seed
        # 900065, TOOL_CALL_THRASH). An identical call returned FAIL at seq 72 —
        # route_coverage tripped on a transient sub-resource
        # ERR_CONNECTION_REFUSED — then PASS at seq 75. Handed a contradictory
        # pair and an EMPTY next_action, the agent re-verified at seq 81 and hit
        # the identical-call limit of 2. Re-checking was the rational move: the
        # verdict had just changed under it and nothing said the claim was
        # settled.
        #
        # `web_app_probe.compute_verdict` already carries this guidance
        # (b61e09e6), and its own comment records the same symptom — a PASS with
        # `next_action: ""` followed by 33-35 further actions. That fix never
        # reached this sibling module, which is what the AppKit scenarios verify
        # through.
        #
        # Scoped to what THIS verifier actually establishes: structure. It does
        # not claim runtime behaviour, and it does not say "finish" — only the
        # finish gate knows whether plan steps remain.
        next_action = (
            "Verified — the structural checks are now proven for this app and "
            "recorded. Re-running the same verification without changing the app "
            "proves nothing new, including after a transient failure that has "
            "since cleared. Move to your remaining plan steps, and if none are "
            "outstanding, finish; re-verify only after a material change."
        )
    else:
        summary = f"verify_appkit_app: {first_fail['name']} FAILED — {first_fail['evidence']}"
        next_action = str(first_fail["evidence"])
    fp = _composite_fingerprint(checks, str(emb.get("failure_fingerprint") or ""))
    return {
        "ok": passed,
        "passed": passed,
        "verdict": "pass" if passed else "fail",
        "summary": summary,
        "next_action": next_action,
        "failure_fingerprint": fp,
        # W-45 surface (read by the finish gate's preview-binding + payload builder).
        "url": str(emb.get("url") or ""),
        "http_status": emb.get("http_status"),
        "console_errors": emb.get("console_errors") or [],
        "network_failures": emb.get("network_failures") or [],
        "screenshot_path": str(emb.get("screenshot_path") or ""),
        "checks": checks,
        "verify_web_app": emb,
    }


def _render(verdict: dict[str, Any]) -> str:
    """Stable, agent-facing text. Excludes screenshot path / sequence so an
    identical failure renders byte-identically (the no-progress breaker matches)."""
    state = "pass" if verdict["passed"] else "not passing"
    lines = [
        f"VERIFY_APPKIT_APP: {verdict['verdict'].upper()} ({state})",
        f"fingerprint: {verdict['failure_fingerprint']}",
        f"summary: {verdict['summary']}",
        "checks:",
    ]
    for c in verdict["checks"]:
        mark = "PASS" if c["passed"] else "FAIL"
        lines.append(f"  [{mark}] {c['name']} — {c['evidence']}")
    if verdict["next_action"]:
        lines.append(f"next_action: {verdict['next_action']}")
    return "\n".join(lines)


# ---- the tool ------------------------------------------------------------------


class VerifyAppKitAppArgs(BaseModel):
    url: str = Field(
        default="",
        description=(
            "Preview base URL of the running app (e.g. http://127.0.0.1:8000/). "
            "Leave empty to auto-detect the running preview server."
        ),
    )


class VerifyAppKitAppTool:
    """[CONTRACT boundary] Strict AppKit app verifier: design-lint + schema/worker/
    form contract + a local D1 lead/admin round-trip + route & section coverage.
    Returns a W-45-compatible PASS/FAIL verdict the finish gate consumes directly."""

    definition = ToolDef(
        name="verify_appkit_app",
        description=(
            "STRUCTURALLY verify the generated AppKit lead-gen app and return a STRUCTURED "
            "pass/fail verdict (presence + ordering + parameterization + guard-first; "
            "local runtime proof lives in packages/core/tests/test_workerd_persistence.py). "
            "Runs nine checks: "
            "design_lint clean, schema.sql valid (sqlite round-trip + NOT NULL), Drizzle "
            "schema valid (src/db/schema.ts matches schema.sql and package deps exist), worker "
            "contract (public POST /api/leads region contains a Drizzle insert in a "
            "non-dead position; GET /api/leads + /admin Bearer-gated, guard-first; fail-closed "
            "when ADMIN_TOKEN unset), the lead form POSTs JSON to /api/leads, a LOCAL D1 "
            "model round-trip (modelled post inserts, unauth reads 401, authed read returns "
            "the row, fail-closed), Cloudflare export readiness (owner guide + secret template "
            "present, wrangler binds DB + ASSETS with worker-first /api/* + /admin routing, no "
            "real secret shipped), every page route renders 2xx with no console/network "
            "errors, and every section's data-appkit-section marker is in the DOM. Embeds a "
            "verify_web_app verdict for the render part. Call ONCE when the app is built and "
            "the preview is running to decide if the structure is sound. When the verdict "
            "PASSES all checks, the build is DONE — call finish immediately; do not keep "
            "editing (further edits invalidate the verified state)."
        ),
        args_model=VerifyAppKitAppArgs,
        needs=frozenset(
            {Capability.NETWORK, Capability.DISPLAY, Capability.SHELL, Capability.FILESYSTEM}
        ),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,  # runs platform-owned npm build/preview work when Vite needs it
        behavior=declares(EffectCapability.ARTIFACT_VERIFY, planner_safe=False),
    )

    async def run(self, args: VerifyAppKitAppArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await self._load_app(ctx)
            design = await self._load_design(ctx)
            tree = await self._assemble_tree(ctx, app, design)

            checks: list[dict[str, Any]] = []

            # 1. design_lint_clean — COMMON; reuse the DesignLintTool scan verbatim.
            checks.append(await self._check_design_lint(ctx))

            # 2..N. primitive-specific contract checks — DISPATCH on the resolved
            # primitive's verify hook (WO-A3). A missing/unreadable app resolves to
            # no primitive, and a primitive WITHOUT a verify hook (records — on
            # purpose: records apps have always fallen through to the lead-gen check
            # bundle, the pre-dispatch else-branch) falls back to `lead_gen_verify`,
            # so those verdicts fail cleanly with the usual "run app_create first"
            # evidence, byte-identical to before. design_lint + route + section
            # coverage stay COMMON to every primitive.
            prim = resolve_primitive(app.app_kind) if app is not None else None
            verify_fn = (
                prim.verify if (prim is not None and prim.verify is not None) else lead_gen_verify
            )
            result = verify_fn(app, design, tree)
            checks.extend(self._primitive_result_checks(result, prim))

            # Security-classed add-ons are derived from strict AppSpec metadata,
            # never solely from deletable workspace provenance.  Each must pass
            # both its pure source/tree verifier and its host-owned live runner.
            security_primitives = required_security_primitives(app)
            for security_prim in security_primitives:
                checks.extend(
                    await self._security_primitive_checks(ctx, security_prim, app, design, tree)
                )

            # A3.2 — applied-primitive enforcement (fail-closed): every
            # `.disco/primitives/*.json` provenance record lands a
            # `primitive_verify:<id>` check in this SAME list, so the W-45 verdict
            # (and the existing finish gate consuming it) enforces template_only
            # primitives without any loop changes.
            checks.extend(
                await self._applied_primitive_checks(
                    ctx,
                    app,
                    design,
                    tree,
                    required_ids=frozenset(item.id for item in security_primitives),
                )
            )

            # last. route + section coverage + the embedded verify_web_app verdict — COMMON.
            # Vite SPAs need a platform-owned compiled preview: the model has no shell in
            # strict AppKit mode, but the browser checks must hit built JS, not /src/*.tsx.
            prepared = await self._prepare_vite_preview_for_browser_checks(ctx, args.url)
            retain_prepared_runtime = False
            try:
                if prepared.failure_evidence is not None:
                    embedded = {
                        "verdict": "fail",
                        "passed": False,
                        "url": prepared.url,
                        "summary": prepared.failure_evidence,
                        "failure_fingerprint": hashlib.sha256(
                            prepared.failure_evidence.encode("utf-8")
                        ).hexdigest()[:16],
                    }
                    route_check = _check("route_coverage", False, prepared.failure_evidence)
                    section_check = _check("section_coverage", False, prepared.failure_evidence)
                else:
                    embedded, route_check, section_check = await self._browser_checks(
                        ctx, app, prepared.url
                    )
                    retain_prepared_runtime = bool(
                        embedded
                        and embedded.get("passed") is True
                        and route_check["passed"]
                        and section_check["passed"]
                    )
            finally:
                if prepared.stop_name is not None and not retain_prepared_runtime:
                    await self._stop_prepared_preview(ctx, prepared.stop_name)
            checks.append(route_check)
            checks.append(section_check)

            artifact_identity: dict[str, str] | None = None
            if retain_prepared_runtime and prepared.runtime is not None:
                artifact_identity, seal_evidence = await _sealed_appkit_output_identity(ctx)
                checks.append(
                    _check(
                        "sealed_output_identity",
                        artifact_identity is not None,
                        seal_evidence,
                    )
                )
            verdict = build_verdict(checks, embedded)
            if app is not None:
                # Target-owned semantic identity. This is intentionally separate
                # from browser-visible text so non-web target adapters can issue
                # the same claim from their own authoritative application model.
                verdict["application_title"] = app.name
            if verdict["passed"] and prepared.runtime is not None and artifact_identity is not None:
                verdict["preview_runtime"] = prepared.runtime
                verdict["canonical_entry_path"] = APPKIT_CANONICAL_ENTRY_RELPATH
                verdict["artifact_identity"] = artifact_identity
            return ToolOutcome(success=True, content=_render(verdict), structured=verdict)
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a verdict-shaped error
            return fail_outcome(f"verify_appkit_app error: {e}")

    def _primitive_result_checks(
        self, result: PrimitiveVerifyResult, prim: object
    ) -> list[dict[str, Any]]:
        """Map a `PrimitiveVerifyResult` into the verdict's `_check` dicts 1:1. A
        verify hook that reports NO per-check breakdown still lands ONE named check
        — an empty tuple must never read as a silent pass (no false affordance)."""
        if result.checks:
            return [_check(c.name, c.passed, c.evidence) for c in result.checks]
        prim_id = getattr(prim, "id", None) or "lead_gen"
        return [
            _check(
                f"primitive_verify:{prim_id}",
                bool(result.ok),
                result.detail or "the primitive verify hook reported no checks.",
            )
        ]

    async def _applied_primitive_checks(
        self,
        ctx: ToolContext,
        app: AppSpec | None,
        design: DesignSpec | None,
        tree: dict[str, str],
        *,
        required_ids: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """A3.2 — applied-primitive enforcement (fail-closed). Every
        `.disco/primitives/*.json` provenance record (written by app_add_primitive)
        lands a `primitive_verify:<id>` check:

        * an unknown/unparseable primitive id FAILS (stale record);
        * a template_only primitive with NO verify hook FAILS (cannot ship
          unverified — fail-closed);
        * a primitive WITH a verify hook (any tier) runs it against the same
          app/design/tree and reports its result (first failing sub-check named).

        A fillable primitive without a verify hook lands no check — only
        template_only output is Disco-owned and gate-enforced."""
        assert ctx.sandbox is not None
        try:
            names = await ctx.sandbox.list_dir(_PRIMITIVES_RELDIR)
        except Exception:  # noqa: BLE001 — no applied-primitive records
            return []
        checks: list[dict[str, Any]] = []
        for name in sorted(names):
            if not name.endswith(".json"):
                continue
            relpath = f"{_PRIMITIVES_RELDIR}/{name}"
            raw = await self._read_text(ctx, relpath)
            prim_id = ""
            try:
                record = json.loads(raw or "")
                if isinstance(record, dict):
                    prim_id = str(record.get("primitive_id") or "")
            except json.JSONDecodeError:
                prim_id = ""
            check_name = f"primitive_verify:{prim_id or name.removesuffix('.json')}"
            prim = get_primitive(prim_id) if prim_id else None
            if prim is None:
                checks.append(
                    _check(
                        check_name,
                        False,
                        f"applied-primitive record {relpath} names an unknown "
                        f"primitive {(prim_id or name.removesuffix('.json'))!r} — "
                        "stale record (fail-closed).",
                    )
                )
                continue
            if prim.id in required_ids:
                # The mandatory AppSpec-derived security path already checked
                # this exact record and dispatched the live exploit harness.
                continue
            if prim.verify is None:
                if prim.tier == "template_only":
                    checks.append(
                        _check(
                            check_name,
                            False,
                            f"template_only primitive '{prim.id}' declares no verify "
                            "harness — cannot ship unverified (fail-closed).",
                        )
                    )
                # fillable + no verify: nothing to enforce — the model owns the output.
                continue
            record_tree = dict(tree)
            if raw is not None:
                record_tree[relpath] = raw
            result = prim.verify(app, design, record_tree)
            evidence = result.detail
            first_fail = next((c for c in result.checks if not c.passed), None)
            if first_fail is not None:
                evidence = (
                    f"{result.detail}; first failure: {first_fail.name} — {first_fail.evidence}"
                )
            checks.append(_check(check_name, bool(result.ok), evidence))
        return checks

    async def _security_primitive_checks(
        self,
        ctx: ToolContext,
        prim: PrimitiveDefinition,
        app: AppSpec | None,
        design: DesignSpec | None,
        tree: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Run the pure and live halves of a mandatory security primitive.

        The live callback is an injected host capability, not a registry global.
        Missing wiring, exceptions, wrong/empty results, duplicate checks, and an
        ``ok`` bit inconsistent with the check breakdown all fail closed.
        """
        static_name = f"security_primitive:{prim.id}"
        live_id = prim.live_verify_id
        live_name = f"primitive_live:{live_id or prim.id}"
        if prim.verify is None or live_id is None:
            return [
                _check(
                    static_name,
                    False,
                    f"security primitive {prim.id!r} is missing static/live verification wiring.",
                ),
                _check(live_name, False, "live verification was not dispatched."),
            ]
        static_result = prim.verify(app, design, tree)
        static_consistency = self._verify_result_problem(static_result)
        if static_consistency is not None:
            return [
                _check(static_name, False, static_consistency),
                _check(
                    live_name,
                    False,
                    "live verification withheld because static verification is invalid.",
                ),
            ]
        static_failure = next((item for item in static_result.checks if not item.passed), None)
        static_evidence = static_result.detail
        if static_failure is not None:
            static_evidence = (
                f"{static_result.detail}; first failure: {static_failure.name} — "
                f"{static_failure.evidence}"
            )
        static_check = _check(static_name, static_result.ok, static_evidence)
        if not static_result.ok:
            return [
                static_check,
                _check(
                    live_name,
                    False,
                    "live verification withheld until trusted static checks pass.",
                ),
            ]
        if app is None or design is None:
            return [
                static_check,
                _check(
                    live_name,
                    False,
                    "valid AppSpec and DesignSpec are required for live verification.",
                ),
            ]
        callback = ctx.primitive_live_verifier
        if callback is None:
            return [
                static_check,
                _check(live_name, False, "host live verifier is not wired (fail-closed)."),
            ]
        try:
            live_result = await callback(live_id, app, design, tree)
        except Exception as exc:  # noqa: BLE001 - host failures become a closed gate
            return [
                static_check,
                _check(
                    live_name,
                    False,
                    f"host live verifier raised {type(exc).__name__} (fail-closed).",
                ),
            ]
        if not isinstance(live_result, PrimitiveVerifyResult):
            return [
                static_check,
                _check(live_name, False, "host live verifier returned an unknown result shape."),
            ]
        live_problem = self._verify_result_problem(live_result)
        if live_problem is not None:
            return [static_check, _check(live_name, False, live_problem)]
        actual_live_checks = frozenset(item.name for item in live_result.checks)
        if actual_live_checks != frozenset(prim.live_verify_checks):
            return [
                static_check,
                _check(
                    live_name,
                    False,
                    "host live verifier returned unknown/missing exploit checks (fail-closed).",
                ),
            ]
        live_failure = next((item for item in live_result.checks if not item.passed), None)
        live_evidence = live_result.detail
        if live_failure is not None:
            live_evidence = (
                f"{live_result.detail}; first failure: {live_failure.name} — "
                f"{live_failure.evidence}"
            )
        return [static_check, _check(live_name, live_result.ok, live_evidence)]

    @staticmethod
    def _verify_result_problem(result: PrimitiveVerifyResult) -> str | None:
        if not result.detail.strip() or not result.checks:
            return "primitive verifier returned an empty result (fail-closed)."
        names = [item.name for item in result.checks]
        if any(not item.name.strip() or not item.evidence.strip() for item in result.checks):
            return "primitive verifier returned an empty check name/evidence (fail-closed)."
        if len(set(names)) != len(names):
            return "primitive verifier returned duplicate check names (fail-closed)."
        checks_ok = all(item.passed for item in result.checks)
        if result.ok != checks_ok:
            return (
                "primitive verifier result is inconsistent with its check breakdown (fail-closed)."
            )
        return None

    # ---- helpers -------------------------------------------------------------

    async def _load_app(self, ctx: ToolContext) -> AppSpec | None:
        data = await self._read_bytes(ctx, APPSPEC_RELPATH)
        if data is None:
            return None
        try:
            return load_app_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — invalid spec → treated as absent (checks fail cleanly)
            return None

    async def _load_design(self, ctx: ToolContext) -> DesignSpec | None:
        data = await self._read_bytes(ctx, DESIGNSPEC_RELPATH)
        if data is None:
            return None
        try:
            return load_design_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — invalid spec → treated as absent (checks fail cleanly)
            return None

    async def _assemble_tree(
        self, ctx: ToolContext, app: AppSpec | None, design: DesignSpec | None
    ) -> dict[str, str]:
        """The relpath → ON-DISK text tree the primitive verify hooks inspect (a
        missing file is an ABSENT key). You are verifying DISK, not the projection:
        when app AND design are loadable, `generate(app, design)` supplies the PATH
        SET and each path's CONTENT is read from the sandbox. The legacy fixed path
        set (the lead-gen/directory reads the pre-dispatch tool performed directly)
        is ALWAYS included so the ported checks see exactly what the old sandbox
        reads saw — including the run-app_create-first failures when app/design are
        missing — plus every on-disk `src/components/*.tsx` (components may be
        model-edited beyond the projection)."""
        assert ctx.sandbox is not None
        paths: set[str] = set(_LEGACY_TREE_PATHS)
        paths.add(APPSPEC_RELPATH)
        if app is not None and design is not None:
            try:
                paths.update(generate(app, design))
            except Exception:  # noqa: BLE001 — a projection failure must not kill verify
                pass  # the legacy path set below still drives the ported checks
        for security_prim in required_security_primitives(app):
            paths.add(f"{_PRIMITIVES_RELDIR}/{security_prim.id}.json")
        try:
            names = await ctx.sandbox.list_dir(_COMPONENTS_DIR)
        except Exception:  # noqa: BLE001 — no components dir
            names = []
        paths.update(f"{_COMPONENTS_DIR}/{name}" for name in names if name.endswith(".tsx"))
        tree: dict[str, str] = {}
        for path in sorted(paths):
            text = await self._read_text(ctx, path)
            if text is not None:
                tree[path] = text
        return tree

    async def _read_bytes(self, ctx: ToolContext, relpath: str) -> bytes | None:
        assert ctx.sandbox is not None
        try:
            if not await ctx.sandbox.file_exists(relpath):
                return None
            return await ctx.sandbox.read_file(relpath)
        except Exception:  # noqa: BLE001 — unreadable → absent
            return None

    async def _read_text(self, ctx: ToolContext, relpath: str) -> str | None:
        data = await self._read_bytes(ctx, relpath)
        return data.decode("utf-8", errors="replace") if data is not None else None

    async def _check_design_lint(self, ctx: ToolContext) -> dict[str, Any]:
        out = await DesignLintTool().run(DesignLintArgs(root="."), ctx)
        st = out.structured or {}
        if not out.success or not st:
            return _check("design_lint_clean", False, "design_lint could not scan the workspace.")
        if st.get("ok"):
            return _check(
                "design_lint_clean",
                True,
                f"no design slop across {st.get('scanned_files', 0)} scanned file(s).",
            )
        return _check("design_lint_clean", False, st.get("summary", "design slop found."))

    async def _prepare_vite_preview_for_browser_checks(
        self, ctx: ToolContext, _requested_url: str
    ) -> _PreparedPreview:
        """Use the real managed Vite runtime, or build a managed runtime if absent.

        A Vite development response is accepted only when it carries Vite's
        ``/@vite/client`` transform; a raw static server exposing ``/src/*.tsx`` is
        still rejected. A missing, failing, raw-source, or opaque probe triggers a
        platform build and managed compiled preview before browser checks.
        """
        assert ctx.sandbox is not None
        package_json = await self._read_text(ctx, _PACKAGE_RELPATH)
        if not _is_vite_app_tree(package_json, await ctx.sandbox.file_exists(_VITE_CONFIG_RELPATH)):
            return _PreparedPreview(url=(_requested_url or "").strip())

        managed = self._canonical_managed_vite_preview(ctx)
        build = await self._ensure_vite_platform_build(ctx, package_json or "")
        if not build.ok:
            return _PreparedPreview(
                url=managed.url if managed is not None else "",
                failure_evidence=build.evidence,
                runtime=managed.runtime if managed is not None else None,
            )
        if managed is not None:
            probe = await self._fetch_preview_index(ctx, managed.url)
            if (
                probe is None
                or probe.error
                or not (
                    _served_preview_is_vite_dev(probe.body)
                    or _served_preview_uses_built_bundle(probe.body) is True
                )
            ):
                evidence = (
                    probe.error
                    if probe is not None and probe.error
                    else "canonical managed Vite runtime did not serve a recognizable Vite page"
                )
                return _PreparedPreview(
                    url=managed.url,
                    failure_evidence=evidence,
                    runtime=managed.runtime,
                )
            return managed
        return await self._start_built_vite_preview(ctx)

    @staticmethod
    def _canonical_managed_vite_preview(ctx: ToolContext) -> _PreparedPreview | None:
        """Return the one host-owned AppKit manager selection, ignoring caller URLs."""

        manager = getattr(ctx.sandbox, "_preview_manager", None)
        session = manager.canonical_session() if manager is not None else None
        if session is None:
            return None
        if getattr(session, "name", None) not in {
            APPKIT_LIVE_PREVIEW_NAME,
            _BUILT_PREVIEW_NAME,
        }:
            return None
        port = getattr(session, "port", None)
        if type(port) is not int:
            return None
        data = session.to_dict()
        if not isinstance(data, dict) or data.get("status") not in {"running", "unavailable"}:
            return None
        return _PreparedPreview(url=f"http://127.0.0.1:{port}/", runtime=data)

    async def _fetch_preview_index(self, ctx: ToolContext, base_url: str) -> _PreviewProbe | None:
        """Fetch the current preview's root from inside the sandbox.

        Returns ``None`` only when the sandbox fake/output is not the JSON frame this
        helper emits; real network errors are framed as ``error`` so they remain loud
        enough to trigger a platform build.
        """
        assert ctx.sandbox is not None
        url = base_url.rstrip("/") + "/"
        script = (
            "import json, urllib.request as U\n"
            "try:\n"
            f"    r=U.urlopen({url!r},timeout=10)\n"
            "    body=r.read(200000).decode('utf-8','replace')\n"
            "    print(json.dumps({'status': getattr(r, 'status', None) or r.getcode(), "
            "'content_type': r.headers.get('content-type',''), 'body': body}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'status': 0, 'content_type': '', 'body': '', "
            "'error': str(e)}))\n"
        )
        try:
            res = await ctx.sandbox.exec_shell(f"python3 -c {shlex.quote(script)}", timeout_s=15)
        except Exception:  # noqa: BLE001 — let browser checks handle opaque fakes
            return None
        try:
            data = json.loads(str(getattr(res, "stdout", "") or ""))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return _PreviewProbe(
            status=int(data.get("status") or 0),
            content_type=str(data.get("content_type") or ""),
            body=str(data.get("body") or ""),
            error=str(data.get("error") or ""),
        )

    async def _ensure_vite_platform_build(
        self, ctx: ToolContext, package_json: str
    ) -> _BuildResult:
        """Run the bounded platform-owned Vite build inside the sandbox.

        ``npm ci`` is skipped only when a node_modules directory exists and the cache
        marker contains the exact current package.json SHA. ``npm run build`` always
        runs because the source tree may have changed while dependencies did not.
        """
        assert ctx.sandbox is not None
        package_sha = hashlib.sha256(package_json.encode("utf-8")).hexdigest()
        node_modules_exists = await self._sandbox_dir_exists(ctx, "node_modules")
        marker = (await self._read_text(ctx, APPKIT_VITE_PACKAGE_SHA_RELPATH) or "").strip()
        need_ci = not (node_modules_exists and marker == package_sha)
        started = time.monotonic()

        if need_ci:
            # The generator emits its reviewed package-lock.json with package.json.
            # Never fall back to `npm install`: that would resolve mutable dependency
            # ranges during verification and make the trusted bundle non-deterministic.
            has_lock = await self._sandbox_file_exists(ctx, "package-lock.json")
            if not has_lock:
                return _BuildResult(
                    False,
                    (
                        "generated Vite tree is missing package-lock.json; "
                        "refusing mutable npm install"
                    ),
                )
            install_cmd = "npm ci --no-audit --no-fund"
            try:
                ci = await ctx.sandbox.exec_shell(install_cmd, timeout_s=_VITE_BUILD_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — loud verdict evidence
                return _BuildResult(
                    False, f"platform Vite build could not run `{install_cmd}`: {exc}"
                )
            if getattr(ci, "exit_code", 1) != 0 or bool(getattr(ci, "timed_out", False)):
                return _BuildResult(False, _exec_failure_evidence(install_cmd, ci))
            try:
                await ctx.sandbox.write_file(
                    APPKIT_VITE_PACKAGE_SHA_RELPATH, (package_sha + "\n").encode("utf-8")
                )
            except Exception:  # noqa: BLE001 — cache marker failure must not hide build evidence
                pass

        elapsed = int(time.monotonic() - started)
        remaining = max(1, _VITE_BUILD_TIMEOUT_S - elapsed)
        try:
            build = await ctx.sandbox.exec_shell("npm run build", timeout_s=remaining)
        except Exception as exc:  # noqa: BLE001 — loud verdict evidence
            return _BuildResult(False, f"platform Vite build could not run `npm run build`: {exc}")
        if getattr(build, "exit_code", 1) != 0 or bool(getattr(build, "timed_out", False)):
            return _BuildResult(False, _exec_failure_evidence("npm run build", build))
        if not await self._sandbox_file_exists(ctx, "dist/index.html"):
            return _BuildResult(
                False,
                "platform Vite build reported success but did not produce dist/index.html",
            )
        return _BuildResult(True)

    async def _start_built_vite_preview(self, ctx: ToolContext) -> _PreparedPreview:
        """Serve the compiled Vite app with Vite's preview server under platform port
        ownership, then return the in-sandbox URL the browser verifier can reach."""
        try:
            from .preview import _manager

            mgr = _manager(ctx)
            session = await mgr.start(
                command=_VITE_PREVIEW_COMMAND,
                name=_BUILT_PREVIEW_NAME,
                supervise=True,
            )
        except Exception as exc:  # noqa: BLE001 — route/section fail loudly
            return _PreparedPreview(
                url="",
                failure_evidence=(
                    "platform Vite build succeeded, but serving the built app failed "
                    f"while starting `{_VITE_PREVIEW_COMMAND}`: {exc}"
                ),
            )

        raw_status = getattr(session, "status", "")
        status = getattr(raw_status, "value", str(raw_status))
        port = getattr(session, "port", None)
        if status not in {"running", "unavailable"} or not isinstance(port, int):
            detail = str(getattr(session, "detail", "") or "preview did not become healthy")
            return _PreparedPreview(
                url="",
                failure_evidence=(
                    "platform Vite build succeeded, but the built-app preview failed "
                    f"to become healthy (status={status or 'unknown'}): {detail}"
                ),
            )
        return _PreparedPreview(
            url=f"http://127.0.0.1:{port}/",
            stop_name=_BUILT_PREVIEW_NAME,
            runtime=(session.to_dict() if callable(getattr(session, "to_dict", None)) else None),
        )

    async def _stop_prepared_preview(self, ctx: ToolContext, name: str) -> None:
        try:
            from .preview import _manager

            await _manager(ctx).stop(name)
        except Exception:  # noqa: BLE001 — cleanup must not mask the verifier result
            pass

    async def _sandbox_file_exists(self, ctx: ToolContext, relpath: str) -> bool:
        assert ctx.sandbox is not None
        try:
            return bool(await ctx.sandbox.file_exists(relpath))
        except Exception:  # noqa: BLE001 — absence probe must never raise
            return False

    async def _sandbox_dir_exists(self, ctx: ToolContext, relpath: str) -> bool:
        assert ctx.sandbox is not None
        try:
            await ctx.sandbox.list_dir(relpath)
            return True
        except Exception:  # noqa: BLE001 — absent or not a directory
            return False

    async def _browser_checks(
        self, ctx: ToolContext, app: AppSpec | None, url: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """Drive the W-45 browser path for route_coverage + section_coverage and
        return (embedded verify_web_app verdict, route_check, section_check)."""
        vtool = VerifyWebAppTool()
        base = (url or "").strip() or await vtool._detect_preview_url(ctx)
        base = base.rstrip("/") or base

        routes = ["/"]
        if app is not None:
            seen: set[str] = set()
            routes = []
            for page in app.pages:
                if page.route not in seen:
                    seen.add(page.route)
                    routes.append(page.route)
            routes = routes or ["/"]

        embedded: dict[str, Any] | None = None
        route_failures: list[str] = []
        for route in routes:
            route_url = base if route == "/" else base + "/" + route.lstrip("/")
            out = await vtool.run(VerifyWebAppArgs(url=route_url), ctx)
            rv = out.structured or {}
            if embedded is None:
                embedded = rv  # the first (base) route is the structural verdict
            if not rv.get("passed"):
                route_failures.append(f"{route}: {rv.get('summary', 'did not pass')}")

        if route_failures:
            route_check = _check(
                "route_coverage",
                False,
                "; ".join(route_failures[:3]),
            )
        else:
            route_check = _check(
                "route_coverage",
                True,
                f"all {len(routes)} route(s) rendered 2xx with no console/network errors.",
            )

        # section_coverage — every section's data-appkit-section marker in the DOM.
        section_check = await self._check_sections(ctx, app, base, routes)
        return embedded, route_check, section_check

    async def _check_sections(
        self, ctx: ToolContext, app: AppSpec | None, base: str, routes: list[str]
    ) -> dict[str, Any]:
        if app is None:
            return _check("section_coverage", False, "no AppSpec to enumerate sections from.")
        expected = {s.id for page in app.pages for s in page.sections}
        if not expected:
            return _check("section_coverage", True, "no sections declared.")
        found: set[str] = set()
        for route in routes:
            route_url = base if route == "/" else base + "/" + route.lstrip("/")
            out = await BrowserTool().run(BrowserArgs(action="navigate", url=route_url), ctx)
            if out.success and out.structured:
                for marker in out.structured.get("appkit_sections", []) or []:
                    if marker:
                        found.add(str(marker))
        missing = sorted(expected - found)
        if missing:
            return _check(
                "section_coverage",
                False,
                "section(s) declared in the AppSpec did NOT render (no data-appkit-section "
                f"marker in the DOM): {', '.join(missing)}.",
            )
        return _check(
            "section_coverage",
            True,
            f"all {len(expected)} AppSpec section(s) rendered "
            "(data-appkit-section markers present).",
        )


__all__ = [
    "VerifyAppKitAppArgs",
    "VerifyAppKitAppTool",
    "WorkerAuthVerdict",
    "build_verdict",
    "inspect_directory_listing",
    "inspect_lead_form",
    "inspect_static_worker",
    "inspect_submit_support",
    "inspect_worker",
]
