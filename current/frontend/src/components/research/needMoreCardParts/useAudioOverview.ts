/**
 * NeedMoreCard — audio-overview generation hook, extracted from AudioSection.
 *
 * Owns the `audio` state machine, `generate`, and `reset`. Contains NO stream
 * parsing and NO raw transport — it calls `streamReportAudio` /
 * `requestReportAudioBlocking` / `absoluteAudioUrl` from the api layer
 * (Amendment A3: components/hooks may not use `fetch`/WebSocket/XHR directly).
 * The `audioReasonMessage` human-message mapping stays UI-side.
 */

import { useCallback, useEffect, useState } from "react";
import {
  absoluteAudioUrl,
  fetchExistingReportAudio,
  requestReportAudioBlocking,
  streamReportAudio,
  type AudioStreamEvent,
} from "@/api/deepResearch";
import {
  audioReasonDetail,
  audioReasonMessage,
  type AudioMode,
  type AudioStage,
  type AudioState,
} from "./audioProgress";

export interface UseAudioOverviewOptions {
  cid: string;
  /** Selected follow-up seqs to include in the audio (WALK-20). */
  followUpSeqs?: number[];
  /** Called with `false` right when generation starts, so the caller can
   *  close the mode-pick dialog (mirrors the former inline `generate`). */
  onModeOpenChange: (open: boolean) => void;
}

export interface UseAudioOverviewResult {
  audio: AudioState;
  /** Run the pipeline. `force` re-runs it even when the server already holds
   *  an artifact for this exact report — what "Regenerate" needs, since the
   *  cache key is a hash of the report and would otherwise hand the same file
   *  straight back. */
  generate: (mode: AudioMode, options?: { force?: boolean }) => Promise<void>;
  reset: () => void;
}

/** Adopt whatever audio the server already holds for this conversation.
 *
 * The MP3 and its transcript live on the agent-server's data volume, so an
 * overview generated earlier survives both a page reload and a server restart.
 * Without asking on mount, a report that HAD audio came back showing only the
 * "Audio Overview" button — no player, no Export MP3, no Regenerate — and
 * pressing the button just handed back the cached file a few seconds later
 * (UI-42). Only ever moves `idle` → `done`: a run started in the meantime wins.
 */
function useRestoreExistingAudio(cid: string, setAudio: (fn: (s: AudioState) => AudioState) => void) {
  useEffect(() => {
    let cancelled = false;
    void fetchExistingReportAudio(cid).then((existing) => {
      const newest = existing[0];
      if (cancelled || !newest) return;
      setAudio((current) =>
        current.status === "idle"
          ? {
              status: "done",
              audioUrl: absoluteAudioUrl(newest.mp3_url),
              note: newest.note || undefined,
              mode: newest.mode === "single" ? "single" : "podcast",
            }
          : current,
      );
    });
    return () => {
      cancelled = true;
    };
  }, [cid, setAudio]);
}

/** Build the `onEvent` handler passed to `streamReportAudio`: maps a parsed
 * SSE frame to an `AudioState` update, returning true once a terminal (done /
 * error) event has been handled. Factored out of `generate` so `generate`'s
 * own branching stays trivially small — this closure carries its own mccabe
 * budget as a nested callable. */
function makeStreamEventHandler(
  setAudio: (s: AudioState) => void,
): (ev: AudioStreamEvent) => boolean {
  return (ev: AudioStreamEvent): boolean => {
    if (ev.stage === "done" && ev.mp3_url) {
      setAudio({
        status: "done",
        audioUrl: absoluteAudioUrl(ev.mp3_url),
        note: ev.note || undefined,
        mode: ev.mode === "single" ? "single" : "podcast",
      });
      return true;
    }
    if (ev.stage === "error") {
      setAudio({
        status: "unavailable",
        reason: audioReasonMessage(ev.reason ?? "unknown", ev.message),
        detail: audioReasonDetail(ev.reason ?? "unknown", ev.detail),
      });
      return true;
    }
    if (ev.stage) {
      setAudio({
        status: "generating",
        progress: {
          stage: ev.stage as AudioStage,
          current: ev.current,
          total: ev.stage === "downloading_model" ? undefined : ev.total,
          downloaded: ev.downloaded,
          bytesTotal: ev.stage === "downloading_model" ? ev.total : undefined,
          pct: ev.pct,
          fileIndex: ev.file_index,
          fileTotal: ev.file_total,
        },
      });
    }
    return false;
  };
}

// Audio overview calls the agent-server endpoint directly (via the api layer)
// so that the `mode` query param can be threaded through. The mode is chosen
// via a popup dialog before generation starts (WALK-21 / D3).
//
// W-09: prefer the SSE streaming endpoint so the UI shows REAL staged progress
// ("Synthesizing turns 3/8…", "Mixing…"). The blocking POST is kept as a
// fallback (W-08/W-09 robustness): if streaming can't start or yields no
// terminal event, we re-issue the blocking request.
export function useAudioOverview({
  cid,
  followUpSeqs,
  onModeOpenChange,
}: UseAudioOverviewOptions): UseAudioOverviewResult {
  const [audio, setAudio] = useState<AudioState>({ status: "idle" });
  useRestoreExistingAudio(cid, setAudio);

  const generate = useCallback(
    async (mode: AudioMode, options?: { force?: boolean }) => {
      onModeOpenChange(false);
      setAudio({ status: "generating" });

      const bodyPayload =
        followUpSeqs && followUpSeqs.length > 0
          ? JSON.stringify({ follow_up_seqs: followUpSeqs })
          : undefined;

      try {
        const force = options?.force ?? false;
        const streamed = await streamReportAudio(
          cid,
          mode,
          bodyPayload,
          makeStreamEventHandler(setAudio),
          force,
        );
        if (!streamed) {
          const data = await requestReportAudioBlocking(cid, mode, bodyPayload, force);
          setAudio({
            status: "done",
            audioUrl: absoluteAudioUrl(data.mp3_url),
            note: data.note || undefined,
            mode,
          });
        }
      } catch (e: unknown) {
        const reason = e instanceof Error ? e.message : String(e);
        setAudio({ status: "unavailable", reason });
      }
    },
    [cid, followUpSeqs, onModeOpenChange],
  );

  const reset = useCallback(() => setAudio({ status: "idle" }), []);

  return { audio, generate, reset };
}
