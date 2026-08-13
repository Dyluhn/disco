/**
 * The backend-aware live preview for a Build conversation. Polls the agent-server (the
 * agent's dev server appears mid-run, so a one-shot fetch would miss it) for the URL to
 * iframe — or the honest reason it's unavailable. Components consume only this hook.
 */

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { getPreview } from "@/api/agent";
import type { PreviewInfo } from "@/types/agent";

export function useBuildPreview(cid: string | null, active: boolean) {
  const query = useQuery<PreviewInfo>({
    queryKey: ["build-preview", cid],
    queryFn: () => getPreview(cid!),
    enabled: !!cid,
    // Poll only while runtime state may change. Terminal transitions receive one
    // explicit final fetch below, then the shared cache freezes until a resume.
    refetchInterval: active ? 4000 : false,
    staleTime: active ? 1000 : Infinity,
    // A remounted terminal conversation may have been restored/rebound by
    // another surface while this component was absent. Fetch once on mount;
    // Infinity still prevents background terminal polling afterward.
    refetchOnMount: "always",
  });
  const refetch = query.refetch;
  const prior = useRef({ cid, active });
  useEffect(() => {
    const previous = prior.current;
    prior.current = { cid, active };
    if (cid && previous.cid === cid && previous.active && !active) {
      void refetch();
    }
  }, [active, cid, refetch]);
  return query;
}
