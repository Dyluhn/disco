/**
 * The Build surface orchestrator hook (parallel to useResearch): create a Build
 * conversation, drive its task through the agent loop, and expose the live trace +
 * the gate (confirm/reject) + the kill switch. Components consume only this.
 */

import { useCallback, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { createBuildConversation, killConversation } from "@/api/agent";
import { useBuildStream, type BuildSession } from "./useBuildStream";

/** Opens a build-like conversation. `surface` is "build" (software framing) or
 * "agent" (general-task framing) — identical machinery. If `resumeCid` is given
 * (the /build/:cid or /agent/:cid route), skip the create call and connect to the
 * existing conversation — the WebSocket replays history-then-live, so prior events
 * restore (the stored surface is authoritative; resume never re-sets it). */
export function useBuild(resumeCid?: string | null, surface: "build" | "agent" = "build") {
  const [session, setSession] = useState<BuildSession | null>(null);
  const [modelId, setModelId] = useState<string | null>(null); // null → server default
  // Create-time choice: run this build headless (no questions, auto-approve plan).
  // Off by default. Locked once the conversation is created (it's a per-run mode).
  const [autonomousChoice, setAutonomousChoice] = useState(false);
  const stream = useBuildStream(session);

  // Resume path: when a route param hands us a cid, jump straight in. The
  // task label is informational on resume; the loop already has its history.
  useEffect(() => {
    if (resumeCid && (session === null || session.cid !== resumeCid)) {
      setSession({ cid: resumeCid, task: "(resumed)" });
    }
  }, [resumeCid, session]);

  const create = useMutation({
    mutationFn: (opts: { modelOverride: string | null; autonomous: boolean }) =>
      createBuildConversation(opts.modelOverride, surface, opts.autonomous),
  });

  const submit = useCallback(
    (task: string) => {
      const trimmed = task.trim();
      if (!trimmed) return;
      create.mutate(
        { modelOverride: modelId, autonomous: autonomousChoice },
        { onSuccess: (cid) => setSession({ cid, task: trimmed, kick: true }) },
      );
    },
    [create, modelId, autonomousChoice],
  );

  const kill = useCallback(async () => {
    stream.cancel(); // reflects locally + (offline) stops the replay
    if (session) await killConversation(session.cid); // live: the hard kill (POST)
  }, [session, stream]);

  const reset = useCallback(() => setSession(null), []);

  return {
    started: session !== null,
    cid: session?.cid ?? null,
    task: session?.task ?? null,
    resumed: Boolean(resumeCid),
    submitting: create.isPending,
    modelId,
    setModelId,
    autonomousChoice,
    setAutonomousChoice,
    submit,
    kill,
    reset,
    ...stream,
    // A failed create (e.g. backend unreachable) was silent — the surface stayed
    // on the empty state with no signal. Expose it so the UI can show an error.
    submitError: create.error ?? null,
  };
}
