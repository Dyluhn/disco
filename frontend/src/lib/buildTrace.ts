/**
 * Selectors that turn the raw agent event stream into the THREE-TIER views the Agent
 * surface shows (BoD §13.4): a plain-language Activity Feed (Project-Manager tier), the
 * workspace Files + Terminal (Inspector tier), and the final answer. Keeping this pure +
 * tested means the components stay thin and the "not a debug log" framing is structural.
 */

import { stripElementMention } from "@/lib/elementMention";
import type {
  ActionEvent,
  AgentEvent,
  ConversationStatus,
  PlanStep,
  SecurityRisk,
} from "@/types/agent";

// Plan-mode meta tools are control signals, not workspace work — they never appear
// as Activity items or Terminal entries; their effect shows in the capstone tracker.
const META_TOOLS = new Set(["submit_plan", "plan_step", "update_plan_progress"]);

export interface ActivityItem {
  id: string;
  /** Discriminates the row's visual style. "action" is the agent's tool call;
   * "user" is the human's message (steer, send_message, revise instruction);
   * "agent_message" is a prose reply from the agent (ask_user free-form,
   * finish-message, etc.); "system_warning" is a ⚠-prefixed ENVIRONMENT
   * message addressed to the human (BP-05's release valve: "finished WITHOUT
   * a clean browser verification") — gate truths are surfaced, other
   * environment meta (nudges, reminders) stays hidden; "system_note" is a
   * NEUTRAL environment line the human caused and should see confirmed
   * (BP-11's upload announcement) — informational, not an alarm, so it gets
   * its own kind rather than borrowing system_warning's ⚠ styling. A
   * "rollback_marker" is the one non-chat audit chip allowed for
   * workspace_restored events. The feed becomes a unified chat-and-actions log
   * rather than an action-only ledger. */
  kind:
    | "action"
    | "user"
    | "agent_message"
    | "system_warning"
    | "system_note"
    | "rollback_marker";
  label: string; // plain language ("Wrote fizzbuzz.py" / "You: skip the cleanup")
  /** The agent's natural-language THOUGHT — its reasoning + plain-English
   * explanation of what it's doing. NEVER truncated; rendered wrapped. This
   * is the model talking to the user, and swallowing it was a bug. */
  thought?: string;
  /** The technical detail — file path, command preview, etc. Single-line OK
   * to truncate (this is a row label, not content). */
  detail?: string;
  mention?: { tag: string; text: string };
  /** Rich expandable content the user can drill into when they want the raw
   * tool call + observation. Hidden by default to keep the feed scannable. */
  expandable?: {
    tool_name: string;
    arguments: Record<string, unknown>;
    output?: string; // observation content (truncated to ~2KB)
    error?: string; // error message if the action failed
    plainError?: string; // plain first line for common failure codes
    screenshot_path?: string; // BP-15: relative .pmx/screenshots/… path from structured
    // rp-11: a generated spreadsheet artifact, downloadable via the declared-artifact
    // route (only present for a successful sheet_generate).
    sheet?: { filename: string; title?: string; sheet_names?: string[] };
    // D2: a generated slide deck (Marp HTML / PDF / PPTX), downloadable via
    // the declared-artifact route. Same shape as the AnswerBlock `slides`
    // kind minus the `id` — the activity item already has one.
    slides?: {
      filename: string;
      title?: string;
      format?: "html" | "pdf" | "pptx";
      slide_count?: number;
      slides?: { title?: string; content?: string }[];
      base?: string; // deck base name (no ext) — for the /deck/export template re-render
      editable?: boolean; // has an authored sidecar → template re-render is available
      renderer?: string; // R7: backend renderer provenance (c3-brand/pptx-native/libreoffice/marp = real; fallback = degraded HTML)
    };
    // F2: an agent-emitted file (via serve(kind="files")). Downloadable via the
    // declared-artifact route. Rendered as a first-class download card in the feed.
    file?: { filename: string; title?: string };
  };
  status: "done" | "running" | "pending" | "failed" | "pending_send";
  attention: boolean; // confidence gradient: risky/novel steps float up, routine recede
  risk?: SecurityRisk;
  /** True when the engine auto-approved a sandboxed op that would have gated
   * under the base risk policy. Informational-only; never raises attention. */
  autoApproved?: boolean;
}

/** A model flake can call a tool with a missing arg — never render "undefined". */
function _arg(v: unknown, fallback: string): string {
  return v == null || v === "" ? fallback : String(v);
}

