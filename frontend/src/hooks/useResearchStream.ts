import { useEffect, useReducer, useRef } from "react";
import { type StreamHandle, subscribeResearch } from "@/api/research";
import type { AnswerBlock, GroundedAnswer, ReScope, StreamFrame } from "@/types/grounded";

export type Phase = "idle" | "running" | "finished" | "error";

export interface StreamState {
  phase: Phase;
  blocks: AnswerBlock[]; // completed structured blocks (source of truth)
  partial: Record<string, string>; // block_id → live token text (EPHEMERAL)
  streamingBlockId: string | null; // the block tokens are currently filling
  answer: GroundedAnswer | null; // the authoritative final answer
  error: string | null;
}

const initial: StreamState = {
  phase: "idle",
  blocks: [],
  partial: {},
  streamingBlockId: null,
  answer: null,
  error: null,
};

type Action = { type: "reset" } | { type: "stop" } | { type: "frame"; frame: StreamFrame };

function reducer(state: StreamState, action: Action): StreamState {
  switch (action.type) {
    case "reset":
      return { ...initial, phase: "running" };
    case "stop":
      return { ...state, phase: "finished" };
    case "frame": {
      const f = action.frame;
      switch (f.type) {
        case "state":
          return { ...state, phase: f.status };
        case "token":
          // Ephemeral typewriter text for the in-flight block only.
          return {
            ...state,
            streamingBlockId: f.block_id,
            partial: { ...state.partial, [f.block_id]: (state.partial[f.block_id] ?? "") + f.token },
          };
        case "block": {
          // The completed block is the source of truth → drop its token text.
          const partial = { ...state.partial };
          delete partial[f.block.id];
          return {
            ...state,
            blocks: [...state.blocks.filter((b) => b.id !== f.block.id), f.block],
            partial,
            streamingBlockId: state.streamingBlockId === f.block.id ? null : state.streamingBlockId,
          };
        }
        case "final":
          // Reconcile entirely to the authoritative answer; tokens discarded.
          return {
            ...state,
            answer: f.answer,
            blocks: f.answer.blocks,
            partial: {},
            streamingBlockId: null,
          };
        case "error":
          return { ...state, phase: "error", error: f.message };
      }
    }
  }
}

/** Subscribe to a research run for `scope`; null = idle. Manages token/block/
 * final reconciliation so the UI never rests in a token-only state. */
export function useResearchStream(scope: ReScope | null): StreamState & { stop: () => void } {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<StreamHandle | null>(null);

  useEffect(() => {
    if (!scope) return;
    dispatch({ type: "reset" });
    const h = subscribeResearch(scope, (frame) => dispatch({ type: "frame", frame }));
    handle.current = h;
    return () => h.cancel();
  }, [scope]);

  const stop = () => {
    handle.current?.cancel();
    dispatch({ type: "stop" });
  };

  return { ...state, stop };
}
