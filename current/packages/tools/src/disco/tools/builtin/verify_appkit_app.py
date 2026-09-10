"""AppKit EPIC G — `verify_appkit_app`, the STRICT app verifier.

Where `verify_web_app` (W-45) answers "does the running web app render without
errors", `verify_appkit_app` answers the stronger AppKit question: "is this a
design-clean lead-gen app whose lead+admin contract is STRUCTURALLY correct —
presence + ordering + parameterization + guard-first". This is STRUCTURAL
verification of the generated source, NOT a proof that the app works at runtime:
runtime reachability / behavioural execution of the Worker is proved separately by
`current/packages/core/tests/test_workerd_persistence.py`, which runs the generated Worker
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
                            current/packages/core/tests/test_workerd_persistence.py): the
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
                            current/packages/core/tests/test_workerd_persistence.py; hosted
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

Package split (PKG-11C): the sandbox IO (`SandboxReader`), the managed-Vite-preview
lifecycle (`PreviewLifecycle`), the check batteries (design_lint/primitive-dispatch/
applied-primitive/security-primitive/route+section coverage), the shared path
constants, and the sealed-output digest computation now live under
`verify_appkit_parts/` — cohesive collaborators + module-level functions each well
under the module/class/callable size + complexity budgets. This module is the sole
public facade: every name external code/tests reach (including several private
ones) is re-imported here unchanged, so nothing importing
`disco.tools.builtin.verify_appkit_app` needs to change. `verify_appkit_parts` is a
private implementation detail; external code must never import from it directly.
"""

from __future__ import annotations

import hashlib
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit.primitives import required_security_primitives
from disco.core.appkit.spec import AppSpec

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
from .verify_appkit_parts.checks import (
    _BrowserPhaseResult,
    _check,
    applied_primitive_checks,
    browser_phase_checks,
    design_lint_check,
    dispatch_primitive_checks,
    security_primitive_checks,
)
from .verify_appkit_parts.constants import (
    _VITE_PREVIEW_COMMAND as _VITE_PREVIEW_COMMAND,
)
from .verify_appkit_parts.constants import (
    APPKIT_LIVE_PREVIEW_NAME as APPKIT_LIVE_PREVIEW_NAME,
)
from .verify_appkit_parts.constants import (
    APPKIT_VITE_PACKAGE_SHA_RELPATH as APPKIT_VITE_PACKAGE_SHA_RELPATH,
)
from .verify_appkit_parts.preview_lifecycle import (
    PreviewLifecycle,
    _BuildResult,
)
from .verify_appkit_parts.preview_lifecycle import (
    _is_vite_app_tree as _is_vite_app_tree,
)
from .verify_appkit_parts.preview_lifecycle import (
    _served_preview_is_vite_dev as _served_preview_is_vite_dev,
)
from .verify_appkit_parts.preview_lifecycle import (
    _served_preview_uses_built_bundle as _served_preview_uses_built_bundle,
)
from .verify_appkit_parts.sandbox_reader import SandboxReader
from .verify_appkit_parts.sealed_identity import _sealed_appkit_output_identity

# ---- verdict assembly ----------------------------------------------------------


