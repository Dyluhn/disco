/**
 * PiKernelRunner — the inner-loop host (Disco Pi Build Kernel Campaign, EPIC B /
 * PR B1).
 *
 * Embeds a Pi `AgentSession` with:
 * - NO built-in tools (`noTools: "all"`) and NO custom tools (those arrive in
 *   EPIC D), so the kernel cannot read/write/exec anything on its own (§2.2/§5.1).
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

import { mapAgentEvent } from "./events.ts";
import {
  PROTOCOL_VERSION,
  type KernelCommand,
  type KernelInitConfig,
  type KernelOutbound,
} from "./protocol.ts";

/** Default heartbeat cadence (ms). Overridable per-init for tests. */
export const DEFAULT_HEARTBEAT_MS = 1000;

export interface RunnerOptions {
  /** Sink for every outbound protocol frame. */
  emit: (event: KernelOutbound) => void;
  /** Invoked exactly once after the `exit` frame is emitted. */
  onExit?: (code: number) => void;
  /** Default heartbeat cadence; an `init` config value overrides it. */
  heartbeatMs?: number;
  /** Clock seam (epoch ms). Defaults to `Date.now`. */
  now?: () => number;
}

export class PiKernelRunner {
  private readonly emit: (event: KernelOutbound) => void;
  private readonly onExit?: (code: number) => void;
  private readonly defaultHeartbeatMs: number;
  private readonly now: () => number;

  private session?: AgentSession;
  private initOptions?: CreateAgentSessionOptions;
  private unsubscribe?: () => void;
  private heartbeat?: ReturnType<typeof setInterval>;
  private tempDirs: string[] = [];
  private initialized = false;
  private closing = false;

  constructor(options: RunnerOptions) {
    this.emit = options.emit;
    this.onExit = options.onExit;
    this.defaultHeartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
    this.now = options.now ?? (() => Date.now());
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

      // Loader that discovers ZERO project-local resources (§5.1).
      const resourceLoader = new DefaultResourceLoader({
        cwd,
        agentDir,
        settingsManager,
        noExtensions: true,
        noSkills: true,
        noPromptTemplates: true,
        noThemes: true,
        noContextFiles: true,
      });
      // When we supply our own loader, createAgentSession does NOT reload it —
      // we must, and we pin project trust to false (never load project `.pi`).
      await resourceLoader.reload({ resolveProjectTrust: async () => false });

      const options: CreateAgentSessionOptions = {
        cwd,
        agentDir,
        authStorage,
        modelRegistry,
        sessionManager,
        settingsManager,
        resourceLoader,
        // No built-in tools, and no custom tools yet (EPIC D adds the bridge).
        noTools: "all",
        customTools: [],
      };
      this.initOptions = options;

      const { session } = await createAgentSession(options);
      this.session = session;

      // Stream every Pi session event out as a mapped agent_event.
      this.unsubscribe = session.subscribe((event: AgentSessionEvent) => {
        this.emit({ type: "agent_event", event: mapAgentEvent(event) });
      });

      this.emit({
        type: "ready",
        protocolVersion: PROTOCOL_VERSION,
        piVersion: PI_VERSION,
        // No real model is configured in B1: the empty in-memory registry makes
        // Pi synthesize an "unknown/unknown" placeholder. Report that as `null` —
        // a real model id only appears once the inference gateway lands (EPIC C).
        model: describeModel(session.model),
        tools: {
          activeToolNames: session.getActiveToolNames(),
          customToolCount: options.customTools?.length ?? 0,
          noTools: "all",
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
      this.emit({ type: "heartbeat", ts: this.now() });
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

    for (const dir of this.tempDirs) {
      try {
        rmSync(dir, { recursive: true, force: true });
      } catch {
        /* best-effort cleanup */
      }
    }
    this.tempDirs = [];

    this.emit({ type: "exit", code });
    this.onExit?.(code);
  }

  // ---- helpers ----------------------------------------------------------

  private mkTemp(prefix: string): string {
    const dir = mkdtempSync(join(tmpdir(), prefix));
    this.tempDirs.push(dir);
    return dir;
  }

  private error(message: string, fatal = false): void {
    this.emit({ type: "error", message, fatal });
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
