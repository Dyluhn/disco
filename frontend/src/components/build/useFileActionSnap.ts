import { useEffect, useRef } from "react";
import { deriveFiles } from "@/lib/buildTrace";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type FileWriteIdentity = { tool: string; path: string };

/** Follow live writes once, including calls too short to emit a partial stream. */
export function useFileActionSnap({ events, streamingFile, replayThrough, cid, status, enabled, setTab }: {
  events: AgentEvent[];
  streamingFile: FileWriteIdentity | null;
  replayThrough: number | null;
  cid: string | null;
  status: ConversationStatus;
  enabled: boolean;
  setTab: (tab: "files") => void;
}) {
  const prior = useRef<{
    cid: string | null; sequence: number; replayThrough: number | null;
    streaming: boolean; streamedWrite: FileWriteIdentity | null;
  } | null>(null);
  useEffect(() => {
    const latest = events.reduce((seq, event) => Math.max(seq, event.seq ?? 0), 0);
    const before = prior.current;
    const sameConversation = before !== null && before.cid === cid;
    const sameConnection = sameConversation && before.replayThrough === replayThrough;
    let streamedWrite = sameConnection && status === "RUNNING" ? before.streamedWrite : null;
    let snap = false;
    // Seed from mounted history and the socket's authoritative replay boundary.
    // A replay/reconnect or duplicate action must never behave like a new write.
    if (sameConversation && replayThrough !== null) {
      const floor = Math.max(before.sequence, replayThrough);
      for (const event of events) {
        if (event.kind !== "action" || event.seq == null || event.seq <= floor) continue;
        const call = event.tool_call;
        const alreadyStreamed = streamedWrite !== null && call?.tool_name === streamedWrite.tool
          && call.arguments.path === streamedWrite.path;
        if (deriveFiles([event]).length > 0 && !alreadyStreamed && status === "RUNNING") snap = true;
        // The authoritative action ends that streamed step, even when the user
        // left Files midway through it. A later write may reuse the same path.
        streamedWrite = null;
      }
    }
    const streaming = streamingFile !== null;
    if (streaming && !(sameConnection && before.streaming)) snap = true;
    if (streaming) streamedWrite = streamingFile;
    prior.current = {
      cid, sequence: Math.max(sameConversation ? before.sequence : 0, latest),
      replayThrough, streaming, streamedWrite,
    };
    if (enabled && snap) setTab("files");
  }, [cid, enabled, events, replayThrough, setTab, status, streamingFile]);
}
