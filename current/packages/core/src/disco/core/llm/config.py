"""Router configuration — llm-router-contract.md §7 (v1.2 deterministic).

All `[VERIFY]` specifics (model ids, context sizes, prices, family tags) live
HERE — one config surface (BoD §19), editable without touching any caller. This
is the single file you revise when models or prices change (R10).

v1.2 LOBOTOMY: model selection is now ABSOLUTE and config-driven. A role resolves
to exactly one model by direct lookup — `default_model` for the driver, an
explicit `assignments[role]` for every other role, optionally overridden per
conversation by the model pill. There is no overflow policy, no difficulty
assessment, no capability-based escalation: the mapping IS the source of truth
and the router obeys it without deviation (see `model_for`). The intelligent-
routing scaffolding (`RoleRouting`, threshold fields) is retained DORMANT for the
documented revival path.

The starting assignments are encoded as a `default_config()` factory with
**placeholder** model ids — deliberately NOT the operator's real homelab model
ids or OpenRouter strings, which are [VERIFY] and filled in at wiring time.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..env import disco_env
from .types import ModelRole, Requirement
from .vision_table import table_vision

_LOG = logging.getLogger("disco.config")

# Reserved provider-map key for the prebuilt auxiliary-role fallback provider.
ROLE_FALLBACK_PROVIDER_KEY = "__role_fallback__"

# Flag to emit the DRIVER_VISION env deprecation warning at most once per process.
_DRIVER_VISION_DEPRECATION_LOGGED: bool = False


class ProviderSettings(BaseModel):
    """First-class model provider saved by the app-server settings surface.

    This is inert runtime metadata until one of its models is enabled into the
    normal catalogue. The secret value itself lives in SecretStore under
    ``secret_name`` and is never embedded in RouterConfig.
    """

    id: str
    label: str
    base_url: str
    kind: Literal["openai-compat", "anthropic", "gemini"]
    secret_name: str
    requires_api_key: bool = True
    # Outcome of the last /models probe made with this provider's key.
    # None = never probed (the key is stored but unproven), True = the provider
    # answered, False = the provider refused and ``key_error`` holds its answer.
    key_verified: bool | None = None
    key_error: str | None = None


class ModelEntry(BaseModel):
    model_id: str  # provider's id string [VERIFY]
    provider: str  # the endpoint key (one OpenAIProvider per distinct backend) [VERIFY]
    context_window: int  # [VERIFY]
    # Provider/model output capability. ``None`` means the catalogue did not
    # report one, so callers must let the provider choose instead of inventing
    # a fixed ceiling. This is distinct from the total context window.
    max_output_tokens: int | None = Field(default=None, ge=1)
    capabilities: frozenset[Requirement] = frozenset()
    quantization: str | None = None  # e.g. "Q4_K_M"; informational provenance
    # per-million-token prices for cost accounting; 0 for local. [VERIFY]
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    # W-05: how the user pays for this model — the single source of truth for the
    # cost surfaces (pill / matrix / catalogue / status meter).
    #   "metered"      — pay per token; the price_*_per_m fields drive the $ display.
    #   "subscription" — a flat-rate plan (e.g. a MiniMax/Claude subscription proxied
    #                    locally): NO per-token price, shown as "Subscription", not "Free".
    #   "free"         — genuinely free (local / no charge).
    #   "unknown"      — pay model NOT verified (the provider's catalogue reports no
    #                    pricing). Shown as "pricing unknown", never as Free: a 0
    #                    price with unknown mode must not read as verified-no-charge.
    # None → DERIVE for back-compat: price 0 → free, else metered (so existing
    # catalogues keep working without a migration).
    pricing_mode: Literal["metered", "subscription", "free", "unknown"] | None = None
    # [EXTENSION] §8 requires a model family for prompt selection but §7's
    # ModelEntry omitted the field. Optional here: if None, the family is
    # derived from model_id (prompts.derive_family). Set it to pin a [VERIFY]
    # family tag explicitly.
    family: str | None = None
    # [VERIFY] live wiring: the OpenAI-compatible base URL for this model's backend
    # (None = no live adapter, e.g. the NLI cross-encoder or a dormant overflow
    # slot). `api_key_env` names an env var holding the key, if the server needs one.
    base_url: str | None = None
    api_key_env: str | None = None
    # Provider ownership remains represented by api_key_env even for keyless
    # local endpoints. This flag decides whether a missing secret blocks wiring.
    requires_api_key: bool = True
    # V1 (§2): manual vision override pin.  None = derive via the runtime probe
    # order (probe → env back-compat → static table → False).  True/False =
    # force the capability, bypassing all probes.  Persisted in disco-config.json.
    vision: bool | None = None
    # Explicit model-execution TIER — the single source of truth for whether this
    # model gets the weak-model assist compensations. None = derive (back-compat:
    # the runtime's hosting heuristic, local→weak). "weak" = enable assist;
    # "standard" = capable model, NO compensations even when locally hosted — so a
    # capable local model (e.g. Qwen 27B) is not sandbagged purely because it runs
    # on localhost. The per-conversation assist toggle still overrides this.
    tier: Literal["standard", "weak"] | None = None


class RoleRouting(BaseModel):
    """[DORMANT — v1.2] The intelligent-routing per-role bundle. No longer read
    by the deterministic router (a role now maps to ONE model via
    `RouterConfig.assignments`/`default_model`). Retained as the revival
    scaffolding referenced by the dormant policy and the v1.2 contract appendix."""

    primary: str  # ModelEntry key for the local default
    overflow: str | None = None  # ModelEntry key for the OpenRouter path
    overflow_eligible: bool = False  # may this role overflow at all? (§5.1)
    overflow_ladder: list[str] = Field(default_factory=list)  # stronger models (§5.3)


class SandboxConnection(BaseModel):
    """[settings] ONE backend's saved connection block. Stored per-backend in
    ``SandboxSettings.connections`` so flipping the active ``backend`` NEVER clears
    another backend's setup.

    The outage this prevents (2026-06-23): toggling gVisor→local→gVisor blanked the
    gVisor ``docker_socket`` to a host-less ``ssh://sandbox@`` (the flat fields were
    SHARED across backends), and every build/agent run then died with
    "gvisor sandbox host ssh://sandbox@ unreachable: ssh: Could not resolve hostname :".
    Each backend now keeps its own block; the ACTIVE block is mirrored to the flat
    ``SandboxSettings`` fields the live backend builder reads."""

    # In-container Docker-compatible endpoint. Compose supplies it from rootless
    # Podman by default; the host source is controlled separately.
    docker_socket: str = "unix:///var/run/docker.sock"
    podman_url: str = "unix:///run/user/1000/podman/podman.sock"
    runtime: str = "runc"
    image: str = "disco-sandbox:base"
    workspace_root: str = "/var/lib/disco/workspaces"


def _ssh_endpoint_hostless(endpoint: str) -> bool:
    """True when an ``ssh://``/``http+ssh://`` endpoint has NO host — e.g.
    ``ssh://sandbox@``, ``ssh://sandbox@:22``, ``http+ssh://sandbox@:22/path`` or
    ``ssh://sandbox@/path`` (all of which produced "ssh: Could not resolve hostname"
    in the outage). The HOST is the authority between ``@`` and the next ``:`` (port)
    or ``/`` (path); if it's empty the endpoint is host-less, EVEN with a port. A local
    ``unix://`` / ``tcp://`` socket is never host-less by this rule (no ``user@host``)."""
    scheme, sep, rest = endpoint.partition("://")
    if not sep or scheme not in ("ssh", "http+ssh"):
        return False
    authority = rest.split("/", 1)[0]  # user@host[:port]
    hostport = authority.rsplit("@", 1)[-1].strip()
    if hostport.startswith("["):  # IPv6 literal (e.g. [::1]:22) — bracketed = has a host
        return hostport[1:].split("]", 1)[0].strip() == ""
    host = hostport.split(":", 1)[0]  # drop any :port so ":22" alone reads as host-less
    return host.strip() == ""


