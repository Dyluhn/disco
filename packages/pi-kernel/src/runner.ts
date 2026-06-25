/**
 * PiKernelRunner — the inner-loop host (Disco Pi Build Kernel Campaign, EPIC B /
 * PR B1).
 *
 * Embeds a Pi `AgentSession` with:
 * - A bridge-gated tool posture: WITHOUT a bridge, NO tools at all
 *   (`noTools: "all"`, empty custom set); WITH a bridge, built-ins disabled
 *   (`noTools: "builtin"`) and only the Disco custom tools, each HTTP-bridging
 *   to the loopback agent-server — so the kernel can read/write/exec NOTHING
 *   except through Disco's executor (§2.2/§5.1).
 * - In-memory auth, model registry, and session manager — nothing is written to
 *   disk as product truth.
 * - A resource loader locked to discover ZERO project-local `.pi` resources and
 *   a project-trust decision that is permanently `false` (§5.1).
 * - A throwaway temp `cwd` / `agentDir` outside any workspace.
 *
 * The model/provider is a PLACEHOLDER: the empty in-memory registry means
 * `session.model` is `undefined` until the Disco inference gateway is wired in
 * EPIC C. The kernel still starts, goes ready, heartbeats, and shuts down
 * cleanly — that lifecycle is what B1 proves. A `prompt` without a model fails
 * loudly (a non-fatal protocol `error`) rather than crashing.
 *
 * I/O is injected via {@link RunnerOptions.emit}: the runner is transport-
 * agnostic so it can be driven in-process by tests and over stdio by index.ts.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  createAgentSession,
  AuthStorage,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager,
  SettingsManager,
  VERSION as PI_VERSION,
  type AgentSession,
  type AgentSessionEvent,
  type CreateAgentSessionOptions,
} from "@earendil-works/pi-coding-agent";

import { registerDiscoProvider, GATEWAY_PROVIDER } from "./discoProvider.ts";
import { mapAgentEvent } from "./events.ts";
import {
  PROTOCOL_VERSION,
  type KernelCommand,
  type KernelInitConfig,
  type KernelOutbound,
} from "./protocol.ts";
import {
  assertOnlyAllowlistedSkills,
  buildSkillLoaderConfig,
  type SkillAllowlistOptions,
} from "./skills.ts";
import { buildDiscoTools } from "./tools.ts";

/** Default heartbeat cadence (ms). Overridable per-init for tests. */
export const DEFAULT_HEARTBEAT_MS = 1000;

/**
 * Hard cap on agent_events buffered in the producer-side emit chain while stdout
 * is stalled (round-3 P1 #2). Pi's `subscribe` listener is SYNCHRONOUS and its
 * return is ignored, so a flooding turn cannot be paused at the source; we bound
 * the chain HERE. Beyond this cap the kernel terminates the session with a fatal
 * error rather than letting the chain (and its captured frames) grow without
 * bound — an honest failure, never a silent drop. Overridable per-runner.
 */
export const DEFAULT_MAX_PENDING_AGENT_EVENTS = 1024;

/**
 * The synthetic provider name under which the Disco inference gateway is
 * registered. It is the ONLY provider the kernel ever exposes as usable.
 * Re-exported from {@link ./discoProvider.ts} (the single source of truth, C3)
 * so existing importers of this symbol from the runner keep working.
 */
export { GATEWAY_PROVIDER };

/**
 * Provider-credential env vars Pi reads to auto-select a real model/provider and
 * fill a missing API key (`@earendil-works/pi-ai` `env-api-keys.ts`). Scrubbed at
 * startup so ambient credentials can NEVER let the sandboxed kernel bypass the
 * Disco gateway and talk to a real provider over the network (P0). This mirrors
 * Pi's full provider→env map plus the Vertex/Bedrock credential discovery vars.
 */
