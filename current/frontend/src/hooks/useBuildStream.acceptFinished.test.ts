import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentHandle } from "@/api/agent";
import type { WSClientFrame, WSServerFrame } from "@/types/agent";
import { useBuildStream } from "./useBuildStream";

const sent: WSClientFrame[] = [];

vi.mock("@/api/agent", () => ({
  subscribeConversation: (
    _cid: string,
    _onFrame: (frame: WSServerFrame) => void,
  ): AgentHandle => {
    void _cid;
    void _onFrame;
    return {
      send: (frame: WSClientFrame) => sent.push(frame),
      cancel: () => {},
    };
  },
}));

afterEach(() => sent.splice(0));

describe("useBuildStream — explicit accept-as-is (F-3 follow-up)", () => {
  it("sends the authoritative accept_finished frame, with no prose to guess from", () => {
    const session = { cid: "c_accept", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => result.current.acceptFinished());

    expect(sent.at(-1)).toEqual({ type: "accept_finished" });
    // The explicit path must never masquerade as free text — no request_plan,
    // no send_message carrying a stop phrase for the server to intent-match.
    expect(sent.filter((f) => f.type === "request_plan")).toHaveLength(0);
  });
});
