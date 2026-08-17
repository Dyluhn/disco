/**
 * Activity Feed vocabulary — turns a raw tool_call / error code into the
 * plain-language text the feed shows. Split out of `buildTrace.ts`; the VERB
 * and PLAIN_ERROR dispatch tables are the idiom the rest of the decomposition
 * extends rather than reinvents.
 */

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
export function _isWorkspaceRoot(path: unknown): boolean {
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

export function plainLabel(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "browser") {
    const act = String(args.action ?? "navigate");
    if (act === "submit" || act === "fill") return `Submitted a web form`;
    return `Opened ${args.url ?? "a page"}`;
  }
  const mcp = splitMcpName(toolName);
  if (mcp) return `${mcp.tool} · via ${mcp.server}`;
  return (VERB[toolName] ?? (() => `Used ${toolName}`))(args);
}

export function detailFor(toolName: string, args: Record<string, unknown>): string | undefined {
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