def sandbox_connection_error(backend: str, conn: SandboxConnection) -> str | None:
    """Validate that ``backend``'s connection is structurally RUNNABLE; return a
    human reason if not, else None. This is the guard against persisting the
    unrunnable state that caused the outage (a ``gvisor`` backend saved with an
    empty / host-less ``docker_socket``). ``process`` needs nothing local-only."""
    if backend in ("gvisor", "local"):
        sock = conn.docker_socket.strip()
        if not sock:
            return "the Docker endpoint (docker_socket) is empty"
        if _ssh_endpoint_hostless(sock):
            return (
                f"the Docker endpoint '{conn.docker_socket}' has no host "
                "(expected e.g. ssh://sandbox@<tailscale-ip-or-host>)"
            )
    elif backend == "podman":
        url = conn.podman_url.strip()
        if not url:
            return "the Podman URL (podman_url) is empty"
        if _ssh_endpoint_hostless(url):
            return f"the Podman URL '{conn.podman_url}' has no host"
    return None


def default_connection_for(backend: str) -> SandboxConnection:
    """A CLEAN, backend-APPROPRIATE connection block for a backend with no saved setup.

    The persistence fix needs this so switching TO a backend restores a sensible default
    for THAT backend, never the previous backend's socket (the P1 bleed: an old flat
    config's bad gvisor ``ssh://sandbox@`` must not carry into Local). gVisor seeds the
    host-less ``ssh://sandbox@`` prefill (the user fills the Tailscale host — flagged
    invalid on save until then, but gvisor-SHAPED, not an inherited local socket); local
    seeds the local Docker-compatible socket; podman seeds the local rootless
    socket (crun)."""
    if backend == "gvisor":
        return SandboxConnection(docker_socket="ssh://sandbox@", runtime="runsc")
    if backend == "podman":
        return SandboxConnection(runtime="crun")  # keeps the local rootless podman_url
    # local / process: the local Docker-compatible endpoint + runc (process
    # ignores it at runtime). Compose supplies this endpoint from rootless Podman.
    return SandboxConnection(docker_socket="unix:///var/run/docker.sock", runtime="runc")


