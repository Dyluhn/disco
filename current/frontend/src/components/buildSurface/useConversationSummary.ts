/**
 * BP-15 + W-01: fetch a conversation's sandbox backend name + stored (auto-titled)
 * title once the cid is known. useBuildStream doesn't expose sandbox_backend yet,
 * so this reads it once via the api layer; the backend name + title are stable for
 * a run's lifetime. Amendment A3: routes through `@/api/agent` (never `@/api/client`
 * directly) so this hook stays clear of the `raw_transport_in_component_or_hook` /
 * client-import boundary.
 */

import { useEffect, useState } from "react";
import { fetchConversationSummary as fetchSummary } from "@/api/agent";

export function useConversationSummary(cid: string | null) {
  const [sandboxBackend, setSandboxBackend] = useState<string | null>(null);
  const [storedTitle, setStoredTitle] = useState<string | null>(null);

  useEffect(() => {
    if (!cid) return;
    void fetchSummary(cid).then((summary) => {
      if (!summary) return;
      setSandboxBackend(summary.sandboxBackend);
      setStoredTitle(summary.title);
    });
  }, [cid]);

  return { sandboxBackend, storedTitle };
}
