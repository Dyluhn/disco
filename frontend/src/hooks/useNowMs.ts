/**
 * A ms-epoch clock that re-renders on an interval.
 *
 * The Deep Research run UI shows ages ("quiet for 35 s") and countdowns
 * ("resumes in 3:40") that must move while nothing arrives from the server —
 * that is the whole point: a screen that stops changing when the backend stops
 * talking is exactly the lie this lane removes. The clock is CLIENT-side and
 * measures only elapsed time; it never invents backend progress.
 *
 * Pass `active: false` when the run is over so a finished report is not holding
 * a 1 Hz timer open.
 */
import { useEffect, useState } from "react";

export function useNowMs(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return now;
}
