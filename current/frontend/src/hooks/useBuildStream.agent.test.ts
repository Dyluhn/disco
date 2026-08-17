import { renderHook, waitFor } from "@testing-library/react";
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

describe("useBuildStream — surface-specific first send", () => {
  it("keeps standalone Agent ingress artifact-neutral", async () => {
    renderHook(() =>
      useBuildStream(
        { cid: "agent_task", task: "Summarize these notes", kick: true },
        { surface: "agent" },
      ),
    );

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      type: "send_message",
      content: "Summarize these notes",
    });
    expect(sent[0]).not.toHaveProperty("build_brief");
  });

  it("retains explicit contract activation for Build", async () => {
    renderHook(() =>
      useBuildStream(
        { cid: "build_task", task: "Build a site", kick: true },
        { surface: "build" },
      ),
    );

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      type: "send_message",
      content: "Build a site",
      build_brief: {},
    });
  });
});
