/**
 * `deriveFiles` — the workspace Files tab (Inspector tier). Split out of
 * `buildTrace.ts`; the per-source accumulation (file actions vs. sandbox-tool
 * observations) is now two small helpers instead of one branchy loop body.
 */
import type { ActionEvent, AgentEvent, ObservationEvent } from "@/types/agent";
import type { ManifestFile, WorkspaceFile } from "../buildTrace";

/** Source (1): action events: file_write / file_append / file_edit — real file
 * content is available client-side from the tool arguments. */
function applyFileAction(byPath: Map<string, string>, e: ActionEvent): void {
  if (!e.tool_call) return;
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
}

/** Source (2): sandbox-tool artifacts (slides_generate, sheet_generate). These
 * tools produce a server-side file that never appears as a file_write action
 * event — the only trace is the observation's structured.filename. Don't
 * overwrite a file_write entry of the same path (existing content is richer). */
function applyFileObservation(byPath: Map<string, string>, e: ObservationEvent): void {
  const { tool_name, structured, success } = e.tool_result;
  if (
    success &&
    (tool_name === "slides_generate" || tool_name === "sheet_generate") &&
    typeof structured?.filename === "string" &&
    !byPath.has(structured.filename)
  ) {
    byPath.set(structured.filename, ""); // content is server-side only
  }
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
      applyFileAction(byPath, e);
    } else if (e.kind === "observation") {
      applyFileObservation(byPath, e);
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