class SandboxSettings(BaseModel):
    """[settings] The active sandbox backend + its (non-secret) connection details.

    Plain config held in `core` (so it persists in the SAME shared ConfigStore as the
    model catalogue — no parallel config path); the agent-server maps it to the concrete
    `SandboxBackend`. Remote connections are KEYLESS over Tailscale SSH — there are no
    secrets here, only host/socket/runtime detail.

    Per-backend persistence: the flat fields below are the ACTIVE backend's live
    connection (what the backend builder reads). ``connections`` additionally retains
    EACH backend's last-saved block so switching the active backend restores its own
    setup instead of inheriting/blanking another's — see :meth:`with_preserved_connections`.
    """

    # seconds a non-RUNNING sandbox may sit idle before suspend
    idle_ttl_s: int = 1800
    # which backend is active. "process" (dev, host) | "gvisor" (strong, remote) |
    # "local" (container, same host) | "podman" (native local/remote API).
    # W3 C-1: a fail-CLOSED Literal allowlist — an unknown value can no longer flow
    # downstream and silently resolve to host execution (`service_from_config`).
    backend: Literal["gvisor", "local", "podman", "process"] = "local"
    # In-container Docker-compatible endpoint. Compose supplies it from rootless
    # Podman by default; gVisor can explicitly select Docker-over-SSH.
    docker_socket: str = "unix:///var/run/docker.sock"
    # Podman native API. The OSS default is the conventional local rootless socket;
    # remote operators explicitly replace it with their own http+ssh:// endpoint.
    podman_url: str = "unix:///run/user/1000/podman/podman.sock"
    # the OCI runtime: runsc (gVisor), runc/crun (local/podman).
    runtime: str = "runc"
    image: str = "disco-sandbox:base"
    # host dir bind-mounted to the container workspace (gVisor); local uses a named volume.
    workspace_root: str = "/var/lib/disco/workspaces"
    # per-backend SAVED connection blocks (backend id → its connection). Retained across
    # switches so a flip of `backend` restores that backend's last-known setup. Empty on a
    # fresh/legacy config; seeded from the active flat fields on the first save/merge.
    connections: dict[str, SandboxConnection] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_unknown_backend(cls, data: object) -> object:
        """W3 C-1: an unknown persisted `backend` (manual edit / corruption / a
        future-removed value) must NOT crash `ConfigStore.load()` — which catches
        the ValidationError and discards the WHOLE user config back to the seed
        (see RouterConfig._migrate_legacy_provider). Coerce it to `gvisor`, the
        MOST-isolated backend, so the fallback is fail-CLOSED (never `process`/host)
        while the rest of the config is preserved.

        Also force `runtime="runsc"` (the gVisor OCI runtime): coercing only the
        backend name while leaving a `runc` runtime would yield a plain-runc
        container *labelled* gVisor — isolation weaker than advertised. The gVisor
        host contract owns docker_socket, but runsc is the load-bearing bit."""
        if isinstance(data, dict):
            b = data.get("backend")
            if isinstance(b, str) and b not in ("gvisor", "local", "podman", "process"):
                return {**data, "backend": "gvisor", "runtime": "runsc"}
        return data

    def active_connection(self) -> SandboxConnection:
        """The flat fields as a connection block (the live/active backend's setup)."""
        return SandboxConnection(
            docker_socket=self.docker_socket,
            podman_url=self.podman_url,
            runtime=self.runtime,
            image=self.image,
            workspace_root=self.workspace_root,
        )

    def with_preserved_connections(self, previous: SandboxSettings) -> SandboxSettings:
        """Return a copy whose per-backend ``connections`` map RETAINS every backend's
        last-saved block. The active backend's block is taken from THIS settings' flat
        fields; all OTHER backends' blocks are carried over verbatim from ``previous``
        (and from any blocks this payload already provided). This is the persistence
        fix: switching the active backend never drops the inactive backends' setup."""
        merged: dict[str, SandboxConnection] = {}
        merged.update(previous.connections)  # history across past switches
        # seed the previous ACTIVE block in case the old config predates the map
        merged.setdefault(previous.backend, previous.active_connection())
        merged.update(self.connections)  # idempotent round-trips carry their own map
        merged[self.backend] = self.active_connection()  # authoritative from THIS save
        return self.model_copy(update={"connections": merged})


class ProjectStorageSettings(BaseModel):
    """[settings] Where Build projects persist on the APP HOST — the user-chosen
    directory under which each project's manifest + workspace tree lives.

    Plain config held in `core` (same shared ConfigStore as the model catalogue);
    the agent-server reads it to snapshot/rehydrate Build workspaces. Empty by
    default — "not configured" — so an unset path is detectable rather than
    accidentally falling back to a hidden default. Validation is done at the
    SET path (the settings PUT endpoint) not at construction; load-time
    construction must not throw."""

    # absolute directory on the app host where projects persist. Empty == unset.
    projects_root: str = ""


