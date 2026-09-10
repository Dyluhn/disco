/**
 * UI-17 — the finish screen's stacking/overflow.
 *
 * On a finished run the pinned footer under the feed is tall (deliverable card
 * + self-host card + composer + schedules). The Activity section is `flex-1`
 * with a zero flex-basis, so it absorbed NONE of the shrink: the footer took
 * every pixel, the section was squeezed to 0px, and its non-shrinkable children
 * — the "Activity" heading and the "Event N of N" replay row — went on painting
 * outside that zero-height box, straight over the deliverable card below it.
 *
 * jsdom has no layout, so this pins the two class contracts that make the
 * overlap impossible rather than measuring boxes: the Activity section keeps a
 * height floor at `lg` and clips its own overflow, and the footer shrinks and
 * scrolls itself at `lg` instead of starving the section above it.
 */

import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
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
  agentLive: () => false,
  agentHttpBase: () => "",
}));

import { BuildSurface } from "@/components/BuildSurface";

function baseBuild(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    cid: "cid-1",
    status: "FINISHED" as ConversationStatus,
    task: "a coffee shop site",
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
    autonomous: true,
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

describe("BuildSurface — finish-screen stacking (UI-17)", () => {
  it("the Activity section keeps a height floor and clips its own overflow", () => {
    buildState = baseBuild();
    renderSurface(<BuildSurface />);
    const section = screen.getByTestId("build-activity-section");
    // A floor at every breakpoint — never squeezed to 0px by the footer.
    expect(section.className).toContain("min-h-[16rem]");
    expect(section.className).toContain("lg:min-h-[9rem]");
    expect(section.className).not.toContain("lg:min-h-0");
    // …and it can never paint outside its own box over the card below.
    expect(section.className).toContain("overflow-hidden");
  });

  it("the deliverable/composer footer shrinks and scrolls itself at lg", () => {
    buildState = baseBuild();
    renderSurface(<BuildSurface />);
    const footer = screen.getByTestId("build-pane-footer");
    expect(footer.className).toContain("lg:min-h-0");
    expect(footer.className).toContain("lg:overflow-y-auto");
  });

  it("the replay row and the footer are siblings, never nested/overlapping", () => {
    buildState = baseBuild({
      events: [{ id: "e1", kind: "message", message: { role: "user", content: "hi" } }],
    });
    renderSurface(<BuildSurface />);
    const section = screen.getByTestId("build-activity-section");
    const footer = screen.getByTestId("build-pane-footer");
    // "Event 1 of 1" lives inside the bounded Activity section…
    expect(section).toContainElement(screen.getByTestId("replay-scrubber"));
    // …and the footer is a following sibling, not a descendant of it.
    expect(section.contains(footer)).toBe(false);
    expect(section.parentElement).toBe(footer.parentElement);
    expect(
      section.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
