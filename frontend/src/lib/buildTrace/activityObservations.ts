/**
 * Activity Feed observation indexing — split out of `buildTrace.ts`'s
 * `deriveActivity`. Builds the per-action-id lookup of observation/error detail
 * (output, plain error, screenshot, generated sheet/slide artifacts) that
 * `deriveActivity` attaches to each action's `expandable` field, plus the small
 * whole-stream scans (observed/failed action ids, the trailing agent-message
 * index, the latest-editable-sidecar-per-base map) that used to live inline in
 * one 79-branch function. Each scan/builder here is its own capped callable.
 */
import type { AgentEvent, ObservationEvent, ToolResult } from "@/types/agent";
import { plainError } from "./activityLabels";

/** action ids that produced an observation / that errored. */
export function collectObservedAndFailed(events: AgentEvent[]): {
  observed: Set<string>;
  failed: Set<string>;
} {
  const observed = new Set<string>();
  const failed = new Set<string>();
  for (const e of events) {
    if (e.kind === "observation") observed.add(e.action_id);
    if (e.kind === "agent_error" && e.action_id) failed.add(e.action_id);
  }
  return { observed, failed };
}

/** The index of the TRAILING agent message — rendered as the final-answer
 * Markdown panel elsewhere; `deriveActivity` excludes it from the feed so the
 * same text never renders twice. -1 when there is no agent message yet. */
export function findLastAgentMessageIndex(events: AgentEvent[]): number {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "message" && e.source === "agent") return i;
  }
  return -1;
}

/** Editability is decided by the LATEST render PER BASE (declared artifacts
 * accumulate forever, so a later non-editable Marp regen supersedes an older
 * editable sidecar). Pre-scan to record, per base, whether the most recent
 * slides_generate still advertised an editable sidecar — mirrors the backend's
 * _sidecar_is_current rule so a superseded card never shows the template picker. */
export function collectLatestEditableByBase(events: AgentEvent[]): Map<string, boolean> {
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
  return latestEditableByBase;
}

export interface ObservationEntry {
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

// D2: slide-deck artifact. Backend's structured output:
//   {filename, base_name, format, slide_count, renderer}
// We only trust the keys we know about; format is one of html|pdf|pptx.
function sheetArtifactFrom(
  toolResult: ToolResult,
  fn: unknown,
  struct: Record<string, unknown> | null | undefined,
) {
  return toolResult.tool_name === "sheet_generate" && toolResult.success && typeof fn === "string"
    ? {
        filename: fn,
        title: typeof struct?.title === "string" ? struct.title : undefined,
        sheet_names: Array.isArray(struct?.sheet_names)
          ? (struct.sheet_names as string[])
          : undefined,
      }
    : undefined;
}

function normalizedSlidesFormat(
  struct: Record<string, unknown> | null | undefined,
): "html" | "pdf" | "pptx" | null {
  const rawFmt = typeof struct?.format === "string" ? struct.format.toLowerCase() : "";
  return rawFmt === "html" || rawFmt === "pdf" || rawFmt === "pptx" ? rawFmt : null;
}

function slidesArtifactFrom(
  toolResult: ToolResult,
  fn: unknown,
  struct: Record<string, unknown> | null | undefined,
  latestEditableByBase: Map<string, boolean>,
) {
  const fmt = normalizedSlidesFormat(struct);
  if (
    !(
      toolResult.tool_name === "slides_generate" &&
      toolResult.success &&
      typeof fn === "string" &&
      fmt !== null
    )
  ) {
    return undefined;
  }
  return {
    filename: fn,
    title: typeof struct?.base_name === "string" ? struct.base_name : undefined,
    format: fmt,
    slide_count: typeof struct?.slide_count === "number" ? struct.slide_count : undefined,
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
  };
}

function observationEntryFor(
  e: ObservationEvent,
  latestEditableByBase: Map<string, boolean>,
): ObservationEntry {
  const struct = e.tool_result.structured;
  const sp = struct?.screenshot_path;
  const fn = struct?.filename;
  const sheet = sheetArtifactFrom(e.tool_result, fn, struct);
  const slides = slidesArtifactFrom(e.tool_result, fn, struct, latestEditableByBase);
  return {
    output: (e.tool_result.content || "").slice(0, 2000),
    screenshotPath: typeof sp === "string" ? sp : undefined,
    sheet,
    slides,
  };
}

/** Index observations + errors by action id so deriveActivity can attach the
 * raw output to each action's expandable detail (the user explicitly asked to
 * be able to drill into commands + results — hiding them is poor design). */
export function buildObservationIndex(
  events: AgentEvent[],
  latestEditableByBase: Map<string, boolean>,
): Map<string, ObservationEntry> {
  const observationByActionId = new Map<string, ObservationEntry>();
  for (const e of events) {
    if (e.kind === "observation") {
      observationByActionId.set(e.action_id, observationEntryFor(e, latestEditableByBase));
    } else if (e.kind === "agent_error" && e.action_id) {
      const plain = plainError(e.error);
      observationByActionId.set(e.action_id, {
        error: e.error,
        ...(plain ? { plainError: plain } : {}),
      });
    }
  }
  return observationByActionId;
}
