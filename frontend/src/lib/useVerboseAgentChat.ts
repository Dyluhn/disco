/**
 * W-43 — "Verbose Agent Chat" preference.
 *
 * Default ON. When ON the Build/Agent control pane shows the full step-by-step
 * ActivityFeed. When OFF (and once the first plan is approved) the feed collapses to
 * a compact AgentStageCard (planning/reading/executing/… with a click-to-expand),
 * while the gates (confirm/decision/question) and the deliverable/download affordances
 * still render. The full narrative — and every inline artifact it carries — stays
 * reachable via the stage card's expand and the inspector's "Agent History" tab.
 *
 * Persisted in localStorage as `verboseAgentChat` ("1" | "0"). Shared across the
 * Settings toggle and the surface via a module-level listener set so a change in one
 * place updates the other live (mirrors `useTheme`).
 *
 * @module useVerboseAgentChat
 */

import { useCallback, useSyncExternalStore } from "react";

const STORAGE_KEY = "verboseAgentChat";

const listeners = new Set<() => void>();
function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}
function emit(): void {
  for (const cb of listeners) cb();
}

/** Read the current preference. Default ON (true) when unset or unreadable. */
export function readVerboseAgentChat(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v == null ? true : v === "1";
  } catch {
    return true;
  }
}

/** Persist the preference and notify all subscribers (same-tab live update). */
export function setVerboseAgentChat(on: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, on ? "1" : "0");
  } catch {
    /* private mode / disabled storage — fine, just won't persist */
  }
  emit();
}

export function useVerboseAgentChat(): { verbose: boolean; setVerbose: (on: boolean) => void } {
  const verbose = useSyncExternalStore(subscribe, readVerboseAgentChat, () => true);
  const setVerbose = useCallback((on: boolean) => setVerboseAgentChat(on), []);
  return { verbose, setVerbose };
}