class EncodersSettings(BaseModel):
    """[settings] Where the non-generative encoders (embeddings / rerank / NLI)
    run. `remote=False` (default) = BUNDLED in-process (ONNX/CPU via fastembed —
    self-contained, no encoder server). `remote=True` = the external LAN endpoints
    (TEI / OpenAI-embeddings / NLI-sidecar). The three endpoint URLs are PERSISTED
    here (UI-editable when Remote is selected); an empty string falls back to the
    PMX_*_URL env default, so an unconfigured remote still resolves.
    These are NOT LLM-router roles — they don't follow the model assignments."""

    remote: bool = False
    reranker_url: str = ""  # empty → PMX_RERANKER_URL env default
    embedder_url: str = ""  # empty → PMX_EMBEDDER_URL env default
    nli_url: str = ""  # empty → PMX_NLI_URL env default


class TtsSettings(BaseModel):
    """[settings] Audio-overview TTS (RP-09) — the universal THREE-tier provider
    pattern (same shape as Search/Extraction):
      (a) BUNDLED `bundled` — in-process Kokoro (ONNX/CPU, keyless, weights download
          on first use; lazy-loaded so enabled-but-unused costs no RAM). The
          FIRST-RUN DEFAULT — overviews work the moment a fresh install runs.
      (b) self-host `speaches` — an OpenAI-compatible `/v1/audio/speech` endpoint you
          run (Speaches / Kokoro-FastAPI / openedai-speech) via `base_url`, keyless.
      (c) paid `openai` — an OpenAI-compatible vendor (`base_url` + `api_key_env`
          naming the secret/env var, never the key itself), e.g. OpenAI `tts-1`.
    `speaches` and `openai` share ONE HTTP client (`/v1/audio/speech`); they differ
    only by the base_url default and whether an Authorization key is sent.
    `enabled=False` turns the feature off AND lets the agent-server unload the model.
    Voices are the ratified af_heart/af_bella (Kokoro ids; override per provider)."""

    enabled: bool = True
    provider: Literal["bundled", "speaches", "openai"] = "bundled"
    base_url: str = ""  # speaches/openai endpoint; empty → provider default (SPEACHES_URL / OpenAI)
    api_key_env: str = ""  # secret/env-var NAME for the paid (openai) key — never the key
    model: str = ""  # remote model id (e.g. "tts-1"); empty → provider default
    voice_a: str = "af_heart"
    voice_b: str = "af_bella"


class SearchSettings(BaseModel):
    """[settings] Web DISCOVERY provider. The THREE tiers of the universal design:
    (a) self-host `searxng` (base_url), (b) paid APIs `tavily`/`brave` (BYO key),
    and (c) BUNDLED keyless adapters — the FIRST-RUN DEFAULT so a fresh install
    searches the moment it's downloaded.

    The bundled tier is `bundled`: a COMPOSITE of Parallel + Exa (keyless hosted
    MCP search, key optional) with Wikipedia, arXiv and Semantic Scholar. Its
    legs are also selectable individually (`exa`, `parallel`, `wikipedia`, …).
    It is keyless and therefore small — a few reports a day — and every leg
    REPORTS its rate limit instead of returning an empty result set. That is the
    whole reason it replaced `ddgs` on 2026-09-02: `ddgs` scraped the same HTML
    endpoints SearXNG does, inherited the same IP bans, and answered a rate limit
    with `[]`, which no caller could distinguish from "the web has nothing".
    """

    provider: Literal[
        "bundled",
        "searxng",
        "tavily",
        "brave",
        "exa",
        "parallel",
        "wikipedia",
        "arxiv",
        "news",
        "semantic_scholar",
        "site_scoped",
    ] = "bundled"
    base_url: str = ""  # searxng URL, arxiv/news override, or site_scoped domains
    api_key_env: str = ""  # secrets key name for tavily/brave/semantic_scholar/exa/parallel
    # searxng ONLY: the engine categories a research query is issued against.
    # Empty → the provider default ("general,science"), which adds the academic
    # engines (arXiv, Crossref, OpenAlex, PubMed) to every research query instead
    # of leaving journal coverage to whatever the general engines happen to index.
    categories: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_removed_provider(cls, data: object) -> object:
        """Load a config that still names the REMOVED `ddgs` tier, loudly.

        The `provider` Literal no longer admits `ddgs`, so a raw validate would
        raise — and `ConfigStore.load()` catches that and falls back to the SEED,
        silently discarding the user's whole catalogue and assignments. So this
        rewrites the value and says exactly what happened, what is running now,
        and how to choose something else. Neither a crash nor a silent swap.
        """
        if not isinstance(data, dict) or data.get("provider") != "ddgs":
            return data
        _LOG.warning(
            "Search provider 'ddgs' has been removed and your config now uses "
            "'bundled' (keyless: Parallel + Exa + Wikipedia + arXiv + Semantic "
            "Scholar). Reason: ddgs scraped the same endpoints SearXNG does, so it "
            "inherited their IP bans, and it reported a rate limit as zero results — "
            "indistinguishable from a genuinely empty search. The bundled tier "
            "reports its limits instead. Keyless capacity is light (a few reports a "
            "day); for volume set Settings -> Data sources -> Search to self-hosted "
            "SearXNG, or pick another tier there."
        )
        return {**data, "provider": "bundled", "base_url": ""}


class RoleFallbackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = ""  # e.g. http://localhost:8080/v1
    model: str = ""  # model id served there
    api_key_env: str = ""  # optional; env var name holding the key


