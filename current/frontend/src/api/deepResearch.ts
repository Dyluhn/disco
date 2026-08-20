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
import type { ReportEvent, ReportSection } from "@/types/agent";

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
      surface: "deep_research",
      // Leave title unset. RunController schedules the shared TitleService after
      // the first user message lands; pre-seeding the query would make that
      // canonical summarizer treat a fallback snippet as a finished title.
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

// ---- Passage-text normalization (mirrors report_export.py's shared seam) ----
//
// Scraped passages arrive with backslash-escaped markdown links and table
// debris; the server export cleans both in _normalize_passage_text. This is
// the SAME algorithm so the client-side serializer stays byte-identical to
// the server's fmt=md output for the same report.

const ESCAPED_LINK_RE = /\\\[((?:[^\\\]]|\\.)*?)\\\]\(([^()\s]+)\)/g;
const MD_ESCAPE_CHAR_RE = /\\([\\`*_{}[\]()<>#+\-.!|~])/g;
const EXTERNAL_URL_RE = /^https?:\/\//i;
const TABLE_ALIGN_RE = /^:?-{3,}:?$/;
const FENCE_RE = /^\s*(```|~~~)/;

function unescapeMd(text: string): string {
  return text.replace(MD_ESCAPE_CHAR_RE, "$1");
}

function rewriteEscapedLink(_m: string, rawText: string, rawUrl: string): string {
  const text = unescapeMd(rawText).trim();
  const url = unescapeMd(rawUrl).trim();
  if (!text) return EXTERNAL_URL_RE.test(url) ? url : "";
  if (EXTERNAL_URL_RE.test(url) && url !== text) return `${text} (${url})`;
  return text;
}

function splitTableRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|")) row = row.slice(0, -1);
  const cells: string[] = [];
  let buf = "";
  let escaped = false;
  for (const ch of row) {
    if (escaped) {
      buf += ch;
      escaped = false;
    } else if (ch === "\\") {
      escaped = true;
    } else if (ch === "|") {
      cells.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (escaped) buf += "\\";
  cells.push(buf.trim());
  return cells;
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((c) => TABLE_ALIGN_RE.test(c.trim()));
}

function isPseudoTableLine(line: string): boolean {
  return (line.match(/\|/g) ?? []).length >= 3 && (line.match(/ \| /g) ?? []).length >= 2;
}

function formatPipeRow(cells: string[]): string {
  return `| ${cells.join(" | ")} |`;
}

function fitRow(cells: string[], width: number, pad: string): string[] {
  return cells.length < width
    ? cells.concat(Array(width - cells.length).fill(pad))
    : cells.slice(0, width);
}

/** Consume one genuine pipe table starting at `lines[i]` (whose header line,
 * already link-rewritten, is `header`): pad/truncate the separator row and
 * every body row to the header's column count. Appends the repaired rows to
 * `out` and returns the index of the first line past the table.
 * Mirrors _normalize_table_block in report_export's _report_normalize.py. */
function consumeTableBlock(lines: string[], i: number, header: string, out: string[]): number {
  const width = splitTableRow(header).length;
  out.push(header);
  const sep = splitTableRow(lines[i + 1]);
  out.push(sep.length === width ? lines[i + 1] : formatPipeRow(fitRow(sep, width, "---")));
  const isRow = (l: string) => Boolean(l.trim()) && l.includes("|") && !isTableSeparator(l);
  for (i += 2; i < lines.length && isRow(lines[i]); i += 1) {
    const rowLine = lines[i].replace(ESCAPED_LINK_RE, rewriteEscapedLink);
    const row = splitTableRow(rowLine);
    out.push(row.length === width ? rowLine : formatPipeRow(fitRow(row, width, "")));
  }
  return i;
}

/** Shared cleanup for passage-bearing markdown (summary + section bodies):
 * escaped links → clean prose, ragged pipe tables → header-width rows,
 * high-pipe-density non-table lines → readable `;`-separated text.
 * Fenced code blocks pass through untouched. Clean input is returned as-is. */
function normalizePassageMarkdown(text: string): string {
  const lines = text.split("\n");
  const out: string[] = [];
  let i = 0;
  let inFence = false;
  while (i < lines.length) {
    let line = lines[i];
    if (FENCE_RE.test(line)) {
      inFence = !inFence;
      out.push(line);
      i += 1;
      continue;
    }
    if (inFence) {
      out.push(line);
      i += 1;
      continue;
    }
    line = line.replace(ESCAPED_LINK_RE, rewriteEscapedLink);
    if (i + 1 < lines.length && line.includes("|") && isTableSeparator(lines[i + 1])) {
      i = consumeTableBlock(lines, i, line, out);
      continue;
    }
    if (isPseudoTableLine(line)) {
      out.push(splitTableRow(line).filter(Boolean).join("; "));
      i += 1;
      continue;
    }
    out.push(line);
    i += 1;
  }
  let result = out.join("\n");
  if (text.endsWith("\n") && !result.endsWith("\n")) result += "\n";
  return result;
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
  lines.push(report.summary ? normalizePassageMarkdown(report.summary) : "*(no summary)*");
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
    lines.push(normalizePassageMarkdown(s.markdown));
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
