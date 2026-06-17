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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
    EventSource,
    LLMMessage,
    LLMSummarizingCondenser,
    MessageEvent,
    NoOpCondenser,
    PlanEvent,
    ReportEvent,
    SkillStore,
    StatusEvent,
    ToolResult,
    render_skills_for_prompt,
)
from disco.core.env import disco_env
from disco.core.inspect import inspect_enabled
from disco.core.inspect import install as install_inspect
from disco.core.inspect import routing_sink_for
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    DriverPrompts,
    ModelRole,
    OperatingMode,
    RouterSummarizer,
    SandboxSettings,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.llm.secrets import OPENROUTER_API_KEY_ENV
from disco.core.llm.wiring import build_providers
from disco.core.loop import (
    AgentLoop,
    BlastRadiusConfirm,
    BuildAgent,
    NeverConfirm,
    ResearchAgent,
    RouterAgent,
)
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
    ProcessSandboxService,
    SandboxService,
    SandboxSession,
    SandboxSpec,
    ToolDef,
    agent_scope,
    build_default_registry,
)

# MCP client pool (RP-05 rung A) — built once at start, snapshotted per conversation.
from disco.tools.mcp import McpPool
from disco.tools.projects import (
    ProjectStore,
    StorageStatus,
)
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    SandboxConfig,
    SandboxInstance,
)
from disco.tools.sandbox._container import PREVIEW_PORT
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

from .control_ops import ControlOps
from .deep_research_service import DeepResearchService
from .lifecycle import LifecycleManager
from .mcp_manager import McpManager
from .preview_service import PreviewService
from .resume_service import ResumeService
from .runtime_model_probe import _do_live_model_probe, _model_label
from .runtime_settings import RuntimeSettings
from .schedule_service import ScheduleService
from .sessions_service import SessionsService
from .share_service import ShareService


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
    # else: advertised_tools remains None → all allowed tools shown (current behavior)


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
    if settings.backend == "gvisor":
        return GvisorSandboxService(cfg)
    if settings.backend == "local":
        return LocalSandboxService(cfg)
    if settings.backend == "podman":
        return PodmanSandboxService(cfg)  # stub-in-this-env (see docstring)
    return ProcessSandboxService()  # "process"/unknown → the dev backend (runs on host)


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


