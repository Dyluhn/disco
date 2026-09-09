/**
 * UI-10 regression — the finish summary never hands the user a sandbox-local link.
 *
 * A finished build's completion message said the site "is served via preview at
 * http://localhost:8000/". That address belongs to the agent's sandbox; pasted
 * into the user's browser it opens nothing. The rewrite happens at the RENDER
 * layer, so this drives the real BuildSurface with a real trailing agent message
 * and checks what the user actually sees — and, just as importantly, that the
 * stored event is untouched.
 *
 * Driven with a MOCKED useBuild (the BuildSurface.markdone.test.tsx pattern) so
 * the terminal status and the event log can be pinned exactly.
 */

import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

let buildState: Record<string, unknown>;

vi.mock("@/hooks/useBuild", () => ({
  useBuild: () => buildState,
}));
vi.mock("@/hooks/useProjects", () => ({
  useDownloadProject: () => ({ mutate: vi.fn(), isPending: false, error: null }),
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

/** The verbatim shape of the completion message the defect was filed against. */
const FINISH_MESSAGE =
  "The coffee shop site is complete. It is served via preview at http://localhost:8000/.";

function agentMessage(content: string): AgentEvent {
  return {
    id: "ev-final",
    kind: "message",
    source: "agent",
    message: { role: "assistant", content },
  } as unknown as AgentEvent;
}

function baseBuild(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    cid: "cid-finish",
    status: "FINISHED" as ConversationStatus,
    task: "a coffee shop site",
    events: [agentMessage(FINISH_MESSAGE)],
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

describe("Finish summary — sandbox-local URLs (UI-10)", () => {
  it("shows no sandbox loopback address, and points at the Preview tab instead", () => {
    buildState = baseBuild();
    renderSurface(<BuildSurface />);

    const summary = document.querySelector('[data-testid="build-final-summary"]');
    expect(summary).not.toBeNull();
    const shown = summary?.textContent ?? "";

    // The dead address is gone in every form.
    expect(shown).not.toMatch(/localhost/i);
    expect(shown).not.toMatch(/127\.0\.0\.1/);
    expect(shown).not.toMatch(/0\.0\.0\.0/);
    // Replaced by the way in that actually works, and the rest of the sentence
    // still reads as the agent wrote it.
    expect(shown).toContain("The coffee shop site is complete.");
    expect(shown).toContain("the Preview tab");
    // The user is told the substitution happened and where to go.
    expect(summary?.querySelector('[data-disco-note="sandbox-url-rewritten"]')).not.toBeNull();
    expect(screen.getByText(/open the site with the preview tab or the open button/i))
      .toBeInTheDocument();
  });

  it("leaves the model's own event untouched — the rewrite is presentation only", () => {
    const events = [agentMessage(FINISH_MESSAGE)];
    buildState = baseBuild({ events });
    renderSurface(<BuildSurface />);

    const stored = events[0] as unknown as { message: { content: string } };
    expect(stored.message.content).toBe(FINISH_MESSAGE);
  });

  it("renders a clean summary verbatim, with no substitution notice", () => {
    const clean = "The signup page is complete and correct. Use the Preview tab to look at it.";
    buildState = baseBuild({ events: [agentMessage(clean)] });
    renderSurface(<BuildSurface />);

    const summary = document.querySelector('[data-testid="build-final-summary"]');
    expect(summary?.textContent).toContain(clean);
    expect(summary?.querySelector('[data-disco-note="sandbox-url-rewritten"]')).toBeNull();
  });
});
