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
 * Report downloads use the agent-server export endpoint so Markdown and PDF
 * share the stored generated title and selected follow-up turns. The local
 * serializer remains available for offline fixtures and focused rendering.
 */

import { agentFetch, agentHttpBase, agentLive, agentSend, fixtureDelay } from "./client";
import { normalizePassageMarkdown } from "./deepResearchParts/passageNormalize";
import { sourceUrlKey } from "@/lib/sources";
import type { ReportEvent, ReportSection } from "@/types/agent";

export interface DeepResearchSubmit {
  query: string;
  /** Per-conversation lead-model override (leader pill). null = use default. */
  leaderId?: string | null;
  /** Depth tier (quick / standard_deep / exhaustive). Defaults to standard_deep. */
  depthTier?: "quick" | "standard_deep" | "exhaustive";
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
      surface: "deep_research",
      // Leave title unset. RunController schedules the shared TitleService after
      // the first user message lands; pre-seeding the query would make that
      // canonical summarizer treat a fallback snippet as a finished title.
      model_override: opts.leaderId ?? null,
      // depth_tier is read by the runtime's set_depth path; pass it through
      // (the backend tolerates the extra field — Pydantic ignores when not
      // declared, and where it IS declared it gets persisted as the run's tier).
      depth_tier: opts.depthTier ?? "standard_deep",
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

/** Export a finished ReportEvent locally for offline/fixture consumers. */
export function exportReportAsMarkdown(
  report: ReportEvent,
  followUps?: Array<[string, string]>,
): void {
  const md = serializeReportToMarkdown(report, followUps);
  downloadBlob(new Blob([md], { type: "text/markdown;charset=utf-8" }), report.query, ".md");
}

/** Server-side report export (Markdown or PDF). Hits the report endpoint so
 * every format uses the stored generated conversation title.
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
  const body = followUpSeqs && followUpSeqs.length > 0
    ? JSON.stringify({ follow_up_seqs: followUpSeqs })
    : undefined;
  const res = await agentFetch(`/api/conversations/${cid}/report/export?fmt=${fmt}`, {
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
  downloadBlob(blob, `report-${cid}`, fmt === "md" ? ".md" : ".pdf");
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
  const res = await agentFetch(
    `/conversations/${cid}/report/audio?mode=${mode}`,
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

// ---- Citation numbering (mirrors lib/sources.ts + _report_citations.py) ----

/** One numbering, everywhere: passage-id → the 1-based [n] numeral the UI
 * renders (lib/sources.ts:citationNumbers) — the position of the passage's
 * SOURCE in first-seen `source_url` order over `report.passages`. Passages
 * from one source share the number; a passage with no URL numbers alone,
 * keyed by its id. Same rule over the report's raw passage dicts; the server
 * mirror is `_citation_numbers` in agent-server's _report_citations.py, and
 * the parity tests pin both to the same assignment. */
function reportCitationNumbers(
  passages: Array<Record<string, unknown>>,
): Map<string, number> {
  const bySource = new Map<string, number>();
  const numbers = new Map<string, number>();
  for (const p of passages) {
    const id = String(p.id ?? "?");
    const url = typeof p.source_url === "string" ? p.source_url : "";
    const key = url ? sourceUrlKey(url) : `#${id}`;
    let n = bySource.get(key);
    if (n === undefined) {
      n = bySource.size + 1;
      bySource.set(key, n);
    }
    numbers.set(id, n);
  }
  return numbers;
}

const CITE_MARKER_RE = /\[\[([^\]]+)\]\]/g;

/** Replace `[[passage_id]]` markers with the UI's `[n]` numeral. Unknown ids
 * degrade to `[?]` — a raw id is never shown. Mirrors `_sub_citation_markers`
 * in report_export.py. */
function subCitationMarkers(text: string, numbers: Map<string, number>): string {
  return text.replace(CITE_MARKER_RE, (_m, rawId: string) => {
    const n = numbers.get(rawId.trim());
    return n !== undefined ? `[${n}]` : "[?]";
  });
}

/** One `[n, title, url]` row per citation number, in numeric order — the
 * sources footer body. Carries the FIRST passage seen for each source (the
 * same row the UI's Sources panel shows). Mirrors `_cited_source_rows`. */
