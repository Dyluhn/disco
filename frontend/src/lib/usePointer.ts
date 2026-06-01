import { useSyncExternalStore } from "react";

/** True on coarse-pointer (touch) devices → citations open as a bottom-sheet
 * instead of a hover card (BoD §13.3 mobile behavior). */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    (cb) => {
      const mq = window.matchMedia("(pointer: coarse)");
      mq.addEventListener("change", cb);
      return () => mq.removeEventListener("change", cb);
    },
    () => window.matchMedia("(pointer: coarse)").matches,
    () => false,
  );
}