const VERB: Record<string, (a: Record<string, unknown>) => string> = {
  file_write: (a) => `Wrote ${_arg(a.path, "a file")}`,
  file_edit: (a) => `Edited ${_arg(a.path, "a file")}`,
  file_read: (a) => `Read ${_arg(a.path, "a file")}`,
  file_list: (a) => (_isWorkspaceRoot(a.path) ? `Listed the workspace` : `Listed ${a.path}`),
  file_append: (a) => `Extended ${_arg(a.path, "a file")}`,
  file_insert_lines: (a) => `Edited ${_arg(a.path, "a file")}`,
  file_replace_lines: (a) => `Edited ${_arg(a.path, "a file")}`,
  file_str_replace: (a) => `Edited ${_arg(a.path, "a file")}`,
  exact_replace: (a) => `Edited ${_arg(a.path, "a file")}`,
  safe_write_file: (a) => (a.expected_sha256 ? `Edited ${_arg(a.path, "a file")}` : `Wrote ${_arg(a.path, "a file")}`),
  preview_start: () => `Started the preview server`,
  preview_stop: () => `Stopped the preview server`,
  preview_status: () => `Checked the preview server`,
  preview_logs: () => `Read the preview logs`,
  server_status: () => `Checked the app server`,
  shell: () => `Ran a command`,
  shell_exec: () => `Ran a command`,
  shell_view: () => `Watched a running command`,
  shell_wait: () => `Waited on a command`,
  shell_kill_process: () => `Stopped a process`,
  shell_write_to_process: () => `Sent input to a process`,
  run_project_script: () => `Ran a project script`,
  code_exec: (a) => `Ran ${a.language ?? "python"} code`,
  search: (a) => (a.query ? `Searched the web for "${a.query}"` : "Searched the web"),
  extract: () => `Read a web page`,
  slides_generate: () => `Generated a slide deck`,
  deck_patch: () => `Edited the slide deck`,
  sheet_generate: () => `Generated a spreadsheet`,
  doc_set_section: () => `Drafted a document section`,
  doc_export: () => `Exported the document`,
  audio_overview: () => `Generated an audio overview`,
  image_generate: (a) => {
    const prompt = a.prompt ? String(a.prompt) : "";
    const preview = prompt.length > 48 ? `${prompt.slice(0, 48)}…` : prompt;
    return `Generated an image${preview ? `: "${preview}"` : ""}`;
  },
  scaffold_starter: () => `Set up the project starter`,
  app_create: () => `Created the app scaffold`,
  app_add_section: () => `Added an app section`,
  app_update_content: () => `Updated app content`,
  app_set_design: () => `Applied the design`,
  app_snapshot_version: () => `Saved a version snapshot`,
  design_lint: () => `Checked the design`,
  verify_web_app: () => `Verified the app in a browser`,
  verify_appkit_app: () => `Verified the app`,
  plan_step: () => `Updated the plan`,
  update_plan_progress: () => `Checked off plan progress`,
  submit_plan: () => `Proposed a plan`,
  think: () => `Thought it through`,
  skip: () => `Skipped a step`,
  context_memory: () => `Saved working notes`,
  delegate_explore: () => `Explored the codebase`,
  draft_workflow: () => `Drafted a workflow`,
  enter_workflow: () => `Started a workflow`,
  list_workflows: () => `Listed workflows`,
  read_workflow_card: () => `Read a workflow card`,
  request_custom_build: () => `Requested a custom build`,
  needs_input: () => `Asked for input`,
};

const PLAIN_ERROR: Record<string, string> = {
  old_text_not_found: "An edit missed — retrying with fresh file contents",
  file_not_found: "That file wasn't there",
  timeout: "The step timed out",
  tool_denied: "That action wasn't allowed here",
  denied: "That action wasn't allowed here",
  FRESH_READ_REQUIRED: "Fresh file contents were needed before editing",
  bad_range: "Those line numbers were stale — retrying with fresh file contents",
  bad_line: "That insert location was stale — retrying with fresh file contents",
  STALE_FILE_CONTEXT: "The file changed — retrying with fresh contents",
  syntax_gate_rejected: "That edit introduced a syntax error",
  cancelled: "The action was cancelled",
  superseded: "A newer action replaced this one",
};

export function plainError(code: string): string | null {
  const key = code.trim();
  if (PLAIN_ERROR[key]) return PLAIN_ERROR[key];
  const lower = key.toLowerCase();
  if (lower.includes("out-of-scope") || lower.includes("not allowed")) {
    return "That tool wasn't available here";
  }
  if (lower.includes("filenotfound") || lower.includes("file not found")) {
    return PLAIN_ERROR.file_not_found;
  }
  if (lower.includes("timeout") || lower.includes("timed out")) return PLAIN_ERROR.timeout;
  if (lower.includes("rejected by user")) return "You rejected that action";
  return null;
}

/** W-14/W-28: a `file_list` whose path is the workspace ROOT (".", "./", "",
 * "/", undefined). The agent's orientation step lists the root before any real
 * work, and the naïve `Listed ${path}` rendered the bare "Listed ." stub as the
 * very FIRST feed row across slides/build/agent — output hygiene noise that
 * looked like a broken empty placeholder. We both relabel it ("Listed the
 * workspace") AND suppress the row entirely in deriveActivity so nothing renders
 * until there is real content. Path normalization is the single source of truth. */
function _isWorkspaceRoot(path: unknown): boolean {
  if (path == null) return true;
  const p = String(path).trim();
  return p === "" || p === "." || p === "./" || p === "/";
}

/** Split an MCP qualified tool name `mcp__<server>__<tool>` into {server, tool};
 * null for a non-MCP name. MCP calls otherwise read as gibberish in the feed. */
function splitMcpName(toolName: string): { server: string; tool: string } | null {
  if (!toolName.startsWith("mcp__")) return null;
  const rest = toolName.slice("mcp__".length);
  const sep = rest.indexOf("__");
  if (sep < 0) return { server: rest, tool: rest };
  return { server: rest.slice(0, sep), tool: rest.slice(sep + 2) };
}

function plainLabel(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "browser") {
    const act = String(args.action ?? "navigate");
    if (act === "submit" || act === "fill") return `Submitted a web form`;
    return `Opened ${args.url ?? "a page"}`;
  }
  const mcp = splitMcpName(toolName);
  if (mcp) return `${mcp.tool} · via ${mcp.server}`;
  return (VERB[toolName] ?? (() => `Used ${toolName}`))(args);
}