function citedSourceRows(
  passages: Array<Record<string, unknown>>,
  numbers: Map<string, number>,
): Array<[number, string, string]> {
  const rows = new Map<number, [number, string, string]>();
  for (const p of passages) {
    const n = numbers.get(String(p.id ?? "?"));
    if (n === undefined || rows.has(n)) continue;
    rows.set(n, [n, String(p.source_title ?? ""), String(p.source_url ?? "")]);
  }
  return Array.from(rows.keys())
    .sort((a, b) => a - b)
    .map((n) => rows.get(n) as [number, string, string]);
}

/** The markdown serializer. Same shape the prior pmx-deep-verify.py script
 * produced — single source of truth for how a report looks as a portable
 * document. Citations render as the UI's [n] numerals (reportCitationNumbers)
 * and the footer lists one source per number, so the document resolves on its
 * own without the live UI — and never shows a raw passage id.
 *
 * Exported so the NeedMoreCard's File System Access API path can get the raw
 * string (to write via showSaveFilePicker) without duplicating the logic.
 *
 * ``followUps`` — optional list of [question, answer] pairs from selected
 * post-report Q&A turns (WALK-20). When provided and non-empty a
 * ``## Follow-up Q&A`` section is appended after the sources footer.
 * When absent or empty the output is byte-identical to the baseline
 * (the byte-parity contract with Python's serialize_markdown is preserved). */
export function serializeReportToMarkdown(
  report: ReportEvent,
  followUps?: Array<[string, string]>,
): string {
  // One numbering, everywhere: the UI's [n] assignment (first-seen source
  // order over report.passages) is the canonical presentation — inline
  // markers and the sources footer never show a raw passage id.
  const numbers = reportCitationNumbers(report.passages);
  const lines: string[] = [];
  lines.push(`# Deep Research: ${report.query}`);
  lines.push("");
  lines.push("## Executive Summary");
  lines.push("");
  lines.push(
    report.summary
      ? subCitationMarkers(normalizePassageMarkdown(report.summary), numbers)
      : "*(no summary)*",
  );
  lines.push("");
  for (const s of report.sections) {
    lines.push(`## ${s.title}`);
    lines.push("");
    if (s.disputed_notes && s.disputed_notes.length > 0) {
      // [[passage_id]] markers become the UI's [n] numerals — resolvable
      // against the sources footer, unlike the raw ids (WALK-03's stripping
      // is superseded by the shared numbering).
      const noted = subCitationMarkers(s.disputed_notes.join("; "), numbers);
      lines.push(`_Conflicts noted: ${noted}_`);
      lines.push("");
    }
    lines.push(subCitationMarkers(normalizePassageMarkdown(s.markdown), numbers));
    lines.push("");
  }
  lines.push("---");
  lines.push("");
  // One row per citation NUMBER (not per passage) — the same list the UI's
  // Sources panel shows, resolvable against the inline [n] markers above.
  const rows = citedSourceRows(report.passages, numbers);
  lines.push(`Sources cited (${rows.length}):`);
  lines.push("");
  for (const [n, title, url] of rows) {
    lines.push(`- [${n}] ${title} — ${url}`);
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
      lines.push(subCitationMarkers(a, numbers));
    });
  }
  return lines.join("\n");
}

/** Re-export the types for use by hooks (avoids importing this file from
 * non-hook code; the hooks import these types from @/types/agent directly). */
export type { ReportEvent, ReportSection };

// ── NeedMoreCard transport (Amendment A3) ───────────────────────────────────
//
// The NeedMoreCard export dialog and audio-overview section used to call
// `agentFetch`/`agentHttpBase` directly (components may not import
// `@/api/client`, nor use raw `fetch`/WebSocket/XHR — that's the api-layer's
// job). These four functions are that transport, moved here verbatim from
// NeedMoreCard.tsx's ExportModal/AudioSection so the component/hook layer
// only ever calls into `@/api/*`.

/** Fetch a report export blob (MD or PDF) from the agent-server report/export
 * endpoint. Used by NeedMoreCard's export dialog for BOTH the File System
 * Access save-picker path and the plain anchor-download fallback. */
export async function fetchReportExportBlob(
  cid: string,
  fmt: "md" | "pdf",
  body: string | undefined,
): Promise<Blob> {
  const res = await agentFetch(
    `/api/conversations/${cid}/report/export?fmt=${fmt}`,
    {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body,
    },
  );
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const errBody = await res.json();
      detail = errBody.detail?.reason || errBody.detail || JSON.stringify(errBody);
    } catch {
      /* not JSON */
    }
    throw new Error(`Export failed (${res.status}): ${detail}`);
  }
  return res.blob();
}

