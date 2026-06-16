/**
 * DeepResearchSurface — resume path tests.
 *
 * A /deep/:cid surface opened from History must show the REAL query (recovered
 * from the replayed ReportEvent or user MessageEvent), not the "(resumed)"
 * placeholder the hook uses as an internal sentinel, and must NOT show a bare
 * "(resumed)" as a title replacement.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import type { WSServerFrame } from "@/types/agent";
import { DeepResearchSurface } from "./DeepResearchSurface";

const RESUME_CID = "conv_resume_test";
const REAL_QUERY = "What is the future of quantum computing?";

// Fake agent subscription: push a complete replay synchronously (via setTimeout 0)
// so that act() flushes it within each test's waitFor.
vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, onFrame: (f: WSServerFrame) => void) => {
    setTimeout(() => {
      // Initial state frame: conversation is already FINISHED.
      onFrame({
        type: "state",
        state: {
          conversation_id: RESUME_CID,
          execution_status: "FINISHED",
          iteration: 1,
          max_iterations: 30,
          last_seq: 5,
          pending_action_id: null,
          pending_plan_id: null,
          extras: {},
        },
      });
      // Replay: user message (carries the query from the original run).
      onFrame({
        type: "event",
        event: {
          id: "e1",
          kind: "message",
          source: "user",
          seq: 1,
          message: { role: "user", content: REAL_QUERY },
        },
      });
      // Replay: finished report (also carries the query).
      onFrame({
        type: "event",
        event: {
          id: "e4",
          kind: "report",
          source: "agent",
          seq: 4,
          query: REAL_QUERY,
          summary: "A brief summary of quantum computing prospects.",
          sections: [],
          passages: [],
          all_hits: [],
          unsupported_count: 0,
          bounded_by: null,
          depth_tier: "standard_deep",
        },
      });
      // Replay: FINISHED status event.
      onFrame({
        type: "event",
        event: {
          id: "e5",
          kind: "status",
          source: "system",
          seq: 5,
          status: "FINISHED",
        },
      });
    }, 0);
    return { send: () => {}, cancel: () => {} };
  },
  killConversation: () => Promise.resolve(),
}));

function renderResumeSurface() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/deep/${RESUME_CID}`]}>
        <ModeProvider>
          <Routes>
            <Route
              path="/deep/:cid"
              element={<DeepResearchSurface resumeCid={RESUME_CID} />}
            />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DeepResearchSurface — resume path", () => {
  it("shows the real query (from replayed report) in the H1, not '(resumed)'", async () => {
    renderResumeSurface();

    const heading = await waitFor(
      () => {
        const h = screen.getByRole("heading", { level: 1 });
        // The heading must contain the real query text before we pass
        expect(h).toHaveTextContent(REAL_QUERY);
        return h;
      },
      { timeout: 3000 },
    );

    // The bare sentinel must NOT appear as the sole heading text
    expect(heading.textContent?.trim()).not.toBe("(resumed)");
  });

  it("appends a '(resumed)' badge after the real title, not instead of it", async () => {
    renderResumeSurface();

    const heading = await waitFor(
      () => {
        const h = screen.getByRole("heading", { level: 1 });
        expect(h).toHaveTextContent(REAL_QUERY);
        return h;
      },
      { timeout: 3000 },
    );

    // The heading should also carry the resumed marker
    expect(heading).toHaveTextContent("(resumed)");
    // But the REAL_QUERY must still be present alongside it
    expect(heading).toHaveTextContent(REAL_QUERY);
  });
});