function detailFor(toolName: string, args: Record<string, unknown>): string | undefined {
  if (toolName === "shell" || toolName === "shell_exec") return String(args.command ?? "");
  if (toolName === "browser") return String(args.url ?? "");
  if (
    toolName.startsWith("file_") ||
    toolName === "exact_replace" ||
    toolName === "safe_write_file"
  ) {
    return String(args.path ?? "");
  }
  return undefined;
}

export function deriveActivity(
  events: AgentEvent[],
  pendingActionId: string | null,
  status: ConversationStatus,
): ActivityItem[] {
  const observed = new Set<string>(); // action ids that produced an observation
  const failed = new Set<string>(); // action ids that errored
  for (const e of events) {
    if (e.kind === "observation") observed.add(e.action_id);
    if (e.kind === "agent_error" && e.action_id) failed.add(e.action_id);
  }
  // Walk events in order — the activity feed is a chronological chat-and-action
  // log, not an action-only ledger. User messages (steer, send_message, revise)
  // and mid-stream agent prose replies (ask_user free-form questions) are
  // first-class items alongside tool calls. This is what makes typed input feel
  // acknowledged: it appears in the timeline the moment the optimistic event is
  // dispatched, then the server's canonical echo replaces the placeholder.
  //
  // The TRAILING agent message is the surface's "final answer" — rendered as
  // a Markdown panel elsewhere in the BuildSurface. Excluding it from the feed
  // prevents the same text rendering twice + keeps the feed the running narrative.
  let lastAgentMessageIdx = -1;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "message" && e.source === "agent") {
      lastAgentMessageIdx = i;
      break;
    }
  }
  // Index observations + errors by action id so we can attach the raw output
  // to each action's expandable detail (the user explicitly asked to be able
  // to drill into commands + results — hiding them is poor design).
  const observationByActionId = new Map<
    string,
    {
      output?: string;
      error?: string;
      plainError?: string;
      screenshotPath?: string;
      sheet?: { filename: string; title?: string; sheet_names?: string[] };
      slides?: {
        filename: string;
        title?: string;
        format?: "html" | "pdf" | "pptx";
        slide_count?: number;
        slides?: { title?: string; content?: string }[];
        base?: string;
        editable?: boolean;
      };
    }
  >();
  // Editability is decided by the LATEST render PER BASE (declared artifacts
  // accumulate forever, so a later non-editable Marp regen supersedes an older
  // editable sidecar). Pre-scan to record, per base, whether the most recent
  // slides_generate still advertised an editable sidecar — mirrors the backend's
  // _sidecar_is_current rule so a superseded card never shows the template picker.
  const latestEditableByBase = new Map<string, boolean>();
  for (const e of events) {
    if (
      e.kind === "observation" &&
      e.tool_result.tool_name === "slides_generate" &&
      e.tool_result.success
    ) {
      const s = e.tool_result.structured;
      const b = typeof s?.base_name === "string" ? s.base_name : undefined;
      if (b) {
        latestEditableByBase.set(
          b,
          typeof s?.editable_source === "string" && s.editable_source.length > 0,
        );
      }
    }
  }
  for (const e of events) {
    if (e.kind === "observation") {
      const struct = e.tool_result.structured;
      const sp = struct?.screenshot_path;
      const fn = struct?.filename;
      const sheet =
        e.tool_result.tool_name === "sheet_generate" &&
        e.tool_result.success &&
        typeof fn === "string"
          ? {
              filename: fn,
              title: typeof struct?.title === "string" ? struct.title : undefined,
              sheet_names: Array.isArray(struct?.sheet_names)
                ? (struct.sheet_names as string[])
                : undefined,
            }
          : undefined;
      // D2: slide-deck artifact. Backend's structured output:
      //   {filename, base_name, format, slide_count, renderer}
      // We only trust the keys we know about; format is one of html|pdf|pptx.
      const rawFmt = typeof struct?.format === "string" ? struct.format.toLowerCase() : "";
      const fmt: "html" | "pdf" | "pptx" | null =
        rawFmt === "html" || rawFmt === "pdf" || rawFmt === "pptx" ? rawFmt : null;
      const slides =
        e.tool_result.tool_name === "slides_generate" &&
        e.tool_result.success &&
        typeof fn === "string" &&
        fmt !== null
          ? {
              filename: fn,
              title: typeof struct?.base_name === "string" ? struct.base_name : undefined,
              format: fmt,
              slide_count:
                typeof struct?.slide_count === "number" ? struct.slide_count : undefined,
              slides: Array.isArray(struct?.slides)
                ? (struct!.slides as { title?: string; content?: string }[])
                : undefined,
              // An editable deck carries an authored sidecar (editable_source) — only
              // then can the slide-deck template selector re-render via /deck/export.
              // Gate on the LATEST render per base (not this event), so a card whose
              // base was later re-rendered non-editable drops the picker (no 404 affordance).
              base: typeof struct?.base_name === "string" ? struct.base_name : undefined,
              editable:
                typeof struct?.base_name === "string"
                  ? latestEditableByBase.get(struct.base_name) === true
                  : false,
              // R7: surface the real renderer so the deck card can be honest about
              // real-vs-fallback instead of mislabeling every deck "Marp-rendered".
              renderer: typeof struct?.renderer === "string" ? struct.renderer : undefined,
            }
          : undefined;
      observationByActionId.set(e.action_id, {
        output: (e.tool_result.content || "").slice(0, 2000),
        screenshotPath: typeof sp === "string" ? sp : undefined,
        sheet,
        slides,
      });
    } else if (e.kind === "agent_error" && e.action_id) {
      const plain = plainError(e.error);
      observationByActionId.set(e.action_id, {
        error: e.error,
        ...(plain ? { plainError: plain } : {}),
      });
    }
  }
  const out: ActivityItem[] = [];
  for (let idx = 0; idx < events.length; idx++) {
    const e = events[idx];
    if (e.kind === "action" && e.tool_call && !META_TOOLS.has(e.tool_call.tool_name)) {
      const tc = e.tool_call;
      // W-14/W-28: a workspace-root `file_list` is the agent's silent orientation
      // step — suppress it so the feed shows nothing until there's real content
      // (no "Listed ." first-output stub). A list of a real subdirectory still
      // renders ("Listed src/").
      if (tc.tool_name === "file_list" && _isWorkspaceRoot(tc.arguments.path)) continue;
      const risk = e.meta?.risk_assessment?.risk ?? e.self_assessed_risk;
      const isPending = e.id === pendingActionId;
      let st: ActivityItem["status"];
      if (isPending) st = "pending";
      else if (failed.has(e.id)) st = "failed";
      else if (observed.has(e.id)) st = "done";
      else if (status === "RUNNING") st = "running";
      else st = "done";
      const obs = observationByActionId.get(e.id);
      out.push({
        id: e.id,
        kind: "action",
        label: plainLabel(tc.tool_name, tc.arguments),
        thought: e.thought || undefined, // the model talking — NEVER truncate
        detail: detailFor(tc.tool_name, tc.arguments),
        expandable: {
          tool_name: tc.tool_name,
          arguments: tc.arguments,
          output: obs?.output,
          error: obs?.error,
          plainError: obs?.plainError,
          screenshot_path: obs?.screenshotPath,
          sheet: obs?.sheet,
          slides: obs?.slides,
        },
        status: st,
        attention: isPending || risk === "HIGH" || risk === "UNKNOWN" || st === "failed",
        risk,
        autoApproved: e.meta?.auto_approved === "sandboxed",
      });
    } else if (e.kind === "message" && e.source === "user") {
      // User input (steer/send_message/revise) — render the human text, not any
      // machine-readable Point-flow element payload. The optimistic
      // echo from useBuildStream stamps id="local-pending-…" so we can show a
      // subtle "sending" affordance until the server's canonical echo replaces it.
      const content = e.message?.content ?? "";
      const { clean, mention } = stripElementMention(content);
      if (!clean && !mention) continue;
      const isPendingSend = e.id.startsWith("local-pending-");
      out.push({
        id: e.id,
        kind: "user",
        label: clean || `Pointed at <${mention?.tag ?? "element"}>`,
        mention: mention ? { tag: mention.tag, text: mention.text } : undefined,
        status: isPendingSend ? "pending_send" : "done",
        attention: false,
      });
    } else if (e.kind === "message" && e.source === "agent") {
      // Mid-stream agent prose (e.g. ask_user free-form questions). Skip the
      // trailing agent message — it's rendered as the final-answer Markdown
      // panel; double-rendering would break getByText assertions + read noisy.
      if (idx === lastAgentMessageIdx) continue;
      const content = e.message?.content ?? "";
      if (!content.trim()) continue;
      out.push({
        id: e.id,
        kind: "agent_message",
        label: content,
        status: "done",
        attention: false,
      });
    } else if (e.kind === "message" && e.source === "environment") {
      // Environment messages are loop meta (nudges, reminders) — hidden, EXCEPT
      // ⚠-prefixed warnings, which the loop explicitly addresses to the human
      // (BP-05 release valve: the run finished WITHOUT a clean browser
      // verification), and BP-11 upload announcements, which confirm an action
      // the HUMAN took. Truths about delivered work must reach the feed.
      const content = e.message?.content ?? "";
      if (content.startsWith("User uploaded:")) {
        // Neutral note, not a warning — the user did this on purpose, and
        // ⚠-styling a routine confirmation would be a false alarm.
        out.push({
          id: e.id,
          kind: "system_note",
          label: content,
          status: "done",
          attention: false,
        });
        continue;
      }
      if (!content.startsWith("⚠")) continue;
      out.push({
        id: e.id,
        kind: "system_warning",
        label: content,
        status: "done",
        attention: true,
      });
    } else if (e.kind === "deliverable") {
      // F2: Agent handed off a file via serve(kind="files"). Render as a
      // download card in the feed so the user can grab it immediately.
      // Only render for "files" kind (not "app" which opens a live URL).
      if (e.artifact_kind === "files") {
        out.push({
          id: e.id,
          kind: "action",
          label: `Delivered: ${e.title}`,
          detail: e.path,
          expandable: {
            tool_name: "serve",
            arguments: {},
            file: { filename: e.path, title: e.title },
          },
          status: "done",
          attention: false,
        });
      }
    } else if (e.kind === "workspace_restored") {
      out.push({
        id: e.id,
        kind: "rollback_marker",
        label: `↩ rolled back to v${e.version_seq}`,
        status: "done",
        attention: false,
      });
    }
  }
  return out;
}