export const PROVIDER_CREDENTIAL_ENV_VARS: readonly string[] = [
  // anthropic (OAuth token takes precedence over the API key in Pi)
  "ANTHROPIC_OAUTH_TOKEN",
  "ANTHROPIC_API_KEY",
  // github copilot
  "COPILOT_GITHUB_TOKEN",
  // direct provider API keys
  "ANT_LING_API_KEY",
  "OPENAI_API_KEY",
  "AZURE_OPENAI_API_KEY",
  "NVIDIA_API_KEY",
  "DEEPSEEK_API_KEY",
  "GEMINI_API_KEY",
  "GOOGLE_CLOUD_API_KEY",
  "GROQ_API_KEY",
  "CEREBRAS_API_KEY",
  "XAI_API_KEY",
  "OPENROUTER_API_KEY",
  "AI_GATEWAY_API_KEY",
  "ZAI_API_KEY",
  "ZAI_CODING_CN_API_KEY",
  "MISTRAL_API_KEY",
  "MINIMAX_API_KEY",
  "MINIMAX_CN_API_KEY",
  "MOONSHOT_API_KEY",
  "HF_TOKEN",
  "FIREWORKS_API_KEY",
  "TOGETHER_API_KEY",
  "OPENCODE_API_KEY",
  "KIMI_API_KEY",
  "CLOUDFLARE_API_KEY",
  "XIAOMI_API_KEY",
  "XIAOMI_TOKEN_PLAN_CN_API_KEY",
  "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
  "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
  // Google Vertex AI — Application Default Credentials discovery
  "GOOGLE_APPLICATION_CREDENTIALS",
  "GOOGLE_CLOUD_PROJECT",
  "GCLOUD_PROJECT",
  "GOOGLE_CLOUD_LOCATION",
  // Amazon Bedrock — every credential source Pi recognizes
  "AWS_PROFILE",
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "AWS_SESSION_TOKEN",
  "AWS_BEARER_TOKEN_BEDROCK",
  "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI",
  "AWS_WEB_IDENTITY_TOKEN_FILE",
];

/**
 * Catch-all patterns for provider-style credential env vars not in the explicit
 * list (future providers, custom gateways). Anything ending in `_API_KEY` /
 * `_KEY` / `_API_TOKEN` / `_OAUTH_TOKEN` is scrubbed too. The sidecar needs no
 * ambient secrets — the gateway credential is injected via init config — so
 * over-scrubbing is safe and deliberately conservative.
 */
const PROVIDER_CREDENTIAL_ENV_PATTERNS: readonly RegExp[] = [
  /_(API_)?KEY$/,
  /_API_TOKEN$/,
  /_OAUTH_TOKEN$/,
];

/**
 * Remove every known/likely provider credential from an environment object
 * (defaults to `process.env`), returning the names removed. Defense in depth for
 * the P0 invariant: even if the spawner forgets to strip them, Pi can never read
 * an ambient key to select a real provider. Idempotent.
 */
export function scrubProviderCredentialEnv(env: NodeJS.ProcessEnv = process.env): string[] {
  const deny = new Set(PROVIDER_CREDENTIAL_ENV_VARS);
  const removed: string[] = [];
  for (const name of Object.keys(env)) {
    if (env[name] === undefined) continue;
    if (deny.has(name) || PROVIDER_CREDENTIAL_ENV_PATTERNS.some((re) => re.test(name))) {
      delete env[name];
      removed.push(name);
    }
  }
  return removed;
}

