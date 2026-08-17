import { useCallback, useMemo, useState, type ReactNode } from "react";
import { type Mode, MODES, ModeContext, type ModeContextValue, modeMeta } from "./mode";

/**
 * Provides the session-local interaction-mode state to the shell (indicator) and
 * the main surface (selector). No browser storage, per the data discipline.
 */
export function ModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<Mode>("search");

  const setMode = useCallback((next: Mode) => {
    // Guard: dormant modes are not selectable. The selector also disables them,
    // but enforce here so the invariant holds regardless of caller.
    if (modeMeta(next).dormant) return;
    setModeState(next);
  }, []);

  const value = useMemo<ModeContextValue>(() => ({ mode, setMode, modes: MODES }), [mode, setMode]);

  return <ModeContext.Provider value={value}>{children}</ModeContext.Provider>;
}
