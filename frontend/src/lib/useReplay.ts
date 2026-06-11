/**
 * RP-06 — useReplay: step-through cursor over the raw AgentEvent[] log.
 *
 * Components pass the sliced window `events.slice(0, position)` into the
 * pure buildTrace selectors (deriveActivity, deriveFiles, deriveTerminal,
 * deriveDeliverable, derivePlan, latestAgentMessage, deriveLiveSignal,
 * derivePlanProgress). Zero reducer changes — the selectors already accept
 * any AgentEvent[] subset.
 *
 * When `live` is true (the conversation is RUNNING), the cursor auto-tracks
 * the tail — the component should hide the scrubber in that state.
 * When `live` is false, the cursor starts at the tail (full history visible)
 * and the user can scrub backward through the event log.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentEvent } from "@/types/agent";

export interface ReplayState {
  /** 0-based cursor position (how many events to include in the window). */
  position: number;
  /** Total number of events (events.length). */
  max: number;
  /** True when position === max (the viewer is at the live tail). */
  atLive: boolean;
  /** Jump to an exact index (clamped to [0, max]). */
  seek: (n: number) => void;
  /** Step forward one event. */
  stepFwd: () => void;
  /** Step backward one event. */
  stepBack: () => void;
}

export function useReplay(events: AgentEvent[], live: boolean): ReplayState {
  const max = events.length;
  const [position, setPosition] = useState(() =>
    // Start at tail on mount when not live (full trace visible by default).
    // When live, position 0 is fine — the effect below immediately tracks to max.
    live ? 0 : max,
  );

  // Track when we last saw max so we can distinguish "events arrived for the
  // first time in non-live mode" from "user scrubbed back, then events grew."
  const lastMaxRef = useRef(max);

  useEffect(() => {
    if (live) {
      // Auto-track the tail during a live run.
      setPosition(max);
    } else if (max > lastMaxRef.current) {
      // Events grew while in non-live mode (e.g. resume, or first load of a
      // finished conversation). Jump to the new tail so the user sees the full
      // trace — scrubbing is for going BACK from a complete view.
      setPosition(max);
    }
    lastMaxRef.current = max;
  }, [live, max]);

  const seek = useCallback(
    (n: number) => setPosition(Math.max(0, Math.min(n, max))),
    [max],
  );

  const stepFwd = useCallback(
    () => setPosition((p) => Math.min(p + 1, max)),
    [max],
  );

  const stepBack = useCallback(() => setPosition((p) => Math.max(0, p - 1)), []);

  const atLive = position >= max;

  return { position, max, atLive, seek, stepFwd, stepBack };
}
