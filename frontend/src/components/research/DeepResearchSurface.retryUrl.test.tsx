/**
 * Retry-from-History URL handoff (router level).
 *
 * Hook cid ownership alone does not prove reload safety: the address must
 * move to the NEW cid only after a persisted event for that run proves the
 * question landed. Stale old-run events must not trigger the handoff before
 * the new socket sends.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WSClientFrame, WSServerFrame } from "@/types/agent";
import type { DeepResearchSubmit } from "@/api/deepResearch";
import { ModeProvider } from "@/shell/ModeProvider";
import { DeepResearchSurface } from "./DeepResearchSurface";

const CONV_OLD = "conv_old_retry_url";
const CONV_NEW = "conv_new_retry_url";
const REAL_QUERY = "What failed query should retry as a fresh run?";

const harness = vi.hoisted(() => ({
  subscribes: [] as string[],
  sendsByCid: new Map<string, WSClientFrame[]>(),
  cancelsByCid: new Map<string, number>(),
  framesByCid: new Map<string, (f: WSServerFrame) => void>(),
  newAutoReplay: false,
  deliveredByCid: new Map<string, string[]>(),
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

function newPersistedFrames(): WSServerFrame[] {
  const at = new Date(Date.now() + 1000).toISOString();
  return [
    {
      type: "event",
      event: {
        id: "new-u1",
        kind: "message",
        source: "user",
        seq: 1,
        timestamp: at,
        message: { role: "user", content: REAL_QUERY },
      },
    } as WSServerFrame,
    {
      type: "event",
      event: {
        id: "new-s1",
        kind: "status",
        source: "system",
        seq: 2,
        timestamp: at,
        status: "RUNNING",
        detail: "research",
      },
    } as WSServerFrame,
    {
      type: "event",
      event: {
        id: "new-m1",
        kind: "message",
        source: "agent",
        seq: 3,
        timestamp: at,
        message: { role: "assistant", content: "brief: retry is running" },
      },
    } as WSServerFrame,
  ];
}

function oldFrames(): WSServerFrame[] {
  const at = new Date().toISOString();
  return [
    {
      type: "event",
      event: {
        id: "old-u1",
        kind: "message",
        source: "user",
        seq: 1,
        timestamp: at,
        message: { role: "user", content: REAL_QUERY },
      },
    } as WSServerFrame,
    {
      type: "event",
      event: {
        id: "old-e1",
        kind: "error",
        source: "system",
        seq: 2,
        timestamp: at,
        detail: "the research run failed",
      },
    } as WSServerFrame,
  ];
}

vi.mock("@/api/agent", async (orig) => ({
  ...(await orig<typeof import("@/api/agent")>()),
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
              for (const f of oldFrames()) onFrame(f);
            }, 0),
          );
        }
        // Fast-open path (opt-in per test): deliver the first persisted NEW
        // events only in a later tick AFTER open + queued send_message flush.
        // Realistic ordering is open -> flush -> server replay; this branch
        // never emits a server frame before the socket opens.
        if (cid === CONV_NEW && harness.newAutoReplay) {
          timers.push(
            setTimeout(() => {
              if (canceled) return;
              for (const f of newPersistedFrames()) {
                const id =
                  f.type === "event"
                    ? (f.event as { id?: string }).id ?? "(state)"
                    : "(state)";
                if (!harness.deliveredByCid.has(cid))
                  harness.deliveredByCid.set(cid, []);
                harness.deliveredByCid.get(cid)?.push(id);
                onFrame(f);
              }
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

function PathLog({ log }: { log: string[] }) {
  const { pathname } = useLocation();
  useEffect(() => {
    if (log[log.length - 1] !== pathname) log.push(pathname);
  }, [log, pathname]);
  return null;
}

function ResumeRoute() {
  const { cid } = useParams();
  return <DeepResearchSurface resumeCid={cid ?? null} />;
}

function mount(log: string[], cid = CONV_OLD) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/deep/${cid}`]}>
        <ModeProvider>
          <PathLog log={log} />
          <Routes>
            <Route path="/" element={<div data-composer />} />
            <Route path="/deep/:cid" element={<ResumeRoute />} />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function sendMessages(cid: string): WSClientFrame[] {
  return (harness.sendsByCid.get(cid) ?? []).filter(
    (f) => (f as { type?: string }).type === "send_message",
  );
}

describe("retry from history moves the URL only on first new-run event", () => {
  beforeEach(() => {
    harness.subscribes.length = 0;
    harness.sendsByCid.clear();
    harness.cancelsByCid.clear();
    harness.framesByCid.clear();
    harness.deliveredByCid.clear();
    harness.newAutoReplay = false;
    createDeepResearchConversation.mockClear();
  });

  it.each(["Try again", "Retry"])("%s moves to the new run only after its first event and reloads read-only", async (button) => {
    const user = userEvent.setup();
    const log: string[] = [];
    const view = mount(log);

    await screen.findByRole("button", { name: button });
    expect(log).toEqual([`/deep/${CONV_OLD}`]);
    expect(sendMessages(CONV_OLD)).toEqual([]);

    await user.click(screen.getByRole("button", { name: button }));

    await waitFor(() =>
      expect(createDeepResearchConversation).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => expect(harness.subscribes).toContain(CONV_NEW));
    // Cid creation alone must not navigate; stale old events must not either.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(log).toEqual([`/deep/${CONV_OLD}`]);

    const at = new Date(Date.now() + 1000).toISOString();
    await act(async () => {
      harness.framesByCid.get(CONV_NEW)?.({
        type: "event",
        event: {
          id: "new-m1",
          kind: "message",
          source: "agent",
          seq: 1,
          timestamp: at,
          message: { role: "assistant", content: "brief: retry is running" },
        },
      } as WSServerFrame);
    });

    await waitFor(() =>
      expect(log).toEqual([`/deep/${CONV_OLD}`, `/deep/${CONV_NEW}`]),
    );
    expect(log).toEqual([`/deep/${CONV_OLD}`, `/deep/${CONV_NEW}`]);
    expect(sendMessages(CONV_NEW)).toHaveLength(1);
    expect(sendMessages(CONV_NEW)[0]).toMatchObject({
      type: "send_message",
      content: REAL_QUERY,
    });
    expect(sendMessages(CONV_OLD)).toEqual([]);
    expect(harness.subscribes).toEqual([CONV_OLD, CONV_NEW]);

    // Reload the resulting address: it must reopen the new conversation
    // read-only without creating a third run or sending the question again.
    view.unmount();
    const reloaded: string[] = [];
    mount(reloaded, CONV_NEW);
    await waitFor(() =>
      expect(harness.subscribes).toEqual([CONV_OLD, CONV_NEW, CONV_NEW]),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(reloaded).toEqual([`/deep/${CONV_NEW}`]);
    expect(createDeepResearchConversation).toHaveBeenCalledTimes(1);
    expect(sendMessages(CONV_NEW)).toHaveLength(1);
  });

  it("fast open flushes the queued query before the first persisted NEW event switches the URL (no empty-render wait)", async () => {
    harness.newAutoReplay = true;
    const user = userEvent.setup();
    const log: string[] = [];
    mount(log);

    await screen.findByRole("button", { name: "Try again" });
    await user.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() =>
      expect(createDeepResearchConversation).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => expect(harness.subscribes).toContain(CONV_NEW));
    // Realistic ordering: the fresh socket opens, flushes the queued
    // send_message, and only then does the server replay the first persisted
    // NEW event. No fixed 20ms empty-render wait here; wait for the flush
    // itself, then for the URL handoff the replay triggers.
    await waitFor(() => expect(sendMessages(CONV_NEW)).toHaveLength(1));
    expect(sendMessages(CONV_NEW)[0]).toMatchObject({
      type: "send_message",
      content: REAL_QUERY,
    });
    await waitFor(() =>
      expect(log).toEqual([`/deep/${CONV_OLD}`, `/deep/${CONV_NEW}`]),
    );
    // The query was flushed exactly once before the first server replay; the
    // old connection never kicked.
    expect(sendMessages(CONV_OLD)).toEqual([]);
    expect(harness.subscribes).toEqual([CONV_OLD, CONV_NEW]);
    expect((harness.deliveredByCid.get(CONV_NEW) ?? []).length).toBeGreaterThan(0);
  });

  it("reloading the NEW url replays persisted query/status so heading and phase restore without resending", async () => {
    harness.newAutoReplay = true;
    const reloaded: string[] = [];
    const view = mount(reloaded, CONV_NEW);

    await waitFor(() =>
      expect(harness.subscribes).toEqual([CONV_NEW]),
    );
    // Persisted NEW replay arrives deferred (setTimeout), like the real
    // history-then-live transport; the heading/phase below prove the replay
    // landed, not just the subscription count.
    await screen.findByText(REAL_QUERY);
    await waitFor(() =>
      expect(
        view.container.querySelector('[data-dr-phase="running"]'),
      ).not.toBeNull(),
    );
    expect(reloaded).toEqual([`/deep/${CONV_NEW}`]);
    expect(createDeepResearchConversation).not.toHaveBeenCalled();
    // Read-only reload: replay restores the view without resending the query.
    expect(sendMessages(CONV_NEW)).toEqual([]);
    view.unmount();
  });
});
