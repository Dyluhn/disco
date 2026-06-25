/**
 * tools.ts — the Disco tool bridge (Disco Pi Build Kernel Campaign, PR D2 + D3).
 *
 * Pi custom tools run INSIDE the sidecar but own NO capability of their own: each
 * one HTTP-bridges to Disco's orchestrator, where the real `DefaultToolExecutor`
 * runs the action against the conversation's sandbox and appends the
 * Action/Observation pair to the event store. The sidecar therefore cannot read,
 * write, or exec anything except through this loopback bridge — the same security
 * posture as the inference gateway (§1.1 "tool bridge is HTTP, not stdio").
 *
 * `buildDiscoTools(cfg)` returns the {@link ToolDefinition}[] that D1 will hand to
 * `createAgentSession({ noTools: "builtin", customTools })` in a LATER PR. This
 * module does NOT wire itself into runner.ts (out of D2's blast radius).
 *
 * D3 — the minimal tool set is fixed HERE (the 14 names in {@link DISCO_TOOL_NAMES})
 * and ALSO enforced server-side by `routes/pi_tools.py` (defense in depth: a tool
 * the sidecar should never call is refused even if it is somehow invoked).
 *
 * Each tool's `execute`:
 *   - POSTs `{call_id, arguments}` to
 *     `{baseUrl}/internal/pi-kernel/{kernelId}/tools/{name}`,
 *   - forwards the ephemeral run-token as `Authorization: Bearer <token>` (the
 *     SAME token the inference gateway uses — bound to the kernel/conversation),
 *   - honors the AbortSignal (a cancelled turn aborts the in-flight fetch),
 *   - maps the JSON `ToolResult` back: `success` → an {@link AgentToolResult}
 *     (content text + structured details); `!success` → a thrown Error so Pi marks
 *     the tool result as an error (the SDK's own convention — its built-in tools
 *     throw on failure).
 */

import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";

/**
 * TypeBox's `TSchema` is the first type parameter of the SDK's `ToolDefinition`.
 * We DERIVE it here instead of `import { TSchema } from "typebox"` because typebox
 * is a transitive (nested) dependency of the Pi SDK and is NOT resolvable from
 * this package under nodenext module resolution. A TypeBox schema IS a JSON-Schema
 * object at runtime, so the parameter schemas below are plain JSON-Schema object
 * literals cast to this type — the same bytes the LLM tool-spec carries.
 */
type TSchema = ToolDefinition extends ToolDefinition<infer P> ? P : never;

/** The minimal, FIXED Disco tool set exposed to the Pi build kernel (D3 / §7.6). */
export const DISCO_TOOL_NAMES = [
  "file_read",
  "file_write",
  "file_replace_lines",
  "file_insert_lines",
  "file_list",
  "shell_exec",
  "preview_start",
  "preview_status",
  "preview_logs",
  "finish",
  "think",
  // Gate tools — their orchestrator-side handlers (plan pause / ask / clarify)
  // land in the E batch. Listed here (and allowlisted server-side) so the bridge
  // is complete and testable now; until E wires them the server returns a
  // structured "not yet wired" result rather than executing anything.
  "submit_plan",
  "ask_user",
  "clarify",
] as const;

/** Default env var the spawner sets so the sidecar can read its run-token. */
export const DEFAULT_RUN_TOKEN_ENV = "DISCO_PI_RUN_TOKEN";

/**
 * Inputs for {@link buildDiscoTools}. `baseUrl` + `kernelId` are the loopback
 * bridge target (D2 contract). The run-token is read at execute time so the
 * spawner (B2/D1) can inject it via `cfg.token` OR an env var (default
 * {@link DEFAULT_RUN_TOKEN_ENV}) — matching how the gateway run-token reaches the
 * sidecar. `_API_KEY`/`_OAUTH_TOKEN`-style env vars are scrubbed at startup, but a
 * plain `*_TOKEN` name survives that scrub, so the env channel is safe.
 */