export interface WorkspaceFile {
  path: string;
  content: string;
  bytes: number;
}

export interface ManifestFile {
  path: string;
  bytes: number;
}

/** The files the agent has written or generated — latest content wins.
 *
 * Sources (unioned by path, no double-count):
 * 1. `action` events: file_write / file_append / file_edit — real file content is
 *    available client-side from the tool arguments.
 * 2. `observation` events for slides_generate / sheet_generate — these tools produce
 *    a server-side artifact that never appears in a file_write action; the only trace
 *    is the observation's structured.filename. Content is server-side only (empty
 *    string here); the declared-artifact download route fetches the real file.
 * 3. `deliverable` events — the agent declared a finished served artifact via `serve`;
 *    add its path when not already captured by (1)/(2). Content is server-side only.
 *
 * WALK-16: source paths (2) and (3) are new; (1) was the only source before.
 *
 * F1b: if the event stream has no file-producing events yet, fall back to the
 * ProjectStore manifest's file list. Imported/pre-seeded builds can have a
 * workspace manifest before any file_write events exist. */
export function deriveFiles(
  events: AgentEvent[],
  manifestFiles: ManifestFile[] = [],
): WorkspaceFile[] {
  const byPath = new Map<string, string>();
  for (const e of events) {
    if (e.kind === "action" && e.tool_call) {
      const { tool_name, arguments: a } = e.tool_call;
      if (tool_name === "file_write" && typeof a.path === "string") {
        byPath.set(a.path, String(a.content ?? ""));
      } else if (tool_name === "file_append" && typeof a.path === "string") {
        // Accumulate appended content so an incrementally-written file still renders.
        byPath.set(a.path, (byPath.get(a.path) ?? "") + String(a.content ?? ""));
      } else if (tool_name === "file_edit" && typeof a.path === "string") {
        const cur = byPath.get(a.path);
        if (cur === undefined) {
          byPath.set(a.path, ""); // edited a file we didn't see created; content unknown here
        } else if (typeof a.old === "string" && typeof a.new === "string") {
          byPath.set(a.path, cur.replace(a.old, a.new)); // mirror the edit so the preview tracks it
        }
      }
    } else if (e.kind === "observation") {
      // Source (2): sandbox-tool artifacts (slides_generate, sheet_generate). These
      // tools produce a server-side file that never appears as a file_write action
      // event — the only trace is the observation's structured.filename. Don't
      // overwrite a file_write entry of the same path (existing content is richer).
      const { tool_name, structured, success } = e.tool_result;
      if (
        success &&
        (tool_name === "slides_generate" || tool_name === "sheet_generate") &&
        typeof structured?.filename === "string" &&
        !byPath.has(structured.filename)
      ) {
        byPath.set(structured.filename, ""); // content is server-side only
      }
    } else if (e.kind === "deliverable") {
      // Source (3): the agent declared a finished served artifact. Add only when
      // not already captured by a file_write or sandbox-tool observation.
      if (!byPath.has(e.path)) {
        byPath.set(e.path, ""); // content is server-side only
      }
    }
  }
  if (byPath.size === 0 && manifestFiles.length > 0) {
    return manifestFiles.map((file) => ({ path: file.path, content: "", bytes: file.bytes }));
  }
  return [...byPath.entries()].map(([path, content]) => ({
    path,
    content,
    bytes: new TextEncoder().encode(content).length,
  }));
}

