import { createContext, useContext } from "react";

/**
 * Two-level mode model.
 *
 * TOP LEVEL (the slider, the single source of mode state): exactly two modes —
 * Search (live) and Build (dormant/SOON). The slider is both the indicator and
 * the selector; there is no separate mode indicator and no mode options on the
 * model pill.
 *
 * CHILD LEVEL (the in-chat scope control): a per-mode option set surfaced by ONE
 * reusable control whose options swap with the active mode. Search → Standard
 * (default) + Deep Research (dormant). Build (future) → Site / Desktop / Mobile /
 * Other. Deep Research is a CHILD of Search, never a top-level mode.
 */
export type Mode = "search" | "build";

export interface ModeMeta {
  id: Mode;
  label: string; // lowercase per the slider design
  dormant: boolean;
}

export const MODES: ModeMeta[] = [
  { id: "search", label: "search", dormant: false },
  { id: "build", label: "build", dormant: true },
];

export function modeMeta(id: Mode): ModeMeta {
  return MODES.find((m) => m.id === id) ?? MODES[0];
}

// ---- the in-chat scope (a function of the active top-level mode) -------------

export type ScopeId =
  | "standard"
  | "deep_research" // Search children
  | "site"
  | "desktop"
  | "mobile"
  | "other"; // Build children (future)

export interface ScopeOption {
  id: ScopeId;
  label: string;
  dormant: boolean;
  /** Build's "Other" prompts the user to specify on the first turn. */
  promptOnFirstTurn?: boolean;
}

/** The option set per mode — same shape across modes so one control renders both. */
export const SCOPE_OPTIONS: Record<Mode, ScopeOption[]> = {
  search: [
    { id: "standard", label: "Standard", dormant: false },
    { id: "deep_research", label: "Deep Research", dormant: true },
  ],
  build: [
    { id: "site", label: "Site", dormant: true },
    { id: "desktop", label: "Desktop Application", dormant: true },
    { id: "mobile", label: "Mobile Application", dormant: true },
    { id: "other", label: "Other", dormant: true, promptOnFirstTurn: true },
  ],
};

/** The default (first non-dormant, else first) scope for a mode. */
export function defaultScope(mode: Mode): ScopeId {
  const opts = SCOPE_OPTIONS[mode];
  return (opts.find((o) => !o.dormant) ?? opts[0]).id;
}

// ---- context ----------------------------------------------------------------

export interface ModeContextValue {
  mode: Mode;
  /** Sets the mode. Dormant modes are ignored (Build isn't switchable yet). */
  setMode: (mode: Mode) => void;
  modes: ModeMeta[];
}

export const ModeContext = createContext<ModeContextValue | null>(null);

export function useMode(): ModeContextValue {
  const ctx = useContext(ModeContext);
  if (!ctx) throw new Error("useMode must be used within a ModeProvider");
  return ctx;
}
