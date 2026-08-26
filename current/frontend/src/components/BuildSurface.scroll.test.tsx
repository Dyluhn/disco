/**
 * BuildSurface — resumed-task recovery (W-01) + feed scroll affordances (W-40/W-41).
 *
 * These exercise the surface with a MOCKED useBuild so we can drive exact statuses
 * and event streams (the App-fixture integration test can't reach the resume path or
 * synthesize gate→gate transitions). jsdom has no layout, so scroll geometry is
 * stubbed on the feed node directly.
 *
 *  - W-01: a resumed build seeds task="(resumed)"; until the canonical stored title
 *          arrives, the H1 must render a neutral label rather than replayed prompt text.
 *  - W-40: the AWAITING_PLAN_APPROVAL gate scrolls the feed to the TOP (the plan
 *          renders there); other gates still scroll to the BOTTOM.
 *  - W-41: a jump-to-latest chevron appears only when scrolled up past the threshold.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

// ── mocks ──────────────────────────────────────────────────────────────────
// The build state the surface reads. Mutable so each test sets it before render.
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
  agentLive: () => false,
  agentHttpBase: () => "",
}));

import { BuildSurface } from "@/components/BuildSurface";

function baseBuild(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    cid: null,
    status: "RUNNING" as ConversationStatus,
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
    resumed: true,
    reset: vi.fn(),
    error: null,
    ...overrides,
  };
}

function userMessage(id: string, content: string): AgentEvent {
  return {
    kind: "message",
    id,
    source: "user",
    message: { role: "user", content },
  } as AgentEvent;
}

function renderSurface(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (node: ReactElement) => (
    <QueryClientProvider client={qc}>{node}</QueryClientProvider>
  );
  const result = render(wrap(ui));
  return {
    ...result,
    rerenderSurface: (node: ReactElement) => result.rerender(wrap(node)),
  };
}

function feedEl(): HTMLElement {
  return screen.getByTestId("build-activity-feed");
}

/** Stub layout geometry on the feed node (jsdom reports 0 for everything). */
function stubGeometry(
  el: HTMLElement,
  geom: { scrollHeight: number; clientHeight: number; scrollTop: number },
) {
  let top = geom.scrollTop;
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    value: geom.scrollHeight,
  });
  Object.defineProperty(el, "clientHeight", {
    configurable: true,
    value: geom.clientHeight,
  });
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = v;
    },
  });
  (el as HTMLElement & { scrollTo: ReturnType<typeof vi.fn> }).scrollTo =
    vi.fn();
}

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("W-01: resumed builds recover the real task in the H1", () => {
  // Keep the sealed historical test id while enforcing the corrected contract:
  // replayed prompt text is no longer an acceptable title source.
  it("renders the first user message instead of the '(resumed)' sentinel", () => {
    buildState = baseBuild({
      task: "(resumed)",
      events: [
        userMessage("u1", "<build_brief>build a habit tracker</build_brief>"),
      ],
    });
    renderSurface(<BuildSurface resumeCid="cid-1" />);
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading.textContent).toBe("Resumed project");
    expect(heading.textContent).not.toContain("build_brief");
    expect(screen.queryByText("(resumed)")).not.toBeInTheDocument();
  });

  it("falls back to 'Resumed project' when no user message exists yet", () => {
    buildState = baseBuild({ task: "(resumed)", events: [] });
    renderSurface(<BuildSurface resumeCid="cid-1" />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "Resumed project",
    );
  });

  it("renders a normal (non-sentinel) task verbatim", () => {
    buildState = baseBuild({ task: "ship the landing page", events: [] });
    renderSurface(<BuildSurface />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "ship the landing page",
    );
  });
});