class ExtractionSettings(BaseModel):
    """[settings] URL → clean content provider. Same three tiers: (a) self-host
    `crawl4ai` (base_url), (b) paid `firecrawl` (BYO key), (c) the BUNDLED `local`
    — in-process httpx fetch + stdlib readability→markdown, no service — the
    FIRST-RUN DEFAULT so extraction works offline-of-services out of the box."""

    provider: Literal["local", "crawl4ai", "firecrawl"] = "local"
    base_url: str = ""  # for crawl4ai (self-host) / firecrawl base; empty → default
    api_key_env: str = ""  # secrets key name for firecrawl (never the key itself)


class ImageGenSettings(BaseModel):
    """[settings] Image generation provider. Real, configured backends only —
    there is NO bundled keyless tier (W-50: the old `procedural` Pillow backend was
    a false affordance — it always "succeeded" with abstract patterns, so the tool
    looked wired when nothing real was connected). Until one of the tiers below is
    fully configured, image generation is NOT CONFIGURED and the tool fails loudly:
      (a) self-host `comfyui` — a self-hosted ComfyUI graph API via `base_url`,
          keyless (assumes local/network-accessible). Uses the /history poll pattern.
      (b) paid `openai` — an OpenAI-compatible `/v1/images/generations` endpoint
          (`base_url` + `api_key_env` naming the secret/env var, never the key
          itself), e.g. DALL-E 3.
      (c) paid `openrouter` — image models via OpenRouter chat-completions using the
          shared OpenRouter key (no api_key_env).
    Default is `openrouter` (the lowest-friction real tier — reuses the OpenRouter
    key) but it is INACTIVE until that key is stored. The provider is persisted; the
    agent-server honors it on the next image-gen call. NOT an LLM-router role."""

    provider: Literal["comfyui", "openai", "openrouter"] = "openrouter"
    base_url: str = ""  # for comfyui (self-host) / openai-compatible endpoint; empty → default
    api_key_env: str = ""  # secrets key name for openai (never the key itself)
    # openrouter: image gen via /chat/completions (modalities:[image,text]); paid, uses
    # the OpenRouter key (reserved "openrouter" slot). base_url → openrouter.ai default;
    # model → an image model id (e.g. "google/gemini-2.5-flash-image"). No api_key_env.
    # openai: the image model id (e.g. "gpt-image-1", "dall-e-3"); empty → provider default.
    # comfyui: the checkpoint filename to load (required; must exist on the install).
    model: str = ""
    # comfyui ONLY: an optional ComfyUI "Save (API Format)" graph that overrides the
    # built-in SDXL default. Empty → the built-in default graph (CLIP+VAE from the
    # checkpoint). Tokens substituted per call: %prompt% %negative% %seed% %width%
    # %height% %ckpt%. Lets FLUX / SD3 / custom shapes work without code changes.
    workflow_json: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_provider(cls, data: object) -> object:
        """W-50 migration: an older persisted config may still name the REMOVED
        `procedural` Pillow tier. The `provider` Literal no longer admits it, so a
        raw ``RouterConfig.model_validate`` would raise — and ``ConfigStore.load()``
        catches that and silently falls back to the SEED, discarding the user's whole
        catalogue + assignments. Rewrite the legacy value to the safe default
        (``openrouter`` — the lowest-friction real tier, INACTIVE until its key is
        stored) so an existing `procedural` config upgrades silently on load instead
        of crashing the whole config. base_url/api_key_env are left as-is; the
        OpenRouter tier ignores them (it pins its own origin + reserved key)."""
        if isinstance(data, dict) and data.get("provider") == "procedural":
            return {**data, "provider": "openrouter"}
        return data


class McpSettings(BaseModel):
    """[settings] MCP (Model Context Protocol) client settings — RP-05.
    The explicit McpServerConfig typed dict lives in tools/mcp; this is the
    top-level toggle + server map the Settings surface reads/writes. Off by
    default so a fresh install is unchanged."""

    enabled: bool = False  # off by default — opt-in
    servers: dict[str, dict] = Field(default_factory=dict)  # name -> {transport, command, url, ...}
    max_active_schemas: int = 20  # cap; beyond this, tool_search is exposed


class LiveBrowserSettings(BaseModel):
    """[settings] Live browser view (noVNC via Xvfb + x11vnc + websockify).
    Off by default — opt-in. When enabled, a 'Live' toggle appears on the
    Agent canvas browser pane. The VNC stack starts lazily (only when the user
    opens the live view); idle sandboxes cost ~0. View-only by default.
    gVisor requires the egress allowlist to admit NOVNC_PORT (deferred to D7
    egress work). Use local or podman backend. VNC is loopback-bound inside
    the sandbox (127.0.0.1 only, never network).

    P5 live jail acceptance (loopback-bind, per-conv jail, view-only, idle
    teardown) is HARDWARE-DEFERRED — VM 201 (the gVisor sandbox host) was
    destroyed. Verify on a real sandbox backend before shipping to production."""

    enabled: bool = False