def _probe_live_model(base_url: str | None, api_key: str | None = None) -> dict[str, Any]:
    """Return {"model_id": str|None, "n_ctx": int|None} for a llama.cpp /
    OpenAI-compatible server, from a cached /props probe. NEVER blocks a running
    event loop — that was the North Star #25 wedge: /props can hang the full 2s when
    the backend is slow/down, and this is reached from async routes (/models,
    /health) AND from kick()'s synchronous loop composition, all on the loop. When a
    loop is running, the blocking probe is offloaded to the default threadpool
    (fire-and-forget — it fills the cache) and THIS call returns the static fallback;
    the next call is served live from cache. Off the loop (CLI / worker thread) it
    blocks directly. Best-effort: Nones on any miss → caller uses the static config."""
    if not base_url:
        return {"model_id": None, "n_ctx": None}
    cached = _LIVE_MODEL_PROBE_CACHE.get(base_url)
    if cached is not None:
        # Canonical form: (result, monotonic_ts). Within _PROBE_TTL_S the
        # cached value is served as-is — no second /props call. Past the
        # TTL we fall through to re-probe so a model hot-swap (driver
        # restarted with a different served model / n_ctx) is observed
        # within a minute (T6/E2).
        if isinstance(cached, tuple) and len(cached) == 2:
            value, ts = cached
            if (time.monotonic() - ts) <= _PROBE_TTL_S:
                return value
        else:
            # Legacy / test-only direct-set: bare dict, no ts. Treat as
            # fresh — preserves the existing non-blocking test that
            # simulates a worker thread filling the cache directly.
            return cached
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

            def _probe_then_clear(u: str = base_url, k: str | None = api_key) -> None:
                try:
                    _do_live_model_probe(u, k)
                finally:
                    _LIVE_MODEL_PROBE_INFLIGHT.discard(u)

            running.run_in_executor(None, _probe_then_clear)
        return {"model_id": None, "n_ctx": None}
    return _do_live_model_probe(base_url, api_key)


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
    ) -> None:
        self._store = store
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
        if config_store is not None:
            self._config_store = config_store
        elif config is not None:
            self._config_store = ConfigStore(base_factory=lambda: config)
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
        # Cooperative-cancellation flags for Deep Research (whose engine isn't an
        # AgentLoop and can't be soft-cancelled the loop's way). Stop sets the flag;
        # the engine polls it at each sub-question/section boundary and halts,
        # keeping the partial report (resumable). See _execute_deep_research.
        self._cancel_flags: dict[str, asyncio.Event] = {}
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

    # The generative (text-producing) roles a model PICK drives. NLI_VERIFIER is a
    # cross-encoder (entailment scorer), NOT a chat model — pointing it at a picked
    # LLM would break verification, so it always follows its own assignment.
    _GENERATIVE_ROLES: tuple[ModelRole, ...] = (
        ModelRole.AGENT_DRIVER,
        ModelRole.RAG_ANSWERER,
        ModelRole.QUERY_REWRITER,
        ModelRole.SUMMARIZER,
    )

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
        # Overlay the decrypted OpenRouter key into the env build_providers reads,
        # so OR models (api_key_env=PMX_OPENROUTER_API_KEY) authenticate without the
        # secret ever being on disk in plaintext.
        env = dict(os.environ)
        or_key = self._secret_store.get_openrouter_key()
        if or_key:
            env[OPENROUTER_API_KEY_ENV] = or_key
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
        time any conversation composes, the cache is hot. Best-effort: never blocks
        boot."""
        try:
            cfg = self._config_store.load()
            key = cfg.model_for(ModelRole.AGENT_DRIVER)
            entry = cfg.entry_for(key)
            if entry and entry.base_url:
                api_key = (
                    os.environ.get(entry.api_key_env) if entry.api_key_env else None
                )
                await asyncio.to_thread(_do_live_model_probe, entry.base_url, api_key)
        except Exception:  # noqa: BLE001 — best effort; the static config is the fallback
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

    def _effective_assist(self, conversation_id: str) -> bool:
        return self._settings._effective_assist(conversation_id)

    def is_assist(self, conversation_id: str) -> bool:
        return self._settings.is_assist(conversation_id)

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
                m.base_url, os.environ.get(m.api_key_env) if m.api_key_env else None
            )
            label = _model_label(live["model_id"] or m.model_id)
            ctx = live["n_ctx"] or m.context_window
            models.append(
                {
                    "id": key,
                    "label": label,
                    "provider": "openrouter" if m.provider == "openrouter" else "local",
                    "free": m.price_out_per_m == 0.0,
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
                )
            # Watch-it-write: wire the loop's stream sink to the store's ephemeral
            # broadcast so streamed file-content frames reach the conversation's WS
            # subscribers live (never persisted). Bound to this cid.
            loop.stream_sink = lambda frame, _cid=conversation_id: self._store.publish_ephemeral(
                _cid, frame
            )
            self._loops[conversation_id] = loop
        return loop

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
        executor = DefaultToolExecutor(
            build_default_registry(),
            agent_scope(),
            # SandboxSession is a drop-in SandboxInstance (it implements the
            # protocol at runtime); the `id` attribute differs only in being a
            # property rather than a plain attribute, which trips the
            # type-checker's invariance check on a protocol field. Cast to
            # the protocol type so the type checker is happy without
            # touching runtime behavior.
            sandbox=cast(SandboxInstance, session),
            broker=broker,
            conversation_id=conversation_id,
            assist=self._effective_assist(conversation_id),
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
            cfg = self._config_store.load()
            max_schemas = cfg.mcp.max_active_schemas if cfg.mcp else 20
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
                {"submit_plan", "file_list", "file_read", "search", "extract"}
            ),
            # Same gated source of truth as the router prefix and the UI badge —
            # _compose_build_loop only runs for build-like surfaces today, but reading
            # the gated value means a future caller can't desync the loop's behavior
            # from the prompt prefix.
            autonomous=self._effective_autonomous(conversation_id),
            assist=self._effective_assist(conversation_id),
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
    ) -> AsyncIterator[dict[str, Any]]:
        return self._dr.research_stream(
            query,
            model_override=model_override,
            drop_weak=drop_weak,
            domains_deny=domains_deny,
            think=think,
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
        existing = self._tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return  # already running; the new message is picked up at the next step
        loop = self._loop_for(conversation_id)
        self._tasks[conversation_id] = asyncio.create_task(
            self._run_with_persistence(conversation_id, loop)
        )

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
        if surface in self._BUILD_LIKE_SURFACES and state.execution_status in _ENDED:
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

    async def sweep_idle_once(self) -> int:
        return await self._lifecycle.sweep_idle_once()

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

    async def confirm(self, conversation_id: str) -> None:
        return await self._control.confirm(conversation_id)

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        return await self._control.reject(conversation_id, reason)

    async def approve_plan(self, conversation_id: str) -> None:
        return await self._control.approve_plan(conversation_id)

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        return await self._control.request_plan(conversation_id, text)

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        """Resume from AWAITING_USER_DECISION by selecting the agent's proposed
        alternative path. The loop synthesizes an ActionEvent from the option's
        ToolCall and executes it directly, then resumes.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._loop_for(conversation_id)
        await loop.pick_alternative(option_id)

    async def cancel(self, conversation_id: str) -> None:
        return await self._control.cancel(conversation_id)

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
        return await self._control.kill(conversation_id)

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
