import { useCallback, useSyncExternalStore } from "react";

/** Theme = a `.dark`/`.light` class on <html>. No storage (kept in the DOM). */
export type Theme = "dark" | "light";

function current(): Theme {
  return document.documentElement.classList.contains("light") ? "light" : "dark";
}

const listeners = new Set<() => void>();
function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useSyncExternalStore(subscribe, current, () => "dark" as Theme);
  const toggle = useCallback(() => {
    const root = document.documentElement;
    const next: Theme = current() === "dark" ? "light" : "dark";
    root.classList.remove("dark", "light");
    root.classList.add(next);
    for (const cb of listeners) cb();
  }, []);
  return { theme, toggle };
}