export interface RunnerOptions {
  /** Sink for every outbound protocol frame. */
  emit: (event: KernelOutbound) => void;
  /**
   * Producer-side backpressure seam (round-3 P1): resolves when the outbound
   * writer has capacity for another data frame. {@link PiKernelRunner.forwardAgentEvent}
   * awaits this BEFORE emitting each agent_event, so a stalled stdout pauses
   * agent_event PRODUCTION from the in-flight prompt — not just inbound commands.
   * Defaults to "always writable" (so in-process tests need not wire it).
   */
  whenWritable?: () => Promise<void>;
  /** Invoked exactly once after the `exit` frame is emitted. */
  onExit?: (code: number) => void;
  /** Default heartbeat cadence; an `init` config value overrides it. */
  heartbeatMs?: number;
  /** Clock seam (epoch ms). Defaults to `Date.now`. */
  now?: () => number;
  /**
   * Hard cap on agent_events buffered in the producer-side emit chain while
   * stdout is stalled (round-3 P1 #2). Exceeding it terminates the session with
   * a fatal error rather than growing memory without bound. Defaults to
   * {@link DEFAULT_MAX_PENDING_AGENT_EVENTS}.
   */
  maxPendingAgentEvents?: number;
  /**
   * Disco skill-allowlist override (EPIC G). Defaults to the in-repo Disco
   * allowlist + controlled skills dir (an EMPTY allowlist → zero skills loaded,
   * the safe default). Tests / the spawner override `allowlist` / `skillsRoot`
   * to opt a reviewed Disco skill in or to exercise the load-time gate.
   */
  skillAllowlist?: SkillAllowlistOptions;
}

export class PiKernelRunner {
  private readonly emit: (event: KernelOutbound) => void;
  private readonly whenWritable: () => Promise<void>;
  private readonly onExit?: (code: number) => void;
  private readonly defaultHeartbeatMs: number;
  private readonly now: () => number;
  private readonly maxPendingAgentEvents: number;
  private readonly skillAllowlist?: SkillAllowlistOptions;

  /**
   * Single-lane ordered tail for agent_event forwarding (round-3 P1). Each
   * agent_event chains off this so frames stay in arrival order AND each awaits
   * writer capacity before it is emitted — see {@link forwardAgentEvent}.
   */
  private emitChain: Promise<void> = Promise.resolve();
  /** agent_events enqueued on {@link emitChain} but not yet emitted (P1 #2). */
  private pendingAgentEvents = 0;
  /** Set once the emit-chain cap is breached; further agent_events are dropped. */
  private overflowTripped = false;
  /** Set the instant the terminal `exit` is emitted; gates {@link emitFrame}. */
  private exitEmitted = false;

  private session?: AgentSession;
  private initOptions?: CreateAgentSessionOptions;
  private resourceLoader?: DefaultResourceLoader;
  private loadedSkillNames: string[] = [];
  private unsubscribe?: () => void;
  private heartbeat?: ReturnType<typeof setInterval>;
  private tempDirs: string[] = [];
  private initialized = false;
  private closing = false;

  constructor(options: RunnerOptions) {
    this.emit = options.emit;
    this.whenWritable = options.whenWritable ?? (() => Promise.resolve());
    this.onExit = options.onExit;
    this.defaultHeartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
    this.now = options.now ?? (() => Date.now());
    this.skillAllowlist = options.skillAllowlist;
    this.maxPendingAgentEvents = Math.max(
      1,
      Math.floor(options.maxPendingAgentEvents ?? DEFAULT_MAX_PENDING_AGENT_EVENTS),
    );
  }

  // ---- introspection seams (used by tests) ------------------------------

  /** The live Pi session, or `undefined` before `init` / after shutdown. */
  getSession(): AgentSession | undefined {
    return this.session;
  }

  /** The exact options passed to `createAgentSession` (for no-tools asserts). */
  getInitOptions(): CreateAgentSessionOptions | undefined {
    return this.initOptions;
  }

  /**
   * Names of the skills the loader actually mounted (EPIC G). Only allowlisted,
   * Disco-owned skills can ever appear here; an empty allowlist yields `[]`.
   */
  getLoadedSkillNames(): string[] {
    return [...this.loadedSkillNames];
  }

  /** agent_events buffered but not yet emitted (round-3 P1 #2 bound check). */
  getPendingAgentEventCount(): number {
    return this.pendingAgentEvents;
  }

