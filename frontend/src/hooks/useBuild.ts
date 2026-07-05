/**
 * The Build surface orchestrator hook (parallel to useResearch): create a Build
 * conversation, drive its task through the agent loop, and expose the live trace +
 * the gate (confirm/reject) + the kill switch. Components consume only this.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  createBuildConversation,
  killConversation,
  patchConversationSettings,
} from "@/api/agent";
import { agentHttpBase, agentLive } from "@/api/client";
import {
  clearFreshMode,
  markConversationKilled,
  markFreshMode,
} from "@/lib/sessionResume";
import { useVerboseAgentChat } from "@/lib/useVerboseAgentChat";
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
  /** R3: optional large context (the full DR report) sent as a hidden
   *  ENVIRONMENT message alongside the short seedTask, so the model receives it
   *  but the chat history isn't flooded with the whole report. */
  seedContext?: string | null,
) {
  const [session, setSession] = useState<BuildSession | null>(null);
  const [modelId, setModelIdRaw] = useState<string | null>(null); // null → server default
  // Track an explicit user pick so the resume-seed (below) never clobbers it.
  const modelTouched = useRef(false);
  const setModelId = useCallback((id: string | null) => {
    modelTouched.current = true;
    setModelIdRaw(id);
  }, []);
  // Create-time choice: run this build headless (no questions, auto-approve plan).
  // Off by default. Locked once the conversation is created (it's a per-run mode).
  const [autonomousChoice, setAutonomousChoice] = useState(false);
  // Create-time choice: the weak-model ASSIST tier. EXPLICIT opt-in only — never
  // auto-enabled by hosting (a capable local model like Qwen 27B is not sandbagged).
  // Off by default; the user flips it for a genuinely weak model. Locked once work begins.
  const [assistChoice, setAssistChoice] = useState(false);
  const { verbose: verboseChat } = useVerboseAgentChat();
  const quietChoice = !verboseChat;

  // G1/DR-4: pre-created cid for the empty state UploadComposer.
  // BW-08: pre-create is LAZY — it does NOT POST /conversations on mount. The
  // eager mount-create minted a fresh server row for every visit to the empty
  // state (25/25 build + 271/271 agent of those rows had 0 events), flooding
  // History/Projects with "(untitled)" ghosts. Instead the cid is created on a
  // REAL signal: an actual upload (ensurePreCid, called by the paperclip) or
  // submit (which falls through to create.mutate when no preCid exists). The
  // same cid is reused so uploads in the pending session survive the kick.
  const [preCid, setPreCid] = useState<string | null>(null);
  const preCreate = useMutation({
    mutationFn: (opts: { modelOverride: string | null; autonomous: boolean; quiet: boolean }) =>
      createBuildConversation(opts.modelOverride, surface, opts.autonomous, null, opts.quiet),
  });

  const stream = useBuildStream(session);

  // On the resume path, seed the model picker with the conversation's CURRENT pinned
  // model (from /state) so it reflects reality rather than a misleading "default" —
  // unless the user has already picked. One-shot, guarded by modelTouched.
  useEffect(() => {
    if (!resumeCid || !agentLive() || modelTouched.current) return;
    let cancelled = false;
    void fetch(`${agentHttpBase()}/conversations/${resumeCid}/state`)
      .then((r) => r.json())
      .then((s: { model_override?: string | null }) => {
        if (!cancelled && !modelTouched.current && s.model_override) {
          setModelIdRaw(s.model_override);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [resumeCid]);

  // #24 + terminal-state model swap: land the model pick on the backend BEFORE a
  // terminal-state continuation (resume / replan / steer) re-kicks, so the NEW model
  // composes the next turn instead of being silently ignored. The state-aware backend
  // gate accepts the change in terminal/parked states (ERROR/STUCK/FINISHED/PAUSED/IDLE)
  // and 409s mid-run — which we swallow, so this is a safe no-op while RUNNING.
  //
  // TOUCHED-vs-UNTOUCHED: only PATCH when the user actually changed the picker
  // (modelTouched). An untouched picker — including one seeded from /state on resume —
  // means "leave the model as-is", so we send NOTHING (the backend leaves it). When
  // touched we ALWAYS send model_override, INCLUDING when the user explicitly chose
  // DEFAULT (modelId === null) — that is an intentional RESET, and the backend clears the
  // override so the next turn runs the server default (NOT a silent-ignore).
  const applyModelBeforeContinue = useCallback(async () => {
    if (!session?.cid) return;
    if (!modelTouched.current) return; // user didn't change the model → leave it
    const s = stream.status;
    const terminal =
      s === "ERROR" || s === "STUCK" || s === "FINISHED" || s === "PAUSED" || s === "IDLE";
    if (!terminal) return;
    try {
      // modelId may be null here — an EXPLICIT default pick → backend resets to default.
      await patchConversationSettings(session.cid, { modelOverride: modelId });
    } catch {
      /* gate rejected (a run is in flight) — keep the existing model */
    }
  }, [session?.cid, stream.status, modelId]);

  // Resume a terminal/paused conversation — PATCH the (possibly newly-picked) model
  // first so the re-kick composes with it, then call the underlying HTTP resume.
  const resume = useCallback(async () => {
    await applyModelBeforeContinue();
    stream.resume();
  }, [applyModelBeforeContinue, stream.resume]);

  // Re-enter plan mode (the "Plan a change…" composer in the settled state) — same
  // patch-before-kick discipline so a model change before replanning takes effect.
  const requestPlan = useCallback(
    async (text: string) => {
      await applyModelBeforeContinue();
      stream.requestPlan(text);
    },
    [applyModelBeforeContinue, stream.requestPlan],
  );

  // Steer / answer a settled (FINISHED/STUCK/PAUSED) conversation — also honor a
  // pending model change. Mid-run steer hits the early-return (no-op patch) above.
  const steer = useCallback(
    async (text: string) => {
      await applyModelBeforeContinue();
      stream.steer(text);
    },
    [applyModelBeforeContinue, stream.steer],
  );

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
          ? { cid: resumeCid, task: seedTask, context: seedContext ?? null, kick: true }
          : { cid: resumeCid, task: "(resumed)" },
      );
    }
  }, [resumeCid, session, seedTask, seedContext]);

  const create = useMutation({
    mutationFn: (opts: {
      modelOverride: string | null;
      autonomous: boolean;
      assist: boolean;
      quiet: boolean;
    }) =>
      createBuildConversation(
        opts.modelOverride,
        surface,
        opts.autonomous,
        opts.assist,
        opts.quiet,
      ),
  });

  // W-07: lazily obtain the pre-created cid for the Attach affordance so uploads
  // work BEFORE the user submits (and even before the eager mount-create lands).
  // Returns the existing preCid or creates one now (default settings, like the
  // mount path) and stores it so submit() reuses the SAME cid (upload survives).
  const ensurePreCid = useCallback(async () => {
    if (!agentLive()) return null;
    if (preCid) return preCid;
    const cid = await preCreate.mutateAsync({
      modelOverride: null,
      autonomous: false,
      quiet: quietChoice,
    });
    setPreCid(cid);
    return cid;
  }, [preCid, preCreate, quietChoice]);

  const submit = useCallback(
    async (task: string) => {
      const trimmed = task.trim();
      if (!trimmed) return;
      clearFreshMode(surface);
      // G1/DR-4: use pre-created cid if available so uploads survive.
      if (preCid) {
        // runthru-v2 ROOT-1: the preCid was created on mount with DEFAULT settings,
        // so apply the user's CURRENT model pick + autonomous + assist choice to it
        // BEFORE the kick (the loop caches the model at kick). await so the patch lands.
        await patchConversationSettings(preCid, {
          modelOverride: modelId,
          autonomous: autonomousChoice,
          quiet: quietChoice,
          assist: assistChoice,
        });
        setSession({ cid: preCid, task: trimmed, kick: true });
        setPreCid(null);
        return;
      }
      create.mutate(
        {
          modelOverride: modelId,
          autonomous: autonomousChoice,
          assist: assistChoice,
          quiet: quietChoice,
        },
        { onSuccess: (cid) => setSession({ cid, task: trimmed, kick: true }) },
      );
    },
    [create, modelId, autonomousChoice, assistChoice, quietChoice, preCid, surface],
  );

  const kill = useCallback(async () => {
    markConversationKilled(session?.cid);
    markFreshMode(surface);
    stream.cancel(); // reflects locally + (offline) stops the replay
    if (session) await killConversation(session.cid); // live: the hard kill (POST)
  }, [session, stream, surface]);

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
    assistChoice,
    setAssistChoice,
    submit,
    kill,
    reset,
    /** G1/DR-4: pre-created cid for the empty state UploadComposer. null once
     *  a session is live (the cid is on session.cid then) or on the resume path. */
    preCid: session === null && !resumeCid ? preCid : null,
    /** W-07: lazily create-or-return the upload cid so Attach works pre-cid.
     *  Exposed only when a server exists (offline → Attach stays disabled). */
    ensurePreCid: agentLive() ? ensurePreCid : undefined,
    ...stream,
    // Override the stream's continuations with the patch-before-kick variants so a
    // terminal-state model change actually drives the next turn (must come AFTER the
    // `...stream` spread, which carries the un-patched originals).
    resume,
    requestPlan,
    steer,
    answer: steer,
    // A failed create (e.g. backend unreachable) was silent — the surface stayed
    // on the empty state with no signal. Expose it so the UI can show an error.
    submitError: create.error ?? null,
  };
}
