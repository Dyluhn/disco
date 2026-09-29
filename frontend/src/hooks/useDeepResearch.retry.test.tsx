/**
 * Deep Research retry regression (bounded).
 *
 * Real UI evidence on an identical tree: opening a failed /deep/conv_old from
 * History and clicking Try again creates conv_new (POST 200), briefly
 * subscribes its websocket, then reattaches conv_old and redisplays the
 * original error. No new research query was sent.
 *
 * Cause: the resume-path effect in useDeepResearchSession forced resumeCid
 * whenever session.cid changed, including retry's explicit new session.
 *
 * This test uses the real session/lifecycle/stream hooks (only the wire API
 * is mocked, never useDeepResearch itself) with a stable resumeCid prop as
 * the router provides it on /deep/:cid. Socket replays are deferred
 * (setTimeout 0), never synchronous, so effect races are preserved. Sends
 * are queued until the delayed socket open; a canceled connection drops its
 * queue and replay, like the real transport.
 */

import { useLayoutEffect, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { WSClientFrame, WSServerFrame } from "@/types/agent";
import type { DeepResearchSubmit } from "@/api/deepResearch";

const CONV_OLD = "conv_old_retry_regression";
const CONV_NEW = "conv_new_retry_regression";
const REAL_QUERY = "What failed query should retry as a fresh run?";
const FRESH_QUERY = "A fresh composer query stays on its own run";

const harness = vi.hoisted(() => ({
  subscribes: [] as string[],
  sendsByCid: new Map<string, WSClientFrame[]>(),
  cancelsByCid: new Map<string, number>(),
  framesByCid: new Map<string, (f: WSServerFrame) => void>(),
}));

vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()),
  agentLive: () => true,
}));

const createDeepResearchConversation = vi.fn<(opts: DeepResearchSubmit) => Promise<string>>(
  async () => CONV_NEW,
);
vi.mock("@/api/deepResearch", () => ({
  createDeepResearchConversation: (opts: DeepResearchSubmit) =>
    createDeepResearchConversation(opts),
  exportReportAsMarkdown: vi.fn(),
  exportReport: vi.fn(async () => true),
}));

vi.mock("@/api/agent", () => ({
  killConversation: vi.fn(async () => undefined),
  patchConversationSettings: vi.fn(async () => undefined),
  subscribeConversation: (cid: string, onFrame: (f: WSServerFrame) => void) => {
    harness.subscribes.push(cid);
    if (!harness.sendsByCid.has(cid)) harness.sendsByCid.set(cid, []);
    harness.cancelsByCid.set(cid, harness.cancelsByCid.get(cid) ?? 0);
    harness.framesByCid.set(cid, onFrame);
    let open = false;
    let canceled = false;
    const queue: WSClientFrame[] = [];
    const timers: Array<ReturnType<typeof setTimeout>> = [];
    timers.push(
      setTimeout(() => {
        if (canceled) return;
        open = true;
        const sends = harness.sendsByCid.get(cid);
        if (sends) sends.push(...queue);
        queue.length = 0;
        if (cid === CONV_OLD) {
          timers.push(
            setTimeout(() => {
              if (canceled) return;
              onFrame({
                type: "state",
                state: {
                  conversation_id: CONV_OLD,
                  execution_status: "RUNNING",
                  iteration: 2,
                  max_iterations: 30,
                  last_seq: 2,
                  pending_action_id: null,
                  pending_plan_id: null,
                  extras: {},
                },
              } as WSServerFrame);
              onFrame({
                type: "event",
                event: {
                  id: "e1",
                  kind: "message",
                  source: "user",
                  seq: 1,
                  message: { role: "user", content: REAL_QUERY },
                },
              } as WSServerFrame);
              onFrame({
                type: "event",
                event: {
                  id: "e2",
                  kind: "error",
                  source: "system",
                  seq: 2,
                  detail: "the research run failed",
                },
              } as WSServerFrame);
            }, 0),
          );
        }
      }, 0),
    );
    return {
      send: (frame: WSClientFrame) => {
        if (canceled) return;
        if (!open) queue.push(frame);
        else harness.sendsByCid.get(cid)?.push(frame);
      },
      cancel: () => {
        if (canceled) return;
        canceled = true;
        harness.cancelsByCid.set(cid, (harness.cancelsByCid.get(cid) ?? 0) + 1);
        queue.length = 0;
        for (const t of timers) clearTimeout(t);
      },
    };
  },
}));

import { useDeepResearch } from "./useDeepResearch";

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function sendMessages(cid: string): WSClientFrame[] {
  return (harness.sendsByCid.get(cid) ?? []).filter(
    (f) => (f as { type?: string }).type === "send_message",
  );
}