  // ---- command dispatch -------------------------------------------------

  async handleCommand(command: KernelCommand): Promise<void> {
    switch (command.type) {
      case "init":
        await this.init(command.config);
        return;
      case "prompt":
        await this.prompt(command.text);
        return;
      case "followup":
        await this.followup(command.text);
        return;
      case "approve":
      case "reject":
        // Reserved for plan / confirmation gates wired in a later epic. The
        // B1 kernel has no gate, so acknowledge as a non-fatal no-op.
        this.error(`'${command.type}' is not supported until plan gates are wired (later epic)`);
        return;
      case "cancel":
        await this.cancel();
        return;
    }
  }

  // ---- lifecycle --------------------------------------------------------

  private async init(config?: KernelInitConfig): Promise<void> {
    if (this.initialized) {
      this.error("kernel already initialized; ignoring duplicate 'init'");
      return;
    }
    this.initialized = true;

    try {
      // P0 (defense in depth): scrub ambient provider credentials BEFORE the Pi
      // session is built, so Pi cannot read an env key to auto-select a real
      // model/provider and bypass the Disco inference gateway. The spawner should
      // also not pass these, but we never rely on that.
      scrubProviderCredentialEnv();

      // Throwaway dirs outside any workspace: nothing here is product truth.
      const cwd = this.mkTemp("disco-pi-kernel-cwd-");
      const agentDir = this.mkTemp("disco-pi-kernel-agent-");

      // In-memory only — no auth.json / models.json / session files on disk.
      const authStorage = AuthStorage.inMemory();
      const modelRegistry = ModelRegistry.inMemory(authStorage);
      const sessionManager = SessionManager.inMemory(cwd);

      // Settings shared with the loader so the (false) project-trust decision is
      // consistent across both.
      const settingsManager = SettingsManager.create(cwd, agentDir);

      // EPIC G (§1.1–§1.3): restrict skill loading to the Disco-owned allowlist.
      // `buildSkillLoaderConfig` resolves the vetted allowlist to absolute,
      // in-repo skill dirs — canonicalized so a symlinked skill dir/SKILL.md that
      // escapes the controlled root is REFUSED, and governance-validated (§5.2) —
      // and returns the `skillsOverride` load-time gate that THROWS on anything
      // the loader resolved that is not contained in a reviewed allowlisted dir.
      // An EMPTY allowlist (the default) → no paths and a gate that rejects
      // everything else.
      const skillCfg = buildSkillLoaderConfig(this.skillAllowlist);

      // Loader that discovers ZERO project-local resources (§5.1). `noSkills:true`
      // disables discovery of project-local (`.pi/skills`), user, and global skill
      // dirs entirely — only the allowlisted `additionalSkillPaths` are even
      // considered, and the `skillsOverride` gate vets whatever survives.
      const resourceLoader = new DefaultResourceLoader({
        cwd,
        agentDir,
        settingsManager,
        noExtensions: true,
        noSkills: true,
        noPromptTemplates: true,
        noThemes: true,
        noContextFiles: true,
        additionalSkillPaths: skillCfg.additionalSkillPaths,
        skillsOverride: skillCfg.skillsOverride,
      });
      this.resourceLoader = resourceLoader;
      // When we supply our own loader, createAgentSession does NOT reload it —
      // we must, and we pin project trust to false (never load project `.pi`).
      // The allowlist `skillsOverride` runs INSIDE this reload: if a non-vetted
      // skill was somehow resolved, reload() rejects and we fail loudly below.
      await resourceLoader.reload({ resolveProjectTrust: async () => false });

      // EPIC G boot assertion (backstop, defense in depth): re-check the loader's
      // FINAL skill set against the canonicalized allowlist. Fires loudly even if
      // the `skillsOverride` gate were ever bypassed or the SDK changed under us.
      const loadedSkills = resourceLoader.getSkills().skills;
      assertOnlyAllowlistedSkills(
        loadedSkills,
        skillCfg.allowedDirs,
        skillCfg.skillsRoot,
        skillCfg.bindings,
      );
      this.loadedSkillNames = loadedSkills.map((s) => s.name);

      // P0 (registry restriction): the gateway is the ONLY usable provider. When
      // supplied, register it and pin the session model to it; built-ins stay
      // registered but unusable (no credential, post-scrub). When absent (B1),
      // no model is selected — a prompt fails loudly instead of falling back to
      // any real provider.
      const selectedModel = config?.gateway
        ? registerDiscoProvider(modelRegistry, config.gateway)
        : undefined;

      // D1 tool posture. WITH a bridge: no built-in tools (`"builtin"`) but the
      // Disco custom-tool set, each HTTP-bridging to the loopback agent-server
      // with the SAME ephemeral run token the gateway uses (so tool calls and
      // inference share one token — exactly what `routes/pi_tools.py` validates
      // against the run-token store). WITHOUT a bridge: the B1 no-tools posture
      // (`"all"` + empty set) is preserved, so the no-gateway lifecycle/security
      // tests keep their `ready`-frame tool assertions.
      const noToolsMode: "all" | "builtin" = config?.bridge ? "builtin" : "all";
      const customTools = config?.bridge
        ? buildDiscoTools({
            baseUrl: config.bridge.baseUrl,
            kernelId: config.bridge.kernelId,
            token: config.gateway?.apiKey,
          })
        : [];

      const options: CreateAgentSessionOptions = {
        cwd,
        agentDir,
        authStorage,
        modelRegistry,
        sessionManager,
        settingsManager,
        resourceLoader,
        ...(selectedModel ? { model: selectedModel } : {}),
        noTools: noToolsMode,
        customTools,
      };
      this.initOptions = options;

      const { session } = await createAgentSession(options);
      this.session = session;

      // P0 (startup assertion): prove no built-in provider became usable. Post
      // env-scrub the only model with configured auth must be the gateway (or
      // none). If a built-in leaked a credential, refuse to start rather than
      // risk routing prompts off the gateway.
      const available = await modelRegistry.getAvailable();
      const leaked = available.filter((m) => m.provider !== GATEWAY_PROVIDER);
      if (leaked.length > 0) {
        const providers = [...new Set(leaked.map((m) => m.provider))].join(", ");
        this.error(
          `SECURITY: built-in provider credential is reachable in the kernel (${providers}); ` +
            `refusing to start so prompts cannot bypass the Disco gateway`,
          true,
        );
        await this.shutdown(1);
        return;
      }

      // Stream every Pi session event out as a mapped agent_event, through the
      // producer-side backpressure point so a stalled stdout pauses event
      // PRODUCTION rather than flooding the outbound queue (round-3 P1).
      this.unsubscribe = session.subscribe((event: AgentSessionEvent) => {
        void this.forwardAgentEvent(event);
      });

      this.emitFrame({
        type: "ready",
        protocolVersion: PROTOCOL_VERSION,
        piVersion: PI_VERSION,
        // Without a gateway the in-memory registry has no usable model and Pi
        // synthesizes an "unknown/unknown" placeholder, reported as `null`. With
        // a gateway, this is the gateway's model id (`disco-gateway/<id>`).
        model: describeModel(session.model),
        tools: {
          activeToolNames: session.getActiveToolNames(),
          customToolCount: options.customTools?.length ?? 0,
          noTools: noToolsMode,
        },
      });

      this.startHeartbeat(config?.heartbeatMs ?? this.defaultHeartbeatMs);
    } catch (err) {
      this.error(`failed to start Pi session: ${describeError(err)}`, true);
      await this.shutdown(1);
    }
  }

