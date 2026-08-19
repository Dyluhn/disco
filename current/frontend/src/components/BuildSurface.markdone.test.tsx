/**
 * BuildSurface — the explicit "Mark done" control (F-3 follow-up).
 *
 * After a build FINISHES, the user's satisfied-with-it signal used to be free
 * text typed into the re-plan composer, leaving the server to GUESS stop-intent
 * from prose ("ship it"). The Mark-done button states the intent explicitly: it
 * calls b.acceptFinished (→ the authoritative `accept_finished` wire frame) and
 * must never route through requestPlan. Driven with a MOCKED useBuild (the
 * BuildSurface.recovery.test.tsx pattern) so we can pin exact statuses.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

let buildState: Record<string, unknown>;

vi.mock("@/hooks/useBuild", () => ({
  useBuild: () => buildState,
}));
vi.mock("@/hooks/useProjects", () => ({
  useDownloadProject: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
  useExportManifest: () => ({ mutate: vi.fn() }),
  useProjectManifest: () => ({ data: { files: [] } }),
  useProjectRelease: () => ({ data: undefined }),
}));
vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()),
  agentLive: () => true,
  agentHttpBase: () => "",
}));

import { BuildSurface } from "@/components/BuildSurface";

function baseBuild(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    cid: "cid-1",
    status: "FINISHED" as ConversationStatus,
    task: "a task",
    events: [] as AgentEvent[],
    pendingActionId: null,
    streamingFile: null,
    started: true,
    modelId: null,
    setModelId: vi.fn(),
    assistChoice: false,
    setAssistChoice: vi.fn(),
    autonomousChoice: false,
    setAutonomousChoice: vi.fn(),
    preCid: null,
    submit: vi.fn(),
    submitting: false,
    submitError: null,
    plan: null,
    awaitingPlan: false,
    buildProgress: new Map(),
    approvePlan: vi.fn(),
    requestPlan: vi.fn(),
    acceptFinished: vi.fn(),
    pendingAction: null,
    confirm: vi.fn(),
    reject: vi.fn(),
    awaitingDecision: false,
    pendingAlternatives: null,
    pickAlternative: vi.fn(),
    awaitingQuestion: false,
    pendingClarify: null,
    answer: vi.fn(),
    pendingQuestion: null,
    steer: vi.fn(),
    canResume: false,
    resume: vi.fn(),
    kill: vi.fn(),
    cancel: vi.fn(),
    sandboxState: null,
    autonomous: false,
    assist: false,
    resumed: false,
    reset: vi.fn(),
    error: null,
    ...overrides,
  };
}

function renderSurface(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      json: async () => ({ sandbox_backend: null, title: null }),
    })) as unknown as typeof fetch,
  );
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("Mark done — the explicit accept-as-is control", () => {
  it("emits the explicit signal (b.acceptFinished), never the free-text replan path", async () => {
    const acceptFinished = vi.fn();
    const requestPlan = vi.fn();
    buildState = baseBuild({ acceptFinished, requestPlan });
    const user = userEvent.setup();
    renderSurface(<BuildSurface />);

    await user.click(
      screen.getByRole("button", { name: /mark the build done/i }),
    );

    expect(acceptFinished).toHaveBeenCalledTimes(1);
    // The whole point of the frame: no prose for the server to intent-match.
    expect(requestPlan).not.toHaveBeenCalled();
  });

  it("renders only on FINISHED — a live or merely-settled run has nothing to accept", () => {
    for (const status of ["RUNNING", "STUCK", "IDLE"] as ConversationStatus[]) {
      buildState = baseBuild({ status });
      const { unmount } = renderSurface(<BuildSurface />);
      expect(
        screen.queryByRole("button", { name: /mark the build done/i }),
      ).not.toBeInTheDocument();
      unmount();
    }
  });
});