describe("useDeepResearch retry from a failed resumed run", () => {
  beforeEach(() => {
    harness.subscribes.length = 0;
    harness.sendsByCid.clear();
    harness.cancelsByCid.clear();
    harness.framesByCid.clear();
    createDeepResearchConversation.mockClear();
  });

  it("cold-open control: a failed resumed run is a safe read (no kick)", async () => {
    const { result } = renderHook(() => useDeepResearch(CONV_OLD), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.status).toBe("ERROR"));
    expect(result.current.cid).toBe(CONV_OLD);
    expect(result.current.query).toBe(REAL_QUERY);
    // Readonly open: subscribes to conv_old, never sends the query back.
    expect(harness.subscribes).toEqual([CONV_OLD]);
    expect(sendMessages(CONV_OLD)).toEqual([]);
  });

  it("retry stays on the fresh cid and sends exactly one query on its own connection", async () => {
    const { result } = renderHook(() => useDeepResearch(CONV_OLD), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.status).toBe("ERROR"));
    expect(sendMessages(CONV_OLD)).toEqual([]);

    act(() => {
      result.current.retry();
    });

    // POST 200 mints the fresh conversation.
    await waitFor(() =>
      expect(createDeepResearchConversation).toHaveBeenCalledTimes(1),
    );
    // The retry must REMAIN on the fresh cid (no reattach to conv_old).
    await waitFor(() => expect(result.current.cid).toBe(CONV_NEW));
    await waitFor(() => expect(harness.subscribes).toContain(CONV_NEW));
    // Settle delayed open/flush plus any trailing resume-effect reattach.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(result.current.cid).toBe(CONV_NEW);
    // Exactly one subscribe per conversation: old open + fresh retry, no reattach.
    expect(harness.subscribes).toEqual([CONV_OLD, CONV_NEW]);
    // Exactly one research-start query, through the fresh connection only.
    // A canceled retry connection would have dropped its queued send.
    expect(sendMessages(CONV_NEW)).toHaveLength(1);
    expect(sendMessages(CONV_NEW)[0]).toMatchObject({
      type: "send_message",
      content: REAL_QUERY,
    });
    // Original remains unchanged: its connection never kicks.
    expect(sendMessages(CONV_OLD)).toEqual([]);
  });

  it("changing the route opens the selected conversation read-only, including returning to the first route", async () => {
    const other = "conv_other_saved_run";
    const { result, rerender } = renderHook(
      ({ route }: { route: string | null }) => useDeepResearch(route),
      { wrapper: wrapper(), initialProps: { route: CONV_OLD as string | null } },
    );
    await waitFor(() => expect(result.current.status).toBe("ERROR"));
    rerender({ route: other });
    await waitFor(() => expect(result.current.cid).toBe(other));
    rerender({ route: CONV_OLD });
    await waitFor(() => expect(result.current.cid).toBe(CONV_OLD));
    await waitFor(() => expect(result.current.status).toBe("ERROR"));
    expect(harness.subscribes).toEqual([CONV_OLD, other, CONV_OLD]);
    expect(sendMessages(CONV_OLD)).toEqual([]);
    expect(sendMessages(other)).toEqual([]);
    expect(createDeepResearchConversation).not.toHaveBeenCalled();
  });

  it("fresh-submit control: a composer submit kicks exactly once and stays", async () => {
    const { result } = renderHook(() => useDeepResearch(), {
      wrapper: wrapper(),
    });

    act(() => {
      result.current.submit(FRESH_QUERY);
    });

    await waitFor(() =>
      expect(createDeepResearchConversation).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => expect(result.current.cid).toBe(CONV_NEW));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(result.current.cid).toBe(CONV_NEW);
    expect(sendMessages(CONV_NEW)).toHaveLength(1);
    expect(sendMessages(CONV_NEW)[0]).toMatchObject({
      type: "send_message",
      content: FRESH_QUERY,
    });
  });

  it("retry transient under the NEW cid exposes no stale OLD error/activity to the consumer", async () => {
    const committed: Array<{ cid: string | null; error: string | null; ids: string[] }> = [];

    const { result } = renderHook(() => {
      const run = useDeepResearch(CONV_OLD);
      useLayoutEffect(() => {
        committed.push({ cid: run.cid, error: run.error, ids: run.events.map((e) => e.id) });
      });
      return run;
    }, { wrapper: wrapper() });

    await waitFor(() => expect(result.current.status).toBe("ERROR"));
    expect(result.current.cid).toBe(CONV_OLD);
    expect(result.current.error).toBe("the research run failed");
    expect(result.current.events.map((e) => e.id)).toContain("e2");

    await act(async () => {
      result.current.retry();
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(createDeepResearchConversation).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => expect(result.current.cid).toBe(CONV_NEW));
    await waitFor(() => expect(harness.subscribes).toContain(CONV_NEW));
    // Realistic ordering: wait for the fresh socket to open and flush the
    // queued send_message BEFORE delivering the first persisted NEW frame.
    // Never inject a server frame before the flush.
    await waitFor(() => expect(sendMessages(CONV_NEW)).toHaveLength(1));
    // Transient consumer view under the NEW cid, before any NEW frame: the
    // stream reset must have cleared the OLD run's error/events, so no stale
    // OLD content is observed under the NEW cid.
    expect(result.current.cid).toBe(CONV_NEW);
    expect(result.current.error).toBeNull();
    expect(result.current.events).toEqual([]);
    expect(result.current.status).toBe("IDLE");
    expect(result.current.brief).toBeNull();
    // Check committed consumer views, not only the settled result after effects.
    // A very fast create response may batch the reset and new session update.
    expect(committed.filter((view) => view.cid === CONV_NEW)).not.toEqual([]);
    expect(committed.filter((view) => view.cid === CONV_NEW).every(
      (view) => view.error === null && !view.ids.includes("e2"),
    )).toBe(true);


    const at = new Date().toISOString();
    await act(async () => {
      harness.framesByCid.get(CONV_NEW)?.({
        type: "event",
        event: {
          id: "new-u1",
          kind: "message",
          source: "user",
          seq: 1,
          timestamp: at,
          message: { role: "user", content: REAL_QUERY },
        },
      } as WSServerFrame);
    });

    await waitFor(() => expect(result.current.query).toBe(REAL_QUERY));
    expect(result.current.events.map((e) => e.id)).toEqual(["new-u1"]);
    expect(result.current.events.map((e) => e.id)).not.toContain("e2");
    expect(result.current.error).toBeNull();
    expect(sendMessages(CONV_NEW)).toHaveLength(1);
    expect(sendMessages(CONV_OLD)).toEqual([]);
  });
});