  private async prompt(text: string): Promise<void> {
    if (!this.session) {
      this.error("received 'prompt' before 'init'");
      return;
    }
    try {
      // No real model is wired until EPIC C, so prompt() validation fails loudly
      // (no API key for the placeholder model); surface it as a non-fatal error
      // rather than crashing the sidecar.
      await this.session.prompt(text);
    } catch (err) {
      this.error(`prompt failed: ${describeError(err)}`);
    }
  }

  /**
   * Abort the in-flight agent turn (if any) and discard queued messages, but
   * keep the sidecar alive so the process manager can issue another turn or
   * resume. Termination is a separate concern (stdin EOF / SIGTERM → shutdown);
   * `cancel` MUST NOT tear the process down (campaign BuildKernel protocol:
   * cancel and resume are distinct from teardown).
   *
   * PUBLIC because the stdio transport routes `cancel` on a separate CONTROL
   * plane that PREEMPTS the data-plane serial queue: a hung `prompt()` must be
   * abortable without first waiting for that prompt to settle. `session.abort()`
   * is exactly what unblocks an in-flight turn, so it must run immediately.
   */
  async cancel(): Promise<void> {
    if (!this.session) {
      this.error("received 'cancel' before 'init'");
      return;
    }
    try {
      await this.session.abort();
      // Drop anything the model queued but had not yet consumed.
      this.session.clearQueue();
    } catch (err) {
      this.error(`cancel failed: ${describeError(err)}`);
    }
  }

