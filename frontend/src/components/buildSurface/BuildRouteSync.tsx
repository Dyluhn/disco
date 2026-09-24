import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";

/** Once the server has accepted the first instruction, reload must reopen it. */
export function BuildRouteSync({ cid, resumeCid, framing, hasPersistedEvents }: {
  cid: string | null;
  resumeCid?: string | null;
  framing: "build" | "agent";
  hasPersistedEvents: boolean;
}) {
  const navigate = useNavigate();
  const urlCarriesRun = useRef<string | null>(null);
  useEffect(() => {
    if (resumeCid) {
      urlCarriesRun.current = resumeCid;
      return;
    }
    // Creation alone is not enough: navigating would unmount the socket while
    // its first send_message is still queued, leaving an empty conversation.
    if (!cid || !hasPersistedEvents || urlCarriesRun.current === cid) return;
    urlCarriesRun.current = cid;
    navigate(`/${framing}/${cid}`, { replace: true });
  }, [cid, resumeCid, framing, hasPersistedEvents, navigate]);
  return null;
}
