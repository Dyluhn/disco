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

import { agentHttpBase, agentLive, agentSend, fixtureDelay } from "./client";
import type { ReportEvent, ReportSection } from "@/types/agent";

const OWNER_ID = (import.meta.env.VITE_OWNER_ID as string | undefined) ?? "local";

export interface DeepResearchSubmit {
  query: string;
  /** Per-conversation lead-model override (leader pill). null = use default. */
  leaderId?: string | null;
  /** Depth tier (quick / standard_deep / exhaustive). Defaults to standard_deep. */
  depthTier?: "quick" | "standard_deep" | "exhaustive";
  /** A4: iterative grounding — re-search weakly-grounded claims + re-check (up to
   * 3 rounds). Defaults to false (the standard non-iterative run). */
  iterative?: boolean;
  /** DR-3 recency filter: "month" = past 30 days, "week" = past 7 days, null = off. */
  recencyWindow?: "month" | "week" | null;
  /** Per-query research source ids. Empty means use the Settings default. */
  sources?: string[];
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
      // BW-09: this is a SEED, not the final stored title. The backend sanitizes
      // it (word-boundary, ~60-char `fallback_title`) before persisting, so the
      // verbose raw query is never written + masked by a CSS truncate at render.
      // The slice just caps what we send over the wire.
      title: opts.query.slice(0, 100),
      model_override: opts.leaderId ?? null,
      // depth_tier is read by the runtime's set_depth path; pass it through
      // (the backend tolerates the extra field — Pydantic ignores when not
      // declared, and where it IS declared it gets persisted as the run's tier).
      depth_tier: opts.depthTier ?? "standard_deep",
      // A4: iterative grounding toggle — read by the runtime's set_iterative path.
      // Defaults to false (the standard non-iterative run).
      iterative: opts.iterative ?? false,
      // DR-3: recency_window is optional; omit (undefined) when null/off so the
      // backend receives no field rather than explicit null (cleaner log).
      ...(opts.recencyWindow != null ? { recency_window: opts.recencyWindow } : {}),
      ...(opts.sources && opts.sources.length > 0 ? { sources: opts.sources } : {}),
    },
  );
  return res.conversation_id;
}

/** Export format for the report export endpoint. */
export type ReportExportFmt = "md" | "pdf";

/** Export a finished ReportEvent as a markdown document downloaded to the
 * user's machine. Reuses Projects' Blob → URL.createObjectURL → invisible
 * <a download> pattern; client-side serialization (no new endpoint). */
export function exportReportAsMarkdown(
  report: ReportEvent,
  followUps?: Array<[string, string]>,
): void {
  const md = serializeReportToMarkdown(report, followUps);
  downloadBlob(new Blob([md], { type: "text/markdown;charset=utf-8" }), report.query, ".md");
}

/** Server-side report export (PDF). Hits the new backend endpoint.
 * For `md`, we still use the client-side serializer to avoid the round-trip.
 * The UI should gate pdf: if the call returns a non-OK response (e.g.
 * weasyprint isn't installed), surface the error to the user.
 * Never silent — missing report → error detail; unknown fmt → error detail.
 *
 * ``followUpSeqs`` — optional list of USER MessageEvent seq values whose Q&A
 * pairs to include in the exported document (WALK-20). Passed to the server
 * via the JSON body; empty / absent = report only. */
export async function exportReport(
  cid: string,
  fmt: ReportExportFmt,
  followUpSeqs?: number[],
): Promise<boolean> {
  // MD stays client-side for now (the endpoint also supports it, but the
  // client-side path is zero-latency and byte-identical).
  if (fmt === "md") {
    throw new Error("Use exportReportAsMarkdown for md exports (client-side).");
  }
  const body = followUpSeqs && followUpSeqs.length > 0
    ? JSON.stringify({ follow_up_seqs: followUpSeqs })
    : undefined;
  const res = await fetch(`${agentHttpBase()}/api/conversations/${cid}/report/export?fmt=${fmt}`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body,
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
  downloadBlob(blob, `report-${cid}`, ".pdf");
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

/** Request a server-side audio overview for a finished report (C2).
 * POSTs to the agent-server, which runs the audio pipeline in-process and caches
 * the mp3 + transcript. Returns the agent-server-relative URLs. Throws a clear,
 * typed reason on failure (e.g. TTS disabled in Settings) — never a fake success.
 *
 * ``mode`` — "podcast" (default) or "single".
 * ``followUpSeqs`` — optional USER MessageEvent seq values to include (WALK-20). */
export async function requestReportAudio(
  cid: string,
  mode: "podcast" | "single" = "podcast",
  followUpSeqs?: number[],
): Promise<{ mp3_url: string; transcript_url: string }> {
  const body = followUpSeqs && followUpSeqs.length > 0
    ? JSON.stringify({ follow_up_seqs: followUpSeqs })
    : undefined;
  const res = await fetch(
    `${agentHttpBase()}/conversations/${cid}/report/audio?mode=${mode}`,
    {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body,
    },
  );
  if (!res.ok) {
    let reason = `${res.status}`;
    try {
      const body = await res.json();
      reason = body?.detail?.reason ?? body?.detail ?? reason;
    } catch {
      /* opaque; keep status */
    }
    if (reason === "tts_disabled") {
      throw new Error("Audio overview is disabled in Settings → Audio — enable it to generate.");
    }
    throw new Error(`Audio overview failed: ${reason}`);
  }
  return res.json();
}

/** The markdown serializer. Same shape the prior pmx-deep-verify.py script
 * produced — single source of truth for how a report looks as a portable
 * document. Includes citations as a footer table so the user can resolve
 * [[passage_id]] markers without the live UI.
 *
 * Exported so the NeedMoreCard's File System Access API path can get the raw
 * string (to write via showSaveFilePicker) without duplicating the logic.
 *
 * ``followUps`` — optional list of [question, answer] pairs from selected
 * post-report Q&A turns (WALK-20). When provided and non-empty a
 * ``## Follow-up Q&A`` section is appended after the passages footer.
 * When absent or empty the output is byte-identical to the baseline
 * (the byte-parity contract with Python's serialize_markdown is preserved). */
export function serializeReportToMarkdown(
  report: ReportEvent,
  followUps?: Array<[string, string]>,
): string {
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
      // Strip [[passage_id]] citation markers — they are UI artefacts that
      // can't resolve in a plain-text document.
      const stripped = s.disputed_notes
        .map((n) => n.replace(/\[\[[\w-]+\]\]/g, "").trim())
        .filter(Boolean)
        .join("; ");
      if (stripped) {
        lines.push(`_Conflicts noted: ${stripped}_`);
        lines.push("");
      }
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
  // WALK-20: optional follow-up Q&A section — byte-identical output when
  // followUps is absent or empty (preserves the py↔ts byte-parity contract).
  if (followUps && followUps.length > 0) {
    lines.push("");
    lines.push("## Follow-up Q&A");
    followUps.forEach(([q, a], i) => {
      lines.push("");
      lines.push(`### Follow-up ${i + 1}`);
      lines.push("");
      lines.push(`**Q:** ${q}`);
      lines.push("");
      lines.push(a);
    });
  }
  return lines.join("\n");
}

/** Re-export the types for use by hooks (avoids importing this file from
 * non-hook code; the hooks import these types from @/types/agent directly). */
export type { ReportEvent, ReportSection };
