import { useQuery } from "@tanstack/react-query";
import { getActivity } from "@/api/activity";
import type { ActivityFeed } from "@/types/activity";

const ACTIVITY_KEY = ["activity"] as const;

/** The background-task dashboard feed. Polled (running tasks change without a user
 * action), so both the Activity view and the NavRail "N running" badge stay live off
 * ONE shared query — TanStack dedupes the interval across consumers. */
export function useActivity() {
  return useQuery<ActivityFeed>({
    queryKey: ACTIVITY_KEY,
    queryFn: getActivity,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
}

/** Just the live running-task count (for the global indicator) — 0 until loaded. */
export function useRunningCount(): number {
  const { data } = useActivity();
  return data?.counts.running ?? 0;
}