export interface DiscoToolsConfig {
  /** Base URL of the loopback Disco agent-server (e.g. `http://127.0.0.1:<port>`). */
  baseUrl: string;
  /** Opaque kernel id; binds every bridged tool call to this kernel's run-token. */
  kernelId: string;
  /** Explicit run-token. When omitted, read from {@link DiscoToolsConfig.tokenEnvVar}. */
  token?: string;
  /** Env var the run-token is read from at execute time. Default {@link DEFAULT_RUN_TOKEN_ENV}. */
  tokenEnvVar?: string;
}

/** The JSON shape the Python bridge returns (mirror of `disco.core.ToolResult`). */
interface BridgeToolResult {
  call_id: string;
  tool_name: string;
  success: boolean;
  content: string;
  structured?: unknown;
  error?: string | null;
}

// ---------------------------------------------------------------------------
// JSON-Schema helpers (TypeBox-shaped object literals).
// ---------------------------------------------------------------------------

interface JsonProp {
  type: "string" | "integer" | "number" | "boolean" | "array";
  description?: string;
  items?: { type: string };
}

function prop(type: JsonProp["type"], description: string): JsonProp {
  return { type, description };
}

function objectSchema(properties: Record<string, JsonProp>, required: string[]): TSchema {
  return {
    type: "object",
    properties,
    required,
    additionalProperties: false,
  } as unknown as TSchema;
}

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

/** Build the bridged `execute` for one tool name. */
function makeExecute(
  cfg: DiscoToolsConfig,
  name: string,
): (
  toolCallId: string,
  params: unknown,
  signal: AbortSignal | undefined,
) => Promise<AgentToolResult<unknown>> {
  const base = cfg.baseUrl.replace(/\/+$/, "");
  const tokenEnv = cfg.tokenEnvVar ?? DEFAULT_RUN_TOKEN_ENV;
  const url = `${base}/internal/pi-kernel/${encodeURIComponent(cfg.kernelId)}/tools/${encodeURIComponent(name)}`;

  return async (toolCallId, params, signal) => {
    const token = cfg.token ?? process.env[tokenEnv];
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (token) headers["authorization"] = `Bearer ${token}`;

    const args = params !== null && typeof params === "object" ? params : {};
    let resp: Response;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ call_id: toolCallId, arguments: args }),
        signal,
      });
    } catch (err) {
      // Network failure OR an aborted turn (fetch rejects with an AbortError when
      // the signal fires). Surface as a tool error — Pi's convention is to throw.
      throw new Error(`disco tool bridge ${name} request failed: ${describeError(err)}`);
    }

    const text = await resp.text();
    let parsed: BridgeToolResult | null = null;
    try {
      parsed = text ? (JSON.parse(text) as BridgeToolResult) : null;
    } catch {
      parsed = null;
    }
    if (!resp.ok || parsed === null) {
      // Transport / auth / shape failure (401/403/4xx or a non-JSON body): the
      // bridge contract is a JSON ToolResult on 200, so anything else is an error.
      throw new Error(`disco tool bridge ${name} failed (HTTP ${resp.status})`);
    }
    if (!parsed.success) {
      // The executor reported a structured failure. Throw so Pi records the tool
      // result as an error and shows the diagnosis to the model on its next turn.
      throw new Error(parsed.error ?? parsed.content ?? `tool ${name} failed`);
    }
    return {
      content: [{ type: "text", text: parsed.content ?? "" }],
      details: parsed.structured ?? null,
    } satisfies AgentToolResult<unknown>;
  };
}

interface ToolSpec {
  name: string;
  description: string;
  parameters: TSchema;
}