/** One parsed SSE frame from the audio-overview stream. */
export interface AudioStreamEvent {
  stage?: string;
  current?: number;
  total?: number;
  mp3_url?: string;
  reason?: string;
  // W-08: real voice-model download byte counters.
  downloaded?: number;
  pct?: number;
  file_index?: number;
  file_total?: number;
}

/** Parse complete `data: {...}\n\n` SSE frames out of `buffer`, returning the
 * parsed events plus the unconsumed remainder (a partial frame still being
 * accumulated). A frame with no `data:` line, or a `data:` line that isn't
 * valid JSON, is silently skipped — the driver just keeps reading. Split out
 * of `streamReportAudio` so neither callable's branching exceeds the mccabe
 * cap. */
function parseSseFrames(buffer: string): [AudioStreamEvent[], string] {
  const events: AudioStreamEvent[] = [];
  let rest = buffer;
  let sep: number;
  while ((sep = rest.indexOf("\n\n")) >= 0) {
    const frame = rest.slice(0, sep);
    rest = rest.slice(sep + 2);
    const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
    if (!dataLine) continue;
    try {
      events.push(JSON.parse(dataLine.slice(5).trim()) as AudioStreamEvent);
    } catch {
      continue;
    }
  }
  return [events, rest];
}

/** Stream the SSE audio-overview endpoint, invoking `onEvent` once per parsed
 * frame; `onEvent` returns true once it has handled a terminal (done / error)
 * event. Resolves `false` on network failure or a non-OK / bodyless response
 * so the caller can fall back to `requestReportAudioBlocking`; otherwise
 * resolves whether a terminal event was ever handled (mirrors the prior
 * `viaStream` semantics: the read loop always runs to completion, it doesn't
 * stop early just because a terminal event arrived). */
export async function streamReportAudio(
  cid: string,
  mode: string,
  body: string | undefined,
  onEvent: (ev: AudioStreamEvent) => boolean,
): Promise<boolean> {
  const headers = body ? { "Content-Type": "application/json" } : undefined;
  let res: Response;
  try {
    res = await agentFetch(`/conversations/${cid}/report/audio/stream?mode=${mode}`, {
      method: "POST",
      headers,
      body,
    });
  } catch {
    return false; // network / endpoint unavailable → fall back
  }
  if (!res.ok || !res.body) return false;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let handled = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const [events, rest] = parseSseFrames(buffer);
    buffer = rest;
    for (const ev of events) {
      if (onEvent(ev)) handled = true;
    }
  }
  return handled;
}

/** Blocking fallback for audio-overview generation — the original single-shot
 * POST used when the SSE stream can't start or never yields a terminal event.
 * Throws with the same reason-extraction + message mapping as the former
 * inline `viaBlocking` (tts_disabled gets the friendly Settings message;
 * anything else becomes "Audio overview failed: <reason>"). */
export async function requestReportAudioBlocking(
  cid: string,
  mode: string,
  body: string | undefined,
): Promise<{ mp3_url: string }> {
  const headers = body ? { "Content-Type": "application/json" } : undefined;
  const res = await agentFetch(`/conversations/${cid}/report/audio?mode=${mode}`, {
    method: "POST",
    headers,
    body,
  });
  if (!res.ok) {
    let reason = `${res.status}`;
    try {
      const resBody = (await res.json()) as { detail?: { reason?: string } | string };
      const detail = resBody?.detail;
      if (typeof detail === "object" && detail !== null) {
        reason = detail.reason ?? reason;
      } else if (typeof detail === "string") {
        reason = detail;
      }
    } catch {
      /* opaque */
    }
    throw new Error(
      reason === "tts_disabled"
        ? "Audio overview is disabled in Settings → Audio — enable it to generate."
        : `Audio overview failed: ${reason}`,
    );
  }
  return res.json();
}

/** The agent-server's absolute URL for a report-relative audio asset path
 * (e.g. the `mp3_url` an audio-overview response returns). */
export function absoluteAudioUrl(mp3Url: string): string {
  return `${agentHttpBase()}${mp3Url}`;
}
