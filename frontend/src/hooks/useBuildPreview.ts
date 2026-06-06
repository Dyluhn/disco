/**
 * The backend-aware live preview for a Build conversation. Polls the agent-server (the
 * agent's dev server appears mid-run, so a one-shot fetch would miss it) for the URL to
 * iframe — or the honest reason it's unavailable. Components consume only this hook.
 */

import { useQuery } from "@tanstack/react-query";
import { getPreview } from "@/api/agent";
import type { PreviewInfo } from "@/types/agent";

export function useBuildPreview(cid: string | null, active: boolean) {
  return useQuery<PreviewInfo>({
    queryKey: ["build-preview", cid],
    queryFn: () => getPreview(cid!),
    enabled: !!cid,
    // poll while the run is live (the dev server may not be up yet); ease off when idle.
    refetchInterval: active ? 4000 : false,
  });
}