/** Per-tool name/description/parameter schemas (faithful to Disco's arg models). */
function toolSpecs(): ToolSpec[] {
  return [
    {
      name: "file_read",
      description: "Read a UTF-8 file from the workspace.",
      parameters: objectSchema(
        {
          path: prop("string", "Workspace-relative path to read."),
          offset: prop("integer", "1-based first line to read (optional)."),
          limit: prop("integer", "Maximum number of lines to read (optional)."),
        },
        ["path"],
      ),
    },
    {
      name: "file_write",
      description: "Create or overwrite a file with the full given content.",
      parameters: objectSchema(
        {
          path: prop("string", "Workspace-relative path to write."),
          content: prop("string", "Full UTF-8 content to write."),
        },
        ["path", "content"],
      ),
    },
    {
      name: "file_replace_lines",
      description: "Replace an inclusive 1-based line range with new text.",
      parameters: objectSchema(
        {
          path: prop("string", "Workspace-relative path to edit."),
          start_line: prop("integer", "First line to replace (1-based, inclusive)."),
          end_line: prop("integer", "Last line to replace (1-based, inclusive)."),
          new_text: prop("string", "Replacement text (may be multi-line)."),
        },
        ["path", "start_line", "end_line", "new_text"],
      ),
    },
    {
      name: "file_insert_lines",
      description: "Insert text after a given 1-based line number.",
      parameters: objectSchema(
        {
          path: prop("string", "Workspace-relative path to edit."),
          after_line: prop("integer", "Insert after this 1-based line (0 = at top)."),
          text: prop("string", "Text to insert (may be multi-line)."),
        },
        ["path", "after_line", "text"],
      ),
    },
    {
      name: "file_list",
      description: "List the entries of a workspace directory.",
      parameters: objectSchema(
        {
          path: prop("string", "Workspace-relative directory to list (default '.')."),
        },
        [],
      ),
    },
    {
      name: "shell_exec",
      description: "Execute a shell command in a persistent session (one foreground process per session).",
      parameters: objectSchema(
        {
          command: prop("string", "The shell command to execute."),
          session: prop("string", "Shell session name (default 'main')."),
          exec_dir: prop("string", "Directory to run the command in (optional)."),
        },
        ["command"],
      ),
    },
    {
      name: "preview_start",
      description: "Start a live preview server for the workspace.",
      parameters: objectSchema(
        {
          serve_dir: prop("string", "Directory to serve (optional)."),
          command: prop("string", "Custom start command (optional)."),
          framework: prop("string", "Framework hint (optional)."),
          cwd: prop("string", "Working directory for the command (optional)."),
          name: prop("string", "Preview name (optional)."),
        },
        [],
      ),
    },
    {
      name: "preview_status",
      description: "Report the status of a running preview server.",
      parameters: objectSchema(
        {
          name: prop("string", "Preview name (optional)."),
        },
        [],
      ),
    },
    {
      name: "preview_logs",
      description: "Fetch recent logs from a running preview server.",
      parameters: objectSchema(
        {
          name: prop("string", "Preview name (optional)."),
          tail_chars: prop("integer", "How many trailing characters of log to return (optional)."),
        },
        [],
      ),
    },
    {
      name: "finish",
      description: "Declare the task complete with a short summary.",
      parameters: objectSchema(
        {
          summary: prop("string", "One or two sentences describing what was delivered."),
        },
        ["summary"],
      ),
    },
    {
      name: "think",
      description: "Record a private reasoning note (no side effects).",
      parameters: objectSchema(
        {
          thought: prop("string", "The reasoning to record."),
        },
        ["thought"],
      ),
    },
    {
      name: "submit_plan",
      description: "Propose a plan for human approval before execution (gate tool).",
      parameters: objectSchema(
        {
          summary: prop("string", "One or two sentences: what this plan delivers."),
          steps: { type: "array", description: "Ordered plan steps.", items: { type: "object" } },
          context: prop("string", "Optional notes/assumptions for the reviewer."),
        },
        ["summary", "steps"],
      ),
    },
    {
      name: "ask_user",
      description: "Pause and ask the user a question (gate tool).",
      parameters: objectSchema(
        {
          question: prop("string", "The question to put to the user."),
          options: { type: "array", description: "Optional choices.", items: { type: "string" } },
        },
        ["question"],
      ),
    },
    {
      name: "clarify",
      description: "Ask the user to clarify an ambiguous requirement (gate tool).",
      parameters: objectSchema(
        {
          question: prop("string", "The clarification needed before continuing."),
        },
        ["question"],
      ),
    },
  ];
}

/**
 * Build the Disco custom-tool definitions for one kernel. Every tool bridges to
 * `{baseUrl}/internal/pi-kernel/{kernelId}/tools/{name}` with the run-token; the
 * returned array is exactly the D3 minimal set, in a stable order.
 */
export function buildDiscoTools(cfg: DiscoToolsConfig): ToolDefinition[] {
  return toolSpecs().map((spec) => ({
    name: spec.name,
    label: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    execute: makeExecute(cfg, spec.name),
  }));
}