def _composite_fingerprint(checks: list[dict[str, Any]], embedded_fp: str) -> str:
    """Stable hash over the FAILING check names + the embedded verify_web_app
    fingerprint. Identical failures → identical fingerprint (the finish-gate
    loop-breaker key); all-clean → a fixed 'clean' hash."""
    failing = sorted(c["name"] for c in checks if not c["passed"])
    raw = "|".join(failing) + "#" + (embedded_fp or "")
    if not failing:
        raw = "CLEAN#" + (embedded_fp or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# F49 (2026-08-06x) — the checks whose truth is an OBSERVATION of a running
# preview, not a property of the bytes.
#
# `route_coverage` and `section_coverage` are the W-45 browser path; every
# `primitive_live:*` check is a live host verification. Each of them is true
# *at the moment it ran* and can lapse with no agent action at all — the preview
# can stop, restart, or rotate generation. Every other check in this battery is
# derived from the workspace tree and stays true until the agent changes it.
#
# The distinction exists because the receipt used to extend ONE validity licence
# ("re-verify only after a material change") over both kinds, which told the
# agent that a live observation was durable. `p4_appkit_semantic_edit` seed 97705
# finished on this receipt and `OutputTruthOracle` faulted the run
# (FALSE_FINISH_PREVIEW_BROKEN, preview health 409).
_LIVE_OBSERVATION_CHECKS = frozenset({"route_coverage", "section_coverage"})
_LIVE_OBSERVATION_PREFIX = "primitive_live:"


def observation_scoped_checks(checks: list[dict[str, Any]]) -> list[str]:
    """The names of the checks above that are present in this battery.

    Returned in battery order and surfaced on the verdict so a consumer can see
    WHICH claims are perishable rather than having to know the list.
    """
    return [
        name
        for check in checks
        if (name := str(check["name"])) in _LIVE_OBSERVATION_CHECKS
        or name.startswith(_LIVE_OBSERVATION_PREFIX)
    ]


def _live_observation_clause(live_names: list[str]) -> str:
    """The sentence naming this battery's perishable claims, or "" if it has none.

    Its own function rather than an expression inside `build_verdict`: that
    callable sits at the McCabe budget, and the number/agreement branches below
    would push it over — which is the arch budget doing its job, not an obstacle
    to route around with a debt row.
    """
    if not live_names:
        return ""
    single = len(live_names) == 1
    return (
        " {names} {is_are} {a_live} of the preview as it was AT THIS CHECK "
        "— not {a_durable} of the code. {It_They} can lapse without any "
        "change of yours if the preview stops, restarts or changes "
        "generation, so this receipt does not establish that the app is "
        "still serving later; the finish gate settles that."
    ).format(
        names=", ".join(f"`{name}`" for name in live_names),
        is_are="is" if single else "are",
        a_live="a live observation" if single else "live observations",
        a_durable="a durable property" if single else "durable properties",
        It_They="It" if single else "They",
    )


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
            "current/packages/core/tests/test_workerd_persistence.py."
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
        #
        # F49 (2026-08-06x): it used to say "finish" anyway. The clause
        # "…if none are outstanding, finish; re-verify only after a material
        # change" extended ONE validity licence over both kinds of check in the
        # battery — and `route_coverage` / `section_coverage` / `primitive_live:*`
        # are live observations of a running preview, not properties of the bytes.
        # Telling the agent to re-verify them "only after a material change" tells
        # it that a perishable observation is durable: they can lapse with no
        # change of the agent's at all. Seed 97705 finished on this receipt while
        # the preview was 409.
        #
        # The licence is now scoped to the durable checks, the perishable ones are
        # named as observation-scoped, and the finish decision is left where the
        # original comment already said it belongs — with the finish gate.
        # `next_action` stays NON-EMPTY: an empty one is what caused the
        # 2026-07-27 counted-promotion re-verify thrash this block was written for.
        live_clause = _live_observation_clause(observation_scoped_checks(checks))
        next_action = (
            "Verified — the structural checks are now proven for this app and "
            "recorded. Re-running the same verification without changing the app "
            "proves nothing new about them, including after a transient failure "
            "that has since cleared; re-verify the structure only after a material "
            "change." + live_clause + " Move to your remaining plan steps."
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
        # F49 — which of this battery's claims are perishable, so a consumer
        # never has to infer the scope from the prose.
        "observation_scoped_checks": observation_scoped_checks(checks),
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
            "Exact in-sandbox preview URL when already known. Leave empty to auto-detect "
            "the running preview server; never guess a fixed port."
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
            "local runtime proof lives in "
            "current/packages/core/tests/test_workerd_persistence.py). "
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
            "PASSES all checks the STRUCTURE is done — stop editing (further edits invalidate "
            "the verified state) and move to your remaining plan steps. The route/section "
            "coverage and primitive_live checks are observations of the preview AT THAT "
            "MOMENT, so a PASS here is not by itself a finish condition: the finish gate "
            "decides that. This optional probe spends one of the TWO debug calls shared "
            "with verify_web_app and design_lint for the current artifact bytes; only a "
            "real successful artifact mutation resets that allowance."
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

    def __init__(self) -> None:
        self._sandbox = SandboxReader()
        self._preview = PreviewLifecycle(self._sandbox)

    async def run(self, args: VerifyAppKitAppArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await self._sandbox.load_app(ctx)
            design = await self._sandbox.load_design(ctx)
            tree = await self._sandbox.assemble_tree(ctx, app, design)

            checks: list[dict[str, Any]] = []

            # 1. design_lint_clean — COMMON; reuse the DesignLintTool scan verbatim.
            checks.append(await design_lint_check(ctx))

            # 2..N. primitive-specific contract checks — DISPATCH on the resolved
            # primitive's verify hook (WO-A3). design_lint + route + section
            # coverage stay COMMON to every primitive.
            checks.extend(dispatch_primitive_checks(app, design, tree))

            # Security-classed add-ons are derived from strict AppSpec metadata,
            # never solely from deletable workspace provenance.  Each must pass
            # both its pure source/tree verifier and its host-owned live runner.
            security_primitives = required_security_primitives(app)
            for security_prim in security_primitives:
                checks.extend(
                    await security_primitive_checks(ctx, security_prim, app, design, tree)
                )

            # A3.2 — applied-primitive enforcement (fail-closed): every
            # `.disco/primitives/*.json` provenance record lands a
            # `primitive_verify:<id>` check in this SAME list, so the W-45 verdict
            # (and the existing finish gate consuming it) enforces template_only
            # primitives without any loop changes.
            checks.extend(
                await applied_primitive_checks(
                    ctx,
                    self._sandbox,
                    app,
                    design,
                    tree,
                    required_ids=frozenset(item.id for item in security_primitives),
                )
            )

            # last. route + section coverage + the embedded verify_web_app verdict — COMMON.
            # Vite SPAs need a platform-owned compiled preview: the model has no shell in
            # strict AppKit mode, but the browser checks must hit built JS, not /src/*.tsx.
            phase = await browser_phase_checks(ctx, self._preview, app, args.url)
            checks.append(phase.route_check)
            checks.append(phase.section_check)

            return await self._finalize(ctx, checks, app, phase)
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a verdict-shaped error
            return fail_outcome(f"verify_appkit_app error: {e}")

    async def _finalize(
        self,
        ctx: ToolContext,
        checks: list[dict[str, Any]],
        app: AppSpec | None,
        phase: _BrowserPhaseResult,
    ) -> ToolOutcome:
        artifact_identity: dict[str, str] | None = None
        if phase.retain_prepared_runtime and phase.prepared.runtime is not None:
            artifact_identity, seal_evidence = await _sealed_appkit_output_identity(ctx)
            checks.append(
                _check(
                    "sealed_output_identity",
                    artifact_identity is not None,
                    seal_evidence,
                )
            )
        verdict = build_verdict(checks, phase.embedded)
        if app is not None:
            # Target-owned semantic identity. This is intentionally separate
            # from browser-visible text so non-web target adapters can issue
            # the same claim from their own authoritative application model.
            verdict["application_title"] = app.name
        runtime_sealed = phase.prepared.runtime is not None and artifact_identity is not None
        if verdict["passed"] and runtime_sealed:
            verdict["preview_runtime"] = phase.prepared.runtime
            verdict["canonical_entry_path"] = APPKIT_CANONICAL_ENTRY_RELPATH
            verdict["artifact_identity"] = artifact_identity
        return ToolOutcome(success=True, content=_render(verdict), structured=verdict)

    async def _ensure_vite_platform_build(
        self, ctx: ToolContext, package_json: str
    ) -> _BuildResult:
        """Thin delegator kept for the existing direct-call test surface — the real
        logic lives on the `PreviewLifecycle` collaborator this tool holds."""
        return await self._preview.ensure_build(ctx, package_json)


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
