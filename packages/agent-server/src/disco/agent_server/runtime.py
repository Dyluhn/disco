"""The conversation runtime — runs the agent loop with real inference (Stage 2).

Phase 0 only appended events. This wires `AgentLoop` (core) + a real Qwen-backed
router into the request path: a user message KICKS the loop, which runs in the
background, calls the model via the OpenAI adapter, and appends events — which the
store already publishes to the WebSocket the UI consumes (history-then-live).

The Research surface defaults: `NeverConfirm` (no human gate) + no tools (the
model answers directly). The grounded research pipeline (Stage 4) is exposed
separately via `research_stream()` — it is NOT the agent loop; it is the
rewrite→search→extract→rerank→generate→verify pipeline streamed as the UI's
grounded-answer frames. The Build surface's `BlastRadiusConfirm` stays dormant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    LLMSummarizingCondenser,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    PlanEvent,
    ReportEvent,
    SkillStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    render_skills_for_prompt,
)
from disco.core.context.artifact_projection import (
    artifact_paths_from_events,
    manifest_path_divergence,
    manifest_shadow_enabled,
)
from disco.core.context.ledger import ArtifactRecord
from disco.core.context.store import ArtifactMemoryStore
from disco.core.contract import (
    BuildContract,
    BuildContractRegistry,
    BuildPhaseTracker,
    ContractKind,
    ContractScopeGuard,
)
from disco.core.env import disco_env
from disco.core.inspect import inspect_enabled, routing_sink_for
from disco.core.inspect import install as install_inspect
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    ConfigStore,
    DefaultLLMRouter,
    DriverPrompts,
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
    ModelExecutionPolicy,
    ModelRole,
    NoEligibleModel,
    OperatingMode,
    RouterSummarizer,
    SandboxSettings,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.llm.secrets import OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY
from disco.core.llm.wiring import build_providers, probe_all_vision
from disco.core.loop import (
    AgentLoop,
    BlastRadiusConfirm,
    BuildAgent,
    NeverConfirm,
    ResearchAgent,
    RouterAgent,
    signals,
)
from disco.core.loop.context_budget import derive_context_caps  # noqa: E402
from disco.core.security import RuleBasedAnalyzer
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.deep_research import (
    DepthTier,
)
from disco.retrieval.wiring import retrieval_capability_handlers
from disco.tools import (
    REGISTRY_EGRESS_ALLOW,
    Capability,
    CapabilityBroker,
    DefaultToolExecutor,
    SandboxService,
    SandboxSession,
    SandboxSpec,
    ToolDef,
    agent_scope,
    artifact_scope,
    build_default_registry,
)

# MCP client pool (RP-05 rung A) — built once at start, snapshotted per conversation.
from disco.tools.mcp import McpPool
from disco.tools.projects import (
    ProjectStore,
    StorageStatus,
)
from disco.tools.sandbox import (
    SandboxConfig,
    SandboxInstance,
    SandboxUnavailableError,
    preflight_build_sandbox_backend,
    service_from_config,
)
from disco.tools.sandbox._container import PREVIEW_PORT
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

from .build_kernel import BuildKernel, DiscoKernel, PiKernel, select_kernel  # noqa: E402
from .control_ops import ControlOps
from .deep_research_service import DeepResearchService
from .lifecycle import _GATE_STATES, LifecycleManager
from .mcp_manager import McpManager
from .pi_inference import PiInferenceTokenStore  # noqa: E402
from .preview_service import PreviewService
from .resume_service import ResumeService
from .runtime_model_probe import _do_live_model_probe, _model_label
from .runtime_settings import RuntimeSettings
from .schedule_service import ScheduleService
from .sessions_service import SessionsService
from .share_service import ShareService
from .title_service import TitleService

# WALK-18 — max seconds resume waits for a cooperatively-cancelled loop task to
# wind down before hard-cancelling it (a cooperative Stop already persisted the
# terminal status, so the task returns at its next step boundary; the cap only
# guards against a wedged model step).
_RESUME_DRAIN_TIMEOUT_S = 10.0


class _MCPToolWrapper:
    """Thin Tool-protocol wrapper that adapts an MCP ToolDef for the registry.
    MCP tools run through the pool's per-server client (routed by qualified name),
    not through the default executor's sandbox path. The definition is the ToolDef
    built at pool start; run() invokes the real tool via the pool."""

    def __init__(self, tdef: Any, pool: McpPool) -> None:
        self.definition = tdef
        self._pool = pool

    async def run(self, args: Any, ctx: Any) -> Any:
        from disco.tools.anatomy import ToolOutcome
        from disco.tools.mcp import fence_mcp_result
        from disco.tools.mcp.naming import split_qualified_name

        qn = self.definition.name
        parts = split_qualified_name(qn)
        if parts is None:
            return ToolOutcome(
                success=False,
                content=f"MCP tool {qn!r}: not a valid qualified name",
                error="invalid qualified name",
            )
        server, tool = parts
        try:
            # Convert validated pydantic model back to plain dict
            if hasattr(args, "model_dump"):
                raw_args = args.model_dump()
            else:
                raw_args = dict(args)
            result = await self._pool.call_tool(server, tool, raw_args)
            # RP-05b §4: MCP output is UNTRUSTED — every MCP channel is injectable
            # (CyberArk/MCPTox). Fence the raw result so the model sees it as data,
            # not instruction; the fenced block is appended as text and is NEVER fed
            # back into the tool-call parser. THIS is the production call site for
            # fence_mcp_result — the fence is dead unless it wraps output here.
            content = fence_mcp_result(server, tool, result)
            return ToolOutcome(
                success=not result.get("isError", False),
                content=content,
            )
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content=f"MCP tool {qn!r} call failed: {exc}",
                error=str(exc),
            )

class _MetaToolSearchWrapper:
    """Wraps the tool_search meta-tool with the full MCP tool list for searching.

    The LLM calls this when the pool is over the schema cap. It runs an
    orchestrator-side keyword search over ALL MCP tools (including those hidden
    from the advertised set) and returns qualified names + descriptions. The
    active schema set is never mutated — discovery only.
    """

    def __init__(self, tdef: Any, all_tool_descs: list[dict]) -> None:
        self.definition = tdef
        self._all_tool_descs = all_tool_descs

    async def run(self, args: Any, ctx: Any) -> Any:
        import json

        from disco.tools.anatomy import ToolOutcome
        from disco.tools.mcp.tool_search import _tool_search_handler

        results = await _tool_search_handler(
            query=args.query,
            limit=args.limit,
            all_tools=self._all_tool_descs,
        )
        content = json.dumps(results, indent=2) if results else "No matching tools found."
        return ToolOutcome(success=True, content=content, structured={"results": results})


def _apply_mcp_scope(
    executor: DefaultToolExecutor,
    all_mcp_tools: list,
    call_target: Any,
    *,
    max_active_schemas: int = 20,
) -> None:
    """Register MCP tools in the executor and apply the §6 advertised/callable split.

    All MCP tools are registered as callable (added to allowed_tools). Over the
    cap, only non-MCP tools + tool_search are advertised to the LLM; under the
    cap, advertised_tools stays None so all allowed tools are shown (unchanged
    behavior). The planner-safety readonly_tool_names backstop always keys off
    allowed_tools, not the advertised subset.
    """
    if not all_mcp_tools:
        return

    mcp_names = frozenset(t.name for t in all_mcp_tools)
    non_mcp_allowed = executor._scope.allowed_tools

    # Extend the security allowlist: ALL MCP tools are callable by qualified name.
    executor._scope = executor._scope.model_copy(
        update={"allowed_tools": non_mcp_allowed | mcp_names}
    )
    for tdef in all_mcp_tools:
        executor._registry.register(_MCPToolWrapper(tdef, call_target))

    # §6 cap: over the limit, restrict the ADVERTISED set to non-MCP + tool_search.
    if len(all_mcp_tools) > max_active_schemas:
        from disco.tools.mcp.tool_search import meta_tool_search

        ts_def = meta_tool_search()
        all_tool_descs = [
            {"name": t.name, "description": t.description}
            for t in all_mcp_tools
        ]
        executor._registry.register(_MetaToolSearchWrapper(ts_def, all_tool_descs))
        # tool_search must be in allowed_tools (callable) and advertised_tools (visible).
        executor._scope = executor._scope.model_copy(
            update={
                "allowed_tools": executor._scope.allowed_tools | {"tool_search"},
                "advertised_tools": non_mcp_allowed | {"tool_search"},
            }
        )
    elif executor._scope.advertised_tools is not None:
        # Under cap but advertised_tools was already explicitly set (e.g. W4 weak-tier
        # withholding). Extend it with the MCP tool names so they appear in the LLM's
        # tool list. If advertised_tools is None (show-all), leave it None — no change.
        executor._scope = executor._scope.model_copy(
            update={"advertised_tools": executor._scope.advertised_tools | mcp_names}
        )


_LOG = logging.getLogger(__name__)


def _has_unfinished_plan(events: list) -> bool:
    """True when an approved plan exists but FINISHED was never recorded — the
    conversation was interrupted mid-execution (server crash, manual cancel)."""
    has_plan = any(isinstance(e, PlanEvent) for e in events)
    if not has_plan:
        return False
    has_finished = any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
        for e in events
    )
    return not has_finished


def build_sandbox_service(settings: SandboxSettings) -> SandboxService:
    """Map the persisted SandboxSettings → the concrete SandboxBackend — the ONE place
    that knows the backend↔config mapping (settings drive the active backend). Podman is
    real, verified backend code, but a STUB in THIS environment (VM 202 destroyed) — it
    constructs but isn't live/verifiable here; completed at the Meta deployment."""
    cfg = SandboxConfig(
        backend=settings.backend,
        docker_socket=settings.docker_socket,
        podman_url=settings.podman_url,
        runtime=settings.runtime,
        image=settings.image,
        workspace_root=settings.workspace_root,
        # the host previews are reachable at — set PMX_PREVIEW_HOST to a LAN/tailnet IP so
        # previews work from other devices, not just the agent-server's host (else derived).
        preview_host=disco_env("PREVIEW_HOST", ""),
    )
    # The backend↔config mapping lives in ONE place (disco.tools.sandbox) so the
    # Settings connectivity preflight (ConfigState.test_sandbox) builds the SAME
    # backend this live builder does — no parallel mapping to drift. Podman remains a
    # stub in THIS environment (see docstring); it constructs but isn't live here.
    service = service_from_config(cfg)
    # EPIC H (P0) FAIL-CLOSED production-validity preflight. This is THE Build/soak
    # sandbox builder, so it refuses the unisolated `process` dev backend BY DEFAULT —
    # `process` shares the host PID + network namespace and is the source of the
    # `kill <pid>` takedown incidents. A developer running plain local dev re-permits it
    # with DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1. This INVERTS the old fail-OPEN
    # DISCO_REQUIRE_PRODUCTION_SANDBOX opt-in (which silently left soak/Build on `process`
    # unless an operator REMEMBERED the protection var). Container backends always pass.
    return preflight_build_sandbox_backend(service)


# Live model-server probe: derive the ACTUALLY-SERVED model name + context window
# from the backend, rather than trusting the static ModelEntry — which drifts (a
# config still saying "Qwen3.6-27B @131072" while llama.cpp serves gemma-4-E4B at
# whatever -c it was launched with). Same ground-truth-over-declaration principle
# as the build loop's live workspace snapshot: read the truth, don't assume it.
# Best-effort + cached per base_url; ANY failure falls back to the static config.
# Each entry is (result, monotonic_ts) so a model hot-swap (the driver is
# restarted with a different served model / n_ctx) is re-probed once the
# cached entry ages past _PROBE_TTL_S. Bare-dict entries are tolerated as a
# legacy / test-only direct-set form and are always served fresh.
_LIVE_MODEL_PROBE_CACHE: dict[str, tuple[dict[str, Any], float] | dict[str, Any]] = {}
# 60s is long enough that an idle agent-server isn't re-probing on every
# /models or /health hit, and short enough that a llama.cpp restart with a
# different served model is picked up within a minute.
_PROBE_TTL_S: float = 60.0
# base_urls with a probe thread currently in flight — so N concurrent on-loop
# callers (a /models + /health + first kick arriving together) schedule at most ONE
# worker thread per url instead of one each.
_LIVE_MODEL_PROBE_INFLIGHT: set[str] = set()
# CW-1: MODEL-SPECIFIC context windows derived from the OpenAI-compatible /models
# listing (OpenRouter `context_length`), kept SEPARATE from the base_url-keyed
# _LIVE_MODEL_PROBE_CACHE: one /models fetch covers every model at a base_url, but
# the value is per-model, so it is cached as a {model_id: context_length} MAP and
# looked up by model_id. Same _PROBE_TTL_S + non-blocking off-loop fill as /props.
# Written by _models_context_length (runtime_model_probe.py, via late-bound import).
_MODELS_CTX_CACHE: dict[str, tuple[dict[str, int], float]] = {}


def _probe_live_model(
    base_url: str | None, api_key: str | None = None, model_id: str | None = None
) -> dict[str, Any]:
    """Return {"model_id": str|None, "n_ctx": int|None} for a llama.cpp /
    OpenAI-compatible server, from cached probes. NEVER blocks a running event loop —
    that was the North Star #25 wedge: /props (and now /models) can hang the full 2s
    when the backend is slow/down, and this is reached from async routes (/models,
    /health) AND from kick()'s synchronous loop composition, all on the loop. When a
    loop is running, the blocking probe is offloaded to the default threadpool
    (fire-and-forget — it fills the caches) and THIS call returns the static fallback;
    the next call is served live from cache. Off the loop (CLI / worker thread) it
    blocks directly. Best-effort: Nones on any miss → caller uses the static config.

    Two cache sources (CW-1): the SERVER-WIDE /props n_ctx (base_url-keyed) and, when
    /props yields no n_ctx and a `model_id` is given, the MODEL-SPECIFIC /models
    context_length served from the separate `_MODELS_CTX_CACHE` map (looked up by
    model_id — never pinned base_url-wide)."""
    if not base_url:
        return {"model_id": None, "n_ctx": None}

    # Server-wide /props value (cached by base_url, TTL-guarded). `props` is the cached
    # {model_id, n_ctx} dict if a FRESH entry exists, else None (miss/stale → re-probe).
    props: dict[str, Any] | None = None
    cached = _LIVE_MODEL_PROBE_CACHE.get(base_url)
    if cached is not None:
        # Canonical form: (result, monotonic_ts). Within _PROBE_TTL_S the cached value
        # is served as-is. Past the TTL we treat it as a miss so a model hot-swap
        # (driver restarted with a different served model / n_ctx) is observed within a
        # minute (T6/E2).
        if isinstance(cached, tuple) and len(cached) == 2:
            value, ts = cached
            if (time.monotonic() - ts) <= _PROBE_TTL_S:
                props = value
        else:
            # Legacy / test-only direct-set: bare dict, no ts. Treat as fresh —
            # preserves the existing non-blocking test that simulates a worker thread
            # filling the cache directly.
            props = cached

    # Model-specific /models map (cached by base_url, TTL-guarded), looked up by
    # model_id. A FRESH entry — even an empty map (negative cache: this backend's
    # /models has no context_length, e.g. MiniMax) — is an answer and suppresses a
    # re-probe within the TTL.
    models_map: dict[str, int] | None = None
    if model_id:
        mcached = _MODELS_CTX_CACHE.get(base_url)
        if isinstance(mcached, tuple) and len(mcached) == 2:
            cmap, mts = mcached
            if (time.monotonic() - mts) <= _PROBE_TTL_S:
                models_map = cmap

    if model_id:
        # The window can come from EITHER source; if either cache is fresh we can
        # answer (props n_ctx wins; else the model's /models context_length).
        if props is not None or models_map is not None:
            n_ctx = props["n_ctx"] if props is not None else None
            if n_ctx is None and models_map is not None:
                n_ctx = models_map.get(model_id)
            return {
                "model_id": props["model_id"] if props is not None else None,
                "n_ctx": n_ctx,
            }
    elif props is not None:
        return props

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        # Don't block the loop. Schedule ONE probe thread per base_url; concurrent
        # callers that arrive before the cache fills skip (in-flight) rather than each
        # spawning a redundant thread. The wrapper clears the in-flight marker in a
        # finally, so a FAILED probe (cache stays empty) can be retried on a later
        # call. The in-flight check runs only on the single-threaded loop, so the
        # check-then-add is race-free; the worker thread only discards.
        if base_url not in _LIVE_MODEL_PROBE_INFLIGHT:
            _LIVE_MODEL_PROBE_INFLIGHT.add(base_url)

            def _probe_then_clear(
                u: str = base_url, k: str | None = api_key, mid: str | None = model_id
            ) -> None:
                try:
                    # Pass model_id only when present so the legacy 2-arg probe
                    # contract (existing monkeypatched test fakes) keeps working.
                    if mid is None:
                        _do_live_model_probe(u, k)
                    else:
                        _do_live_model_probe(u, k, mid)
                finally:
                    _LIVE_MODEL_PROBE_INFLIGHT.discard(u)

            running.run_in_executor(None, _probe_then_clear)
        return {"model_id": None, "n_ctx": None}
    if model_id is None:
        return _do_live_model_probe(base_url, api_key)
    return _do_live_model_probe(base_url, api_key, model_id)


class _NoToolExecutor:
    """A read-only surface with no tools: the model answers directly. Any tool the
    model hallucinates fails loudly as an observation (it has none to call)."""

    def available_tools(self) -> list:
        return []

    async def execute(self, call) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content="",
            error="no tools are available on this surface",
        )


def _release_process_memory() -> None:
    """Hand freed heap pages back to the OS after a memory-heavy run.

    Python frees objects, but glibc's allocator keeps the arenas instead of
    `munmap`-ing them — so after a bursty Deep Research run (concurrent legs
    fetching full pages + stacking ONNX batches) the process RSS stays pinned at
    the peak high-water mark forever, leaving a long-lived agent-server bloated
    and the next heavy op with less headroom. `gc.collect()` drops any lingering
    cycle-held buffers; `malloc_trim(0)` then returns the now-free arena pages.

    This is the retention half of the OOM root cause — its companion is the
    gather-leg concurrency cap that bounds the PEAK in the first place. Best
    effort: a non-glibc libc (musl / macOS) simply has no `malloc_trim`, and the
    gc pass still ran. Set `DISCO_DR_MALLOC_TRIM=0` to disable.
    """
    import gc

    gc.collect()
    if (os.environ.get("DISCO_DR_MALLOC_TRIM") or "1").strip().lower() in (
        "0", "off", "false", "none",
    ):
        return
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except (OSError, AttributeError, ValueError):  # non-glibc / unavailable
        pass


