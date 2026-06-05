/**
 * The driver-eligible models for the Build chat model picker (from the agent-server).
 * A TanStack Query hook so components never call the api layer directly.
 */

import { useQuery } from "@tanstack/react-query";
import { listDriverModels } from "@/api/agent";
import type { DriverModels } from "@/types/agent";

export function useDriverModels() {
  return useQuery<DriverModels>({
    queryKey: ["driver-models"],
    queryFn: listDriverModels,
    staleTime: 60_000,
  });
}
