"""AppKit EPIC F — phase-based tool-scope enforcement (the L3 enforcement ladder).

AppKit builds run under a STRICT, phase-aware allowlist that BARS the raw
free-form action space (file_write / shell / code_exec / browser / …) and offers
ONLY the validated-patch AppKit mutators (app_create / app_add_section /
app_update_content / app_set_design) plus reads, design probes, and the plan
gate.  The enforcement is on the SECURITY allowlist (`ToolScope.allowed_tools`),
not merely the advertised/visible set: a barred raw tool called by its qualified
name is REFUSED (`unknown_tool`) by the executor, never silently executed.

Three phases (a function of the loop's operating mode AND the AppKit phase):

* ``planning``      — the loop is gathering context / proposing a plan
                      (loop ``mode == PLANNING``): reads + ``submit_plan`` only.
                      No mutators, escape hatch, or raw tools.
* ``planning`` (execution) — plan approved, app not yet scaffolded: the bootstrap
                      mutator ``app_create`` unlocks (plus reads + the escape
                      hatch).  A SUCCESSFUL ``app_create`` advances to ``build``.
* ``build``         — ``.disco/appspec.json`` + ``.disco/designspec.json`` exist:
                      the full AppKit mutator set + design probes (design_lint /
                      verify_web_app) + reads + the escape hatch.  Raw free-form
                      tools stay BARRED.
* ``custom_build``  — the ``request_custom_build`` escape hatch was CONFIRMED:
                      scope widens to the normal ``agent_scope`` (full action
                      space, incl. MCP after the delta is applied).

The single source of truth is :func:`appkit_effective_scope`; the executor reads
it dynamically each turn so a loop-mode flip (PLANNING→execution) or an AppKit
phase advance is reflected immediately, without mutating the loop or the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from disco.core.llm import OperatingMode

from .registry import ToolScope

# The qualified name of the F3 escape-hatch tool. Kept here (not imported from the
# builtin module) so the scope layer has no dependency on the tool implementation.
REQUEST_CUSTOM_BUILD = "request_custom_build"

# Read / probe tools available in EVERY strict AppKit phase. Pure observation —
# never a durable change to the workspace or the outside world.
APPKIT_READ_TOOLS: frozenset[str] = frozenset(
    {"file_read", "file_list", "search", "extract", "server_status", "think"}
)

# The validated-patch AppKit mutators (spec mutate → regenerate, lint-gated). The
# ONLY world-affecting tools offered in strict AppKit mode.
APPKIT_MUTATORS: frozenset[str] = frozenset(
    {
        "app_create",
        "app_add_section",
        "app_update_content",
        "app_set_design",
        "app_add_primitive",
    }
)

# Design / readiness PROBES — verification, not productive edits. Available in the
# build phase (verify_web_app may inform readiness but does NOT widen scope).
# EPIC G — verify_appkit_app: the STRICT app verifier. It is in the build-phase
# allowlist (so it is callable + advertised in strict AppKit mode) but in NO
# normal surface scope, so its presence in the executor's tool set is exactly the
# finish gate's "strict AppKit mode" signal (normal Build keeps verify_web_app).
APPKIT_PROBES: frozenset[str] = frozenset({"design_lint", "verify_web_app", "verify_appkit_app"})

# EPIC H3 — lifecycle tools available in the BUILD phase only. app_snapshot_version
# versions the specs + generated tree (writing ONLY a `.disco/app_snapshots/` record,
# never the app tree/specs), so an app must already exist; like verify_appkit_app it
# is build-scope-only and absent from every normal surface scope.
APPKIT_LIFECYCLE: frozenset[str] = frozenset({"app_snapshot_version"})

# The bootstrap mutator — the ONLY mutator callable before an app exists.
_BOOTSTRAP = frozenset({"app_create"})

# The plan-proposal signal (loop PLANNING mode only).
_PLAN_TOOL = frozenset({"submit_plan"})


class AppKitPhase(str, Enum):
    """The AppKit lifecycle phase (orthogonal to the loop's OperatingMode)."""

    PLANNING = "planning"
    BUILD = "build"
    CUSTOM_BUILD = "custom_build"


@dataclass
class AppKitPhaseState:
    """Mutable per-conversation AppKit phase. Shared between the executor (which
    advances it on a successful app_create / a confirmed request_custom_build) and
    the scope reader. Plain dataclass — no Pydantic frozen-ness; the phase mutates."""

    phase: AppKitPhase = AppKitPhase.PLANNING


def _escape_hatch(autonomous: bool) -> frozenset[str]:
    # P1: the request_custom_build widening escape hatch is UNAVAILABLE in
    # autonomous mode (no human to confirm the widening) — it is dropped from the
    # allowlist so an autonomous call is refused (unknown_tool), never auto-approved.
    return frozenset() if autonomous else frozenset({REQUEST_CUSTOM_BUILD})


def appkit_allowed_tools(
    *,
    loop_mode: OperatingMode | None,
    phase: AppKitPhase,
    autonomous: bool,
) -> frozenset[str]:
    """The strict AppKit SECURITY allowlist for (loop_mode, phase, autonomous).

    custom_build is handled by the caller (it returns the normal agent_scope); this
    only computes the planning/build narrowed sets.
    """
    hatch = _escape_hatch(autonomous)
    # None (loop not yet registered / mode unknown) is treated as PLANNING — the
    # fail-safe direction: never auto-grant mutators or raw tools when the live
    # operating mode can't be read. The build loop ALWAYS starts in PLANNING.
    if loop_mode is None or loop_mode == OperatingMode.PLANNING:
        # Context-gathering + plan proposal. No mutators, raw tools, or widening
        # hatch: the loop's read-only planning backstop cannot offer or allow the
        # HIGH-risk request_custom_build signal, so the executor must not claim it
        # is callable in this phase either.
        return APPKIT_READ_TOOLS | _PLAN_TOOL
    # Execution mode:
    if phase == AppKitPhase.BUILD:
        return APPKIT_READ_TOOLS | APPKIT_PROBES | APPKIT_MUTATORS | APPKIT_LIFECYCLE | hatch
    # phase == PLANNING in execution mode → bootstrap: only app_create unlocked.
    return APPKIT_READ_TOOLS | _BOOTSTRAP | hatch


def appkit_effective_scope(
    *,
    loop_mode: OperatingMode | None,
    phase: AppKitPhase,
    base_scope: ToolScope,
    autonomous: bool,
) -> ToolScope:
    """The ToolScope an AppKit executor enforces RIGHT NOW.

    * ``custom_build`` → ``base_scope`` (the normal agent_scope; raw tools + MCP).
    * otherwise        → a narrowed allowlist (no raw free-form tools, no MCP).

    advertised_tools is left None (advertise all allowed): the loop's own
    PLANNING-mode tool filter handles plan-mode visibility; the security boundary
    is the allowlist this returns.
    """
    if phase == AppKitPhase.CUSTOM_BUILD:
        return base_scope
    allowed = appkit_allowed_tools(loop_mode=loop_mode, phase=phase, autonomous=autonomous)
    preset = (
        "appkit_planning"
        if loop_mode is None or loop_mode == OperatingMode.PLANNING or phase == AppKitPhase.PLANNING
        else "appkit_build"
    )
    return ToolScope(allowed_tools=allowed, preset=preset)