class RouterConfig(BaseModel):
    models: dict[str, ModelEntry]  # key -> entry (the assignable catalogue)
    # Model-provider connections managed by app-server Settings. They do not
    # affect routing directly; enabled provider models are ordinary entries in
    # ``models`` above.
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    # the active sandbox backend + connection (settings-driven; agent-server maps it).
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    # the user-chosen Build-project persistence root (empty = unset).
    projects: ProjectStorageSettings = Field(default_factory=ProjectStorageSettings)
    # where the bundled-vs-remote encoders run (settings-driven; agent-server honors it).
    encoders: EncodersSettings = Field(default_factory=EncodersSettings)
    # audio-overview TTS: bundled in-process Kokoro vs remote Speaches, + the toggle.
    tts: TtsSettings = Field(default_factory=TtsSettings)
    # universal data providers — bundled (keyless composite / local) by default so
    # a fresh install works with no keys; upgradeable to self-host or paid in Settings.
    search: SearchSettings = Field(default_factory=SearchSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    # image generation: real backends only (W-50). Default `openrouter` but INACTIVE
    # until the OpenRouter key is stored; configure ComfyUI / OpenAI / OpenRouter in
    # Settings. No bundled placeholder — unconfigured = the tool fails NOT CONFIGURED.
    image_gen: ImageGenSettings = Field(default_factory=ImageGenSettings)
    # MCP (Model Context Protocol) — external tool servers (RP-05).
    # Off by default; the pool is built at agent-server start when enabled.
    mcp: McpSettings = Field(default_factory=McpSettings)
    # Legacy inert field: origin approval moved to the signed out-of-band
    # OriginApprovalStore. Load paths ignore this field for egress policy.
    trusted_origins: tuple[str, ...] = ()
    # Fail-closed migration diagnostics for quarantined env-name secret refs or
    # origins awaiting operator approval. This is diagnostic only; policy reads
    # the concrete config fields above.
    security_diagnostics: tuple[str, ...] = ()
    # Live browser (noVNC): off by default. When enabled, a 'Live' toggle
    # appears on the Agent canvas browser pane (P4). The VNC stack spins up
    # lazily on first open; idle cost is ~0. gVisor needs D7 egress work.
    live_browser: LiveBrowserSettings = Field(default_factory=LiveBrowserSettings)
    # Optional dedicated visual-inspection model. The browser uses it for one
    # bounded screenshot question and returns text to the main agent; the model
    # never takes over the agent loop or receives its transcript/tools. None uses
    # the main model when it has VISION, otherwise the honest DOM/text fallback.
    vision_escalation_model: str | None = None
    # Auxiliary-role resilience: on transient primary failure, eligible non-driver
    # roles may retry against this prebuilt local OpenAI-compatible provider.
    # Driver-class roles never use it.
    role_fallback: RoleFallbackSettings = Field(default_factory=RoleFallbackSettings)
    # Vestigial compatibility field. Old configs may carry removed build-kernel
    # choices; the before-validator coerces those to `disco` before Literal
    # validation so a stale config never falls back to the seed and loses settings.
    build_kernel: Literal["disco"] = "disco"
    # v1.2 deterministic assignment — the source of truth (R10):
    default_model: str  # AGENT_DRIVER's model + fallback for any unassigned role
    assignments: dict[ModelRole, str] = Field(default_factory=dict)  # explicit per-role
    # cost governance (§5.2) — PASSIVE spend backstop in v1.2: it observes spend
    # and refuses to exceed a HARD cap, but it no longer influences which model is
    # chosen (selection is `assignments`-driven, full stop).
    soft_budget_usd_per_conversation: float | None = None
    hard_budget_usd_per_conversation: float | None = None
    soft_budget_usd_global_daily: float | None = None
    hard_budget_usd_global_daily: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_build_kernel(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("build_kernel") != "disco":
            return {**data, "build_kernel": "disco"}
        return data

    # === INTELLIGENT ROUTING (DORMANT) — see policy.py / routing.py ============
    # The intelligent router consumed per-role `RoleRouting` (primary + overflow +
    # eligibility + ladder) PLUS the thresholds below to decide local-vs-frontier
    # escalation. v1.2 lobotomized selection to deterministic assignment, so these
    # inputs are no longer read. Kept commented for the documented revival path
    # (llm-router-contract.md v1.2 appendix). To revive: restore these fields,
    # re-enable the policy in policy.py, and switch routing.py's `_resolve` back to
    # the intelligent body preserved there.
    #     roles: dict[ModelRole, RoleRouting]
    #     local_retry_before_overflow: int = 1
    #     errors_before_overflow: int = 2
    #     confidence_floor: float = 0.5
    # === END DORMANT ==========================================================

    def model_for(self, role: ModelRole, *, override: str | None = None) -> str:
        """Deterministic role -> model-key resolution (v1.2). Precedence:
        per-conversation `override` (the main-screen model pill) > explicit
        settings `assignments[role]` > `default_model`. No policy, no capability
        escalation — the mapping is obeyed without deviation. Returns a KEY into
        `models`; `entry_for` resolves it (and fails loud if it is unknown)."""
        if override is not None:
            return override
        return self.assignments.get(role, self.default_model)

    def entry_for(self, key: str) -> ModelEntry:
        return self.models[key]


# [VERIFY] live-wiring endpoints for the two on-LAN llama.cpp backends the
# default catalogue points at (see api-endpoints.md / the live-wiring build).
_QWEN_BASE_URL = "http://192.168.1.231:18080/v1"
_GEMMA_BASE_URL = "http://192.168.1.81:8087/v1"


def _driver_capabilities() -> frozenset[Requirement]:
    """[BP-00] Vision gate: the driver is vision-capable ONLY if enabled via env."""
    caps = {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.LONG_CONTEXT}
    if disco_env("DRIVER_VISION") == "1":
        caps.add(Requirement.VISION)
    return frozenset(caps)


def _driver_local_entry(driver_caps: frozenset[Requirement]) -> ModelEntry:
    """AGENT_DRIVER + RAG_ANSWERER → Qwen 27B (reasoning, 128K ctx)."""
    return ModelEntry(
        model_id="Qwen3.6-27B-UD-Q5_K_XL.gguf",
        provider="qwen",
        base_url=_QWEN_BASE_URL,
        context_window=131_072,
        capabilities=driver_caps,
        quantization="Q5_K_XL",
        family="qwen",
    )


def _rag_local_entry() -> ModelEntry:
    return ModelEntry(
        model_id="Qwen3.6-27B-UD-Q5_K_XL.gguf",
        provider="qwen",
        base_url=_QWEN_BASE_URL,
        context_window=131_072,
        capabilities=frozenset(
            {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.LONG_CONTEXT}
        ),
        quantization="Q5_K_XL",
        family="qwen",
    )


def _rewriter_local_entry() -> ModelEntry:
    """QUERY_REWRITER + SUMMARIZER → Gemma E2B (cheap, fast, not a reasoning
    model). The server needs a key in $PMX_GEMMA_API_KEY (read at wiring time
    via api_key_env — never hardcoded)."""
    return ModelEntry(
        model_id="gemma-4-e2b-mtp",
        provider="gemma",
        base_url=_GEMMA_BASE_URL,
        api_key_env="gemma",
        context_window=32_768,
        capabilities=frozenset({Requirement.JSON_MODE}),
        family="gemma",
    )


def _summarizer_local_entry() -> ModelEntry:
    return ModelEntry(
        model_id="gemma-4-e2b-mtp",
        provider="gemma",
        base_url=_GEMMA_BASE_URL,
        api_key_env="gemma",
        context_window=32_768,
        family="gemma",
    )


def _nli_local_entry() -> ModelEntry:
    """NLI verifier — the cross-encoder sidecar, NOT a chat model (no base_url;
    the grounding NLIVerifier calls it directly, §9.2)."""
    return ModelEntry(
        model_id="bge-reranker-v2-m3",
        provider="local",
        context_window=512,
        family="deberta",
    )


def _driver_overflow_entry() -> ModelEntry:
    """Dormant, assignable OpenRouter overflow (no key → fails loud if assigned).
    W4 (§10.8): claude-3.5-sonnet benchmarks well on anchored diff edits →
    ANCHORED_EDIT enabled; local/unknown models default to whole-file writes."""
    return ModelEntry(
        model_id="anthropic/claude-3.5-sonnet",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        context_window=200_000,
        capabilities=frozenset(
            {
                Requirement.TOOL_CALLING,
                Requirement.JSON_MODE,
                Requirement.LONG_CONTEXT,
                Requirement.VISION,
                Requirement.ANCHORED_EDIT,
            }
        ),
        family="anthropic",
        price_in_per_m=3.0,
        price_out_per_m=15.0,
    )


def _driver_minimax_entry() -> ModelEntry:
    """W-05: a SUBSCRIPTION-tier driver — MiniMax accessed through a local,
    flat-rate proxy (e.g. the Pi relay). pricing_mode="subscription" so the cost
    surfaces show "Subscription" (NOT "Free" — it costs a flat plan fee — and NOT
    a per-token price, which doesn't apply). Dormant/assignable; the local relay
    must be running for it to actually answer."""
    return ModelEntry(
        model_id="minimax/minimax-m2",
        provider="minimax",
        base_url="http://localhost:8080/v1",
        context_window=200_000,
        capabilities=frozenset(
            {
                Requirement.TOOL_CALLING,
                Requirement.JSON_MODE,
                Requirement.LONG_CONTEXT,
            }
        ),
        family="minimax",
        pricing_mode="subscription",
    )


def _gemini_vision_entry() -> ModelEntry:
    """DF-08: Vision escalation target — Gemini 3 Flash via OpenRouter. Proven
    4/4 in the vision bake-off. Slug google/gemini-3-flash-preview is the real
    GA-track id; bare google/gemini-3-flash does NOT exist. Documented fallback
    (one-line disco-config.json swap, no code change): google/gemini-3.5-flash
    (newer, non-preview)."""
    return ModelEntry(
        model_id="google/gemini-3-flash-preview",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        context_window=1_048_576,
        capabilities=frozenset(
            {
                Requirement.TOOL_CALLING,
                Requirement.LONG_CONTEXT,
                Requirement.VISION,
                Requirement.JSON_MODE,
            }
        ),
        price_in_per_m=0.075,
        price_out_per_m=0.30,
    )


def _default_models() -> dict[str, ModelEntry]:
    """The default catalogue keyed by assignable model id. Each `provider` is
    the endpoint KEY (one OpenAIProvider per distinct backend); `base_url` is
    its OpenAI-compatible URL."""
    driver_caps = _driver_capabilities()
    return {
        "driver-local": _driver_local_entry(driver_caps),
        "rag-local": _rag_local_entry(),
        "rewriter-local": _rewriter_local_entry(),
        "summarizer-local": _summarizer_local_entry(),
        "nli-local": _nli_local_entry(),
        "driver-overflow": _driver_overflow_entry(),
        "driver-minimax": _driver_minimax_entry(),
        "or-gemini-3-flash": _gemini_vision_entry(),
    }


def _default_assignments() -> dict[ModelRole, str]:
    # AGENT_DRIVER intentionally omitted: it resolves to `default_model`.
    return {
        ModelRole.RAG_ANSWERER: "rag-local",
        ModelRole.QUERY_REWRITER: "rewriter-local",
        ModelRole.SUMMARIZER: "summarizer-local",
        ModelRole.NLI_VERIFIER: "nli-local",
        ModelRole.VERIFIER: "rewriter-local",
    }


def default_config() -> RouterConfig:
    """The starting catalogue + assignments, wired to the real LAN endpoints
    ([VERIFY] — discovered at build: see api-endpoints.md / the live-wiring build).

    Roles: AGENT_DRIVER + RAG_ANSWERER → Qwen 27B (.231); QUERY_REWRITER +
    SUMMARIZER → Gemma E2B (.81, needs a key in $PMX_GEMMA_API_KEY);
    NLI_VERIFIER → the cross-encoder sidecar (not a chat model, handled by the
    grounding NLIVerifier, no base_url). "driver-overflow" stays as a dormant,
    assignable OpenRouter slot (no key → fails loud if assigned).
    """
    return RouterConfig(
        models=_default_models(),
        default_model="driver-local",
        assignments=_default_assignments(),
        vision_escalation_model=None,
    )


def apply_runtime_capabilities(
    config: RouterConfig,
    *,
    probe_results: dict[str, bool | None] | None = None,
) -> RouterConfig:
    """V4 (§2): overlay runtime vision capabilities onto every ModelEntry.

    Runs on every config load (ConfigStore) and after a network probe
    (wiring.py).  config.py never imports httpx — probing is done in
    wiring.py which passes the results in via ``probe_results``.

    Probe order for each live entry (``base_url is not None``):
      (1) ``entry.vision`` pin     → overrides everything; True/False forces cap.
      (2) probe_results[key]       → from wiring.py's async probe; beats the table.
      (2b) DRIVER_VISION env       → back-compat alias for ``driver-local`` only;
                                     emits a one-time deprecation log.
      (3) vision_table.table_vision → static best-effort (Anthropic / OpenAI / Gemini).
      (4) False                    → fail-safe; never claim unproven vision.

    Returns the SAME config object when nothing changed (identity test stable).
    """
    global _DRIVER_VISION_DEPRECATION_LOGGED

    new_models = dict(config.models)
    changed = False

    for key, entry in config.models.items():
        if entry.base_url is None:
            # NLI cross-encoder and other non-chat backends — leave untouched.
            continue

        # ---- resolve target vision bool -----------------------------------
        if entry.vision is not None:
            # (1) Manual pin: explicit True or False — highest priority.
            target: bool = entry.vision
        elif probe_results is not None and key in probe_results and probe_results[key] is not None:
            # (2) Runtime probe result passed in from wiring.py.
            target = bool(probe_results[key])
        elif key == "driver-local":
            # (2b) DRIVER_VISION env back-compat: authoritative only for the
            # named driver-local entry, where the env reflects whether an
            # mmproj is loaded RIGHT NOW in the running llama-server.
            dv = disco_env("DRIVER_VISION")
            if dv in ("0", "1"):
                if not _DRIVER_VISION_DEPRECATION_LOGGED:
                    _LOG.warning(
                        "DISCO_DRIVER_VISION / PMX_DRIVER_VISION is deprecated "
                        "for vision detection; use a vision= pin on ModelEntry "
                        "or the runtime probe in wiring.py instead."
                    )
                    _DRIVER_VISION_DEPRECATION_LOGGED = True
                target = dv == "1"
            else:
                # (3) Fall through to the static table.
                target = table_vision(entry.model_id, entry.family)
        else:
            # (3) Static capability table (Anthropic family rule, known OpenAI/Gemini).
            target = table_vision(entry.model_id, entry.family)

        # ---- apply if different ------------------------------------------
        has_vision = Requirement.VISION in entry.capabilities
        if target == has_vision:
            continue  # already correct; leave the entry untouched

        caps = set(entry.capabilities)
        if target:
            caps.add(Requirement.VISION)
        else:
            caps.discard(Requirement.VISION)
        new_models[key] = entry.model_copy(update={"capabilities": frozenset(caps)})
        changed = True

    if not changed:
        return config
    return config.model_copy(update={"models": new_models})