/** Cluster 5 (UI 2.1): assemble HTML from the written files for a CLIENT-SIDE
 * `srcdoc` preview. Picks the entry HTML (index.html, else any *.html). With no
 * conversation URL it inlines known local CSS/JS as an offline fallback; with a
 * preview base it leaves assets external so all relative file classes preserve
 * browser URL semantics. Returns null when there's no renderable HTML artifact
 * (so the pane falls back to the live-server preview / placeholder).
 *
 * @param injectionScript - Optional JavaScript source to inject as the FIRST
 *   `<script>` inside `<body>` (or appended when no body tag is present).
 *   Used by the selection overlay (§4.1 / C-EDIT-1) to install the in-frame
 *   selection agent.  Pass `undefined` for untrusted / no-scripts iframes.
 * @param baseUrl - Optional authenticated preview route used as the document
 *   base.  A srcdoc document otherwise has no useful URL, so images, fonts,
 *   nested links, and runtime fetches with relative URLs all fail even when the
 *   corresponding workspace files exist.
 * @param selectedEntryPath - Optional canonical handoff path from the latest
 *   DeliverableEvent. When present, that exact HTML is the only eligible entry;
 *   a missing/empty selection fails closed instead of falling back to a stale
 *   workspace-root index.html.
 */
export function deriveSrcDoc(
  files: WorkspaceFile[],
  injectionScript?: string,
  baseUrl?: string,
  selectedEntryPath?: string,
): string | null {
  if (files.length === 0) return null;
  const byName = new Map<string, string>();
  for (const f of files) {
    // index by basename and by path so both `href="style.css"` and
    // `href="./css/style.css"` resolve.
    byName.set(f.path, f.content);
    byName.set(f.path.split("/").pop() ?? f.path, f.content);
  }
  const selectedPath = selectedEntryPath?.trim();
  const entry = selectedPath
    ? files.find((f) => f.path === selectedPath)
    : files.find((f) => /(^|\/)index\.html$/i.test(f.path)) ??
      files.find((f) => /\.html$/i.test(f.path));
  // C5: server-side artifacts land with content="" (bytes=0) — return null so the
  // existing `srcDoc != null` guards suppress the blank white frame and the pane
  // can fall back to the ?inline=true route or the placeholder instead.
  if (!entry || !entry.content) return null;
  let html = entry.content;
  if (baseUrl) {
    const escapedBase = baseUrl
      .replaceAll("&", "&amp;")
      .replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
    const baseTag = `<base href="${escapedBase}">`;
    if (/<head[\s>]/i.test(html)) {
      html = html.replace(/<head[\s>][^>]*>/i, (match) => `${match}\n${baseTag}`);
    } else if (/<html[\s>]/i.test(html)) {
      html = html.replace(/<html[\s>][^>]*>/i, (match) => `${match}\n<head>${baseTag}</head>`);
    } else {
      html = `<head>${baseTag}</head>\n${html}`;
    }
  }
  if (!baseUrl) {
    // Offline/no-conversation fallback: inline the two file classes the trace
    // carries completely.  With an authenticated base route, keep references
    // external so the browser preserves each asset's own URL base (especially
    // nested CSS url(../fonts/x.woff2)) and can load every file class generally.
    html = html.replace(
      /<link[^>]*rel=["']?stylesheet["']?[^>]*href=["']([^"']+)["'][^>]*>/gi,
      (m, href) => {
        const css = byName.get(href) ?? byName.get(href.replace(/^\.?\//, ""));
        return css != null ? `<style>\n${css}\n</style>` : m;
      },
    );
    html = html.replace(
      /<script[^>]*src=["']([^"']+)["'][^>]*><\/script>/gi,
      (m, src) => {
        const js = byName.get(src) ?? byName.get(src.replace(/^\.?\//, ""));
        return js != null ? `<script>\n${js}\n</script>` : m;
      },
    );
  }
  // Inject the selection agent script (§4.1 C-EDIT-1) when provided.
  // Injected as the first child of <body> so it runs before user scripts and
  // can intercept events; falls back to appending at the end when no <body>.
  if (injectionScript) {
    const tag = `<script>\n${injectionScript}\n</script>`;
    if (/<body[\s>]/i.test(html)) {
      html = html.replace(/<body[\s>][^>]*>/i, (m) => `${m}\n${tag}`);
    } else {
      html = `${tag}\n${html}`;
    }
  }
  return html;
}

export interface TerminalEntry {
  id: string;
  command: string;
  output: string;
  success: boolean;
  running: boolean;
}

/** The terminal/output Inspector view: shell + code_exec commands paired with their
 * observed output. The "raw" tier lives here, not on the Activity Feed. */
export function deriveTerminal(events: AgentEvent[]): TerminalEntry[] {
  const obs = new Map<string, { output: string; success: boolean }>();
  for (const e of events) {
    if (e.kind === "observation") {
      obs.set(e.action_id, {
        output: e.tool_result.content || e.tool_result.error || "",
        success: e.tool_result.success,
      });
    } else if (e.kind === "agent_error" && e.action_id) {
      obs.set(e.action_id, { output: e.error, success: false });
    }
  }
  const out: TerminalEntry[] = [];
  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const { tool_name, arguments: a } = e.tool_call;
    if (tool_name !== "shell" && tool_name !== "code_exec") continue;
    const cmd = tool_name === "shell" ? String(a.command ?? "") : `${a.language ?? "python"} «code»`;
    const r = obs.get(e.id);
    out.push({
      id: e.id,
      command: cmd,
      output: r?.output ?? "",
      success: r?.success ?? true,
      running: !r,
    });
  }
  return out;
}

export function latestAgentMessage(events: AgentEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "message" && e.source === "agent") return e.message.content;
  }
  return null;
}

/** W-01: recover the REAL task from the replayed event stream — the first user
 * message's text. Build's resume path seeds `session.task` with the internal
 * "(resumed)" sentinel (useBuild.ts), which must never reach the UI; the surface
 * uses this to render the actual task instead, mirroring how DR recovers its query
 * (useDeepResearch.ts). Matches on `source` OR `message.role` so it's robust to
 * however the backend tags the human turn. Returns null before any user message
 * exists (the caller falls back to a neutral "Resumed project" label). */
export function firstUserTask(events: AgentEvent[]): string | null {
  for (const e of events) {
    if (e.kind === "message" && (e.source === "user" || e.message?.role === "user")) {
      const { clean, mention } = stripElementMention(e.message?.content ?? "");
      if (clean) return clean;
      if (mention) return `Pointed at <${mention.tag}>`;
    }
  }
  return null;
}

// ---- deliverable handoff --------------------------------------------------

export interface DeliverableView {
  id: string;
  title: string;
  path: string;
  kind: "app" | "files";
  /** Canonical URL the deliverable is reachable at, if the agent served one. */
  deploymentUrl?: string;
}

/** The latest finished-artifact handoff the agent declared via `serve` (a
 * DeliverableEvent). The newest wins — a later serve supersedes an earlier one
 * (the agent refined or replaced the deliverable). Returns null before any
 * handoff, so the panel stays hidden until there is a real thing to hand off
 * (no false affordance). */
export function deriveDeliverable(events: AgentEvent[]): DeliverableView | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "deliverable") {
      return {
        id: e.id,
        title: e.title,
        path: e.path,
        kind: e.artifact_kind,
        deploymentUrl: e.deployment_url || undefined,
      };
    }
  }
  return null;
}

