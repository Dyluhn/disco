/**
 * The Ask-gate wiring in useBuildStream: an AWAITING_USER_QUESTION status frame
 * resolves the agent's question event into `pendingQuestion` + `awaitingQuestion`,
 * and `answer()` sends a steer frame (the loop's resume signal).
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentHandle } from "@/api/agent";
import type { WSClientFrame, WSServerFrame } from "@/types/agent";
import { useBuildStream } from "./useBuildStream";

// Capture the onFrame sink + the frames the hook sends, so the test can drive
// the stream and assert what goes back over the wire.
let sink: ((f: WSServerFrame) => void) | null = null;
const sent: WSClientFrame[] = [];

vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle => {
    sink = onFrame;
    return { send: (f: WSClientFrame) => sent.push(f), cancel: () => {} };
  },
}));

afterEach(() => {
  sink = null;
  sent.length = 0;
});

describe("useBuildStream — Ask-gate", () => {
  it("surfaces the question and answers via a steer frame", () => {
    // stable session identity: useBuildStream re-subscribes (and resets) when the
    // session object changes, so a fresh literal each render would wipe state.
    const session = { cid: "c_ask", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    // the agent asks a free-form question, then the loop parks at the gate
    act(() => {
      sink!({
        type: "event",
        event: {
          id: "q1",
          kind: "message",
          source: "agent",
          message: { role: "assistant", content: "Light theme or dark theme?" },
        },
      });
      sink!({
        type: "event",
        event: { id: "s1", kind: "status", source: "system", status: "AWAITING_USER_QUESTION", detail: "q1" },
      });
    });

    expect(result.current.status).toBe("AWAITING_USER_QUESTION");
    expect(result.current.awaitingQuestion).toBe(true);
    expect(result.current.pendingQuestion?.message?.content).toBe("Light theme or dark theme?");

    // answering sends a steer frame carrying the typed reply
    act(() => result.current.answer("dark theme please"));
    const steer = sent.find((f) => f.type === "steer");
    expect(steer).toEqual({ type: "steer", steer_text: "dark theme please" });
  });

  it("clears the question gate when the loop resumes RUNNING", () => {
    const session = { cid: "c_ask2", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));
    act(() => {
      sink!({
        type: "event",
        event: { id: "q1", kind: "message", source: "agent", message: { role: "assistant", content: "Which port?" } },
      });
      sink!({
        type: "event",
        event: { id: "s1", kind: "status", source: "system", status: "AWAITING_USER_QUESTION", detail: "q1" },
      });
    });
    expect(result.current.awaitingQuestion).toBe(true);

    act(() => {
      sink!({
        type: "event",
        event: { id: "s2", kind: "status", source: "system", status: "RUNNING" },
      });
    });
    expect(result.current.awaitingQuestion).toBe(false);
    expect(result.current.pendingQuestion).toBeNull();
  });

  it("resolves a questions_v2 status detail into the structured intake gate", () => {
    const session = { cid: "c_qv2", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({
        type: "event",
        event: {
          id: "qv2",
          kind: "questions_v2",
          source: "agent",
          question: "A few details before I plan.",
          items: [
            {
              id: "style",
              question: "Which starting style?",
              options: ["Minimal", "Editorial", "Other"],
              allow_free_text: true,
              answer: "",
            },
          ],
        },
      });
      sink!({
        type: "event",
        event: {
          id: "s1",
          kind: "status",
          source: "system",
          status: "AWAITING_USER_QUESTION",
          detail: "qv2",
        },
      });
    });

    expect(result.current.awaitingQuestion).toBe(true);
    expect(result.current.pendingQuestionsV2?.items[0]?.id).toBe("style");
    expect(result.current.pendingClarify).toBeNull();
    expect(result.current.pendingQuestion).toBeNull();
  });
});
