/**
 * The Build surface orchestrator hook (parallel to useResearch): create a Build
 * conversation, drive its task through the agent loop, and expose the live trace +
 * the gate (confirm/reject) + the kill switch. Components consume only this.
 */

import { useCallback, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { createBuildConversation, killConversation } from "@/api/agent";
import { agentLive } from "@/api/client";
import { useBuildStream, type BuildSession } from "./useBuildStream";

/** Opens a build-like conversation. `surface` is "build" (software framing) or
 * "agent" (general-task framing) — identical machinery. If `resumeCid` is given
 * (the /build/:cid or /agent/:cid route), skip the create call and connect to the
 * existing conversation — the WebSocket replays history-then-live, so prior events
 * restore (the stored surface is authoritative; resume never re-sets it). */
export function useBuild(
  resumeCid?: string | null,
  surface: "build" | "agent" = "build",
  /** A5: a seeded handoff. When the /build/:cid route is opened with a seedTask
   *  (e.g. "build a deck from this report"), KICK it once instead of a plain resume.
   *  The conversation was already created (autonomous) by the handoff caller. */
  seedTask?: string | null,
) {
  const [session, setSession] = useState<BuildSession | null>(null);
  const [modelId, setModelId] = useState<string | null>(null); // null → server default
  // Create-time choice: run this build headless (no questions, auto-approve plan).
  // Off by default. Locked once the conversation is created (it's a per-run mode).
  const [autonomousChoice, setAutonomousChoice] = useState(false);

  // G1/DR-4: pre-created cid for the empty state UploadComposer.
  // Created eagerly on mount with default settings so a paperclip is rendered
  // before the user types anything. On submit, this cid is USED instead of
  // creating a new one, so uploads already in the pending session survive.
  const [preCid, setPreCid] = useState<string | null>(null);
  const preCreate = useMutation({
    mutationFn: (opts: { modelOverride: string | null; autonomous: boolean }) =>
      createBuildConversation(opts.modelOverride, surface, opts.autonomous),
  });

  useEffect(() => {
    if (resumeCid || session !== null || !agentLive()) return;
    if (preCid) return; // already pre-created; don't re-create
    preCreate.mutate(
      { modelOverride: null, autonomous: false },
      { onSuccess: setPreCid },
    );
  }, [resumeCid, session]); // eslint-disable-line react-hooks/exhaustive-deps

  const stream = useBuildStream(session);

  // Resume path: when a route param hands us a cid, jump straight in. The
  // task label is informational on resume; the loop already has its history.
  useEffect(() => {
    if (resumeCid && (session === null || session.cid !== resumeCid)) {
      // A5: a seeded handoff (seedTask) KICKS the loop once with the report markdown;
      // a plain resume just subscribes/replays (view ≠ start). The session guard
      // (cid !== resumeCid) prevents a re-kick on re-render; a later refresh loses the
      // router state → plain resume (the build already ran), never a double-seed.
      setSession(
        seedTask
          ? { cid: resumeCid, task: seedTask, kick: true }
          : { cid: resumeCid, task: "(resumed)" },
      );
    }
  }, [resumeCid, session, seedTask]);

  const create = useMutation({
    mutationFn: (opts: { modelOverride: string | null; autonomous: boolean }) =>
      createBuildConversation(opts.modelOverride, surface, opts.autonomous),
  });

  const submit = useCallback(
    (task: string) => {
      const trimmed = task.trim();
      if (!trimmed) return;
      // G1/DR-4: use pre-created cid if available so uploads survive.
      if (preCid) {
        setSession({ cid: preCid, task: trimmed, kick: true });
        setPreCid(null);
        return;
      }
      create.mutate(
        { modelOverride: modelId, autonomous: autonomousChoice },
        { onSuccess: (cid) => setSession({ cid, task: trimmed, kick: true }) },
      );
    },
    [create, modelId, autonomousChoice, preCid],
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
    /** G1/DR-4: pre-created cid for the empty state UploadComposer. null once
     *  a session is live (the cid is on session.cid then) or on the resume path. */
    preCid: session === null && !resumeCid ? preCid : null,
    ...stream,
    // A failed create (e.g. backend unreachable) was silent — the surface stayed
    // on the empty state with no signal. Expose it so the UI can show an error.
    submitError: create.error ?? null,
  };
}