// ---- plan mode ------------------------------------------------------------

export type StepState = "pending" | "active" | "done" | "stalled";

export interface PlanView {
  id: string;
  summary: string;
  steps: PlanStep[];
  revision: number;
  context: string;
}

// The legacy backend inserted this sentinel step when the model submitted a plan with
// a summary but no real steps; it rendered as a broken "1. (the planner…)" numbered
// step. The backend no longer emits it (it keeps `steps` empty now), but historical
// events still carry it — drop it defensively so old plans render the clean no-steps
// state. MUST stay byte-identical to the former backend constant in plans.py.
const LEGACY_NO_STEPS_SENTINEL = "(the planner returned no concrete steps)";

/** The latest proposed plan (highest revision wins — a re-plan supersedes the prior
 * one). Returns null before any plan exists. */
export function derivePlan(events: AgentEvent[]): PlanView | null {
  let latest: PlanView | null = null;
  for (const e of events) {
    if (e.kind !== "plan") continue;
    if (latest === null || e.revision >= latest.revision) {
      latest = {
        id: e.id,
        summary: e.summary,
        // Drop the legacy no-steps placeholder so a summary-only plan renders cleanly
        // instead of showing a broken numbered "1. (the planner…)" step.
        steps: e.steps.filter((s) => s.title !== LEGACY_NO_STEPS_SENTINEL),
        revision: e.revision,
        context: e.context ?? "",
      };
    }
  }
  return latest;
}