  /**
   * Producer-side backpressure point for agent_events (round-3 P1).
   *
   * The real producer is the in-flight `session.prompt()` / `followUp()` turn,
   * which fires events through Pi's SYNCHRONOUS `subscribe` listener — and Pi
   * ignores that listener's return value (see `AgentSession._emit`), so the
   * listener itself cannot pause the agent loop. Backpressure is therefore
   * advisory here, exactly like Node's own `stream.write() === false`. We honor
   * it at the single choke point: every agent_event is forwarded on a
   * single-lane ordered chain that AWAITS the writer's capacity
   * ({@link RunnerOptions.whenWritable}) BEFORE emitting. Consequences:
   *  - a stalled stdout parks frames HERE, before they reach (and grow) the
   *    writer's bounded queue — so the writer queue never exceeds its cap;
   *  - a COOPERATING in-flight producer that `await`s this method is fully
   *    bounded to one in-flight frame plus the writer cap (it stops producing
   *    while stdout is stalled, instead of dropping events);
   *  - ordering is preserved by the single lane; no agent_event is dropped.
   *
   * Returns a promise that settles once this frame has been emitted, so an
   * awaiting producer is backpressured. The sync `subscribe` path calls it
   * fire-and-forget (its return is ignored upstream, like any Pi listener).
   *
   * HONEST BOUND (round-3 P1 #2): because that fire-and-forget path cannot be
   * paused at the source, a turn flooding events under a permanently stalled
   * stdout would otherwise grow the chain (and its captured frames) without
   * bound. So the in-flight chain is capped at {@link maxPendingAgentEvents}.
   * Past the cap we do NOT silently drop frames and do NOT grow memory: we emit
   * a FATAL error (reserved/cap-exempt, so it still flushes) and terminate the
   * Pi session. No agent_event is ever dropped without a terminal signal.
   *
   * PUBLIC so the producer-side backpressure + bound contract can be unit-tested
   * against a stalled writer without a live model.
   */
  forwardAgentEvent(event: AgentSessionEvent): Promise<void> {
    // Once tearing down / terminated, never produce another data frame.
    if (this.closing || this.exitEmitted || this.overflowTripped) {
      return Promise.resolve();
    }
    // Hard memory bound: a sustained flood under a stalled stdout terminates the
    // session rather than buffering without limit.
    if (this.pendingAgentEvents >= this.maxPendingAgentEvents) {
      this.overflowTripped = true;
      this.error(
        `agent_event backlog exceeded ${this.maxPendingAgentEvents} frames while stdout is stalled; ` +
          `terminating the Pi session to keep memory bounded`,
        true,
      );
      void this.shutdown(1);
      return Promise.resolve();
    }

    const frame: KernelOutbound = { type: "agent_event", event: mapAgentEvent(event) };
    this.pendingAgentEvents++;
    const next = this.emitChain.then(async () => {
      await this.whenWritable();
      this.emitFrame(frame);
    });
    // A rejection on one link must not break ordering for the next frame; the
    // pending counter is released whether the frame emitted or failed.
    this.emitChain = next.then(
      () => {
        this.pendingAgentEvents--;
      },
      () => {
        this.pendingAgentEvents--;
      },
    );
    return next;
  }

