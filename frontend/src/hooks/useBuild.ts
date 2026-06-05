/**
 * The Build surface orchestrator hook (parallel to useResearch): create a Build
 * conversation, drive its task through the agent loop, and expose the live trace +
 * the gate (confirm/reject) + the kill switch. Components consume only this.
 */

import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { createBuildConversation, killConversation } from "@/api/agent";
import { useBuildStream, type BuildSession } from "./useBuildStream";

export function useBuild() {
  const [session, setSession] = useState<BuildSession | null>(null);
  const stream = useBuildStream(session);

  const create = useMutation({ mutationFn: createBuildConversation });

  const submit = useCallback(
    (task: string) => {
      const trimmed = task.trim();
      if (!trimmed) return;
      create.mutate(undefined, { onSuccess: (cid) => setSession({ cid, task: trimmed }) });
    },
    [create],
  );

  const kill = useCallback(async () => {
    stream.cancel(); // reflects locally + (offline) stops the replay
    if (session) await killConversation(session.cid); // live: the hard kill (POST)
  }, [session, stream]);

  const reset = useCallback(() => setSession(null), []);

  return {
    started: session !== null,
    task: session?.task ?? null,
    submitting: create.isPending,
    submit,
    kill,
    reset,
    ...stream,
  };
}