/** Per-step progress (1-based index → state), derived from the agent's `plan_step`
 * reports in the stream. Honest by construction: a step the agent never reported
 * stays "pending" — progress is agent-driven, never inferred from action counts.
 *
 * Status-aware: when the conversation reaches a terminal-without-completion state
 * (FINISHED/STUCK/ERROR — anything that means "the loop stopped before this step
 * could be marked done"), any lingering "active" step is rewritten to "stalled"
 * so the UI doesn't lie with a spinning blue icon on work that isn't progressing. */
export function derivePlanProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  const progress = new Map<number, StepState>();
  // Only count plan_step marks AFTER the latest plan — a re-plan starts a fresh
  // checklist, so the prior plan's "done" marks must not show on the new one
  // (else every step looks done after a re-plan).
  let latestPlanIdx = -1;
  let latestRev = -1;
  events.forEach((e, i) => {
    if (e.kind === "plan" && e.revision >= latestRev) {
      latestRev = e.revision;
      latestPlanIdx = i;
    }
  });
  events.forEach((e, i) => {
    if (i < latestPlanIdx) return; // belongs to a superseded plan
    if (e.kind !== "action" || !e.tool_call || e.tool_call.tool_name !== "plan_step") return;
    const idx = Number(e.tool_call.arguments.index);
    const state = String(e.tool_call.arguments.state);
    if (!Number.isFinite(idx)) return;
    if (state === "done") progress.set(idx, "done");
    else if (state === "active" && progress.get(idx) !== "done") progress.set(idx, "active");
  });
  // Stalled-step reconciliation: in terminal states, an "active" marker means
  // the agent started a step and the loop stopped before it finished. Show that
  // honestly instead of pretending it's still working.
  const terminalStopped =
    status === "FINISHED" || status === "STUCK" || status === "ERROR" || status === "IDLE";
  if (terminalStopped) {
    for (const [idx, st] of progress) {
      if (st === "active") progress.set(idx, "stalled");
    }
  }
  return progress;
}

/** runthru-v2 (#3): Build progress for CAPABLE models — derived from the LATEST
 * declarative `update_plan_progress` snapshot (a full-state rewrite, not a delta).
 * Because the latest snapshot carries the COMPLETE state of every step, it is
 * self-correcting: a dropped/garbled prior update can't leave a step wrong, the
 * next snapshot re-establishes truth. Returns an EMPTY map when no snapshot exists
 * (small models on the assist path never emit it, or none yet) — the UI then falls
 * back to the honest status chip rather than a lying empty checklist. Never inferred
 * from action counts. */
