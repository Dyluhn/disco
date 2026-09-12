import { useQuery } from "@tanstack/react-query";
import { getAppServerHealth, getDiagnostics } from "@/api/diagnostics";
import type { AppServerHealth, Diagnostics } from "@/types/diagnostics";

export const DIAGNOSTICS_KEY = ["diagnostics"] as const;
export const APP_HEALTH_KEY = ["app-server-health"] as const;

export function useDiagnostics() {
  return useQuery<Diagnostics>({
    queryKey: DIAGNOSTICS_KEY,
    queryFn: getDiagnostics,
    staleTime: 10_000,
    refetchOnWindowFocus: true,
  });
}

export function useAppServerHealth() {
  return useQuery<AppServerHealth>({
    queryKey: APP_HEALTH_KEY,
    queryFn: getAppServerHealth,
    staleTime: 10_000,
    refetchOnWindowFocus: true,
  });
}
