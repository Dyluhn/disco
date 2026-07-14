/**
 * Deep Research orchestrator hook (parallel to useBuild + useResearch):
 * creates a deep_research conversation, drives its run through the agent
 * loop's plan-mode flow, exposes the derived state + lifecycle controls.
 *
 * LIFECYCLE MODEL (universal background-task pattern):
 *   - The run is a SERVER-SIDE resource (cid + persisted event log). It survives
 *     navigation away; closing the surface never kills it.
 *   - VIEW (open from History / a /deep/:cid route) is a SAFE read: subscribe +
 *     replay only — it NEVER starts the run (Command–Query Separation). No
 *     localStorage stash; selecting the deep-research scope = a fresh compose.
 *   - START is the only path that kicks (a fresh `submit()`).
 *   - CONTROL is explicit: stop (pause), kill (end), resume (continue), retry.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  createDeepResearchConversation,
  exportReportAsMarkdown,
  // Aliased: a local legacy `exportReport` (the MD-only button) used to SHADOW this
  // import, so pdf exports silently ran the markdown exporter instead.
  exportReport as exportReportApi,
  type ReportExportFmt,
} from "@/api/deepResearch";
import { killConversation, patchConversationSettings } from "@/api/agent";
import { agentLive } from "@/api/client";
import { markConversationKilled } from "@/lib/sessionResume";
import type { MessageEvent } from "@/types/agent";
import {
  useDeepResearchStream,
  type DeepResearchSession,
} from "./useDeepResearchStream";

type Tier = "quick" | "standard_deep" | "exhaustive";

export function useDeepResearch(
  resumeCid?: string | null,
  initialLeaderId?: string | null,
  initialSources: string[] = ["ddgs"],
) {
  // No localStorage auto-restore: a fresh surface starts EMPTY (compose). An
  // existing run is reached as a server resource via History (/deep/:cid), not a
  // client stash — so selecting the scope can never re-attach/trap you.
  const [session, setSession] = useState<DeepResearchSession | null>(null);
  // fix-c #2: seed from the parent ResearchSurface's leader pick so a
  // search → deep-research switch carries the model the user just chose
  // (otherwise the leader pill reset to the default and the submit re-routed
  // the run to a different model silently).
  const [leaderId, setLeaderId] = useState<string | null>(initialLeaderId ?? null);
  const [sources, setSources] = useState<string[]>(initialSources);
  const [depthTier, setDepthTier] = useState<Tier>("standard_deep");
  // A4: iterative grounding toggle — false = standard run, true = re-search
  // weakly-grounded claims + re-check (up to 3 rounds). Flows into submit's
  // create frame as `iterative`, read by the runtime's set_iterative path.
  const [iterative, setIterative] = useState(false);
  // DR-3: recency filter — null = off (any time), "month"/"week" = date-bounded.
  const [recencyWindow, setRecencyWindow] = useState<"month" | "week" | null>(null);
  // fix-c #4: PDF/DOCX export runs on the server (WeasyPrint / pandoc) and can
  // take seconds — without a pending signal the button looked dead. MD stays
  // synchronous (client-side blob) so it never sets this.
  const [exportPending, setExportPending] = useState<ReportExportFmt | null>(null);

  // G1/DR-4: upload cid for the empty state. Creation is LAZY: merely visiting
  // the composer must not persist an empty History row. The first attachment
  // calls ensurePreCid; a plain submit creates the real conversation directly.
  // If controls change after an attachment, submit patches the SAME cid before
  // kickoff so uploaded files are retained and the final settings win.
  const [preCid, setPreCid] = useState<string | null>(null);
  const preCidRef = useRef<string | null>(null);
  const preCreateFlightRef = useRef<Promise<string> | null>(null);
  const preCreate = useMutation({ mutationFn: createDeepResearchConversation });

  const stream = useDeepResearchStream(session);

  // Recover the real query from replayed events (resume path).
  // Priority: report.query (arrives with the finished report, most reliable) >
  // first user MessageEvent (available from the very first replay frame, so the
  // loading window is brief). The "(resumed)" sentinel in session.query is purely
  // internal — it MUST NOT reach the UI.
  const recoveredQuery: string | null =
    stream.report?.query ??
    stream.events
      .filter((e): e is MessageEvent => e.kind === "message")
      .find((e) => e.message.role === "user")?.message.content ??
    null;

  // Resume path: a /deep/:cid route hands us a cid → open it READ-ONLY (kick is
  // absent, so the stream subscribes + replays but never sends the query).
  useEffect(() => {
    if (resumeCid && (session === null || session.cid !== resumeCid)) {
      setSession({ cid: resumeCid, query: "(resumed)", depthTier });
    }
  }, [resumeCid, session, depthTier]);

  const create = useMutation({ mutationFn: createDeepResearchConversation });

  const startPrecreated = useMutation({
    mutationFn: async ({
      targetCid,
      query,
      modelOverride,
      runDepthTier,
      runIterative,
      runRecencyWindow,
      runSources,
    }: {
      targetCid: string | Promise<string>;
      query: string;
      modelOverride: string | null;
      runDepthTier: Tier;
      runIterative: boolean;
      runRecencyWindow: "month" | "week" | null;
      runSources: string[];
    }) => {
      const cid = await targetCid;
      await patchConversationSettings(cid, {
        modelOverride,
        depthTier: runDepthTier,
        iterative: runIterative,
        recencyWindow: runRecencyWindow,
        sources: runSources,
      });
      return { cid, query, depthTier: runDepthTier, kick: true } as DeepResearchSession;
    },
    onSuccess: (nextSession) => {
      preCidRef.current = null;
      setPreCid(null);
      setSession(nextSession);
    },
  });

  // W-07: lazily obtain the pre-created cid for the Attach affordance so uploads
  // work BEFORE the user submits. Returns the existing preCid, or creates one NOW
  // carrying the CURRENT depth/recency/iterative/leader settings (so the upload
  // lands in the conversation that will actually run) and stores it so submit()
  // reuses the SAME cid. null offline.
  const ensurePreCid = useCallback(async () => {
    if (!agentLive()) return null;
    const existing = preCidRef.current ?? preCid;
    if (existing) return existing;
    if (preCreateFlightRef.current) return preCreateFlightRef.current;
    const flight = preCreate.mutateAsync({
      query: "",
      leaderId,
      depthTier,
      iterative,
      recencyWindow,
      sources,
    }).then((cid) => {
      preCidRef.current = cid;
      setPreCid(cid);
      return cid;
    });
    preCreateFlightRef.current = flight;
    try {
      return await flight;
    } finally {
      if (preCreateFlightRef.current === flight) preCreateFlightRef.current = null;
    }
  }, [
    preCid,
    preCreate,
    depthTier,
    iterative,
    recencyWindow,
    leaderId,
    sources,
  ]);

  const submit = useCallback(
    (query: string) => {
      const trimmed = query.trim();
      if (!trimmed) return;
      // G1/DR-4: if we pre-created a cid (for upload support in the empty state),
      // USE IT instead of creating a new one — so any uploaded files are already
      // associated with the conversation that will run. Current settings are
      // patched immediately before kickoff. An in-flight attachment create is
      // shared rather than forking a second conversation.
      const uploadCid = preCidRef.current ?? preCid ?? preCreateFlightRef.current;
      if (uploadCid) {
        startPrecreated.mutate({
          targetCid: uploadCid,
          query: trimmed,
          modelOverride: leaderId,
          runDepthTier: depthTier,
          runIterative: iterative,
          runRecencyWindow: recencyWindow,
          runSources: sources,
        });
        return;
      }
      create.mutate(
        { query: trimmed, leaderId, depthTier, iterative, recencyWindow, sources },
        {
          // kick:true — this is the ONLY path that starts the run.
          onSuccess: (cid) =>
            setSession({ cid, query: trimmed, depthTier, kick: true }),
        },
      );
    },
    [create, startPrecreated, leaderId, depthTier, iterative, recencyWindow, sources, preCid],
  );

  // fix-c #5: the bounded-by "Run on exhaustive tier" button used to call
  // setDepthTier("exhaustive") then submit() in the same tick — but submit is
  // a useCallback closed over the OLD depthTier, so the POST raced the state
  // update and re-ran at the same bounded tier. This callback takes the tier
  // as a direct argument so it never closes over depthTier.
  const runExhaustive = useCallback(
    (query: string) => {
      const q = query.trim();
      if (!q) return;
      setDepthTier("exhaustive");
      create.mutate(
        {
          query: q,
          leaderId,
          depthTier: "exhaustive",
          iterative,
          recencyWindow,
          sources,
        },
        {
          onSuccess: (cid) =>
            setSession({ cid, query: q, depthTier: "exhaustive", kick: true }),
        },
      );
    },
    [create, leaderId, iterative, recencyWindow, sources],
  );

  // Stop = pause (cooperative; the engine halts at the next checkpoint and keeps
  // the partial report). The Stop button maps to the stream's cancel.
  const stop = stream.cancel;

  // Kill = end the run for good (force-cancel the server task; final).
  const kill = useCallback(async () => {
    markConversationKilled(session?.cid);
    stream.cancel();
    if (session) await killConversation(session.cid);
  }, [session, stream]);

  // Resume = continue a stopped/incomplete run (explicit; never on open). It's
  // surfaced via `...stream` below — no need to re-export it here.

  // Retry = a fresh run of the same query (a NEW conversation). Use the recovered
  // query first (handles the resume path where session.query is the sentinel).
  const retry = useCallback(() => {
    const q =
      recoveredQuery ??
      (session?.query && session.query !== "(resumed)" ? session.query : null);
    if (q) {
      setSession(null);
      submit(q);
    }
  }, [recoveredQuery, session, submit]);

  const reset = useCallback(() => setSession(null), []);

  const exportMd = useCallback(
    (followUps?: Array<[string, string]>) => {
      if (stream.report) exportReportAsMarkdown(stream.report, followUps);
    },
    [stream.report],
  );

  const exportReportByFmt = useCallback(
    async (fmt: ReportExportFmt, followUpSeqs?: number[]) => {
      if (!session?.cid) return;
      if (fmt === "md") {
        // Build [question, answer] pairs from followUpSeqs for client-side MD.
        // The seqs are user-message seqs; find them in stream.events.
        let followUps: Array<[string, string]> | undefined;
        if (followUpSeqs && followUpSeqs.length > 0 && stream.report) {
          const selected = new Set(followUpSeqs);
          const post = stream.events.filter(
            (e) =>
              e.kind === "message" &&
              (e.seq ?? 0) > (stream.report!.seq ?? -1),
          );
          const pairs: Array<[string, string]> = [];
          for (let i = 0; i < post.length; i++) {
            const e = post[i];
            if (
              e.kind === "message" &&
              e.message.role === "user" &&
              selected.has(e.seq ?? -1)
            ) {
              const next = post[i + 1];
              const answer =
                next && next.kind === "message" && next.message.role === "assistant"
                  ? (next.message.content ?? "")
                  : "";
              pairs.push([e.message.content ?? "", answer]);
            }
          }
          if (pairs.length > 0) followUps = pairs;
        }
        exportMd(followUps);
        return;
      }
      // Server-side export for pdf. The UI reports errors via toast/surface.
      // fix-c #4: mark pending around the await so the top-bar button can show
      // a spinner + disable itself (mirrors the ExportModal pattern in
      // NeedMoreCard — those buttons would deadlock-looking because the
      // server call takes seconds).
      setExportPending(fmt);
      try {
        await exportReportApi(session.cid, fmt, followUpSeqs);
      } finally {
        setExportPending(null);
      }
    },
    [session?.cid, exportMd, stream.report, stream.events],
  );

  // Legacy export (single-button MD download) — kept for backward compat.
  const exportReport = exportMd;

  // Follow-up Q&A render (Wave 2 — the silent-drop fix). The RP-13 follow-up
  // path appends the user's question + the agent's grounded answer as
  // MessageEvents AFTER the report. DeepReportView only renders the ReportEvent,
  // so these were generated server-side but NEVER shown (the answer vanished).
  // We surface them here. DISPOSITION RULE (honors the whitelist boundary): only
  // user + assistant messages render; environment/system plumbing
  // (<system-reminder>, <reground-anchors>) is deliberately SUPPRESSED, not
  // dumped. Anything past the report seq that isn't a clean Q/A is dropped.
  //
  // STALE-FOLLOW-UP FIX (WALK-08 A4): when no report exists yet, reportSeq
  // would be -1 (the ?? -1 fallback), so the run's OWN initiating user message
  // (seq ≥ 0 > -1) gets misclassified as a "follow-up".  Bail to [] whenever
  // stream.report is null — a follow-up can't exist without a report.
  const followUps: MessageEvent[] =
    stream.report == null
      ? []
      : stream.events.filter(
          (e): e is MessageEvent =>
            e.kind === "message" &&
            (e.seq ?? 0) > (stream.report!.seq ?? -1) &&
            (e.message.role === "user" || e.message.role === "assistant") &&
            !(e.message.content ?? "").includes("<system-reminder>") &&
            !(e.message.content ?? "").includes("<reground-anchors>"),
        );

  // The effective query for display: real query once recovered, a neutral loading
  // string while mid-replay, or null when no session exists.
  const effectiveQuery =
    session === null
      ? null
      : recoveredQuery ??
        (session.query !== "(resumed)" ? session.query : "Resuming research…");

  return {
    started: session !== null,
    cid: session?.cid ?? null,
    /** G1/DR-4: the pre-created cid for the empty state (before first submit).
     *  Pass to UploadComposer so text files can be attached before submitting.
     *  null when a session is already live or on the resume path. */
    preCid: session === null && !resumeCid ? preCid : null,
    /** W-07: lazily create-or-return the upload cid so Attach works pre-cid.
     *  Exposed only when a server exists (offline → Attach stays disabled). */
    ensurePreCid: agentLive() ? ensurePreCid : undefined,
    query: effectiveQuery,
    /** True once the real query text is available (not the loading placeholder). */
    queryResolved:
      recoveredQuery !== null ||
      (session !== null && session.query !== "(resumed)"),
    resumed: Boolean(resumeCid),
    submitting: create.isPending || startPrecreated.isPending,
    leaderId,
    setLeaderId,
    selectedSources: sources,
    setSelectedSources: setSources,
    depthTier,
    setDepthTier,
    iterative,
    setIterative,
    recencyWindow,
    setRecencyWindow,
    submit,
    runExhaustive,
    stop,
    kill,
    retry,
    reset,
    /** Wave 2: post-report follow-up Q&A (user question + agent answer),
     *  in event order. Previously generated server-side but never rendered. */
    followUps,
    exportReport,
    exportReportByFmt,
    /** fix-c #4: which export (if any) is currently in-flight on the server.
     *  null when idle. The surface uses this to disable + spin the matching
     *  top-bar button. MD is excluded (synchronous). */
    exportPending,
    ...stream,
    // A failed create was silent (empty state, no message). Expose it to the UI.
    submitError: create.error ?? startPrecreated.error ?? null,
  };
}
