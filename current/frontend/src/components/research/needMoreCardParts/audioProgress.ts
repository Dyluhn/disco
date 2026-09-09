/**
 * NeedMoreCard — audio overview types + pure progress/label helpers
 * (extracted verbatim from NeedMoreCard.tsx).
 */

export type AudioMode = "podcast" | "single";

// W-08 / W-09: real pipeline stages streamed from the server (SSE). The UI keys
// its status copy on these so the "downloading voice model…" note ONLY shows on
// a genuine first-run download — never on a warm cache or remote backend.
export type AudioStage =
  | "preparing"
  | "downloading_model"
  | "synthesizing"
  | "mixing"
  | "cache_hit";

export interface AudioProgress {
  stage: AudioStage;
  current?: number;
  total?: number;
  // W-08: REAL voice-model download byte counters (only on `downloading_model`).
  downloaded?: number;
  bytesTotal?: number;
  pct?: number;
  fileIndex?: number;
  fileTotal?: number;
}

export type AudioState =
  | { status: "idle" }
  | { status: "generating"; progress?: AudioProgress }
  | { status: "done"; audioUrl: string }
  /** `reason` is the sentence shown to the operator; `detail` is the
   *  secondary technical line (failing stage + the server's exception text). */
  | { status: "unavailable"; reason: string; detail?: string };

/** The sentence to show for a server error. The server sends `message`; the
 *  reason-only fallback covers an older server (and the pre-stream blocking
 *  path), so the UI never degrades to printing a bare stage name again. */
export function audioReasonMessage(reason: string, message?: string): string {
  if (message) return message;
  if (reason === "tts_disabled") {
    return "Audio overview is disabled in Settings → Audio — enable it to generate.";
  }
  return `Audio overview failed at the ${reason} stage.`;
}

/** The secondary line under the message: which stage failed, and why. */
export function audioReasonDetail(reason: string, detail?: string): string | undefined {
  if (!reason) return detail;
  return detail ? `${reason}: ${detail}` : reason;
}

/** Human-readable label for the current generation stage (W-09). */
export function audioStageLabel(progress: AudioProgress | undefined): string {
  if (!progress) return "Generating audio…";
  switch (progress.stage) {
    case "preparing":
      return "Preparing script…";
    case "downloading_model": {
      const part =
        progress.fileTotal && progress.fileTotal > 1
          ? ` (file ${progress.fileIndex ?? 1}/${progress.fileTotal})`
          : "";
      return progress.pct != null
        ? `Downloading voice model${part}… ${progress.pct}%`
        : "Downloading voice model…";
    }
    case "synthesizing":
      return progress.total
        ? `Synthesizing turns ${progress.current ?? 0}/${progress.total}…`
        : "Synthesizing audio…";
    case "mixing":
      return "Mixing audio…";
    case "cache_hit":
      return "Loading cached audio…";
    default:
      return "Generating audio…";
  }
}

/** Bytes → a compact "12.3 MB" label for the download progress note. */
export function fmtMB(bytes: number | undefined): string {
  if (!bytes || bytes <= 0) return "0 MB";
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Determinate fill 0–100 for the current generation stage, or null when the
 * stage has no measurable progress (indeterminate spinner only). */
export function audioProgressPct(progress: AudioProgress | undefined): number | null {
  if (!progress) return null;
  if (progress.stage === "downloading_model" && progress.pct != null) {
    return Math.max(0, Math.min(100, progress.pct));
  }
  if (progress.stage === "synthesizing" && progress.total) {
    return Math.max(0, Math.min(100, ((progress.current ?? 0) / progress.total) * 100));
  }
  return null;
}
