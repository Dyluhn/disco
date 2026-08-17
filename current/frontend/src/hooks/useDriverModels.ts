/**
 * The driver-eligible models for the Build chat model picker (from the agent-server).
 * A TanStack Query hook so components never call the api layer directly.
 */

import { useQuery } from "@tanstack/react-query";
import { getLastSelectedModel, listDriverModels } from "@/api/agent";
import type { DriverModels } from "@/types/agent";

export function useDriverModels() {
  return useQuery<DriverModels>({
    queryKey: ["driver-models"],
    queryFn: listDriverModels,
    staleTime: 60_000,
  });
}

/** P3 — the globally-persisted last-picked driver model. Returns null when no
 * pick has ever been made (the pill defaults to the settings assignment then).
 * Short stale time so a pick in one tab surfaces in the next conversation. */
export function useLastSelectedModel() {
  return useQuery<string | null>({
    queryKey: ["last-selected-model"],
    queryFn: getLastSelectedModel,
    staleTime: 5_000,
  });
}
