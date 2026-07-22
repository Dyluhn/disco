/**
 * Whether the Build inspector should follow a newly-started file write to the
 * Files tab. This is a local UI preference: it never changes the agent loop or
 * the event stream, and defaults ON to preserve the existing watch-it-write
 * behavior.
 */

import { useCallback, useSyncExternalStore } from "react";

const STORAGE_KEY = "disco-snap-to-action";

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  for (const listener of listeners) listener();
}

export function readSnapToAction(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored == null ? true : stored === "1";
  } catch {
    return true;
  }
}

export function setSnapToAction(enabled: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, enabled ? "1" : "0");
  } catch {
    // Storage can be unavailable in hardened/private browser contexts. Leave
    // the preference at its readable default rather than breaking the UI.
  }
  emit();
}

export function useSnapToAction(): {
  snapToAction: boolean;
  setSnapToAction: (enabled: boolean) => void;
} {
  const snapToAction = useSyncExternalStore(
    subscribe,
    readSnapToAction,
    () => true,
  );
  const setPreference = useCallback(
    (enabled: boolean) => setSnapToAction(enabled),
    [],
  );
  return { snapToAction, setSnapToAction: setPreference };
}
