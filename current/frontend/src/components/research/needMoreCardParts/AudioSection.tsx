/**
 * NeedMoreCard — audio overview section (extracted from NeedMoreCard.tsx).
 * Purely presentational: all state + generation logic lives in
 * `useAudioOverview`; this renders the mode dialog + the four status
 * branches (generating / done / unavailable / implicit idle).
 */

import { ArrowDown, Headphones } from "lucide-react";
import { AudioPlayer } from "../AudioPlayer";
import { AudioModeDialog } from "./AudioModeDialog";
import { AudioProgressRow } from "./AudioProgressRow";
import { sanitizeFilename } from "./filename";
import { CTRL_BTN } from "./styles";
import { useAudioOverview } from "./useAudioOverview";

// ── Audio section ─────────────────────────────────────────────────────────────

export interface AudioSectionProps {
  cid: string;
  /** Selected follow-up seqs to include in the audio (WALK-20). */
  followUpSeqs?: number[];
  /** Controlled: whether the mode-pick dialog is open (parent drives this
   *  when it needs to insert the include-follow-ups step first). */
  modeOpen: boolean;
  onModeOpenChange: (open: boolean) => void;
  /** B4: report title/query for a meaningful download filename. */
  reportQuery: string;
}

export function AudioSection({
  cid,
  followUpSeqs,
  modeOpen,
  onModeOpenChange,
  reportQuery,
}: AudioSectionProps) {
  const { audio, generate, reset } = useAudioOverview({ cid, followUpSeqs, onModeOpenChange });

  // B3: AudioModeDialog is ALWAYS rendered (outside the status branches) so
  // it remains reachable after audio is generated, not just in the idle branch.
  // Radix Dialog handles open/close via the `open` prop; unmounting it would
  // lose any open-animation state and break the dialog after first generation.
  return (
    // Gap #46: keep both lifecycle and the live server stage on one stable
    // marker. The visible progress row is intentionally transient, so putting
    // `data-tts-stage` only on that child made external observers lose the
    // stage as soon as the row was replaced by the player.
    <div
      className="contents"
      data-tts-state={audio.status}
      data-tts-stage={audio.status === "generating" ? audio.progress?.stage ?? "" : ""}
    >
      <AudioModeDialog
        open={modeOpen}
        onOpenChange={onModeOpenChange}
        onChoose={generate}
      />

      {audio.status === "generating" && <AudioProgressRow progress={audio.progress} />}

      {audio.status === "done" && (
        // Real play + export affordances — gated on a real audio URL, never a fake.
        <div className="flex flex-wrap items-center gap-inline">
          {/* WALK-14 / D2: custom AudioPlayer instead of native <audio controls> */}
          <AudioPlayer src={audio.audioUrl} className="max-w-[18rem] flex-1" />
          <button
            type="button"
            onClick={() => {
              // B4: filename derived from report title, not hardcoded "audio-overview.mp3".
              const a = document.createElement("a");
              a.href = audio.audioUrl;
              a.download = sanitizeFilename(reportQuery) + ".mp3";
              document.body.appendChild(a);
              a.click();
              a.remove();
            }}
            data-disco-control="dr.audio.export"
            className={CTRL_BTN}
          >
            <ArrowDown className="size-3.5" aria-hidden />
            Export MP3
          </button>
          <button
            type="button"
            onClick={reset}
            data-disco-control="dr.audio.regenerate"
            className={CTRL_BTN}
          >
            <Headphones className="size-3.5" aria-hidden />
            Regenerate
          </button>
        </div>
      )}

      {audio.status === "unavailable" && (
        // Honest error, NO false success, resettable
        <div className="flex flex-col gap-hair">
          <div className="flex flex-wrap items-center gap-inline">
            <Headphones className="size-3.5 shrink-0 text-text-faint" aria-hidden />
            <span className="font-ui text-[0.78rem] text-text-faint">{audio.reason}</span>
            <button
              type="button"
              onClick={reset}
              aria-label="Reset audio overview state"
              className={CTRL_BTN}
            >
              Try again
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