class ConversationRuntime:
    """Builds a router from the CURRENT persisted config per request, plus one
    AgentLoop per conversation. `kick(cid)` schedules the loop in the background.

    Model assignments live in a shared ConfigStore (PMX_CONFIG) that the Settings
    UI writes; `_router_now()` reloads it each request, so reassigning a role takes
    effect on the next research call / new conversation without a restart."""

    def __init__(
        self,
        store: SqliteEventStore,
        *,
        config: RouterConfig | None = None,
        config_store: ConfigStore | None = None,
        secret_store: SecretStore | None = None,
        router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
        research_providers: dict[str, Any] | None = None,
        sandbox_service: SandboxService | None = None,
        sandbox_spec: SandboxSpec | None = None,
        skill_store: SkillStore | None = None,
        pi_token_store: PiInferenceTokenStore | None = None,
    ) -> None:
        self._store = store
        # EPIC C — the DiscoInferenceGateway's ephemeral, run-scoped token store. The
        # token authorizes a Pi kernel to drive the UI-selected model over the loopback
        # gateway; it MUST be revoked the moment a run ends so a kernel can't keep
        # calling the model after FINISHED/ERROR/STUCK/IDLE/cancel/kill/delete. Wired
        # by `create_app` (which owns the store on app.state) via `attach_pi_token_store`
        # — OPTIONAL/None here so tests + the non-gateway paths never depend on it. All
        # revocation flows through `_revoke_pi_tokens` (None-safe + idempotent).
        self._pi_token_store = pi_token_store
        # The user's reusable instruction modules (.md skills). Read per-request
        # so a skill toggled in Settings affects the next conversation without a
        # restart — same live-reload model as the config + sandbox stores.
        self._skill_store = skill_store or SkillStore()
        # The Build surface runs tools through a SandboxBackend. An explicitly injected
        # service is an OVERRIDE (tests / `PMX_SANDBOX` at startup); otherwise the backend
        # is read PER-REQUEST from the persisted SandboxSettings (the Settings selector),
        # mirroring how `_router_now()` reloads model assignments — so a settings change
        # drives the next conversation's sandbox without a restart.
        self._injected_sandbox = sandbox_service
        self._sandbox_spec = sandbox_spec or SandboxSpec()
        # per-conversation driver model override (the Build chat model picker → the
        # AGENT_DRIVER for that conversation; RouterAgent applies it).
        # B0: PERSISTED (not just in-memory) — a server restart used to silently revert
        # every conversation's picked model to the default. Persisted to a JSON sidecar
        # next to the event DB (PMX_DB) so a resumed conversation keeps its model.
        db_path = disco_env("DB", "")
        # Persisted per-conversation settings (B0): override / surface / autonomous /
        # assist accessors. The dicts + sidecar paths stay declared below on the
        # runtime; the stateless service reaches them via a back-ref. Constructed
        # FIRST because the _load_* calls in this __init__ route through it.
        self._settings = RuntimeSettings(self)
        # Auto-titling: derive a short display title from the first user message so
        # History/Projects show real names, not a wall of "(untitled)". Fire-and-forget
        # from kick(); uses the cheap SUMMARIZER role; idempotent + non-blocking.
        self._title_service = TitleService(self._store, self._router_now)
        self._override_path = f"{db_path}.overrides.json" if db_path else ""
        self._model_override: dict[str, str] = self._load_overrides()
        # Per-conversation surface ("research" | "build" | "deep_research"); set at
        # create time. PERSISTED to a JSON sidecar (B0 pattern, same as the model
        # override above) — DC-05 re-run #7 (2026-06-11): after a server restart the
        # in-memory dict was empty, _surface_of's recovery ladder needs a project
        # manifest to derive "build" but the manifest is only written by
        # _maybe_snapshot (which no-ops without a configured projects_root), so a
        # resumed Build conversation silently composed on the research surface →
        # _NoToolExecutor → the model's ONLY offered tool was `finish`. The whole
        # post-resume "degeneration" was a toolless loop, not model failure. The
        # _surface_of ladder stays as the fallback for pre-fix sidecar-less DBs.
        self._surface_path = f"{db_path}.surfaces.json" if db_path else ""
        self._surface: dict[str, str] = self._load_surfaces()
        # Per-conversation AUTONOMOUS flag (issue A), same B0 sidecar pattern as
        # surface. True = headless/unattended: the loop withholds ask_user, auto-
        # approves the plan, and forfeits cleanly instead of halting for a human.
        self._autonomous_path = f"{db_path}.autonomous.json" if db_path else ""
        self._autonomous: dict[str, bool] = self._load_autonomous()
        # Per-conversation ASSIST tier flag (T1), same B0 sidecar pattern.
        self._assist_path = f"{db_path}.assist.json" if db_path else ""
        self._assist: dict[str, bool] = self._load_assist()
        # C6: per-conversation artifact_mode flag. In-memory only — set at create
        # time from the body; artifact sessions are short-lived, no sidecar needed.
        self._artifact_mode: dict[str, bool] = {}
        # CONTRACT-ACTIVATE: per-conversation Build contract + live phase tracker. In an
        # artifact-mode run the executor's ContractScopeGuard reads the tracker's phase
        # to gate tools (e.g. no raw rewrite during the edit phase). Default contract is
        # CUSTOM (permissive but real); an optional declared kind narrows it.
        self._build_contract_registry = BuildContractRegistry.default()
        self._build_kind: dict[str, str] = {}
        self._build_trackers: dict[str, tuple[BuildContract, BuildPhaseTracker]] = {}
        # P3 — global last-selected driver model (single-value sidecar). Persisted
        # so a new conversation seeds from whatever the user picked last; falls back
        # to RouterConfig.default_model when never set. B0 pattern (atomic writes).
        self._last_model_path = f"{db_path}.last_model.json" if db_path else ""
        # Per-conversation server-side uploads sidecar directory (B0 pattern).
        # DC-07 (2026-06-11): uploads survive sandbox recreation.
        self._uploads_base = f"{db_path}.uploads" if db_path else ""
        self._executors: dict[str, DefaultToolExecutor] = {}
        self._pending_sessions: dict[str, SandboxSession] = {}
        self._cap_handlers: dict[str, Any] | None = None
        # per-conversation Deep Research depth tier (set at submit time).
        self._depth: dict[str, str] = {}
        # The encrypted-at-rest secret store (OpenRouter key). Its decrypted key is
        # overlaid into the provider env per request; if it's locked/empty the env
        # value (if any) is used instead.
        self._secret_store = secret_store or SecretStore()
        # The live retrieval/grounding providers for research_stream(). Injected
        # in tests (hermetic fakes); else lazily built from env on first use so
        # importing the runtime doesn't pull httpx until research is actually run.
        self._injected_research_providers = research_providers  # test fake (or None)
        self._research_providers: dict[str, Any] | None = None  # lazy cache (non-test)
        # the (remote, reranker_url, embedder_url, nli_url) tuple the cache was built
        # for — rebuild when the mode OR any endpoint URL changes.
        self._research_encoders_key: tuple | None = None
        # A statically-injected router (test seam) pins routing; otherwise the
        # router is rebuilt per request from the shared, persisted config store so
        # Settings assignments are actually honored.
        self._injected_router = router
        self._enable_thinking = enable_thinking
        # W-35: short-TTL success cache for the driver pre-flight, keyed by the
        # RESOLVED driver model key → monotonic timestamp of the last OK probe. A
        # healthy driver is re-probed at most once per _DRIVER_PREFLIGHT_TTL_S, so
        # back-to-back kicks don't each pay a live round-trip.
        self._driver_preflight_ok: dict[str, float] = {}
        # W-35 (resilience): (conversation_id, role, resolved_model_key) tuples whose
        # driver has ALREADY answered a pre-flight successfully in THIS process. An
        # established run that has proven THAT SPECIFIC driver reachable must NOT be
        # hard-failed by a single transient pre-flight timeout (a remote reasoning
        # model — e.g. minimax — is intermittently slow under load): for these we
        # soft-degrade a transient probe failure to a warning and let the REAL call
        # surface a genuine error. Keying on (role, model) — not the conversation
        # alone — means a build's proven AGENT_DRIVER can NOT mask a genuinely-dead
        # DR RAG_ANSWERER (or a switched model) in the same conversation.
        self._driver_proven: set[tuple[str, ModelRole, str]] = set()
        if config_store is not None:
            self._config_store = config_store
        elif config is not None:
            self._config_store = ConfigStore(base_factory=lambda: config)
            # P4 footgun guard: a passed `config=` is only the SEED — ConfigStore.load()
            # prefers an on-disk config file (DISCO_CONFIG / disco-config.json) and
            # silently shadows the in-process object. Warn once so this doesn't cost
            # debugging time (it cost a live driver-acceptance debug on 2026-06-18).
            if self._config_store.path.exists():
                _LOG.warning(
                    "ConversationRuntime(config=...) is shadowed by the on-disk config "
                    "at %s — _router_now reloads from disk per request. Pass config_store= "
                    "or point DISCO_CONFIG at your file to override routing.",
                    self._config_store.path,
                )
        else:
            self._config_store = ConfigStore()
        self._mode = mode
        # DISCO_INSPECT: attach the span-capture handler once, at construction, so
        # both the live server and any test that builds a runtime get per-request
        # traces. Idempotent + no-op when the flag is off.
        if inspect_enabled():
            install_inspect()
        self._loops: dict[str, AgentLoop] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # W11 (silent RUNNING-stall recovery): bounded re-kick budget per cid. When
        # loop.run() RETURNS while the conversation is still RUNNING — a dropped /
        # unparseable model response ended the turn with no terminal StatusEvent, so
        # the loop's `return await self.get_state()` carries RUNNING straight out (the
        # silent hang Dylan hit on the MiniMax builds) — we re-kick ONCE to recover a
        # transient drop, then terminalize honestly to STUCK so a wedged build is
        # VISIBLE, never RUNNING forever. Reset whenever a run reaches a real status.
        self._nonterminal_rekicks: dict[str, int] = {}
        # [W-48 P1] Last status a run task ENDED at, per cid (in-process). The sync
        # `_evict_stale_backend` (called at kick) can't await the store, so it reads
        # this to SKIP a conversation PARKED at a gate — evicting a gated conv on a
        # backend change would discard its mid-gate workspace. Sound because eviction
        # only ever touches an IN-PROCESS cached session, and the only way to reach a
        # gate with such a session is a run that ended here (→ _finalize_clean_return,
        # which records it). After a restart the caches are empty → nothing to evict.
        self._last_status: dict[str, ConversationStatus] = {}
        # Cooperative-cancellation flags for Deep Research (whose engine isn't an
        # AgentLoop and can't be soft-cancelled the loop's way). Stop sets the flag;
        # the engine polls it at each sub-question/section boundary and halts,
        # keeping the partial report (resumable). See _execute_deep_research.
        self._cancel_flags: dict[str, asyncio.Event] = {}
        # D3: per-cid mid-run steer / inject-source queues. Populated by the WS
        # `steer` / `inject_source` frame handlers when a DR run is in progress;
        # drained at each section boundary by the engine's pop_steers /
        # pop_injected_sources hooks. Keys are ONLY present during an active DR run
        # (_execute_deep_research initialises them, `finally` removes them).
        # _dr_injected_sources stores pre-converted Passage objects (the WS handler
        # converts raw text to a Passage immediately on receipt).
        self._dr_steer: dict[str, list[str]] = {}
        self._dr_injected_sources: dict[str, list[Any]] = {}  # list[Passage]
        # G1/DR-4: per-conversation upload corpus (text files ingested at upload
        # time → citable Passages for both basic research and DR runs).
        # Keyed by conversation_id; value is a flat list of Passage objects.
        # In-process only (cleared on restart); the sandbox sidecar holds the
        # raw bytes for persistence (the corpus is re-ingested lazily on demand
        # in a future persistence upgrade).
        self._upload_passages: dict[str, list[Any]] = {}  # list[Passage]
        # Auto-suspend (lifecycle G): a build session is live only while a UI is
        # watching it. Track open WS connections per conversation; when the last one
        # closes, free the idle sandbox after a grace period (a quick reconnect — or
        # the WS-reconnect backoff — cancels it).
        self._connections: dict[str, int] = {}
        self._suspend_tasks: dict[str, asyncio.Task] = {}
        # BP-14: per-(cid, name) coalescing cache for capture-pane calls.
        self._session_view_cache: dict[tuple[str, str], tuple[float, SessionView]] = {}
        self._session_view_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._wake_locks: dict[str, asyncio.Lock] = {}
        # DC-04b: last-known session list per cid for stale-on-failure degradation.
        self._last_sessions: dict[str, list[SessionInfo]] = {}
        # RP-05 rung A: MCP client pool — built once at start from RouterConfig.mcp.
        self._mcp_pool: McpPool | None = None
        # Servers that need re-approval (ApprovalRequired at startup). Emitted to
        # WS clients as mcp_approval_required frames.
        self._mcp_approval_pending: dict[str, dict] = {}
        # RP-05 rung B: MCP HTTP clients (streamable_http transport). Managed
        # separately from the stdio pool; tools are merged at compose time.
        self._mcp_http_clients: dict[str, Any] = {}  # server_name -> McpHttpClient
        self._mcp_http_tools: dict[str, ToolDef] = {}  # qualified_name -> ToolDef
        # RP-05 rung B: retrieval-tier MCP providers (search/extract Protocol wrappers).
        self._mcp_retrieval_searches: list = []
        self._mcp_retrieval_extractions: list = []
        # MCP lifecycle logic (pool start/stop, HTTP clients, retrieval-tier,
        # egress/proxy posture, approval drift). The state above stays on the
        # runtime (tests + _compose_build_loop read it); the manager reaches it
        # via a back-reference. See mcp_manager.py.
        self._mcp = McpManager(self)
        # Share surface (RP-06): scrubbed bundle export/import + revocable
        # share-link issuance. Stateless; wired with the store + resolvers.
        self._share = ShareService(
            self._store, self._surface_of, self._project_store_now
        )
        # Resume path (DC-05b/c): trailing-degeneracy condensation + resume-context
        # reconstruction + the resume_conversation orchestrator. Reaches live
        # runtime state via a back-reference. See resume_service.py.
        self._resume = ResumeService(self)
        # Sandbox lifecycle (auto-suspend / idle sweep / orphan reconcile /
        # snapshot+rehydrate). _connections/_suspend_tasks stay on the runtime
        # (tests read them); the manager reaches state via a back-ref. See
        # lifecycle.py.
        self._lifecycle = LifecycleManager(self)
        # Deep Research surface (plan→iterate→report) + live research stream.
        # _depth / _research_* cache / _cancel_flags stay on the runtime
        # (_cancel_flags is shared with kill/cancel/resume); the service reaches
        # state via a back-ref. See deep_research_service.py.
        self._dr = DeepResearchService(self)
        # Scheduled tasks (RP-08): lazy ScheduleManager accessor + CRUD/preview +
        # run-history read. The lazily-created _sched_manager handle stays on the
        # runtime; the service reaches it + _store via a back-ref. See
        # schedule_service.py.
        self._schedule = ScheduleService(self)
        # Shell-session reads + upload write-through. The caches + tuning
        # constants stay on the runtime; the service reaches them + live_session
        # via a back-ref. See sessions_service.py.
        self._sessions = SessionsService(self)
        # Live-preview proxy + suspended-sandbox wake. Stateless; reaches
        # _executors / _wake_locks / _store + the loop/rehydrate resolvers via a
        # back-ref. See preview_service.py.
        self._preview = PreviewService(self)
        # Conversation control ops (the confirmation gate + kill switch). Stateless;
        # reaches _loops / _tasks / _executors / _pending_sessions / _cancel_flags /
        # _store + _loop_for / kick via a back-ref. See control_ops.py.
        self._control = ControlOps(self)
        # Build kernel seam (Disco Pi campaign A1/A2). The current loop runs through
        # `DiscoKernel` (a thin pass-through to _control / kick / _store, ZERO behavior
        # change); the public plan/action-gate methods route through `_kernel_for`, so a
        # future PiKernel can be selected in Settings without the routes caring which
        # inner agent ran. Both kernels hold only a back-ref (like _control). See
        # build_kernel/.
        self._disco_kernel = DiscoKernel(self)
        self._pi_kernel = PiKernel(self)
        # The kernel PINNED to each conversation's in-flight run (codex finding #1).
        # A run resolves its kernel ONCE, at the turn that starts it (via
        # `_ensure_kernel_pinned`), and every later op (gate resume, steer, control
        # op) reuses the pinned instance — so a mid-run Settings/flag change can
        # never split a run across kernels (half-disco/half-pi). Cleared when the
        # run reaches a terminal status (FINISHED/ERROR/STUCK) or is killed, so the
        # NEXT turn re-resolves the current selection. A pause/gate-park keeps it.
        self._pinned_kernels: dict[str, BuildKernel] = {}
        # Per-conversation RUN-GENERATION counter (finding #3, the pin set/clear race).
        # `kick` bumps it every time it spawns a NEW run task; the done-callback closes
        # over the generation it was spawned under and `_finalize_clean_return` only
        # CLEARS the pin if that generation is STILL current. So a finalizer that runs
        # AFTER a new turn already reused the pin (the async-finalize race) cannot clear
        # the pin out from under the newer run. A steer (kick early-returns over a live
        # task) does NOT bump it — same run, same generation, same pin.
        #
        # TERMINALIZER AUDIT — every path that appends a terminal status / clears the pin
        # AFTER an await (where a newer run can reuse the conversation) must be guarded by
        # this generation, re-checking it with NO await between the guard and the append:
        #   - `_finalize_clean_return`  (clean STUCK)  — guarded [#3]
        #   - `_terminalize_crashed`    (crash ERROR)  — guarded [#3]
        #   - `sweep_abandoned_gates_once` (gate STUCK) — guarded [#3, lifecycle.py]
        #   - `kill` / `ControlOps.kill` (IDLE 'killed') — guarded [#4]: captures the
        #     generation at entry, threads it through, and skips teardown + the terminal
        #     IDLE append (guard A after `await task`, guard B before the append) if a
        #     newer run has taken over the conversation in the teardown-await window.
        self._run_generation: dict[str, int] = {}
        # Engine-rekick fix: latest USER seq that last triggered a POST-TERMINAL
        # re-kick, per cid. A follow-up appended while a run finalizes is stranded
        # (kick() is a no-op on the live task; the done-callback finalizers don't
        # re-kick). `_maybe_rekick_for_stranded_followup` re-kicks at every terminal
        # conclusion when the (now-correct) work-gate is open, and records the
        # follow-up's seq here so it re-kicks ONCE per new follow-up: a stalled
        # same-seq segment (no real progress) never loops, a clean no-follow-up
        # finish never re-kicks. SEPARATE from `_nonterminal_rekicks` (the W11
        # RUNNING-stall budget) — this guards the terminal-conclusion path only.
        self._post_terminal_rekick_seq: dict[str, int] = {}
        # REL-2a: shadow artifact-manifest fold guard. Keyed by the latest FINISHED
        # StatusEvent seq so duplicate terminal observers fold once, while a later
        # resumed segment that emits a new FINISHED folds once for that finish too.
        self._shadow_folded_finished_seq: dict[str, int] = {}

    # The generative (text-producing) roles a model PICK drives. NLI_VERIFIER is a
    # cross-encoder (entailment scorer), NOT a chat model — pointing it at a picked
    # LLM would break verification, so it always follows its own assignment.
    _GENERATIVE_ROLES: tuple[ModelRole, ...] = (
        ModelRole.AGENT_DRIVER,
        ModelRole.RAG_ANSWERER,
        ModelRole.QUERY_REWRITER,
        ModelRole.SUMMARIZER,
    )

    def _overlay_stored_secrets(self, env: dict[str, str]) -> None:
        """Overlay every encrypted-at-rest provider key into `env` under its
        env-var name, so build_providers authenticates from the store. The
        reserved "openrouter" slot maps to OPENROUTER_API_KEY_ENV; all other
        stored secrets are keyed BY their api_key_env name, so name == env var.
        A stored value WINS over a pre-existing plaintext env var of the same
        name (the encrypted source is authoritative)."""
        for name in self._secret_store.secret_names():
            value = self._secret_store.get_secret(name)
            if not value:
                continue
            if name == "openrouter":
                # Overlay under BOTH the canonical and legacy env names: existing
                # disco-config.json entries still declare api_key_env="PMX_OPENROUTER_API_KEY",
                # so build_providers resolves the legacy name. Without this the stored key
                # never attaches → anonymous OpenRouter calls → paid models 402 "no credits".
                env[OPENROUTER_API_KEY_ENV] = value
                env[OPENROUTER_API_KEY_ENV_LEGACY] = value
            else:
                env[name] = value

    def _resolve_secret(self, name: str | None) -> str | None:
        """A provider key by its api_key_env var name: the encrypted store wins,
        else the live process env (back-compat for env-var-configured keys).
        Used by the search/extract/TTS paths that read a key directly rather than
        through build_providers' env."""
        if not name:
            return None
        return self._secret_store.get_secret(name) or os.environ.get(name)

    def _router_now(
        self,
        pick: str | None = None,
        *,
        enable_thinking: bool | None = None,
        surface: str | None = None,
        autonomous: bool = False,
        conversation_id: str | None = None,
    ) -> DefaultLLMRouter:
        """The router for the CURRENT assignments. Cheap to rebuild (providers are
        plain objects; the HTTP client is created per call), so we reload the config
        each request rather than cache a stale router. `pick` (the model pill) is a
        catalogue key the user explicitly chose for THIS conversation; `enable_thinking`
        overrides the default reasoning mode for this request (the Think toggle)."""
        if self._injected_router is not None:
            return self._injected_router
        cfg = self._config_store.load()
        # A model PICK drives the ENTIRE generative pipeline, not just one role.
        # When the user picks (say) an OpenRouter DeepSeek, the expectation is that
        # DeepSeek does the whole job — the brain AND the context condensation AND
        # query rewriting AND answer synthesis — not that the picked model "leads"
        # while local models quietly do the summarizing/rewriting underneath. So we
        # reassign every generative role to the pick for this request. (Unknown keys
        # are ignored — fail safe to the saved assignment.)
        if pick and pick in cfg.models:
            reassigned = {**cfg.assignments, **{r: pick for r in self._GENERATIVE_ROLES}}
            cfg = cfg.model_copy(update={"assignments": reassigned})
        # Overlay decrypted provider keys into the (per-request copy of the) env
        # that build_providers reads, so any model authenticates from the encrypted
        # store without its key being on disk in plaintext. OpenRouter uses a
        # reserved slot mapped to its env-var name; every other stored secret is
        # keyed BY its api_key_env var name, so it overlays onto itself.
        env = dict(os.environ)
        self._overlay_stored_secrets(env)
        thinking = self._enable_thinking if enable_thinking is None else enable_thinking
        providers = build_providers(cfg, env=env, enable_thinking=thinking)
        # DriverPrompts gives the AGENT_DRIVER role phase-aware system prompts (the
        # plan→approve→build flow); every other role/mode defers to the default
        # provider, so Research is unaffected. Enabled SKILLS (the user's reusable
        # .md instructions) are rendered and prepended to the driver prompts —
        # read fresh each request so a Settings toggle takes effect next run.
        # `surface` filters to skills scoped to it (build vs agent) — None = all.
        skills_block = render_skills_for_prompt(self._skill_store.enabled(), surface=surface)
        # The "agent" surface gets the task-agent prompt flavor (an identity reframe);
        # build + every other surface keep the build driver prompts unchanged.
        flavor = "agent" if surface == "agent" else "build"
        # DISCO_INSPECT: when on, bind a per-conversation routing sink so every
        # RoutingDecision this (per-conversation) router emits lands in the trace.
        # Off → None → the router's NullRoutingSink, i.e. zero overhead.
        sink = routing_sink_for(conversation_id)
        return DefaultLLMRouter(
            cfg,
            providers,
            prompt_provider=DriverPrompts(
                skills_block=skills_block, flavor=flavor, autonomous=autonomous
            ),
            sink=sink,
        )

    # Four surfaces: research (single-pass /ws/research stream), build (agent +
    # tools + plan gate, framed for software), agent (the SAME agent machinery
    # framed as a general task agent), deep_research (long-horizon plan → iterate
    # → report). "agent" is a framing copy of "build" — identical loop/tools/
    # sandbox/persistence — so it travels with build through every surface branch.
    _VALID_SURFACES: frozenset[str] = frozenset({"research", "build", "agent", "deep_research"})
    # The surfaces that compose the build agent loop (tools + sandbox + gate +
    # workspace snapshot/rehydrate). "build" and "agent" are behaviorally identical;
    # they differ only in frontend framing + entry. Branch on this set, never on the
    # bare string, so a new build-like surface can't silently miss a call site.
    _BUILD_LIKE_SURFACES: frozenset[str] = frozenset({"build", "agent"})
    # Surfaces that have a PLAN GATE the autonomous flag should auto-approve.
    # deep_research is NOT build-like (no build tools/sandbox) but its
    # plan→iterate→report flow DOES halt at AWAITING_PLAN_APPROVAL — so a
    # headless/autonomous DR run must auto-approve here too, or it stalls
    # forever. (research has no plan gate, so it stays excluded.)
    _AUTONOMOUS_SURFACES: frozenset[str] = frozenset({"build", "agent", "deep_research"})

    def set_surface(self, conversation_id: str, surface: str) -> None:
        """Select a conversation's surface before it runs. Build composes tools +
        sandbox + the BlastRadiusConfirm gate; Deep Research composes the plan-gate +
        the long-horizon engine; Research stays read-only + ungated. Idempotent
        until the loop is built. PERSISTED so a server restart cannot demote a
        Build/DR conversation to the toolless research default (DC-05 re-run #7)."""
        self._surface[conversation_id] = (
            surface if surface in self._VALID_SURFACES else "research"
        )
        self._save_surfaces()

    def _surface_of(self, conversation_id: str) -> str:
        """The conversation's surface, recovered durably across server restarts.
        In-memory `_surface` is authoritative when set — at create time via
        set_surface (which persists to the sidecar) or reloaded from the sidecar
        at startup. When it's missing — a pre-sidecar DB, or a sidecar lost with
        its DB — we recover from durable signals, MOST AUTHORITATIVE FIRST:
        - the `conversations.surface` column (written at create, app.py) — the
          real answer, durable in the same DB. This is the only rung that can tell
          "agent" from "build" (the project manifest can't — they snapshot
          identically) and it also fixes a pre-existing hole where a plan-gated
          Build with no manifest yet mis-derived deep_research below.
        - a project manifest on disk → a build-like surface (Research never snapshots).
        - a ReportEvent on the conversation log → Deep Research.
        Defaults to "research" when no durable signal exists. Recoveries are
        written through to the sidecar so the ladder runs at most once per cid."""
        cached = self._surface.get(conversation_id)
        if cached is not None:
            return cached
        # Rung 0 — the authoritative DB column (set at create). Cheap sync SQL on
        # the same connection the rungs below already use. This makes the
        # heuristic rungs a fallback only for legacy rows with a NULL surface.
        try:
            conn0 = getattr(self._store, "_conn", None)
            if conn0 is not None:
                row = conn0.execute(
                    "SELECT surface FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is not None and row["surface"] in self._VALID_SURFACES:
                    self._surface[conversation_id] = row["surface"]
                    self._save_surfaces()
                    return row["surface"]
        except Exception:  # noqa: BLE001 — best-effort recovery, fall through to heuristics
            pass
        store = self._project_store_now()
        if store is not None and store.status() == StorageStatus.OK:
            try:
                if store.get(conversation_id) is not None:
                    # cache + persist the recovery so subsequent lookups (and the
                    # next restart) don't re-derive
                    self._surface[conversation_id] = "build"
                    self._save_surfaces()
                    return "build"
            except Exception:  # noqa: BLE001 — best-effort recovery
                pass
        # Deep Research recovery: a ReportEvent on the log is the durable marker
        # (Research/Build never emit ReportEvent). Cheap SQL check — no need to
        # deserialize the whole log just to read the discriminator column.
        try:
            conn = getattr(self._store, "_conn", None)
            if conn is not None:
                kinds = {
                    row["kind"]
                    for row in conn.execute(
                        "SELECT DISTINCT kind FROM events WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                }
                if "report" in kinds:
                    self._surface[conversation_id] = "deep_research"
                    self._save_surfaces()
                    return "deep_research"
                # plan-event without any action-event marks a deep-research paused
                # at its plan gate (Build that survived a restart would also have
                # an on-disk project manifest, caught above).
                if "plan" in kinds and "action" not in kinds:
                    self._surface[conversation_id] = "deep_research"
                    self._save_surfaces()
                    return "deep_research"
        except Exception:  # noqa: BLE001 — best-effort recovery
            pass
        return "research"

    def _sandbox_service_now(self) -> SandboxService:
        """The active sandbox backend: the injected override if present, else built from
        the persisted SandboxSettings (reloaded each time — the Settings selector drives it)."""
        if self._injected_sandbox is not None:
            return self._injected_sandbox
        return build_sandbox_service(self._config_store.load().sandbox)

    def _driver_context_window(self) -> int | None:
        """The context window of the model currently assigned to AGENT_DRIVER, for
        A-S1's model-aware condensation threshold. Best-effort: None on any lookup
        miss so the condenser falls back to its safe defaults."""
        try:
            cfg = self._config_store.load()
            key = cfg.model_for(ModelRole.AGENT_DRIVER)
            entry = cfg.entry_for(key)
            # Prefer the model server's ACTUAL n_ctx over the static config — the
            # condenser must budget against the window the backend really serves,
            # not a config that may assume 128k (the "assuming 128k context" bug).
            live = _probe_live_model(
                entry.base_url,
                os.environ.get(entry.api_key_env) if entry.api_key_env else None,
                entry.model_id,
            )
            return live["n_ctx"] or entry.context_window
        except Exception:  # noqa: BLE001 — never block loop construction on this
            return None

    async def prewarm_model_probe(self) -> None:
        """Warm the live-model /props cache OFF the event loop at startup, so the
        FIRST build's condenser budgets against the REAL context window, not the
        static config. _probe_live_model is non-blocking on the loop (it returns the
        static fallback and fills the cache from a worker thread), so without a
        pre-warm the very first _compose_build_loop reads the fallback and caches it
        for that loop's life. Here we await the blocking probe in a thread, so by the
        time any conversation composes, the cache is hot. Passing the driver model_id
        ALSO warms the /models context_length path (CW-1) so an OpenRouter driver's
        first build budgets against its live window, not the static config.
        Best-effort: never blocks boot."""
        try:
            cfg = self._config_store.load()
            key = cfg.model_for(ModelRole.AGENT_DRIVER)
            entry = cfg.entry_for(key)
            if entry and entry.base_url:
                api_key = (
                    os.environ.get(entry.api_key_env) if entry.api_key_env else None
                )
                await asyncio.to_thread(
                    _do_live_model_probe, entry.base_url, api_key, entry.model_id
                )
        except Exception:  # noqa: BLE001 — best effort; the static config is the fallback
            pass

    async def prewarm_vision_probe(self) -> None:
        """V2/V4 (§2): run the async network vision probe ONCE at startup and install
        the results as a process-lifetime overlay on the shared ConfigStore, so every
        per-request config load reflects a model server's REAL vision modality
        (llama.cpp `/props.modalities.vision`, OpenRouter `input_modalities`) over the
        static table. Fail-soft: `probe_all_vision` never raises, and this call is
        additionally wrapped so a probe failure can NEVER block boot — the overlay is
        just not installed and load() keeps the static table."""
        try:
            self._config_store.apply_vision_probe(
                await probe_all_vision(self._config_store.load())
            )
        except Exception:  # noqa: BLE001 — best effort; the static table is the fallback
            pass

    # ---- persisted settings (B0) — delegators to RuntimeSettings ------------

    def _load_overrides(self) -> dict[str, str]:
        return self._settings._load_overrides()

    def _save_overrides(self) -> None:
        self._settings._save_overrides()

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        self._settings.set_model_override(conversation_id, model_id)

    def _load_surfaces(self) -> dict[str, str]:
        return self._settings._load_surfaces()

    def _save_surfaces(self) -> None:
        self._settings._save_surfaces()

    def _load_autonomous(self) -> dict[str, bool]:
        return self._settings._load_autonomous()

    def _save_autonomous(self) -> None:
        self._settings._save_autonomous()

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
        self._settings.set_autonomous(conversation_id, value)

    def _effective_autonomous(self, conversation_id: str) -> bool:
        return self._settings._effective_autonomous(conversation_id)

    def is_autonomous(self, conversation_id: str) -> bool:
        return self._settings.is_autonomous(conversation_id)

    def _is_small_assist_default(self, entry: Any) -> bool:
        return self._settings._is_small_assist_default(entry)

    def _load_assist(self) -> dict[str, bool]:
        return self._settings._load_assist()

    def _save_assist(self) -> None:
        self._settings._save_assist()

    def set_assist(self, conversation_id: str, value: bool = True) -> None:
        self._settings.set_assist(conversation_id, value)

    def _effective_policy(self, conversation_id: str) -> ModelExecutionPolicy:
        """Delegator: the SINGLE source of truth for model-tier execution. See
        RuntimeSettings._effective_policy for the full contract."""
        return self._settings._effective_policy(conversation_id)

    def _effective_driver_endpoint(
        self, conversation_id: str
    ) -> tuple[str, str, str | None] | None:
        """Delegator (ROOT-5): the conversation's override-aware driver endpoint for
        LLM-using tools. See RuntimeSettings._effective_driver_endpoint."""
        return self._settings._effective_driver_endpoint(conversation_id)

    def _effective_assist(self, conversation_id: str) -> bool:
        return self._settings._effective_assist(conversation_id)

    def is_assist(self, conversation_id: str) -> bool:
        return self._settings.is_assist(conversation_id)

    async def apply_settings_change(
        self,
        conversation_id: str,
        *,
        model_override: str | None = None,
        assist: bool | None = None,
        model_provided: bool | None = None,
    ) -> bool:
        """Delegator: atomically apply pre-kick / terminal-state settings under the per-cid
        lock. `model_provided` distinguishes an explicit null (reset-to-default) from an
        omitted field. Returns True if settable and applied; False → caller should 409."""
        return await self._settings.apply_settings_change(
            conversation_id,
            model_override=model_override,
            assist=assist,
            model_provided=model_provided,
        )

    # ---- artifact_mode (C6) ------------------------------------------------

    def set_artifact_mode(self, conversation_id: str, on: bool) -> None:
        self._settings.set_artifact_mode(conversation_id, on)

    def _effective_artifact_mode(self, conversation_id: str) -> bool:
        return self._settings._effective_artifact_mode(conversation_id)

    # ---- CONTRACT-ACTIVATE: build contract + live phase ---------------------

    def set_build_kind(self, conversation_id: str, kind: str | None) -> None:
        """Declare the build contract kind for a conversation (e.g. 'appkit.leadgen').
        Unset/None ⇒ the CUSTOM contract. Resets any existing tracker for the run."""
        if kind:
            self._build_kind[conversation_id] = kind
        else:
            self._build_kind.pop(conversation_id, None)
        self._build_trackers.pop(conversation_id, None)

    def expected_delivery_mode(self, conversation_id: str) -> str | None:
        """P5: the host-owned delivery SHAPE ("app"|"files") the conversation's build
        contract declares — the agent-server deliverable surface reads this to label /
        validate a handoff (so a deck run can't be handed off as a runnable app).
        Resolves the contract on first use; None when no build contract governs the run
        (a plain chat conversation has no delivery shape)."""
        entry = self._build_trackers.get(conversation_id)
        if entry is None:
            # only a build/artifact run has a delivery shape — don't fabricate a
            # contract for an ordinary conversation.
            if not (self._effective_artifact_mode(conversation_id) or conversation_id in self._build_kind):
                return None
            self._build_scope_guard(conversation_id)  # resolves + caches the contract
            entry = self._build_trackers.get(conversation_id)
        return entry[0].artifact.delivery_mode if entry is not None else None

    def _starter_kit_for(self, conversation_id: str) -> str | None:
        """P7: the active contract's starter_kit name (app_shell / lead_form), stamped on
        the executor's ToolContext so scaffold_starter materializes THIS build's starter.
        None for a non-build run or a contract with no starter_kit."""
        if conversation_id not in self._build_kind:
            return None
        self._build_scope_guard(conversation_id)  # resolve + cache (contract, tracker)
        return self._build_trackers[conversation_id][0].artifact.starter_kit

    def _finalizer_alias_for(self, conversation_id: str) -> str | None:
        """P6: the contract's verification finalizer to advertise as a `finish` alias —
        ONLY for a RESOLVED, NON-CUSTOM contract. A plain build, a declared "custom" kind,
        or an unknown kind that falls back to CUSTOM gets None (never fabricate the generic
        ready_for_artifact_verification finalizer)."""
        if conversation_id not in self._build_kind:
            return None
        self._build_scope_guard(conversation_id)  # resolve + cache (contract, tracker)
        resolved = self._build_trackers[conversation_id][0]
        if resolved.kind is ContractKind.CUSTOM:
            return None
        return resolved.verify.finalizer

    def note_build_verify_result(self, conversation_id: str, *, passed: bool) -> None:
        """Advance the build-phase tracker on a host VERIFY outcome (pass → EXPORT, fail
        → REPAIR so the model may use the repair tools to fix). The BOOTSTRAP→EDIT→VERIFY
        edges are driven automatically by tool success (on_tool_success); this is the
        entry point the verification gate calls for the VERIFY→EXPORT/REPAIR edge.
        No-op if the conversation has no active build tracker."""
        entry = self._build_trackers.get(conversation_id)
        if entry is not None:
            entry[1].note_verifier_result(passed=passed)

    def _build_scope_guard(
        self, conversation_id: str
    ) -> tuple[ContractScopeGuard | None, Callable[[str], None] | None]:
        """The (guard, on_tool_success) pair governing tools for an artifact run, or
        (None, None). Resolves+caches the conversation's contract and a fresh phase
        tracker on first use; the guard reads the tracker's LIVE phase per call."""
        entry = self._build_trackers.get(conversation_id)
        if entry is None:
            kind = self._build_kind.get(conversation_id)
            brief = {"kind": kind} if kind else None
            contract = self._build_contract_registry.get_for_brief(brief, strict_kind=False)
            entry = (contract, BuildPhaseTracker(contract))
            self._build_trackers[conversation_id] = entry
        contract, tracker = entry
        return ContractScopeGuard.for_contract(contract, tracker.current), tracker.note_tool_success
    # ---- last-selected model (P3) — delegators to RuntimeSettings -----------

    def get_last_selected_model(self) -> str | None:
        """The globally-persisted last-picked driver model (P3). Used by
        conversation-create routes to seed a new conversation's model when no
        explicit override is provided. None = no pick ever made."""
        return self._settings.get_last_selected_model()

    def set_last_selected_model(self, model_id: str | None) -> None:
        """Persist the last-picked driver model. Called from set_model_override
        automatically; exposed here for tests."""
        self._settings.set_last_selected_model(model_id)

    def driver_models(self) -> dict[str, Any]:
        """The driver-eligible models (live + tool-calling), deduped by underlying model,
        for the Build model picker — with the current default. Cost-legible: provider +
        free flag. Sourced from the live router config (Settings assignments honored)."""
        from disco.core.llm import ModelRole, Requirement

        cfg = self._config_store.load()
        seen: set[str] = set()
        models: list[dict[str, Any]] = []
        for key, m in cfg.models.items():
            if m.base_url is None or Requirement.TOOL_CALLING not in m.capabilities:
                continue
            if m.model_id in seen:
                continue
            seen.add(m.model_id)
            # Ground truth over declaration: prefer the live-served model name +
            # context window; fall back to the static ModelEntry on any probe miss.
            live = _probe_live_model(
                m.base_url,
                os.environ.get(m.api_key_env) if m.api_key_env else None,
                m.model_id,
            )
            label = _model_label(live["model_id"] or m.model_id)
            ctx = live["n_ctx"] or m.context_window
            # W-05-fu: expose pricing_mode so the picker can tell a SUBSCRIPTION
            # model (flat-rate plan, price 0/token) apart from a genuinely FREE
            # one. None → derive for back-compat (price 0 → free, else metered),
            # matching the frontend isFree/isSubscription helpers. `free` is then
            # keyed off the effective mode so a subscription model is NOT free.
            pricing_mode = m.pricing_mode
            if pricing_mode is None:
                pricing_mode = "free" if m.price_out_per_m == 0.0 else "metered"
            models.append(
                {
                    "id": key,
                    "label": label,
                    "provider": "openrouter" if m.provider == "openrouter" else "local",
                    "free": pricing_mode == "free",
                    "pricing_mode": pricing_mode,
                    "context_window": ctx,
                }
            )
        try:
            default = cfg.model_for(ModelRole.AGENT_DRIVER)
        except Exception:  # noqa: BLE001 — no assignment → no default highlight
            default = None
        return {"models": models, "default": default}

    def _retrieval_handlers(self) -> dict[str, Any]:
        """Lazily build the search/extract capability handlers from the research
        providers — so the Build agent toolset's search/extract are REAL, not a false
        affordance, without eagerly importing httpx."""
        if self._cap_handlers is None:
            deps = self._research()
            search, extraction = self._compose_mcp_retrieval(deps)
            self._cap_handlers = retrieval_capability_handlers(search, extraction)
        return self._cap_handlers

    def _compose_mcp_retrieval(self, deps: dict[str, Any]) -> tuple[Any, Any]:
        return self._mcp._compose_mcp_retrieval(deps)

    def _build_broker(self) -> CapabilityBroker:
        """The orchestrator-side capability broker for a Build conversation. search/
        extract are backed by the research providers (lazy); provider keys never reach
        the sandbox (§6). The kill switch calls broker.revoke_all()."""
        broker = CapabilityBroker()

        async def _search(*, query: str, limit: int = 8) -> Any:
            return await self._retrieval_handlers()["search"](query=query, limit=limit)

        async def _extract(*, url: str) -> Any:
            return await self._retrieval_handlers()["extract"](url=url)

        broker.register("search", _search)
        broker.register("extract", _extract)
        return broker

    def _loop_for(self, conversation_id: str) -> AgentLoop:
        loop = self._loops.get(conversation_id)
        if loop is None:
            # The per-conversation model PICK drives the WHOLE generative pipeline:
            # build the router with it so the brain AND the summarizer (context
            # condensation) AND any other generative role all run on the picked
            # model — not just the driver while local models summarize underneath.
            override = self._model_override.get(conversation_id)
            # Surface FIRST: it scopes which skills the router injects (a build-only
            # skill shouldn't reach an agent conversation's prompt, and vice versa).
            surface = self._surface_of(conversation_id)
            # Single source of truth (gated by surface) shared with the loop compose
            # below and the UI badge — they can't desync.
            autonomous = self._effective_autonomous(conversation_id)
            router = self._router_now(
                pick=override,
                surface=surface,
                autonomous=autonomous,
                conversation_id=conversation_id,
            )
            # Research↔Build isolation: the SURFACE picks the agent class, so
            # completion semantics (prose=answer for Research vs affirmative
            # `finish` for Build) are owned by type, not a shared mode flag.
            # "agent" is a build-like surface — same BuildAgent + build loop.
            if surface in self._BUILD_LIKE_SURFACES:
                agent: RouterAgent = BuildAgent(
                    router, conversation_id=conversation_id, model_override=override
                )
            else:
                agent = ResearchAgent(
                    router, conversation_id=conversation_id, model_override=override
                )
            if surface in self._BUILD_LIKE_SURFACES:
                loop = self._compose_build_loop(conversation_id, router, agent)
            elif surface == "deep_research":
                loop = self._compose_deep_research_loop(conversation_id, router, agent)
            else:
                loop = AgentLoop(
                    conversation_id,
                    self._store,
                    agent,
                    _NoToolExecutor(),
                    router,
                    RuleBasedAnalyzer(),
                    NeverConfirm(),  # Research surface: no human gate
                    NoOpCondenser(),
                    RouterSummarizer(router),
                    mode=self._mode,
                    # Research surface: always standard (no assist compensations);
                    # explicit default so the positive PRODUCTION gate fires.
                    model_policy=ModelExecutionPolicy.standard(),
                )
            # Watch-it-write: wire the loop's stream sink to the store's ephemeral
            # broadcast so streamed file-content frames reach the conversation's WS
            # subscribers live (never persisted). Bound to this cid.
            loop.stream_sink = lambda frame, _cid=conversation_id: self._store.publish_ephemeral(
                _cid, frame
            )
            self._loops[conversation_id] = loop
        return loop

    # ---- Pi tool bridge (PR D2) — additive; existing paths unchanged --------

    def _executor_for(self, conversation_id: str) -> DefaultToolExecutor:
        """[D2] Read-only accessor for a conversation's tool executor, lazily
        building the loop (which constructs + REGISTERS the executor in
        ``self._executors``) on first access. Used by the Pi tool bridge
        (``routes/pi_tools.py``) to drive ONE externally-proposed tool call through
        the SAME ``DefaultToolExecutor`` the in-process agent loop uses — same
        sandbox, scope, broker, and policy.

        Purely additive: building the loop here is exactly what ``_loop_for`` already
        does for any caller; no existing behavior changes. Raises ``LookupError``
        when the conversation's surface has no tool executor (e.g. a research
        surface uses a no-tool executor and never populates ``_executors``)."""
        executor = self._executors.get(conversation_id)
        if executor is None:
            # Building the loop populates self._executors for build-like surfaces.
            self._loop_for(conversation_id)
            executor = self._executors.get(conversation_id)
        if executor is None:
            raise LookupError(
                f"no tool executor for conversation {conversation_id!r} "
                "(its surface has no executable tools)"
            )
        return executor

    async def execute_pi_tool(
        self, conversation_id: str, tool_call: ToolCall
    ) -> ToolResult:
        """[D2] Execute ONE externally-driven (Pi sidecar) tool call against this
        conversation's executor, MIRRORING ``observe.execute_and_observe``'s
        Action→Observation pairing: append an ``ActionEvent``, run the executor
        (which always returns a ``ToolResult`` and never raises), then append the
        paired ``ObservationEvent`` (success) or ``AgentErrorEvent`` (failure), and
        return the ``ToolResult``.

        Additive single-call helper for the bridge; the in-process loop's own
        ``execute_and_observe`` path is untouched. The F9/W-39/K1 observer guards
        are loop-internal concerns and deliberately NOT replicated here — Pi owns
        its own loop, so this is the one externally-driven execution per call."""
        executor = self._executor_for(conversation_id)
        action = ActionEvent(
            source=EventSource.AGENT,
            thought=f"[pi] {tool_call.tool_name}",
            tool_call=tool_call,
        )
        await self._store.append(conversation_id, action)
        result = await executor.execute(tool_call)
        if result.success:
            await self._store.append(
                conversation_id,
                ObservationEvent(tool_result=result, action_id=action.id),
            )
        else:
            await self._store.append(
                conversation_id,
                AgentErrorEvent(
                    error=result.error or "tool failed",
                    action_id=action.id,
                    tool_call_id=tool_call.call_id,
                ),
            )
        return result

    def _mcp_egress_hosts(self) -> frozenset[str]:
        return self._mcp._mcp_egress_hosts()

    def _mcp_proxy_env(self) -> dict[str, str] | None:
        return self._mcp._mcp_proxy_env()

    def _build_sandbox_spec(
        self,
        *,
        surface: str = "build",
        mcp_egress_hosts: frozenset[str] | None = None,
    ) -> SandboxSpec:
        """The egress-posture spec for a Build sandbox. FILTERED by default
        (BP-G10: build boxes get the allowlisting proxy now that E8 wired the
        proxy on every backend — gVisor, podman, local). Open only when
        PMX_BUILD_EGRESS=open is set explicitly (an escape hatch for debug
        / dev when a real network is genuinely required). Used both by
        _compose_build_loop and upload_session so pending sessions and build
        sessions share the same spec.

        When mcp_egress_hosts is provided, they are UNIONed into the egress_allow set
        (SUPERSET, not replacement) — the pre-existing registry hosts AND the MCP
        hosts both survive (rung B egress-proxy routing)."""
        if surface == "agent":
            egress = disco_env("AGENT_EGRESS", "open").lower().strip()
        else:
            egress = disco_env("BUILD_EGRESS", "filtered").lower().strip()
        if egress == "filtered":
            base_allow = REGISTRY_EGRESS_ALLOW
            if mcp_egress_hosts:
                base_allow = frozenset(base_allow | mcp_egress_hosts)
            return self._sandbox_spec.model_copy(update={"egress_allow": base_allow})
        # Open mode grants full NETWORK — there is no allowlist to union MCP hosts
        # into; they are already reachable. mcp_egress_hosts only matters under
        # filtered posture (above).
        spec_update = {"permitted": self._sandbox_spec.permitted | {Capability.NETWORK}}
        return self._sandbox_spec.model_copy(update=spec_update)

    def upload_session(self, conversation_id: str) -> SandboxSession:
        return self._sessions.upload_session(conversation_id)

    def store_upload(self, conversation_id: str, filename: str, data: bytes) -> None:
        """[DC-07] Store an uploaded file in the server-side sidecar directory."""
        if not self._uploads_base:
            return
        path = Path(self._uploads_base) / conversation_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_upload_names(self, conversation_id: str) -> set[str]:
        """[DC-07] List filenames currently held server-side for this conversation."""
        if not self._uploads_base:
            return set()
        path = Path(self._uploads_base) / conversation_id
        if not path.is_dir():
            return set()
        return {p.name for p in path.iterdir() if p.is_file()}

    def get_upload_size(self, conversation_id: str) -> int:
        """[DC-07] Total bytes of server-side uploads for this conversation."""
        if not self._uploads_base:
            return 0
        path = Path(self._uploads_base) / conversation_id
        if not path.is_dir():
            return 0
        return sum(p.stat().st_size for p in path.iterdir() if p.is_file())

    def add_upload_passages(self, conversation_id: str, passages: list[Any]) -> None:
        """[G1/DR-4] Append Passages from a text upload to the per-conversation
        upload corpus. Called by the upload route after parsing .txt/.md/.csv
        files. The passages are keyed by conversation_id and retrieved at run
        time to seed basic research (F3) and DR runs (F2)."""
        bucket = self._upload_passages.setdefault(conversation_id, [])
        bucket.extend(passages)

    def get_upload_passages(self, conversation_id: str) -> list[Any]:
        """[G1/DR-4] Return the accumulated upload Passages for this conversation
        (empty list if none were ingested, e.g. non-text uploads only)."""
        return list(self._upload_passages.get(conversation_id, []))

    def _compose_build_loop(
        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
    ) -> AgentLoop:
        """[Agent surface] Compose — not reinvent — the loop for Build mode: the agent
        toolset (Prompt 1) over a resilient SandboxSession, the SecurityAnalyzer, the
        BlastRadiusConfirm gate (NOT Research's NeverConfirm), and the real condenser. The
        executor is held so the kill switch can revoke caps + tear down the sandbox."""
        broker = self._build_broker()
        # Adopt a pending session (created by upload_session for a pre-kick upload) so
        # the build's workspace IS the one uploads landed in — object identity, not a
        # re-read of the directory. Create a fresh session only when there is none.
        session = self._pending_sessions.pop(conversation_id, None)
        if session is None:
            session = SandboxSession(
                self._sandbox_service_now(),
                self._build_sandbox_spec(
                    surface=self._surface_of(conversation_id),
                    mcp_egress_hosts=self._mcp_egress_hosts(),
                ),
                conversation_id=conversation_id,
                # Mid-run death (transport drop / OOM): restore the last snapshot
                # into the fresh instance before the agent retries (bp-13 §2).
                on_recreate=lambda: self._rehydrate_after_recreate(conversation_id),
            )
        # Order C: resolve the policy ONCE — anchored_edit + tier/assist both come
        # from _effective_policy, eliminating the former separate caps-resolution path
        # (W4 anchored-edit heuristic). _effective_policy IS the single source of truth
        # (runtime_settings.py). No second reader of ModelEntry.capabilities remains here.
        model_policy = self._effective_policy(conversation_id)
        # C6: artifact_mode selects a narrow scope (NO shell/browser/plan-gate);
        # file_str_replace is excluded from ARTIFACT_TOOLS regardless of policy.
        _art_mode = self._effective_artifact_mode(conversation_id)
        _scope = artifact_scope() if _art_mode else agent_scope(model_policy=model_policy)
        # CW-6: derive the capability-aware file_read page budget from the SAME
        # (assist, live-context-window) inputs the snapshot caps use, so a file that
        # fits the assist-OFF snapshot pin also reads in ONE shot. assist-ON resolves
        # to the static 7k default → byte-identical to today.
        _read_char_budget = derive_context_caps(
            assist=model_policy.assist,
            context_window=self._driver_context_window(),
        ).read_char_budget
        # CONTRACT-ACTIVATE: in artifact mode, govern tools by the conversation's build
        # contract + live phase (no raw rewrite during the edit phase, etc.). Plain
        # (non-artifact) runs pass (None, None) → unchanged behavior.
        _scope_guard, _on_tool_success = (
            self._build_scope_guard(conversation_id) if _art_mode else (None, None)
        )
        executor = DefaultToolExecutor(
            build_default_registry(),
            _scope,
            # SandboxSession is a drop-in SandboxInstance (it implements the
            # protocol at runtime); the `id` attribute differs only in being a
            # property rather than a plain attribute, which trips the
            # type-checker's invariance check on a protocol field. Cast to
            # the protocol type so the type checker is happy without
            # touching runtime behavior.
            sandbox=cast(SandboxInstance, session),
            broker=broker,
            conversation_id=conversation_id,
            model_policy=model_policy,
            # ROOT-5: the conversation's effective (override-aware) driver endpoint, so
            # LLM-using tools (slides_generate) author with the model the user picked.
            driver_llm=self._effective_driver_endpoint(conversation_id),
            # CW-6: capability-derived file_read page budget (see above).
            read_char_budget=_read_char_budget,
            # CONTRACT-ACTIVATE: per-phase contract enforcement (artifact mode only).
            scope_guard=_scope_guard,
            on_tool_success=_on_tool_success,
            # P7: the active contract's starter_kit for scaffold_starter.
            starter_kit=self._starter_kit_for(conversation_id),
        )
        # RP-05 rung A+B: extend the registry with MCP tools from the pool
        # snapshot (stdio) AND the HTTP-managed tools (rung B streamable_http).
        # The snapshot is frozen per conversation — list_changed notifications do
        # NOT mutate this list.
        all_mcp_tools: list[ToolDef] = []
        if self._mcp_pool is not None and self._mcp_pool.started:
            all_mcp_tools.extend(self._mcp_pool.snapshot())
        if self._mcp_http_tools:
            all_mcp_tools.extend(list(self._mcp_http_tools.values()))

        if all_mcp_tools:
            # RP-05c: apply advertised/callable split. Over the cap, only
            # non-MCP tools + tool_search are advertised; all remain callable.
            _mcp_cfg = self._config_store.load()
            max_schemas = _mcp_cfg.mcp.max_active_schemas if _mcp_cfg.mcp else 20
            _apply_mcp_scope(
                executor, all_mcp_tools, self._mcp_call_target,
                max_active_schemas=max_schemas,
            )

            # RP-05b §3: the retrieval-tier MCP providers are composed into the
            # bundled search/extraction in `_compose_mcp_retrieval` (consumed by
            # research_stream, _execute_deep_research, and the Build broker's
            # search/extract handlers) — so MCP-discovered URLs flow through the
            # real GroundingPipeline. Registering them here under `mcp_search_*` /
            # `mcp_extract_*` broker names was DEAD: nothing called those names
            # (the Build agent calls `search`/`extract`; Research bypasses the
            # broker). The cap-handler cache is invalidated when the providers are
            # (re)built so the composition picks up the live set.

        # Emit mcp_approval_required frames for any servers that need re-approval
        for server, info in self._mcp_approval_pending.items():
            try:
                self._store.publish_ephemeral(
                    conversation_id,
                    {
                        "type": "mcp_approval_required",
                        "server": server,
                        "description_hash": info.get("new_hash", ""),
                        "old_description_hash": info.get("old_hash", ""),
                    },
                )
            except Exception:
                pass  # best-effort; WS frame emission is not critical
        self._executors[conversation_id] = executor
        # P6: the contract's verification finalizer, advertised as a per-kind alias of
        # `finish`. Bound on BOTH the loop (dispatch/advertisement/requery) and the agent
        # (batched-call selection); guard the agent hook for test fakes that don't have it.
        _finish_alias = self._finalizer_alias_for(conversation_id)
        _set_alias = getattr(agent, "set_finish_alias", None)
        if callable(_set_alias):
            _set_alias(_finish_alias)
        if _art_mode:
            # C6: artifact mode — low-friction authoring path:
            #   • NeverConfirm: artifacts are low-risk; no per-action approval.
            #   • INTERACTIVE: no plan-gate; the model acts directly on first message.
            #   • No planning_tools: submit_plan / plan_step are excluded from scope.
            # Everything else (broker, sandbox, condenser, summarizer, autonomous,
            # assist) is byte-identical to the normal build loop — only the gate,
            # mode, and scope differ.
            return AgentLoop(
                conversation_id,
                self._store,
                agent,
                executor,
                router,
                RuleBasedAnalyzer(),
                # No per-action approval for artifact ops — confinement + narrow scope
                # are the blast-radius controls (NeverConfirm mirrors Research surface).
                NeverConfirm(),
                LLMSummarizingCondenser(context_window=self._driver_context_window()),
                RouterSummarizer(router),
                # INTERACTIVE: no plan-gate; artifact authoring starts immediately.
                mode=OperatingMode.INTERACTIVE,
                # No planning_tools (submit_plan/plan_step not in ARTIFACT_TOOLS scope).
                autonomous=self._effective_autonomous(conversation_id),
                model_policy=model_policy,
                finish_alias=_finish_alias,  # P6 contract finalizer alias
            )
        return AgentLoop(
            conversation_id,
            self._store,
            agent,
            executor,
            router,
            RuleBasedAnalyzer(),
            # DC-03: sandboxed ops auto-approve (confinement is the blast radius);
            # host-scope/unknown ops keep ConfirmRisky semantics; publish always gates.
            BlastRadiusConfirm(),
            # A-S1: derive condensation thresholds from the AGENT_DRIVER model's
            # context window (soft 65% / hard 80%) instead of the old bare 24k/32k.
            LLMSummarizingCondenser(context_window=self._driver_context_window()),
            RouterSummarizer(router),
            # Build starts in PLANNING. The planner has a context-rich surface — it
            # can READ to explore (file_list/file_read in the workspace, search/extract
            # on the web) before calling `submit_plan`. This mirrors Claude Code's plan
            # mode: writes/edits/shell are off the table until approval, but the
            # planner can gather context first. approve_plan flips to execution; the
            # per-action gate above still governs the build that follows.
            mode=OperatingMode.PLANNING,
            planning_tools=frozenset(
                # read/explore + plan + `think`. `think` is a pure NO-OP reasoning
                # scratchpad (read_only=True, no side effect), so it belongs among
                # the allowed PLANNING first moves (§11.1/§15.2/§20.1: submit_plan /
                # ask / clarify / think / safe read). It passes the just-merged phase
                # gate because it is BOTH in this allowlist AND in the read-only
                # capability set (ToolDef.read_only) the gate intersects against.
                {"submit_plan", "file_list", "file_read", "search", "extract", "think"}
            ),
            # Same gated source of truth as the router prefix and the UI badge —
            # _compose_build_loop only runs for build-like surfaces today, but reading
            # the gated value means a future caller can't desync the loop's behavior
            # from the prompt prefix.
            autonomous=self._effective_autonomous(conversation_id),
            model_policy=model_policy,
            finish_alias=_finish_alias,  # P6 contract finalizer alias
        )

    # ---- deep research surface ---------------------------------------------

    def _compose_deep_research_loop(
        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
    ) -> AgentLoop:
        return self._dr._compose_deep_research_loop(conversation_id, router, agent)

    def _depth_for(self, conversation_id: str) -> DepthTier:
        return self._dr._depth_for(conversation_id)

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        return self._dr.set_depth(conversation_id, tier)

    def _iterative_for(self, conversation_id: str) -> bool:
        return self._dr._iterative_for(conversation_id)

    def set_iterative(self, conversation_id: str, enabled: bool) -> None:
        return self._dr.set_iterative(conversation_id, enabled)

    def set_recency(self, conversation_id: str, window: str | None) -> None:
        return self._dr.set_recency(conversation_id, window)

    def _research(self) -> dict[str, Any]:
        return self._dr._research()

    def research_stream(
        self,
        query: str,
        *,
        model_override: str | None = None,
        drop_weak: bool = False,
        domains_deny: frozenset[str] = frozenset(),
        think: bool = False,
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._dr.research_stream(
            query,
            model_override=model_override,
            drop_weak=drop_weak,
            domains_deny=domains_deny,
            think=think,
            conversation_id=conversation_id,
        )

    def kick(self, conversation_id: str) -> None:
        """Schedule the loop to run (idempotent: a no-op if already running). The
        loop itself decides if there's unprocessed work and streams events.

        Wraps `loop.run()` in a task that — for Build conversations with a valid
        ProjectStore configured — first REHYDRATES the persisted workspace on
        the very first kick, and SNAPSHOTS it after every run that ends in
        FINISHED. Failures are reported as ambient system-reminder events on
        the conversation log (the user + model both see them), never as
        exceptions that crash the task."""
        # Auto-title from the first user message (idempotent, detached, no-op once
        # titled). Placed here because kick() is the single chokepoint every user
        # message flows through — covers WS + REST send_message + first-kick alike.
        self._title_service.schedule(conversation_id)
        existing = self._tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return  # already running; the new message is picked up at the next step
        # W-48(c): if Settings switched the sandbox backend since this conversation
        # last composed, evict its stale-backend session here so _loop_for composes a
        # fresh sandbox on the new backend (best-effort destroy of the old box).
        self._evict_stale_backend(conversation_id)
        loop = self._loop_for(conversation_id)
        # finding #3: this is a NEW run task → bump the conversation's run-generation
        # and bind it into the done-callback, so the finalizer for THIS run can tell
        # whether a newer run has since reused the pin (and must not clear it).
        generation = self._run_generation.get(conversation_id, 0) + 1
        self._run_generation[conversation_id] = generation
        task = asyncio.create_task(self._run_with_persistence(conversation_id, loop))
        # W2 supervision: the run task ALWAYS resolves to a terminal status. Without this
        # callback an exception escaping loop.run() killed the task silently and left the
        # conversation at RUNNING forever (the silent hang Dylan hit).
        task.add_done_callback(
            lambda t, _cid=conversation_id, _gen=generation: self._on_run_task_done(
                _cid, t, _gen
            )
        )
        self._tasks[conversation_id] = task

    # W2: statuses that mean "the run already concluded" — terminalization must not clobber.
    _CONCLUDED_STATUSES = frozenset(
        {
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
            ConversationStatus.STUCK,
            ConversationStatus.PAUSED,
        }
    )

    # W11: statuses where it is LEGITIMATE for loop.run() to return and wait — the loop
    # parked intentionally (for the user, or a control op like confirm/resume/pick).
    # A clean return at any OTHER status (i.e. RUNNING) means the turn ended WITHOUT
    # concluding: the silent stall. IDLE = "ready, no unprocessed work" (also fine).
    _RUN_PARKED_STATUSES = frozenset(
        {
            ConversationStatus.IDLE,
            ConversationStatus.PAUSED,
            ConversationStatus.WAITING_FOR_CONFIRMATION,
            ConversationStatus.AWAITING_PLAN_APPROVAL,
            ConversationStatus.AWAITING_USER_DECISION,
            ConversationStatus.AWAITING_USER_QUESTION,
        }
    )
    _MAX_NONTERMINAL_REKICKS = 1

    # Statuses at which a run is BETWEEN turns (terminal or idle) — the kernel pin
    # is released so the next run re-resolves the current selection (finding #1).
    # NB: PAUSED and the AWAITING_*/WAITING_* gate-parks are deliberately EXCLUDED
    # — their resume/approve must continue under the SAME pinned kernel.
    _KERNEL_UNPIN_STATUSES = frozenset(
        {
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
            ConversationStatus.STUCK,
            ConversationStatus.IDLE,
        }
    )

    def _on_run_task_done(
        self, conversation_id: str, task: asyncio.Task[Any], generation: int | None = None
    ) -> None:
        """W2 supervision callback. Deregister the task; on an UNHANDLED exception (NOT
        cancellation), schedule terminalization to ERROR so the conversation can never sit
        at RUNNING forever. `generation` (the run-generation this task was spawned under,
        finding #3) is threaded to the clean-return finalizer so a stale finalizer can't
        clear a pin a newer run has since reused."""
        if self._tasks.get(conversation_id) is task:
            self._tasks.pop(conversation_id, None)
        if task.cancelled():
            return  # cancellation is not an error
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            # W11: a CLEAN return is NOT proof the loop concluded. loop.run() can
            # return while the conversation is still RUNNING — a dropped/unparseable
            # model response ends the turn with no terminal StatusEvent, and the
            # loop's `return await self.get_state()` carries RUNNING out unchanged.
            # W2 only handled the exception case, so this sat at RUNNING forever
            # (the silent MiniMax-build hang). Reconcile it (re-kick once, else STUCK).
            with contextlib.suppress(RuntimeError):  # no running loop (shutdown) → skip
                asyncio.create_task(
                    self._finalize_clean_return(conversation_id, generation)
                )
            return
        # An exception escaped loop.run(). Schedule (best-effort) a terminal ERROR.
        # Thread the run-generation (finding #3) so the crash terminalizer — like the
        # clean-return finalizer above — only terminalizes/unpins if a NEWER run has not
        # since reused the pin (it pops `_tasks` before scheduling this async cleanup, so
        # a fresh user turn can start generation N+1 in the window).
        with contextlib.suppress(RuntimeError):  # no running loop (shutdown) → skip
            asyncio.create_task(
                self._terminalize_crashed(conversation_id, exc, generation)
            )

    async def _maybe_rekick_for_stranded_followup(self, conversation_id: str) -> None:
        """Engine-rekick fix. A follow-up appended while a run is FINALIZING is
        stranded: `kick()` is a no-op while the prior run task is still live, and the
        done-callback finalizers (`_finalize_clean_return` / `_terminalize_crashed`)
        terminalize but never re-kick — so nothing ever starts the new run() that
        would process the turn. Called AFTER terminalization at every terminal
        conclusion site; re-kicks ONLY when the (now-correct, status-markers-excluded)
        work-gate `signals.has_unprocessed_user_message` is open for this cid.

        Ordering-insensitive: the predicate fix means a terminal marker no longer
        masks a preceding follow-up, so it doesn't matter whether this runs before or
        after the marker append. Best-effort: never re-raises.

        INFINITE-LOOP GUARD (`_post_terminal_rekick_seq`): re-kick + record ONLY if
        the latest unprocessed USER seq is STRICTLY NEWER than the seq that last
        triggered a post-terminal re-kick here. A new follow-up recovers once; a
        re-kicked turn that produces NO real progress (same seq) never loops; a clean
        no-follow-up finish never re-kicks. This is ADDITIONAL to the existing
        generation / stale-run guards (unchanged) — it does not replace them."""
        try:
            events = await self._store.get_events(conversation_id)
        except Exception:  # noqa: BLE001 — supervision is best-effort, never re-raise
            logger.exception(
                "post-terminal re-kick could not read events for %s", conversation_id
            )
            return
        if not signals.has_unprocessed_user_message(events):
            return  # clean finish, no stranded follow-up → nothing to recover
        latest_user_seq = max(
            (
                e.seq or 0
                for e in events
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            default=None,
        )
        if latest_user_seq is None:
            return
        last = self._post_terminal_rekick_seq.get(conversation_id)
        if last is not None and latest_user_seq <= last:
            # Same (or older) follow-up already triggered a re-kick that made no real
            # progress — don't loop on it.
            return
        self._post_terminal_rekick_seq[conversation_id] = latest_user_seq
        logger.info(
            "stranded follow-up on %s (user seq %s after a terminal conclusion) — "
            "re-kicking to process it",
            conversation_id,
            latest_user_seq,
        )
        self.kick(conversation_id)

    async def _finalize_clean_return(
        self, conversation_id: str, generation: int | None = None
    ) -> None:
        """W11 supervision. The run task returned WITHOUT raising. If the conversation
        already concluded, or legitimately parked (awaiting the user / a control op),
        there's nothing to do. But if it's still RUNNING the turn ended without
        reaching a terminal state — the silent stall. Re-kick ONCE (recovers a
        transient dropped response), and if it STILL returns non-terminal, terminalize
        to STUCK so the wedge is visible to the operator/UI, never RUNNING forever.
        Best-effort: never re-raises (a supervisor failure must not crash the loop)."""
        try:
            state = await self._store.get_state(conversation_id)
        except Exception:  # noqa: BLE001 — supervision is best-effort, never re-raise
            logger.exception("clean-return finalize could not read state for %s", conversation_id)
            return
        status = state.execution_status
        # [W-48 P1] Remember where this run ENDED so the sync backend-eviction path can
        # tell a gate-parked conv (don't evict — preserve its mid-gate workspace) from
        # a truly idle/finished one (safe to evict).
        self._last_status[conversation_id] = status
        if status in self._CONCLUDED_STATUSES or status in self._RUN_PARKED_STATUSES:
            # Healthy ending → reset the per-cid re-kick budget for the next segment.
            self._nonterminal_rekicks.pop(conversation_id, None)
            # A run that ended between turns (done/errored/stuck/idle) releases the
            # kernel pin so the NEXT run re-resolves the current selection. A
            # gate-park (AWAITING_*) or PAUSED stays pinned — its resume/approve
            # must continue under the SAME kernel the run started on (finding #1).
            # Generation-guarded (finding #3): a fresh turn may already have reused the
            # pin between this task finishing and this finalizer running — don't clear
            # it out from under that newer run.
            if status in self._KERNEL_UNPIN_STATUSES:
                self._unpin_if_current_generation(conversation_id, generation)
                # REL-2a: the main fold now happens in `_run_with_persistence`, before
                # snapshot/teardown-sensitive work can race the sandbox away. Keep this
                # terminal observer as a guarded backstop for any FINISHED path that
                # reaches the finalizer without passing through that wrapper.
                if status is ConversationStatus.FINISHED:
                    await self._maybe_shadow_fold_finished_manifest(conversation_id)
                # Engine-rekick fix: only a genuinely TERMINAL conclusion
                # (FINISHED/ERROR/STUCK/IDLE — the unpin set) can strand a follow-up
                # that landed during finalization. A deliberate PARK (PAUSED /
                # AWAITING_* / WAITING_*) must NOT be auto-resumed here — its resume
                # is the user's explicit reply via the normal kick path. The helper
                # no-ops unless the (now-correct) work-gate is open AND the follow-up
                # seq is strictly newer than the last post-terminal re-kick.
                await self._maybe_rekick_for_stranded_followup(conversation_id)
            return
        # Still RUNNING (the loop emits RUNNING at entry and only leaves it by emitting
        # a different status): the turn ended without concluding.
        attempts = self._nonterminal_rekicks.get(conversation_id, 0)
        if attempts < self._MAX_NONTERMINAL_REKICKS:
            self._nonterminal_rekicks[conversation_id] = attempts + 1
            logger.warning(
                "run task for %s returned at %s without concluding; re-kicking once "
                "(silent-stall recovery)",
                conversation_id,
                status.value,
            )
            self.kick(conversation_id)
            return
        # Already re-kicked and STILL non-terminal → stop spinning, mark STUCK honestly.
        self._nonterminal_rekicks.pop(conversation_id, None)
        try:
            # Re-read under the (rare) race where the re-kick concluded between checks.
            state = await self._store.get_state(conversation_id)
            if state.execution_status in self._CONCLUDED_STATUSES:
                return
            logger.error(
                "run task for %s wedged at RUNNING after re-kick; marking STUCK",
                conversation_id,
            )
            await self._store.append(
                conversation_id,
                StatusEvent(
                    status=ConversationStatus.STUCK,
                    detail="loop ended without reaching a terminal state",
                ),
            )
            # STUCK is terminal → release the kernel pin so the NEXT run re-resolves
            # the current selection (finding #3). Unlike the healthy-return branch
            # above (which unpins via `_KERNEL_UNPIN_STATUSES`), this path appends a
            # FRESH terminal status and must clear the pin itself, else a wedged run
            # would leak its pin forever and later gate/config flips stay ineffective.
            # Generation-guarded (finding #3): if a newer run already reused the pin,
            # leave it (the re-read above already returns on a concluded newer status).
            self._unpin_if_current_generation(conversation_id, generation)
            await self._emit_persistence_reminder(
                conversation_id,
                "The run ended without completing or stopping cleanly (the model turn "
                "produced no actionable response). It's been marked stuck — send a "
                "message to steer it and continue.",
            )
            # Engine-rekick fix: STUCK is terminal here too — recover a follow-up that
            # landed during this wedge terminalization (guarded, so it can't loop).
            await self._maybe_rekick_for_stranded_followup(conversation_id)
        except Exception:  # noqa: BLE001 — supervision is best-effort, never re-raise
            logger.exception("stall terminalization failed for %s", conversation_id)

    async def _maybe_shadow_fold_finished_manifest(
        self, conversation_id: str, *, events: list[Any] | None = None
    ) -> None:
        """Run the REL-2a shadow fold once for the latest FINISHED StatusEvent.

        Multiple observers can see the same terminal finish (`_run_with_persistence`,
        the clean-return finalizer, Pi's direct conclusion path). The event seq is the
        durable finish identity: fold exactly once for that seq, but allow a later
        resumed segment with a new FINISHED marker to fold again.
        """
        if not manifest_shadow_enabled():
            return
        try:
            if events is None:
                events = await self._store.get_events(conversation_id)
            finished_seq = max(
                (
                    e.seq or 0
                    for e in events
                    if isinstance(e, StatusEvent)
                    and e.status is ConversationStatus.FINISHED
                ),
                default=0,
            )
            if finished_seq <= 0:
                logger.info(
                    "shadow fold skipped for %s: no FINISHED status event",
                    conversation_id,
                )
                return
            if self._shadow_folded_finished_seq.get(conversation_id) == finished_seq:
                logger.debug(
                    "shadow fold skipped for %s: FINISHED seq %d already folded",
                    conversation_id,
                    finished_seq,
                )
                return
            self._shadow_folded_finished_seq[conversation_id] = finished_seq
            await self._shadow_fold_manifest(conversation_id, events=events)
        except Exception:  # noqa: BLE001 — shadow is telemetry; never perturb a finish
            logger.exception(
                "artifact-manifest shadow fold scheduling failed for %s",
                conversation_id,
            )

    async def _shadow_fold_manifest(
        self, conversation_id: str, *, events: list[Any] | None = None
    ) -> None:
        """[REL-2a step2b-2b] SHADOW dual-write of the artifact manifest at finish-success.

        Gated by the caller on ``manifest_shadow_enabled()`` (default OFF). Projects the emitted
        artifact paths from the event log (the single-source truth the download jail uses), reads
        the maintained manifest, LOGS any divergence, then upserts the projected paths so the
        manifest tracks reality. No reader is switched — this is dual-write + telemetry only, so
        the campaign can prove the manifest faithfully mirrors the projection over many live runs
        before any consumer is migrated onto it. Best-effort: a fold failure never affects the run
        (finish already succeeded); the executor may be evicted by now → the sandbox guard no-ops.
        """
        try:
            sbx = getattr(self._executors.get(conversation_id), "sandbox", None)
            if sbx is None:
                logger.info(
                    "shadow fold skipped for %s: sandbox already released",
                    conversation_id,
                )
                return  # sandbox already torn down → nothing durable to fold into; fail-soft
            if events is None:
                events = await self._store.get_events(conversation_id)
            projected = artifact_paths_from_events(events)
            store = ArtifactMemoryStore(sbx)
            manifest = await store.read_artifacts()
            manifest_paths = {r.path for r in manifest}
            missing, extra = manifest_path_divergence(projected, manifest_paths)
            if missing or extra:
                logger.info(
                    "artifact-manifest shadow divergence for %s: missing=%s extra=%s",
                    conversation_id,
                    sorted(missing),
                    sorted(extra),
                )
            # Dual-write: fold the projection into the manifest (upsert is per-cid RMW-locked, so
            # concurrent folds can't lose an entry). Only paths the manifest lacks need writing.
            for path in sorted(missing):
                await store.upsert_artifact(ArtifactRecord(path=path))
            logger.info(
                "shadow fold ran for %s: projected=%d manifest=%d missing=%d",
                conversation_id,
                len(projected),
                len(manifest_paths),
                len(missing),
            )
        except Exception:  # noqa: BLE001 — shadow is telemetry; never perturb a finished run
            logger.exception("artifact-manifest shadow fold failed for %s", conversation_id)

    async def sweep_stranded_runs_once(self) -> int:
        """W11 backstop watchdog (idle-sweep cadence). A conversation whose status is
        RUNNING but which has NO live run task is STRANDED — nothing will ever advance
        it (the done-callback was lost, a re-kick never landed, or a prior process left
        it RUNNING between reconcile passes). Route it through the same recovery as a
        clean non-terminal return: re-kick once, else STUCK. Returns the count acted on.

        Distinct from reconcile_orphaned_runs (startup-only, store-wide → PAUSED): this
        runs continuously over the IN-PROCESS loops so a runtime stall self-heals
        without waiting for a restart. Zero false-positive risk: a healthy RUNNING run
        always has a live, not-done task in self._tasks, which is skipped here."""
        acted = 0
        for cid in list(self._loops):
            task = self._tasks.get(cid)
            if task is not None and not task.done():
                continue  # a live task is driving it — healthy
            try:
                state = await self._store.get_state(cid)
            except Exception:  # noqa: BLE001 — one bad cid must not abort the sweep
                continue
            if state.execution_status is not ConversationStatus.RUNNING:
                continue
            logger.warning(
                "stranded RUNNING conversation %s (no live task) — reconciling", cid
            )
            with contextlib.suppress(Exception):
                await self._finalize_clean_return(cid)
            acted += 1
        return acted

    async def _terminalize_crashed(
        self, conversation_id: str, exc: BaseException, generation: int | None = None
    ) -> None:
        """Append a terminal ERROR status + a user-visible reminder for a crashed run.
        Idempotent: never overwrites an already-concluded status.

        Generation-guarded (finding #3 — the crash path made SYMMETRIC with the clean
        path). `_on_run_task_done` pops this run's task then schedules THIS terminalizer
        async, so in the window before it runs a fresh user turn can start a NEWER run
        (generation N+1) that REUSES the conversation's pin. A stale crash must then
        neither append ERROR into the newer run's event log nor clear the newer run's
        pin — so if a newer generation already started, skip terminalization entirely.
        `generation` is None for legacy/stranded callers with no competing newer run
        (terminalize as before)."""
        try:
            state = await self._store.get_state(conversation_id)
            # Re-check the live run-generation AFTER the await: a newer run may have been
            # kicked while this stale crash cleanup was scheduled. The check + the ERROR
            # append below are separated only by synchronous statements (no await), so a
            # newer run can never slip in between the guard and the append.
            if (
                generation is not None
                and self._run_generation.get(conversation_id) != generation
            ):
                return  # a newer run owns this conversation — the crash is stale, drop it
            if state.execution_status in self._CONCLUDED_STATUSES:
                return  # already concluded — don't clobber
            detail = f"uncaught {type(exc).__name__}: {exc}"[:200]
            logger.error("run task for %s crashed: %s", conversation_id, detail)
            await self._store.append(
                conversation_id,
                StatusEvent(status=ConversationStatus.ERROR, detail=detail),
            )
            # Terminal ending → release the kernel pin (next run re-resolves). #1.
            # Generation-guarded (finding #3): if a newer run reused the pin during the
            # ERROR append above, leave it — that run owns the pin now.
            self._unpin_if_current_generation(conversation_id, generation)
            await self._emit_persistence_reminder(
                conversation_id,
                f"The run stopped on an unexpected internal error ({type(exc).__name__}). "
                "It has been recorded as failed; you can retry or adjust the task.",
            )
            # Engine-rekick fix: ERROR is terminal — recover a follow-up that landed
            # during this crash terminalization (guarded against looping). Reached
            # only when this generation still owns the conversation (the guard above).
            await self._maybe_rekick_for_stranded_followup(conversation_id)
        except Exception:  # noqa: BLE001 — supervision is best-effort, never re-raise
            logger.exception("crash terminalization failed for %s", conversation_id)

    # W-35: how long a SUCCESSFUL driver pre-flight is trusted before re-probing.
    _DRIVER_PREFLIGHT_TTL_S = 60.0
    # W-35 P1-1: HARD wall-clock bound on a SINGLE pre-flight probe. The whole
    # point of pre-flight is to FAIL FAST — so it must NOT inherit the router's 5
    # same-model transient retries (routing.py _MAX_ATTEMPTS) × the provider's
    # 180s timeout (openai_provider.py). A black-holed driver would otherwise
    # stall kick()/research_stream() for MINUTES before any error. We cap the
    # entire probe (including any internal retries) at this deadline via
    # asyncio.wait_for; a timeout is ITSELF an "unreachable" verdict.
    _DRIVER_PREFLIGHT_TIMEOUT_S = 10.0
    # W-35 (resilience): a slow remote reasoning model can miss a SINGLE probe yet
    # be perfectly reachable (other calls succeed seconds before/after). Retry the
    # probe a few times with a short escalating backoff before declaring the driver
    # unreachable, so a momentary latency blip does not hard-fail a working run. The
    # total stays bounded (ATTEMPTS × TIMEOUT + backoffs) so a genuinely-dead driver
    # still fails reasonably fast. Only TRANSIENT verdicts (timeout / LLMTransientError)
    # are retried; a hard verdict (auth / misconfig / unavailable) fails immediately.
    _DRIVER_PREFLIGHT_ATTEMPTS = 3
    _DRIVER_PREFLIGHT_BACKOFF_S = 0.5

    async def _preflight_driver(
        self,
        conversation_id: str | None,
        *,
        override: str | None = None,
        role: ModelRole = ModelRole.AGENT_DRIVER,
    ) -> str | None:
        """W-35: make ONE cheap (1-token) real call against the RESOLVED driver
        endpoint+key BEFORE the loop composes, so a dead / unauthed / misconfigured
        driver fails fast with a NAMED reason instead of stalling silently on the
        first mid-loop call. Returns None when the driver is reachable, else a
        human-readable reason string (the caller surfaces it as StatusEvent(ERROR)
        / an error frame and does NOT start the loop).

        A SUCCESS is cached for _DRIVER_PREFLIGHT_TTL_S (keyed by the resolved model)
        so a healthy driver adds no latency to every kick. Error classification is
        owned by the provider (openai_provider._raise_typed): LLMAuthError /
        LLMProviderUnavailable / LLMTransientError / NoEligibleModel all block; a
        content-filter or context-window response means the endpoint ANSWERED, so it
        passes (the endpoint is reachable — that's all pre-flight checks)."""
        if self._injected_router is not None:
            # Test/dev seam: a pinned router has no real endpoint to probe.
            return None
        cid = conversation_id or ""
        override = override if override is not None else self._model_override.get(cid)
        cfg = self._config_store.load()
        if override and override in cfg.models:
            key = override
        else:
            try:
                key = cfg.model_for(role)
            except Exception:  # noqa: BLE001 — resolution failure ⇒ generic label
                key = "?"
        # Proven-state is keyed by the SPECIFIC driver being probed — (conversation,
        # role, resolved model) — so a soft-degrade only ever applies to the same
        # driver that previously succeeded HERE, never a different role/model.
        proven_key = (cid, role, key)
        cached = self._driver_preflight_ok.get(key)
        if cached is not None and time.monotonic() - cached < self._DRIVER_PREFLIGHT_TTL_S:
            self._driver_proven.add(proven_key)
            return None
        router = self._router_now(pick=override, conversation_id=cid)
        req = CompletionRequest(
            profile=CapabilityProfile(role=role),
            messages=[LLMMessage(role="user", content="ping")],
            max_tokens=1,
        )
        # A TRANSIENT verdict (timeout / LLMTransientError) is retried up to
        # _DRIVER_PREFLIGHT_ATTEMPTS before it counts; a HARD verdict (auth /
        # misconfig / unavailable / other) returns immediately. transient_reason
        # holds the last transient verdict; it is cleared the moment a probe
        # reaches the endpoint (a real answer OR a content-filter/context reply).
        transient_reason: str | None = None
        for attempt in range(self._DRIVER_PREFLIGHT_ATTEMPTS):
            try:
                # P1-1: bound the probe so it FAILS FAST. asyncio.wait_for caps the
                # whole call (resolution + any same-model transient retries + the
                # provider round-trip) at _DRIVER_PREFLIGHT_TIMEOUT_S; a black-holed
                # driver is cancelled at the deadline instead of stalling for minutes.
                await asyncio.wait_for(
                    router.complete(
                        req,
                        context=CallContext(conversation_id=cid, model_override=override),
                    ),
                    self._DRIVER_PREFLIGHT_TIMEOUT_S,
                )
                transient_reason = None
                break  # reachable
            except TimeoutError:
                transient_reason = (
                    f"Driver '{key}' unreachable: no response within "
                    f"{self._DRIVER_PREFLIGHT_TIMEOUT_S:.0f}s (pre-flight timed out)"
                )
            except (LLMContentFiltered, LLMContextWindowExceeded):
                transient_reason = None
                break  # the endpoint answered → reachable
            except NoEligibleModel as exc:
                return f"Driver '{key}' is misconfigured: {exc}"
            except LLMAuthError as exc:
                return f"Driver '{key}' rejected the API key: {exc}"
            except LLMProviderUnavailable as exc:
                return f"Driver '{key}' is unavailable: {exc}"
            except LLMTransientError as exc:
                transient_reason = f"Driver '{key}' unreachable: {exc}"
            except LLMError as exc:
                return f"Driver '{key}' error: {exc}"
            # transient verdict: brief escalating backoff, then re-probe (unless
            # this was the final attempt).
            if attempt + 1 < self._DRIVER_PREFLIGHT_ATTEMPTS:
                await asyncio.sleep(self._DRIVER_PREFLIGHT_BACKOFF_S * (attempt + 1))

        if transient_reason is not None:
            # Every probe failed with a TRANSIENT verdict. If THIS SAME driver
            # (conversation + role + model) has ALREADY proven itself (e.g. a build
            # that ran for many turns), a momentary slow remote model must NOT
            # terminate the run: soft-degrade to a warning and PROCEED — the real
            # generation will surface a genuine error if the driver is actually down.
            # A driver that has NEVER answered for this (conversation, role, model) is
            # more legitimately terminal, so we keep the named terminal reason —
            # crucially, a DIFFERENT proven driver in the same conversation (e.g. a
            # build AGENT_DRIVER) does NOT mask a genuinely-dead DR RAG_ANSWERER.
            if proven_key in self._driver_proven:
                logger.warning(
                    "driver pre-flight for %r (role=%s) failed transiently (%s) but it "
                    "already succeeded in conversation %s — proceeding (soft-degrade)",
                    key,
                    role,
                    transient_reason,
                    cid or "<none>",
                )
                return None
            return transient_reason
        self._driver_preflight_ok[key] = time.monotonic()
        self._driver_proven.add(proven_key)
        return None

    # W-48: HARD wall-clock bound on a SINGLE sandbox connectivity pre-flight. Like
    # the driver pre-flight, the WHOLE point is to FAIL FAST — a gVisor host that's
    # down (or an ssh:// host that black-holes the connection) must not stall the
    # first kick for the docker-py / system-ssh default minute. The backends' own
    # client_timeout_s bounds the socket, but the SSH transport is outside that, so
    # asyncio.wait_for caps the entire probe here; a timeout is ITSELF "unreachable".
    _SANDBOX_PREFLIGHT_TIMEOUT_S = 12.0

    @staticmethod
    def _sandbox_endpoint_label(service: SandboxService) -> str:
        """[W-48] A human-readable label NAMING the backend's connection endpoint, for
        a typed pre-flight error the user can act on (which host/socket to fix)."""
        name = getattr(service, "name", "sandbox")
        cfg = getattr(service, "_cfg", None)
        if cfg is not None:
            if name in ("gvisor", "local"):
                return f"{name} sandbox host {getattr(cfg, 'docker_socket', '?')}"
            if name == "podman":
                return f"podman sandbox host {getattr(cfg, 'podman_url', '?')}"
        return f"{name} sandbox"

    async def _preflight_sandbox(self, conversation_id: str) -> str | None:
        """W-48: first-use connectivity PREFLIGHT for the Build sandbox. Probe the
        configured backend's endpoint (the Docker socket / ssh:// host for the
        container backends; the Podman native-remote socket; the workspace root for
        the process backend) BEFORE the loop composes a session, so a misconfigured /
        unreachable backend (e.g. gVisor pointed at a host that's down, or the
        local-socket default selected for the REMOTE gVisor tier) fails fast with a
        NAMED reason — "gVisor sandbox host ssh://sandbox@<host> unreachable: …" —
        instead of a generic 500 on the first tool call. Returns None when reachable,
        else the reason string (the caller surfaces it as StatusEvent(ERROR) + an
        ambient reminder and does NOT start the loop). Bounded by
        _SANDBOX_PREFLIGHT_TIMEOUT_S; a hung host is itself an 'unreachable' verdict.
        The process (dev) backend's healthcheck is a cheap local check → ~no latency."""
        if self._injected_sandbox is not None:
            return None  # test/dev seam: the injected backend is authoritative
        try:
            service = self._sandbox_service_now()
        except Exception as exc:  # noqa: BLE001 — backend couldn't even be built
            return f"sandbox backend is misconfigured: {exc}"
        label = self._sandbox_endpoint_label(service)
        try:
            await asyncio.wait_for(service.healthcheck(), self._SANDBOX_PREFLIGHT_TIMEOUT_S)
        except TimeoutError:
            return (
                f"{label} unreachable: no response within "
                f"{self._SANDBOX_PREFLIGHT_TIMEOUT_S:.0f}s (sandbox pre-flight timed out)"
            )
        except SandboxUnavailableError as exc:
            return f"{label} unreachable: {exc}"
        except Exception as exc:  # noqa: BLE001 — any probe failure blocks, with the cause
            return f"{label} error: {exc}"
        return None

    async def _best_effort_destroy_session(self, session: SandboxSession) -> None:
        """[W-48(c)] Tear down a stale-backend session without ever raising — the box
        on the OLD backend is being abandoned because Settings switched backends."""
        with contextlib.suppress(Exception):
            await session.destroy()

    def _clear_evicted_session_markers(self, conversation_id: str) -> None:
        """[W-48 P1] Mirror the per-session marker cleanup that `_teardown_sandbox`
        does, for the backend-eviction paths (which pop the loop/executor caches
        directly rather than going through teardown). Most load-bearing: clearing the
        `_rehydrated` marker — leaving it set makes the next run SKIP rehydrate and
        start in an EMPTY workspace (silently losing prior work). The view-cache /
        wake-lock / last-session entries are dropped too so a stale handle can't ghost
        the fresh backend's session."""
        for key in [k for k in self._session_view_cache if k[0] == conversation_id]:
            del self._session_view_cache[key]
        for key in [k for k in self._session_view_locks if k[0] == conversation_id]:
            del self._session_view_locks[key]
        self._wake_locks.pop(conversation_id, None)
        self._last_sessions.pop(conversation_id, None)
        rehydrated = getattr(self, "_rehydrated", None)
        if rehydrated is not None:
            rehydrated.discard(conversation_id)
        with contextlib.suppress(Exception):
            from disco.tools.builtin.files import clear_conversation_read_state

            clear_conversation_read_state(conversation_id)

    def _evict_loop_for_model_change(self, conversation_id: str) -> None:
        """A deliberate model/assist change on a TERMINAL conversation (ERROR / STUCK /
        FINISHED / PAUSED / IDLE-with-unfinished-plan): drop the cached loop + executor
        that were composed against the OLD model so the NEXT kick (resume / replan)
        re-composes the driver, summarizer, ModelExecutionPolicy, capability scope, and
        driver-LLM endpoint with the NEW model — the model-pill-silently-ignored fix (#24).

        The live sandbox SESSION is preserved: it is re-parked as the pending session so
        `_compose_build_loop` adopts the SAME workspace (object identity — no destroy, no
        leak, no rehydrate round-trip). A later backend change is still caught at the next
        kick by `_evict_stale_backend`, which reconciles a mismatched pending session.

        No-op when a run is in flight (the settings gate already rejected that) — never
        evict a live loop. Called under the per-cid settings lock from
        `RuntimeSettings.apply_settings_change` AFTER the override is persisted."""
        task = self._tasks.get(conversation_id)
        if task is not None and not task.done():
            return  # defensive: never evict under a live run (the gate already blocks it)
        self._loops.pop(conversation_id, None)
        executor = self._executors.pop(conversation_id, None)
        # Preserve the live sandbox so the rebuilt loop adopts the SAME box (workspace +
        # shell sessions intact). Don't clobber an existing pending session (a pre-kick
        # upload session) if one is already parked.
        if conversation_id not in self._pending_sessions:
            sess = getattr(executor, "_sandbox", None) if executor is not None else None
            if sess is not None:
                self._pending_sessions[conversation_id] = sess

    def _evict_stale_backend(self, conversation_id: str) -> None:
        """[W-48(c)] On a persisted sandbox-backend change, reconcile THIS conversation:
        if its cached session runs a DIFFERENT backend than the now-configured one (the
        user switched backends in Settings), evict the cached loop/executor/pending
        session so the NEXT compose builds a fresh sandbox on the new backend, and
        best-effort tear the old box down (never leak the old backend's session).
        No-op when the backend is unchanged, a backend override is injected (tests), a
        live run is in flight (never reconnect mid-turn), or the conversation is PARKED
        at a gate (P1 — evicting mid-gate would discard its workspace). Called
        synchronously at the top of kick(); the async teardown is scheduled so kick()
        stays non-blocking."""
        if self._injected_sandbox is not None:
            return  # injected backend is authoritative; Settings backend is ignored
        try:
            current = self._config_store.load().sandbox.backend
        except Exception:  # noqa: BLE001 — config unreadable ⇒ leave caches alone
            return
        task = self._tasks.get(conversation_id)
        if task is not None and not task.done():
            return  # mid-turn — don't reconnect
        # [W-48 P1] A gate-parked conv has no active task but its mid-gate workspace
        # lives in the OLD backend's box — evicting it on a backend change would lose
        # that state. Treat a gate like an active task: skip. (Sync path → the
        # in-process last-status cache; see _last_status.)
        if self._last_status.get(conversation_id) in _GATE_STATES:
            return
        stale: list[SandboxSession] = []
        executor = self._executors.get(conversation_id)
        sess = getattr(executor, "_sandbox", None) if executor is not None else None
        if sess is not None and getattr(sess, "backend_name", current) != current:
            stale.append(sess)
            self._loops.pop(conversation_id, None)
            self._executors.pop(conversation_id, None)
        pending = self._pending_sessions.get(conversation_id)
        if pending is not None and pending.backend_name != current:
            self._pending_sessions.pop(conversation_id, None)
            stale.append(pending)
        if stale:
            self._clear_evicted_session_markers(conversation_id)
        for s in stale:
            with contextlib.suppress(RuntimeError):  # no running loop (shutdown) → skip
                asyncio.create_task(self._best_effort_destroy_session(s))

    async def reconcile_sandbox_backend(self) -> int:
        """[W-48(c)] Sweep ALL cached sessions and destroy any whose backend != the
        now-configured backend, so the next kick of each composes a fresh sandbox on
        the new backend. Skips conversations with a LIVE run task (don't reconnect
        mid-turn). Returns the count reconciled. The lazy per-conversation
        `_evict_stale_backend` (run at kick) is the cross-process path that needs no
        signal; this is the explicit form (tests + a future config-change hook)."""
        if self._injected_sandbox is not None:
            return 0
        try:
            current = self._config_store.load().sandbox.backend
        except Exception:  # noqa: BLE001
            return 0
        reconciled = 0
        for cid in list(self._executors) + list(self._pending_sessions):
            task = self._tasks.get(cid)
            if task is not None and not task.done():
                continue  # mid-turn — don't reconnect
            # [W-48 P1] Skip a conv PARKED at a gate — evicting mid-gate would discard
            # its workspace. This path is async, so read the store authoritatively.
            try:
                state = await self._store.get_state(cid)
            except Exception:  # noqa: BLE001 — one bad cid must not abort the sweep
                state = None
            if state is not None and state.execution_status in _GATE_STATES:
                continue
            executor = self._executors.get(cid)
            sess = getattr(executor, "_sandbox", None) if executor is not None else None
            pending = self._pending_sessions.get(cid)
            mismatched: list[SandboxSession] = []
            if sess is not None and getattr(sess, "backend_name", current) != current:
                mismatched.append(sess)
                self._loops.pop(cid, None)
                self._executors.pop(cid, None)
            if pending is not None and pending.backend_name != current:
                self._pending_sessions.pop(cid, None)
                mismatched.append(pending)
            if mismatched:
                self._clear_evicted_session_markers(cid)
            for s in mismatched:
                await self._best_effort_destroy_session(s)
                reconciled += 1
        return reconciled

    async def _run_with_persistence(
        self, conversation_id: str, loop: AgentLoop
    ) -> Any:
        """Snapshot/rehydrate wrapper around `loop.run()`. Surface-aware:
        - Build → snapshot+rehydrate the workspace as before.
        - Deep Research → short-circuit `loop.run()` on the post-plan-approval
          turn and run the DeepResearchRun engine; emit the ReportEvent +
          StatusEvent(FINISHED) directly.
        - Research → unchanged (the loop runs, no persistence)."""
        surface = self._surface_of(conversation_id)

        if surface == "deep_research":
            # Short-circuit the loop for Deep Research. The plan-mode intercept
            # the loop would otherwise run isn't useful here — Deep Research
            # decomposes the query algorithmically (not via an LLM planning
            # round) and the engine handles the rest. The loop's plan-approval
            # state machine is reused via control-op routing; the loop's
            # `run()` itself is not the right driver.
            await self._maybe_run_deep_research(conversation_id)
            # Return the (possibly-updated) state. The store reflects whatever
            # we emitted.
            return await self._store.get_state(conversation_id)

        # W-35: driver pre-flight BEFORE the loop runs. A dead / unauthed /
        # misconfigured driver would otherwise stall silently on the first
        # mid-loop call. On failure, surface a NAMED StatusEvent(ERROR) + an
        # ambient reminder and DO NOT start the loop (return the ERROR state).
        reason = await self._preflight_driver(conversation_id)
        if reason is not None:
            await self._store.append(
                conversation_id,
                StatusEvent(status=ConversationStatus.ERROR, detail=reason[:200]),
            )
            await self._emit_persistence_reminder(
                conversation_id,
                f"{reason} The run did not start — check the model's endpoint and "
                "API key in Settings, then send a message to retry.",
            )
            return await self._store.get_state(conversation_id)

        # W-48: sandbox connectivity pre-flight BEFORE a Build loop composes a
        # session. A misconfigured / unreachable backend (gVisor host down, or the
        # local-socket default left on the REMOTE gVisor tier) would otherwise stall
        # silently on the first tool call and surface as a generic error. Probe it
        # up-front and, on failure, surface a NAMED StatusEvent(ERROR) + an ambient
        # reminder and DO NOT start the loop (mirrors the W-35 driver pre-flight).
        if surface in self._BUILD_LIKE_SURFACES:
            sandbox_reason = await self._preflight_sandbox(conversation_id)
            if sandbox_reason is not None:
                await self._store.append(
                    conversation_id,
                    StatusEvent(status=ConversationStatus.ERROR, detail=sandbox_reason[:200]),
                )
                await self._emit_persistence_reminder(
                    conversation_id,
                    f"{sandbox_reason} The run did not start — check the sandbox "
                    "backend's host/connection in Settings → Sandbox, then send a "
                    "message to retry.",
                )
                return await self._store.get_state(conversation_id)

        # Rehydrate hook: BEFORE the loop runs for the first time, if a project
        # storage path is configured AND a prior snapshot exists for this cid,
        # write its files into the (lazy) sandbox so the agent sees them. Reads
        # are cheap; the rehydrate only writes if there are files on disk.
        if surface in self._BUILD_LIKE_SURFACES:
            await self._maybe_rehydrate(conversation_id)
            # DC-07: re-materialize server-side uploads into the fresh sandbox.
            # This is the SINGLE chokepoint that covers ALL paths:
            #   - post-restart resume → fresh sandbox via lazy compose
            #   - first build after upload (pending session adoption or fresh)
            #   - mid-run recreation (also handled by _rehydrate_after_recreate)
            await self._rematerialize_uploads(conversation_id)

        state = await loop.run()

        # Snapshot hook: capture the workspace whenever a build run ENDS — not only
        # on FINISHED. A run that ends STUCK/ERROR/PAUSED still wrote real files (the
        # model may have built most of the deliverable before getting stuck); snapshot
        # so that work is NOT lost (it was — a stuck iteration silently discarded a
        # whole feature it had written). Snapshot is idempotent + cheap.
        _ENDED = {
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
            ConversationStatus.PAUSED,
            ConversationStatus.IDLE,
        }
        # The disco loop sets the RETURNED state's execution_status terminal; the PiKernel
        # instead APPENDS a FINISHED StatusEvent and returns a non-terminal state object
        # (bake-off #5a — its workspace then never snapshotted, scoring it a false 0%). Re-read
        # the AUTHORITATIVE state from the store (computed from the event log, so it is terminal
        # for BOTH kernels) for the end-gate.
        ended_state = await self._store.get_state(conversation_id)
        if surface in self._BUILD_LIKE_SURFACES and ended_state.execution_status in _ENDED:
            # REL-2a shadow fold must run while the build sandbox is still live. Do
            # this at the authoritative end-state boundary, before snapshot/suspend/
            # finalizer work can race executor release. The helper is seq-guarded so
            # the later clean-return finalizer backstop cannot double-fold.
            if ended_state.execution_status is ConversationStatus.FINISHED:
                await self._maybe_shadow_fold_finished_manifest(conversation_id)
            await self._maybe_snapshot(conversation_id)
            # FINISHED now rides the idle sweep like STUCK/ERROR/PAUSED;
            # suspend = sweep_idle_once -> _suspend
        return state

    async def _teardown_sandbox(self, conversation_id: str) -> None:
        return await self._lifecycle._teardown_sandbox(conversation_id)

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        return await self._lifecycle.reconcile_orphaned_runs(owner_id=owner_id)

    # ---- MCP pool lifecycle (RP-05 rung A) — delegators to McpManager --------

    async def _start_mcp_pool(self) -> None:
        return await self._mcp._start_mcp_pool()

    async def _close_mcp_pool(self) -> None:
        return await self._mcp._close_mcp_pool()

    @property
    def _mcp_call_target(self) -> Any:
        return self._mcp._mcp_call_target

    def mcp_approval_state(self) -> dict[str, dict]:
        return self._mcp.mcp_approval_state()

    def on_connect(self, conversation_id: str) -> None:
        return self._lifecycle.on_connect(conversation_id)

    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
        return self._lifecycle.on_disconnect(conversation_id, grace_s=grace_s)

    async def _suspend(self, conversation_id: str) -> None:
        return await self._lifecycle._suspend(conversation_id)

    def sandbox_state(self, conversation_id: str) -> str | None:
        return self._lifecycle.sandbox_state(conversation_id)

    def sandbox_instance_ids(self, conversation_id: str) -> list[str]:
        return self._lifecycle.sandbox_instance_ids(conversation_id)

    async def sweep_idle_once(self) -> int:
        return await self._lifecycle.sweep_idle_once()

    async def sweep_abandoned_gates_once(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        return await self._lifecycle.sweep_abandoned_gates_once(owner_id=owner_id)

    async def _idle_sweep_loop(self) -> None:
        return await self._lifecycle._idle_sweep_loop()

    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
        return await self._dr._maybe_run_deep_research(conversation_id)

    async def _propose_deep_research_plan(self, conversation_id: str, events: list) -> None:
        return await self._dr._propose_deep_research_plan(conversation_id, events)

    async def _execute_deep_research(
        self,
        conversation_id: str,
        plan: PlanEvent,
        *,
        resume_from: ReportEvent | None = None,
    ) -> None:
        return await self._dr._execute_deep_research(
            conversation_id, plan, resume_from=resume_from
        )

    def title_service(self) -> TitleService:
        """Public accessor for the auto-titler (used by the projects backfill route)."""
        return self._title_service

    def project_store(self) -> ProjectStore:
        """Public accessor for the live project store (used by the agent-server's
        projects endpoints). Always returns a ProjectStore — callers check
        ``.status()`` for the full validation classification (ok / not_found /
        not_writable etc.).  An empty ``projects_root`` resolves to the
        auto-default path rather than returning None."""
        return self._project_store_now()

    # ---- share export (RP-06) ----------------------------------------------

    async def share_export(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        return await self._share.share_export(conversation_id, owner_id=owner_id)

    _IMPORT_MAX_EVENTS = 20_000

    async def share_import(
        self, bundle: Any, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> dict[str, Any]:
        return await self._share.share_import(bundle, owner_id=owner_id)

    @staticmethod
    def _has_fresh_user_message(events, reports) -> bool:
        return DeepResearchService._has_fresh_user_message(events, reports)

    async def export_report(
        self,
        conversation_id: str,
        fmt: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> tuple[bytes, str, str] | None:
        return await self._dr.export_report(conversation_id, fmt, owner_id=owner_id)

    def create_share_link(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        return self._share.create_share_link(conversation_id, owner_id=owner_id)

    async def create_share_link_async(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        return await self._share.create_share_link_async(
            conversation_id, owner_id=owner_id
        )

    def lookup_share_link(self, token: str) -> dict | None:
        return self._share.lookup_share_link(token)

    def list_share_links(self, *, owner_id: str) -> list[dict]:
        return self._share.list_share_links(owner_id=owner_id)

    def revoke_share_link(self, token: str, *, owner_id: str) -> bool:
        return self._share.revoke_share_link(token, owner_id=owner_id)

    def _project_store_now(self) -> ProjectStore:
        """Build a ProjectStore from the current settings. Built per-call (cheap;
        matches the rest of the runtime's "reload the config each request"
        discipline). An empty ``projects_root`` (fresh install) resolves to the
        auto-default path via :func:`resolve_projects_root` inside
        :class:`ProjectStore`; the validity status is always queryable via
        ``store.status()`` at the use site."""
        root = self._config_store.load().projects.projects_root
        return ProjectStore(root)

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        return await self._lifecycle._maybe_rehydrate(conversation_id)

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
        return await self._lifecycle._rehydrate_after_recreate(conversation_id)

    async def _rematerialize_uploads(self, conversation_id: str) -> None:
        return await self._lifecycle._rematerialize_uploads(conversation_id)

    async def _maybe_snapshot(self, conversation_id: str) -> None:
        return await self._lifecycle._maybe_snapshot(conversation_id)

    async def _emit_persistence_reminder(self, conversation_id: str, body: str) -> None:
        """Surface a project-persistence problem on the event log as an implicit
        system-reminder — same pattern as the loop's other gates, so the model
        and the UI both see what went wrong, named."""
        await self._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"Project persistence note: {body}\n"
                        "</system-reminder>"
                    ),
                ),
            ),
        )

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        return self._preview.resolve_cid_prefix(cid8)

    def preview_upstream(self, conversation_id: str) -> str | None:
        """The URL the AGENT-SERVER can reach the conversation's dev server at (the backend
        owns how — localhost for local, the remote host's tailnet IP for gVisor). The
        browser never touches this; the agent-server proxies it (single origin)."""
        return self.port_upstream(conversation_id, PREVIEW_PORT)

    def port_upstream(self, conversation_id: str, port: int) -> str | None:
        return self._preview.port_upstream(conversation_id, port)

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        return await self._preview.wake_for_preview(cid8, port)

    def live_session(self, conversation_id: str) -> SandboxSession | None:
        """Read-only sandbox accessor (BP-14). Does NOT create a session — a GET must
        have no creation side-effects. Returns None when no executor/sandbox exists."""
        executor = self._executors.get(conversation_id)
        return getattr(executor, "_sandbox", None) if executor is not None else None

    def sandbox_backend_name(self) -> str | None:
        """Name of the active sandbox backend ('gvisor', 'podman', 'local', 'process').
        Returns None on any lookup failure."""
        try:
            return self._sandbox_service_now().name
        except Exception:  # noqa: BLE001
            return None

    _SESSIONS_LIST_RETRIES: int = 2
    _SESSIONS_LIST_BACKOFF_S: float = 0.25

    async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
        return await self._sessions.sessions_snapshot(conversation_id)

    async def sessions_list(self, conversation_id: str) -> list[SessionInfo]:
        """Compat wrapper — returns only the list, degraded on failure (DEFECT-1)."""
        return (await self.sessions_snapshot(conversation_id))[0]

    _SESSION_VIEW_CACHE_TTL: float = 0.5
    _SESSION_VIEW_MAX_CHARS: int = 100_000

    async def session_view(
        self, conversation_id: str, name: str, tail_chars: int
    ) -> SessionView | None:
        return await self._sessions.session_view(conversation_id, name, tail_chars)

    async def preview(self, conversation_id: str) -> dict[str, Any]:
        return await self._preview.preview(conversation_id)

    async def ensure_preview(self, conversation_id: str) -> bool:
        return await self._preview.ensure_preview(conversation_id)

    # ---- control ops: the confirmation gate + kill switch (BoD §13.4/§13.6) -----

    def _kernel_for(self, conversation_id: str) -> BuildKernel:
        """The active Build kernel for this conversation (Disco Pi campaign A1/A2).

        If a kernel is PINNED to this conversation's in-flight run, return it —
        a run must not re-resolve mid-flight (codex finding #1), so every control
        op on a live run lands on the SAME kernel the run started under, even if
        Settings (or the experimental flag) changed since.

        Otherwise resolve fresh: read the persisted `build_kernel` setting and
        resolve it against the experimental gate — `disco` (default) →
        `DiscoKernel`; `pi_experimental` → `PiKernel` ONLY when the experimental
        flag is on, else `DiscoKernel`. Both kernels are constructed once (back-ref
        only); this just selects. The config is reloaded per call, mirroring
        `_router_now`, so a Settings change takes effect on the next NEW run
        without a restart."""
        pinned = getattr(self, "_pinned_kernels", None)
        if pinned is not None:
            existing = pinned.get(conversation_id)
            if existing is not None:
                return existing
        selected = self._config_store.load().build_kernel
        return select_kernel(
            self, disco=self._disco_kernel, pi=self._pi_kernel, selected=selected
        )

    def _ensure_kernel_pinned(self, conversation_id: str) -> BuildKernel:
        """Resolve + PIN the kernel for a (continuing) run, if not already pinned
        (codex finding #1, point b). Called at the start/send/steer entry points
        BEFORE the first append/kick: the first turn of a run resolves the current
        selection and stores it; a steer / gate-resume reuses the existing pin so a
        run can never be split across kernels. The pin is cleared on terminalization
        (`_clear_pinned_kernel`), so the next run re-resolves the selection."""
        existing = self._pinned_kernels.get(conversation_id)
        if existing is not None:
            return existing
        # No pin yet → `_kernel_for` resolves fresh (the pin read above is empty).
        kernel = self._kernel_for(conversation_id)
        self._pinned_kernels[conversation_id] = kernel
        return kernel

    def _clear_pinned_kernel(self, conversation_id: str) -> None:
        """Drop the run's kernel pin so the next run re-resolves the current
        selection. Called only on a TERMINAL ending (FINISHED/ERROR/STUCK) or kill
        — NOT on a pause / gate-park, which stay pinned for resume."""
        self._pinned_kernels.pop(conversation_id, None)

    def attach_pi_token_store(self, store: PiInferenceTokenStore | None) -> None:
        """Wire the DiscoInferenceGateway's run-scoped token store onto the runtime so
        the terminalizers / control ops / delete / shutdown can revoke a conversation's
        tokens when its run ends (EPIC C deferred finding C#3). `create_app` calls this
        after it mounts the store on `app.state`. None-safe (passing None detaches)."""
        self._pi_token_store = store

    def _revoke_pi_tokens(self, conversation_id: str) -> None:
        """Revoke EVERY DiscoInferenceGateway token bound to `conversation_id` so a Pi
        kernel can no longer drive the model once the run has ended. None-safe (a no-op
        when no gateway store is wired — tests + non-gateway paths) and idempotent (the
        store skips already-revoked records and never raises). Best-effort: a revoke
        failure must never crash a terminalizer / kill / delete / shutdown."""
        store = self._pi_token_store
        if store is None:
            return
        with contextlib.suppress(Exception):
            store.revoke_conversation(conversation_id)

    def _unpin_if_current_generation(
        self, conversation_id: str, generation: int | None
    ) -> None:
        """Clear the kernel pin ONLY if no NEWER run has started since the finalizing
        task began (finding #3, the pin set/clear race). `_on_run_task_done` schedules
        the clean-return finalizer ASYNC; before it runs, a fresh user turn can append +
        `kick` a new run that REUSES this conversation's pin and bumps its run-generation.
        A stale finalizer must NOT then clear the pin out from under that newer run. When
        `generation` is None (the stranded-run sweep / legacy callers, which have no
        competing newer run) clear unconditionally.

        The gateway-token revoke is CO-LOCATED here so it shares the EXACT generation
        guard as the pin clear (EPIC C finding C#3): a stale/old-generation terminalizer
        that loses the guard returns WITHOUT revoking, so it can never revoke a token that
        now belongs to a NEWER run on the same conversation. Every guarded terminal path
        (`_finalize_clean_return` FINISHED/STUCK/IDLE, `_terminalize_crashed` ERROR, the
        abandoned-gate sweep, and `kill`) revokes here, with the winning generation."""
        if generation is not None and self._run_generation.get(conversation_id) != generation:
            return  # a newer run owns the pin/token now — leave both for that run
        self._clear_pinned_kernel(conversation_id)
        self._revoke_pi_tokens(conversation_id)

    def start(self, conversation_id: str) -> None:
        """Start/continue the conversation's run THROUGH the pinned kernel (codex
        finding #1, point a). For the default `disco` kernel this is a behaviour-
        identical pass-through to `kick`.

        If the kernel's `start` RAISES (e.g. a `PiKernel.start` failure) and we just
        created the pin in this call, roll it back (finding #2): the pin is committed
        only once the kernel call succeeds, so a failed start leaves NO pin and the
        next attempt re-resolves the current selection. A pre-existing pin (a steer /
        resume of a live run) is NOT rolled back — that run stays on its kernel."""
        newly_pinned = conversation_id not in self._pinned_kernels
        kernel = self._ensure_kernel_pinned(conversation_id)
        try:
            kernel.start(conversation_id)
        except BaseException:
            if newly_pinned:
                self._clear_pinned_kernel(conversation_id)
            raise

    async def send_user_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        context: str | None = None,
        steer: bool = False,
    ) -> MessageEvent:
        """Append a user turn (optional hidden context, optional steer) and
        start/continue the run — routed THROUGH the pinned kernel (codex finding
        #1, point a). For the default `disco` kernel this is byte-identical to the
        store-append + `kick` the routes performed inline before the seam. Returns
        the stored USER message so the REST routes can report its id/seq.

        If the kernel's `send_user_turn` RAISES (e.g. a `PiKernel.send_user_turn`
        failure, before any task is spawned) and we just created the pin in this call,
        roll it back (finding #2): the pin commits only once the kernel call succeeds,
        so a failed send leaves NO pin and the next attempt re-resolves. A pre-existing
        pin (a steer of a live run) is NOT rolled back — that run stays on its kernel."""
        newly_pinned = conversation_id not in self._pinned_kernels
        kernel = self._ensure_kernel_pinned(conversation_id)
        try:
            return await kernel.send_user_turn(
                conversation_id, text, context=context, steer=steer
            )
        except BaseException:
            if newly_pinned:
                self._clear_pinned_kernel(conversation_id)
            raise

    async def _run_continuing_control(
        self, conversation_id: str, call: Callable[[BuildKernel], Awaitable[Any]]
    ) -> Any:
        """Route a RUN-CONTINUING control op (confirm/reject/approve_plan/request_plan/
        pick_alternative) through the conversation's PINNED kernel (finding #1).

        These ops continue an in-flight (or gate-parked) run, so they MUST land on the
        SAME kernel the run started under. The previous code called `_kernel_for`
        directly: when no in-memory pin existed (after a restart, a legacy pre-A1 gated
        conversation, or any direct unpinned kick) it RE-RESOLVED the selection instead
        of pinning it — so a control op could start/continue a run on a kernel different
        from the one a later op would resolve, the exact half-disco/half-pi split #1
        forbids. Now we `_ensure_kernel_pinned` first: an existing pin is reused; a
        missing one is resolved + committed here. The same rollback-on-raise semantics as
        `start`/`send_user_turn` (finding #2): a FRESHLY-created pin is rolled back if the
        kernel call raises (so a failed op leaves no stuck pin), while a pre-existing pin
        from the live run is preserved. Byte-identical for the default `disco` kernel —
        the op still lands on `_control`/`_loop_for` exactly as before, just pinned."""
        newly_pinned = conversation_id not in self._pinned_kernels
        kernel = self._ensure_kernel_pinned(conversation_id)
        try:
            return await call(kernel)
        except BaseException:
            if newly_pinned:
                self._clear_pinned_kernel(conversation_id)
            raise

    async def confirm(self, conversation_id: str) -> None:
        return await self._run_continuing_control(
            conversation_id, lambda k: k.confirm(conversation_id)
        )

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        return await self._run_continuing_control(
            conversation_id, lambda k: k.reject(conversation_id, reason)
        )

    async def approve_plan(self, conversation_id: str) -> None:
        return await self._run_continuing_control(
            conversation_id, lambda k: k.approve_plan(conversation_id)
        )

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        return await self._run_continuing_control(
            conversation_id, lambda k: k.request_plan(conversation_id, text)
        )

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        """Resume from AWAITING_USER_DECISION by selecting the agent's proposed
        alternative path. The loop synthesizes an ActionEvent from the option's
        ToolCall and executes it directly, then resumes.

        Routed through the PINNED kernel (finding #1): previously this bypassed the
        kernel entirely and called `_loop_for(...).pick_alternative` directly, so a run
        could be continued unpinned. `DiscoKernel.pick_alternative` performs the SAME
        `_loop_for(...).pick_alternative(option_id)` call, so the disco path is
        byte-identical — just pinned."""
        return await self._run_continuing_control(
            conversation_id, lambda k: k.pick_alternative(conversation_id, option_id)
        )

    async def pause(self, conversation_id: str) -> None:
        return await self._control.pause(conversation_id)

    async def cancel(self, conversation_id: str) -> None:
        return await self._control.cancel(conversation_id)

    async def _drain_finishing_task(self, conversation_id: str) -> None:
        """WALK-18 resume fix. A cooperative Stop (cancel) persists a terminal
        status, but its loop task may still be finishing its in-flight model
        step. `kick()` is idempotent over a non-done task, so re-kicking before
        that task clears silently NO-OPs (the "resume does nothing" bug). Pop the
        lingering task and await it to completion so the subsequent kick spawns a
        FRESH run; bound the wait and hard-cancel a wedged step (resume then
        reconstructs context for the dangling action). Already-done / absent →
        nothing to drain."""
        task = self._tasks.pop(conversation_id, None)
        if task is None or task.done():
            return
        with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
            await asyncio.wait_for(task, _RESUME_DRAIN_TIMEOUT_S)

    async def resume(self, conversation_id: str) -> None:
        """Continue a stopped (PAUSED) Deep Research run. Clears the cancel flag and
        re-kicks; `_maybe_run_deep_research` detects the PAUSED state and re-runs the
        execution to completion (the new full report supersedes the partial)."""
        self._cancel_flags.pop(conversation_id, None)
        self.kick(conversation_id)

    def _condense_trailing_degeneracy(self, events: list):
        return self._resume._condense_trailing_degeneracy(events)

    async def _reconstruct_resume_context(
        self, conversation_id: str, events: list
    ) -> list:
        return await self._resume._reconstruct_resume_context(conversation_id, events)

    async def resume_conversation(self, conversation_id: str) -> dict:
        return await self._resume.resume_conversation(conversation_id)

    async def kill(self, conversation_id: str) -> None:
        # Capture the run-generation this kill is issued against (finding #4 — the LAST
        # terminalizer path). The control-op teardown AWAITS the killed task (+ the
        # executor/sandbox teardown), and in that window a fresh user turn can start a
        # NEWER run (generation N+1) that REUSES this conversation's loop/executor/pin.
        # Thread the captured generation so the kill terminalizes + tears down ONLY its
        # own run (mirrors the crash/clean-return/abandoned-gate terminalizers).
        generation = self._run_generation.get(conversation_id)
        # Hard kill = terminal → release the kernel pin (next run re-resolves). #1.
        # Generation-guarded (finding #4): clear only THIS generation's pin via the same
        # `_unpin_if_current_generation` semantics the other terminalizers use, so a
        # newer run that re-pins during teardown keeps its own pin.
        self._unpin_if_current_generation(conversation_id, generation)
        return await self._control.kill(conversation_id, generation)

    async def forget_conversation(self, conversation_id: str) -> None:
        """Drop ALL in-memory runtime state for a conversation that is being DELETED
        (finding #5 — the `_pinned_kernels` leak). The library/delete route removes the
        DB rows, but the agent-server process keeps per-conversation caches (the kernel
        pin, the run-generation, the cached loop + live task, sandbox executor/session,
        and the lightweight per-cid settings/DR caches) until process exit. A deleted
        conversation can never be reached again, so every such entry is pure leak — and a
        leaked pin/loop bound to a freed cid is a latent correctness hazard if the id is
        ever reused. Best-effort + idempotent: cancels the live task, tears down the
        sandbox executor/session, and pops every per-cid map. Never raises (a delete must
        not fail because cleanup hit a wedged sandbox)."""
        # Stop any in-flight run first so its done-callback can't re-pin/re-kick.
        self._clear_pinned_kernel(conversation_id)
        # The conversation is DELETED — unconditionally revoke any gateway token bound
        # to it (no generation guard: a deleted id can never be reused by a newer run,
        # and its rows are gone, so the token is pure leak). None-safe + idempotent.
        self._revoke_pi_tokens(conversation_id)
        task = self._tasks.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
        executor = self._executors.pop(conversation_id, None)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()
        session = self._pending_sessions.pop(conversation_id, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.destroy()
        # Pop every remaining per-conversation cache (no-op if absent).
        for cache in (
            self._run_generation,
            self._loops,
            self._nonterminal_rekicks,
            self._last_status,
            self._cancel_flags,
            self._model_override,
            self._surface,
            self._autonomous,
            self._assist,
            self._artifact_mode,
            self._depth,
            self._driver_preflight_ok,
            self._dr_steer,
            self._dr_injected_sources,
            self._upload_passages,
            self._last_sessions,
            self._mcp_approval_pending,
            # CONTRACT-ACTIVATE: the per-conversation build contract kind + phase tracker.
            self._build_kind,
            self._build_trackers,
            self._shadow_folded_finished_seq,
        ):
            cache.pop(conversation_id, None)

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for executor in self._executors.values():
            with contextlib.suppress(Exception):
                await executor.kill()
        for session in self._pending_sessions.values():
            with contextlib.suppress(Exception):
                await session.destroy()

    # ---- RP-08: scheduled tasks ---------------------------------------------

    def _schedule_manager(self) -> Any:
        return self._schedule._schedule_manager()

    async def _schedule_manager_loop(self) -> None:
        await self._schedule._schedule_manager_loop()

    def create_schedule(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        rrule: str,
        description: str,
        depth: str | None = None,
        model_override: str | None = None,
    ) -> dict:
        return self._schedule.create_schedule(
            conversation_id=conversation_id,
            owner_id=owner_id,
            rrule=rrule,
            description=description,
            depth=depth,
            model_override=model_override,
        )

    def list_schedules(
        self, *, owner_id: str, conversation_id: str | None = None
    ) -> list[dict]:
        return self._schedule.list_schedules(
            owner_id=owner_id, conversation_id=conversation_id
        )

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        return self._schedule.delete_schedule(schedule_id, owner_id=owner_id)

    async def fire_schedule_now(self, schedule_id: str, *, owner_id: str) -> bool:
        """Run a schedule immediately, out of band (gap #98). False if not found."""
        return await self._schedule.fire_now(schedule_id, owner_id=owner_id)

    def preview_schedule_runs(self, rrule: str, n: int = 3) -> list[str]:
        return self._schedule.preview_schedule_runs(rrule, n)

    # -- activity dashboard ----------------------------------------------------

    def running_conversation_ids(self) -> set[str]:
        """The conversation ids with a LIVE (not-yet-done) run task in THIS process —
        the ground truth of "what's executing right now" (cached status can lag a
        crash). Done tasks are filtered, so a finished-but-uncleaned entry never
        counts. Not owner-scoped; the endpoint intersects with the owner's summaries."""
        return {cid for cid, task in self._tasks.items() if not task.done()}

    def list_recent_schedule_runs(self, *, owner_id: str, limit: int = 50) -> list[dict]:
        return self._schedule.list_recent_schedule_runs(owner_id=owner_id, limit=limit)