export function deriveBuildProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  const progress = new Map<number, StepState>();
  // The latest plan supersedes prior ones; only count snapshots after it (a re-plan
  // starts a fresh checklist). Capture its step COUNT to bound the snapshot.
  let latestPlanIdx = -1;
  let latestRev = -1;
  let nSteps = 0;
  events.forEach((e, i) => {
    if (e.kind === "plan" && e.revision >= latestRev) {
      latestRev = e.revision;
      latestPlanIdx = i;
      nSteps = Array.isArray(e.steps) ? e.steps.length : 0;
    }
  });
  // Declarative: the LAST update_plan_progress action wins — it's the full picture.
  // ActionEvent-typed (the forEach guard only assigns action events); the cast at the
  // use site defeats TS's callback-assignment blindness (it narrows `latest` to null).
  let latest: ActionEvent | null = null;
  events.forEach((e, i) => {
    if (i < latestPlanIdx) return;
    if (e.kind === "action" && e.tool_call?.tool_name === "update_plan_progress") latest = e;
  });
  // Apply the declarative snapshot IF one exists — but do NOT early-return when it's
  // absent. A SMALL model (the NL-done-at-finish tier, e.g. local Qwen) never calls
  // update_plan_progress, so `latest` stays null; a `return` here skipped the FINISHED
  // reconciliation below and left every step unchecked on a build that actually finished
  // (the live #3 bug on Qwen). Fall through so terminal reconciliation runs regardless.
  const steps = ((latest as ActionEvent | null)?.tool_call?.arguments?.steps ?? []) as Array<{
    index: number;
    state: string;
  }>;
  if (Array.isArray(steps)) {
    for (const s of steps) {
      const idx = Number(s?.index);
      const st = String(s?.state);
      // Bound to the CURRENT plan's range. A malformed or stale index (e.g. index 99
      // on a 3-step plan, or a leftover from a longer prior plan) must NOT create a
      // phantom step or inflate the done-count into a lying checklist (a snapshot of
      // [{index:99,state:"done"}] would otherwise render "1/3" with every real step
      // pending). Out-of-range / non-integer indices are dropped.
      if (!Number.isInteger(idx) || idx < 1 || (nSteps > 0 && idx > nSteps)) continue;
      if (st === "done" || st === "active" || st === "pending") progress.set(idx, st as StepState);
    }
  }
  // Terminal reconciliation:
  //  • FINISHED — the build passed the real finish gate, so the deliverable is
  //    complete: EVERY plan step reads done. This also stops a stale early snapshot
  //    from showing e.g. "2/3" (with no Done signal) after the build actually
  //    finished — the model isn't required to send a final 100%-done snapshot.
  //  • STUCK/ERROR/IDLE (stopped without completing) → "active" reads as stalled.
  if (status === "FINISHED" && nSteps > 0) {
    for (let i = 1; i <= nSteps; i++) progress.set(i, "done");
  } else if (status === "STUCK" || status === "ERROR" || status === "IDLE") {
    for (const [idx, st] of progress) {
      if (st === "active") progress.set(idx, "stalled");
    }
  }
  return progress;
}

/** The live activity signal — what the agent is doing RIGHT NOW between events.
 * Without true token streaming, the UI would otherwise show a static "Working"
 * label that feels frozen during slow local-model turns (5-30s for Qwen 27B).
 * This selector inspects the event tail and the status to produce a precise
 * label for what the user is waiting on. */
export type LiveSignal =
  | { kind: "idle" }
  | { kind: "thinking_about_user_message"; preview: string }
  | { kind: "tool_executing"; tool_name: string; detail?: string }
  | { kind: "composing_next_step" }
  | { kind: "starting" }
  | { kind: "waiting_for_you"; label: string };

export function deriveLiveSignal(
  events: AgentEvent[],
  status: ConversationStatus,
): LiveSignal {
  // Cluster 6: liveness is no longer RUNNING-only. During gate/await states the
  // bar narrates what the agent is waiting ON, so the surface never reads as
  // frozen between turns or while a gate is open.
  if (status === "WAITING_FOR_CONFIRMATION")
    return { kind: "waiting_for_you", label: "Waiting for you to approve an action" };
  if (status === "AWAITING_PLAN_APPROVAL")
    return { kind: "waiting_for_you", label: "Waiting for you to review the plan" };
  if (status === "AWAITING_USER_DECISION")
    return { kind: "waiting_for_you", label: "Waiting for your decision" };
  if (status === "AWAITING_USER_QUESTION")
    return { kind: "waiting_for_you", label: "The agent asked you a question" };
  if (status !== "RUNNING") return { kind: "idle" };
  // Walk the tail backward to classify what we're waiting on. Skip noise
  // events (environment reminders) — they're meta, not the live signal.
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "status") continue;
    if (e.kind === "message" && e.source === "environment") continue;
    if (e.kind === "message" && e.source === "user") {
      // Just received a user message → the model is reading + composing a reply.
      const { clean, mention } = stripElementMention(e.message?.content ?? "");
      const text = (clean || (mention ? `Pointed at <${mention.tag}>` : "")).slice(0, 80);
      return { kind: "thinking_about_user_message", preview: text };
    }
    if (e.kind === "action" && e.tool_call) {
      // Action emitted but no observation yet → tool is executing.
      // (If a later observation/error existed, we'd have hit it first walking back.)
      const tc = e.tool_call;
      return {
        kind: "tool_executing",
        tool_name: tc.tool_name,
        detail:
          tc.tool_name === "shell" || tc.tool_name === "shell_exec"
            ? String(tc.arguments.command ?? "")
            : tc.tool_name.startsWith("file_") ||
                tc.tool_name === "exact_replace" ||
                tc.tool_name === "safe_write_file"
              ? String(tc.arguments.path ?? "")
              : undefined,
      };
    }
    if (e.kind === "observation" || e.kind === "agent_error") {
      // Last event was a tool result; model is composing its next step.
      return { kind: "composing_next_step" };
    }
    if (e.kind === "message" && e.source === "agent") {
      // Agent just spoke; another step is in progress.
      return { kind: "composing_next_step" };
    }
  }
  // No prior signal → we just kicked off; model is reading the goal.
  return { kind: "starting" };
}

/** Cluster 6: aggregate plan progress for a glanceable "N of M" + bar. Built
 * from the same progress Map the PlanPanel already has. */
export function planProgressSummary(
  totalSteps: number,
  progress: Map<number, StepState>,
): { done: number; total: number; fraction: number } {
  let done = 0;
  for (const st of progress.values()) if (st === "done") done += 1;
  const total = Math.max(totalSteps, 0);
  return { done, total, fraction: total > 0 ? done / total : 0 };
}