describe("W-40: gate scroll target — plan gate pulls to TOP, others to BOTTOM", () => {
  it("scrolls the feed to the TOP when the plan-approval gate opens", () => {
    buildState = baseBuild({ status: "RUNNING" });
    const { rerenderSurface } = renderSurface(<BuildSurface />);
    const feed = feedEl();
    stubGeometry(feed, { scrollHeight: 1234, clientHeight: 300, scrollTop: 0 });
    const scrollTo = (
      feed as HTMLElement & { scrollTo: ReturnType<typeof vi.fn> }
    ).scrollTo;

    buildState = baseBuild({ status: "AWAITING_PLAN_APPROVAL" });
    rerenderSurface(<BuildSurface />);

    expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
  });

  it("scrolls the feed to the BOTTOM for a confirmation gate", () => {
    buildState = baseBuild({ status: "RUNNING" });
    const { rerenderSurface } = renderSurface(<BuildSurface />);
    const feed = feedEl();
    stubGeometry(feed, { scrollHeight: 1234, clientHeight: 300, scrollTop: 0 });
    const scrollTo = (
      feed as HTMLElement & { scrollTo: ReturnType<typeof vi.fn> }
    ).scrollTo;

    buildState = baseBuild({ status: "WAITING_FOR_CONFIRMATION" });
    rerenderSurface(<BuildSurface />);

    expect(scrollTo).toHaveBeenCalledWith({ top: 1234, behavior: "smooth" });
  });
});

describe("WO-9: the workspace-zip action is labelled 'Download source'", () => {
  it("renders a 'Download source' button (not 'Export') in the resumed header", () => {
    buildState = baseBuild({
      cid: "cid-9",
      resumed: true,
      status: "FINISHED",
      events: [
        { id: "finish-status", seq: 10, kind: "status", status: "FINISHED", source: "system" },
        {
          id: "finish-version",
          seq: 12,
          kind: "workspace_version",
          version_seq: 3,
          tree_digest: "tree-3",
          trigger: "finish",
          source: "system",
          final_seal: {
            schema_version: 1,
            scope: { namespace: "workspace.tree", identifier: "cid-9" },
            terminal_seq: 10,
            latest_effect_seq: null,
            version_seq: 3,
            tree_digest: "tree-3",
            file_count: 1,
            total_bytes: 1,
          },
        },
      ] as AgentEvent[],
    });
    renderSurface(<BuildSurface resumeCid="cid-9" />);
    expect(
      screen.getByRole("button", { name: "Download source" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^export$/i }),
    ).not.toBeInTheDocument();
  });
});

describe("W-41: scroll-to-latest chevron", () => {
  it("is hidden at the bottom and appears once scrolled up past the threshold", async () => {
    const user = userEvent.setup();
    buildState = baseBuild({ status: "RUNNING" });
    renderSurface(<BuildSurface />);
    const feed = feedEl();
    // Mounted at the bottom → no chevron.
    stubGeometry(feed, {
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    fireEvent.scroll(feed);
    expect(
      screen.queryByRole("button", { name: /scroll to latest activity/i }),
    ).not.toBeInTheDocument();

    // Scroll up well past the 200px threshold → chevron appears.
    feed.scrollTop = 0;
    fireEvent.scroll(feed);
    const chevron = screen.getByRole("button", {
      name: /scroll to latest activity/i,
    });
    expect(chevron).toBeInTheDocument();
    expect(screen.getByTestId("build-scroll-to-latest-dock")).toContainElement(
      chevron,
    );
    expect(chevron).not.toHaveClass("absolute");

    // Clicking jumps to the bottom and dismisses the chevron.
    const scrollTo = (
      feed as HTMLElement & { scrollTo: ReturnType<typeof vi.fn> }
    ).scrollTo;
    await user.click(chevron);
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "smooth" });
    expect(
      screen.queryByRole("button", { name: /scroll to latest activity/i }),
    ).not.toBeInTheDocument();
  });

  it("uses an immediate scroll when the operator prefers reduced motion", async () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    const user = userEvent.setup();
    buildState = baseBuild({ status: "RUNNING" });
    renderSurface(<BuildSurface />);
    const feed = feedEl();
    stubGeometry(feed, { scrollHeight: 1000, clientHeight: 300, scrollTop: 0 });
    fireEvent.scroll(feed);

    await user.click(
      screen.getByRole("button", { name: /scroll to latest activity/i }),
    );

    const scrollTo = (
      feed as HTMLElement & { scrollTo: ReturnType<typeof vi.fn> }
    ).scrollTo;
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "auto" });
  });
});
