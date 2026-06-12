/**
 * Deep Research data layer — the ONE place that talks to the deep_research
 * agent-server endpoints + WebSocket. Components NEVER import this; only
 * the Deep Research hooks do (the data-flow discipline, mirroring the rest
 * of the codebase).
 *
 * The conversation model is Build-style: POST /conversations with
 * surface="deep_research" returns a cid; the existing subscribeConversation
 * (from api/agent.ts) opens /ws/conversations/{cid} and streams events. We
 * REUSE that WebSocket subscription verbatim — no new transport layer.
 *
 * Export is markdown (the same Blob → URL.createObjectURL → invisible
 * <a download> pattern as Projects). The agent-server doesn't currently
 * have a report-export endpoint, so we serialize a ReportEvent to markdown
 * client-side using the exact shape the prior pmx-deep-verify.py script
 * produced — keeping the artifact format consistent between server-driven
 * and client-driven exports.
 */

import { agentLive, agentSend, fixtureDelay } from "./client";
import type { ReportEvent, ReportSection } from "@/types/agent";

const OWNER_ID = (import.meta.env.VITE_OWNER_ID as string | undefined) ?? "local";

export interface DeepResearchSubmit {
  query: string;
  /** Per-conversation lead-model override (leader pill). null = use default. */
  leaderId?: string | null;
  /** Depth tier (quick / standard_deep / exhaustive). Defaults to standard_deep. */
  depthTier?: "quick" | "standard_deep" | "exhaustive";
}

/** Fixture cid for offline rendering — the fixture stream replays a canned
 * Deep Research conversation so the UI renders without a live backend. */
export const FIXTURE_DEEP_CID = "conv_deep_fixture";

/** Create a deep_research conversation. Returns the cid; the caller then
 * subscribes via the existing subscribeConversation. */
export async function createDeepResearchConversation(
  opts: DeepResearchSubmit,
): Promise<string> {
  if (!agentLive()) {
    await fixtureDelay();
    return FIXTURE_DEEP_CID;
  }
  const res = await agentSend<{ conversation_id: string }>(
    "POST",
    "/conversations",
    {
      owner_id: OWNER_ID,
      surface: "deep_research",
      title: opts.query.slice(0, 100),
      model_override: opts.leaderId ?? null,
      // depth_tier is read by the runtime's set_depth path; pass it through
      // (the backend tolerates the extra field — Pydantic ignores when not
      // declared, and where it IS declared it gets persisted as the run's tier).
      depth_tier: opts.depthTier ?? "standard_deep",
    },
  );
  return res.conversation_id;
}

/** Export format for the report export endpoint. */
export type ReportExportFmt = "md" | "pdf" | "docx";

/** Export a finished ReportEvent as a markdown document downloaded to the
 * user's machine. Reuses Projects' Blob → URL.createObjectURL → invisible
 * <a download> pattern; client-side serialization (no new endpoint). */
export function exportReportAsMarkdown(report: ReportEvent): void {
  const md = serializeReportToMarkdown(report);
  downloadBlob(new Blob([md], { type: "text/markdown;charset=utf-8" }), report.query, ".md");
}

/** Server-side report export (PDF / DOCX). Hits the new backend endpoint.
 * For `md`, we still use the client-side serializer to avoid the round-trip.
 * The UI should gate pdf/docx: if the call returns a non-OK response (e.g.
 * the sandbox image hasn't been rebuilt yet), surface the error to the user.
 * Never silent — missing report → error detail; unknown fmt → error detail. */
export async function exportReport(cid: string, fmt: ReportExportFmt): Promise<boolean> {
  // MD stays client-side for now (the endpoint also supports it, but the
  // client-side path is zero-latency and byte-identical).
  if (fmt === "md") {
    throw new Error("Use exportReportAsMarkdown for md exports (client-side).");
  }
  const res = await fetch(`/api/conversations/${cid}/report/export?fmt=${fmt}`, {
    method: "POST",
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail?.reason || body.detail || JSON.stringify(body);
    } catch {
      // not JSON
    }
    throw new Error(`Export failed (${res.status}): ${detail}`);
  }
  const blob = await res.blob();
  const ext = fmt === "pdf" ? ".pdf" : ".docx";
  downloadBlob(blob, `report-${cid}`, ext);
  return true;
}

/** Download a Blob as a file. Shared helper for client-side and server-side
 * export paths. */
function downloadBlob(blob: Blob, fallbackName: string, ext: string): void {
  const url = URL.createObjectURL(blob);
  const safeTitle = (fallbackName || "research-report")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safeTitle || "research-report"}${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** The markdown serializer. Same shape the prior pmx-deep-verify.py script
 * produced — single source of truth for how a report looks as a portable
 * document. Includes citations as a footer table so the user can resolve
 * [[passage_id]] markers without the live UI. */
function serializeReportToMarkdown(report: ReportEvent): string {
  const lines: string[] = [];
  lines.push(`# Deep Research: ${report.query}`);
  lines.push("");
  lines.push("## Executive Summary");
  lines.push("");
  lines.push(report.summary || "*(no summary)*");
  lines.push("");
  for (const s of report.sections) {
    lines.push(`## ${s.title}`);
    lines.push("");
    if (s.disputed_notes && s.disputed_notes.length > 0) {
      lines.push(`_Conflicts noted: ${s.disputed_notes.join("; ")}_`);
      lines.push("");
    }
    lines.push(s.markdown);
    lines.push("");
  }
  if (report.bounded_by) {
    lines.push("---");
    lines.push("");
    lines.push(
      `_This run was bounded by **${report.bounded_by}**. Some planned ` +
        `sub-questions were not covered. Consider running the EXHAUSTIVE ` +
        `tier or assigning a faster driver model for deeper coverage._`,
    );
    lines.push("");
  }
  lines.push("---");
  lines.push("");
  lines.push(`Passages cited (${report.passages.length}):`);
  lines.push("");
  for (const p of report.passages) {
    const id = String((p as Record<string, unknown>).id ?? "?");
    const title = String((p as Record<string, unknown>).source_title ?? "");
    const url = String((p as Record<string, unknown>).source_url ?? "");
    lines.push(`- [${id}] ${title} — ${url}`);
  }
  return lines.join("\n");
}

/** Re-export the types for use by hooks (avoids importing this file from
 * non-hook code; the hooks import these types from @/types/agent directly). */
export type { ReportEvent, ReportSection };