  private async followup(text: string): Promise<void> {
    if (!this.session) {
      this.error("received 'followup' before 'init'");
      return;
    }
    try {
      await this.session.followUp(text);
    } catch (err) {
      this.error(`followup failed: ${describeError(err)}`);
    }
  }

  private startHeartbeat(intervalMs: number): void {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => {
      this.emitFrame({ type: "heartbeat", ts: this.now() });
    }, Math.max(1, intervalMs));
    // Don't let the heartbeat alone keep the event loop (and process) alive.
    this.heartbeat.unref?.();
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = undefined;
    }
  }

  /** Abort in-flight work, tear down the session, and emit a clean `exit`. */
  async shutdown(code: number): Promise<void> {
    if (this.closing) return;
    // Set BEFORE any await so no agent_event enqueued after this point (the
    // `closing` guard in forwardAgentEvent), and no new turn can start.
    this.closing = true;

    this.stopHeartbeat();

    if (this.unsubscribe) {
      try {
        this.unsubscribe();
      } catch {
        /* ignore */
      }
      this.unsubscribe = undefined;
    }

    if (this.session) {
      try {
        await this.session.abort();
      } catch {
        /* abort is best-effort */
      }
      try {
        this.session.dispose();
      } catch {
        /* ignore */
      }
      this.session = undefined;
    }

    // P1 #3: flush the agent_event chain so the terminal `exit` is strictly the
    // LAST frame and no earlier agent_event is reordered after it or lost. Skip
    // the drain only when terminating BECAUSE the outbound is wedged (overflow):
    // that chain can never settle, and the control-plane force-exit deadline is
    // the backstop. The `closing` guard above already stopped new frames, so the
    // chain is finite here.
    if (!this.overflowTripped) {
      try {
        await this.emitChain;
      } catch {
        /* a failed link still released its slot; nothing more to flush */
      }
    }

    for (const dir of this.tempDirs) {
      try {
        rmSync(dir, { recursive: true, force: true });
      } catch {
        /* best-effort cleanup */
      }
    }
    this.tempDirs = [];

    this.emitFrame({ type: "exit", code });
    this.onExit?.(code);
  }

  // ---- helpers ----------------------------------------------------------

  private mkTemp(prefix: string): string {
    const dir = mkdtempSync(join(tmpdir(), prefix));
    this.tempDirs.push(dir);
    return dir;
  }

  /**
   * The single outbound choke point (P1 #3). Drops EVERYTHING once the terminal
   * `exit` has been emitted, so a late frame — an agent_event still parked on the
   * chain, or an error from an in-flight handler racing teardown — can never
   * appear after `exit`. `exit` itself flips the latch as it goes out.
   */
  private emitFrame(frame: KernelOutbound): void {
    if (this.exitEmitted) return;
    if (frame.type === "exit") this.exitEmitted = true;
    this.emit(frame);
  }

  private error(message: string, fatal = false): void {
    this.emitFrame({ type: "error", message, fatal });
  }
}

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * Project the session's current model to a `provider/id` string, or `null` when
 * no real model is configured. Pi represents "no model" with an `unknown`
 * provider sentinel, which we normalize to `null`.
 */
function describeModel(model: { provider: string; id: string } | undefined): string | null {
  if (!model || model.provider === "unknown") return null;
  return `${model.provider}/${model.id}`;
}
