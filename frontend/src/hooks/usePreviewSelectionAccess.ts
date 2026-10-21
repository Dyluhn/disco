import { useCallback, useRef, useState } from "react";

/** Expiry also disables host selection overlays above the retained iframe. */
export function usePreviewSelectionAccess(disarm: () => void) {
  const [expired, setExpired] = useState(false);
  const actions = useRef(disarm);
  actions.current = disarm;
  const onExpiredChange = useCallback((next: boolean) => {
    setExpired(next);
    if (next) {
      actions.current();
    }
  }, []);
  return { expired, onExpiredChange };
}
