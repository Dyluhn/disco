/**
 * NeedMoreCard — the "generating" status row (extracted from AudioSection's
 * inline IIFE at former L719–765). Pulling this whole branch cluster into its
 * own component is what gets AudioSection under the logical-line cap.
 */

import { Loader2 } from "lucide-react";
import { audioProgressPct, audioStageLabel, fmtMB, type AudioProgress } from "./audioProgress";

export interface AudioProgressRowProps {
  progress: AudioProgress | undefined;
}

export function AudioProgressRow({ progress }: AudioProgressRowProps) {
  const pct = audioProgressPct(progress);
  const isDownload = progress?.stage === "downloading_model";
  return (
    <div
      className="flex min-w-[12rem] flex-col gap-hair"
      data-tts-stage={progress?.stage ?? ""}
      data-tts-pct={pct != null ? Math.round(pct) : ""}
    >
      <div className="flex items-center gap-hair">
        <Loader2 className="size-3.5 animate-spin text-text-faint" aria-hidden />
        <span className="font-ui text-[0.78rem] text-text-muted">
          {audioStageLabel(progress)}
        </span>
      </div>
      {/* W-08: REAL progress bar next to the button. Determinate fill for a
          voice-model download (actual bytes/percent) or synthesis (turn
          n/m); indeterminate (no bar) for the un-measurable prep/mix
          stages. Never fabricated — `pct` is the server's byte count. */}
      {pct != null && (
        <div
          className="h-1 w-full overflow-hidden rounded-full bg-hairline"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(pct)}
          aria-label={isDownload ? "Voice model download progress" : "Audio synthesis progress"}
        >
          <div
            className="h-full rounded-full bg-accent transition-[width] duration-200"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {/* The ~300 MB note is HONEST — shown only during a genuine first-run
          voice-model download, with the real running byte count. */}
      {isDownload && (
        <span className="font-ui text-[0.73rem] tabular-nums text-text-faint">
          {progress?.bytesTotal
            ? `${fmtMB(progress.downloaded)} / ${fmtMB(progress.bytesTotal)}`
            : "Downloading voice model (~300 MB, first run only)"}
          {" — this may take a minute."}
        </span>
      )}
    </div>
  );
}
