import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BuildSurface } from "@/components/BuildSurface";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import { setVerboseAgentChat } from "@/lib/useVerboseAgentChat";
import { deriveActivity } from "@/lib/buildTrace";
import type { AgentEvent, ConversationStatus, PlanEvent, ActionEvent } from "@/types/agent";
import type { SelectionEnvelope } from "@/lib/selectionBridge";

const buildState = vi.hoisted(() => ({ value: null as unknown }));

vi.mock("@/hooks/useBuild", () => ({
  useBuild: () => buildState.value,
}));

vi.mock("@/hooks/useBuildNotifications", () => ({
  useBuildNotifications: () => {},
}));

vi.mock("@/hooks/useProjects", () => ({
  useDownloadProject: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useExportManifest: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useProjectManifest: () => ({ data: { files: [] } }),
  useProjectRelease: () => ({ data: undefined }),
}));

vi.mock("@/hooks/useModels", () => ({
  useLiveBrowserConfig: () => ({ data: { enabled: false } }),
}));

vi.mock("@/components/build/ResizableSplit", () => ({
  ResizableSplit: ({ chat }: { chat: React.ReactNode }) => <div>{chat}</div>,
}));

vi.mock("@/components/build/BuildModelPicker", () => ({
  BuildModelPicker: () => <div data-testid="stubbed-model-picker" />,
}));

vi.mock("@/components/settings/ScheduleSection", () => ({
  ScheduleSection: () => <div data-testid="stubbed-schedule-section" />,
}));

const plan: PlanEvent = {
  id: "plan-1",
  kind: "plan",
  summary: "Build the thing",
  steps: [{ title: "Create files" }],
  revision: 1,
  source: "agent",
};

const rootListAction: ActionEvent = {
  id: "a-root-list",
  kind: "action",
  thought: "I will inspect the workspace.",
  tool_call: { tool_name: "file_list", arguments: { path: "." } },
  source: "agent",
};

const srcListAction: ActionEvent = {
  id: "a-src-list",
  kind: "action",
  thought: "I will inspect source files.",
  tool_call: { tool_name: "file_list", arguments: { path: "src" } },
  source: "agent",
};

const pendingShell: ActionEvent = {
  id: "a-shell",
  kind: "action",
  thought: "I need to run the command.",
  tool_call: { tool_name: "shell", arguments: { command: "npm install" } },
  source: "agent",
  meta: {
    risk_assessment: {
      risk: "MEDIUM",
      rationale: "Installs packages",
      analyzer: "test",
    },
  },
};

function baseBuildState(status: ConversationStatus, events: AgentEvent[]) {
  return {
    started: true,
    cid: "conv_authorb",
    task: "Make a small app",
    resumed: false,
    submitting: false,
    modelId: null,
    setModelId: vi.fn(),
    autonomousChoice: false,
    setAutonomousChoice: vi.fn(),
    assistChoice: false,
    setAssistChoice: vi.fn(),
    submit: vi.fn(),
    kill: vi.fn(),
    reset: vi.fn(),
    preCid: null,
    ensurePreCid: undefined,
    events,
    status,
    pendingActionId: status === "WAITING_FOR_CONFIRMATION" ? pendingShell.id : null,
    pendingAction: status === "WAITING_FOR_CONFIRMATION" ? pendingShell : null,
    confirm: vi.fn(),
    reject: vi.fn(),
    awaitingPlan: false,
    plan: { id: plan.id, summary: plan.summary, steps: plan.steps, revision: plan.revision, context: "" },
    approvePlan: vi.fn(),
    requestPlan: vi.fn(),
    buildProgress: new Map(),
    streamingFile: null,
    awaitingDecision: false,
    pendingAlternatives: null,
    pickAlternative: vi.fn(),
    awaitingQuestion: false,
    pendingClarify: null,
    pendingQuestion: null,
    answer: vi.fn(),
    steer: vi.fn(),
    cancel: vi.fn(),
    canResume: false,
    resume: vi.fn(),
    sandboxState: undefined,
    autonomous: false,
    assist: false,
    error: null,
    submitError: null,
  };
}

const deckSelection: SelectionEnvelope = {
  v: 1,
  channel: "disco-select",
  nonce: "n",
  type: "disco:selection",
  selection_ref: { kind: "deck", slide_id: "slide-2", element_id: "headline" },
  human_label: "Headline text",
  rect: { x: 10, y: 20, width: 120, height: 40 },
};

describe("AuthorB unbiased gate — W-14/W-26/W-28/W-43 chat and inspect", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("W-14/W-28 suppresses workspace-root 'Listed .' but still renders a real subdirectory list", () => {
    const rootOnly = deriveActivity([rootListAction], null, "RUNNING");
    expect(rootOnly.map((item) => item.label)).not.toContain("Listed .");
    expect(rootOnly).toHaveLength(0);

    const srcList = deriveActivity([srcListAction], null, "RUNNING");
    expect(srcList.map((item) => item.label)).toContain("Listed src");
  });

  it("W-26 formats any selected artifact element as steer context and discusses it only when steerable", async () => {
    const user = userEvent.setup();
    const discuss = vi.fn();

    expect(formatSelectionContext(deckSelection)).toContain("Headline text");
    expect(formatSelectionContext(deckSelection)).toContain("slide slide-2");
    expect(formatSelectionContext(deckSelection)).toContain("element headline");

    const { rerender } = render(
      <SelectionOverlay
        armed={false}
        untrusted={false}
        selection={deckSelection}
        onArm={() => {}}
        onDisarm={() => {}}
        onWalkUp={() => {}}
        onDiscuss={discuss}
      />,
    );

    await user.click(screen.getByRole("button", { name: /discuss/i }));
    expect(discuss).toHaveBeenCalledWith(deckSelection);

    rerender(
      <SelectionOverlay
        armed={false}
        untrusted={false}
        selection={deckSelection}
        onArm={() => {}}
        onDisarm={() => {}}
        onWalkUp={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: /discuss/i })).not.toBeInTheDocument();
  });

  it("W-43 verbose-chat off swaps the feed for a stage card but keeps confirmation gates rendered", () => {
    setVerboseAgentChat(false);
    buildState.value = baseBuildState("WAITING_FOR_CONFIRMATION", [plan, pendingShell]);

    render(<BuildSurface />);

    expect(screen.getByTestId("agent-stage-card")).toBeInTheDocument();
    expect(
      screen.getByRole("alertdialog", { name: /action needs your approval/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
  });

  it("W-43 quiet mode preserves artifact downloads through the stage card expansion", async () => {
    const user = userEvent.setup();
    setVerboseAgentChat(false);
    const deliverable: AgentEvent = {
      id: "d1",
      kind: "deliverable",
      title: "Research appendix",
      path: "appendix.zip",
      artifact_kind: "files",
      source: "agent",
    };
    buildState.value = baseBuildState("FINISHED", [plan, deliverable]);

    render(<BuildSurface />);

    expect(screen.getByTestId("agent-stage-card")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /download the deliverable/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /expand agent history/i }));
    const expanded = screen.getByTestId("agent-stage-card");
    expect(within(expanded).getByText(/Delivered: Research appendix/i)).toBeInTheDocument();
    expect(
      within(expanded).getByRole("link", { name: /Research appendix/i }),
    ).toHaveAttribute("data-download-kind", "file");
  });

  it("W-43 exposes the full feed in the Agent History inspector tab", async () => {
    const user = userEvent.setup();
    const events: AgentEvent[] = [
      {
        id: "write-1",
        kind: "action",
        thought: "Create the page",
        tool_call: {
          tool_name: "file_write",
          arguments: { path: "index.html", content: "<h1>Hello</h1>" },
        },
        source: "agent",
      },
      {
        id: "obs-1",
        kind: "observation",
        action_id: "write-1",
        tool_result: {
          tool_name: "file_write",
          success: true,
          content: "ok",
          structured: null,
        },
        source: "environment",
      },
    ];

    render(<AgentCanvas events={events} status="FINISHED" cid="conv_authorb" onSteer={() => {}} />);
    await user.click(screen.getByRole("tab", { name: /agent history/i }));

    // The ACTION (file_write) is in the feed…
    expect(screen.getByText(/Wrote index.html/i)).toBeInTheDocument();

    // …and its MATCHING observation/tool-result is preserved too — the full feed, not
    // just one row. Drill into the action's expandable and assert BOTH the tool-call
    // detail (path + content) AND the observation output ("ok") are surfaced together.
    await user.click(screen.getByRole("button", { name: /index\.html/i }));

    // The matched tool-call detail (command/path + the written content) — scoped to the
    // leaf <pre> so we assert the raw arguments block itself, not an ancestor.
    expect(screen.getByText("file_write")).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, node) =>
          node?.tagName === "PRE" &&
          (node.textContent?.includes('"index.html"') ?? false) &&
          (node.textContent?.includes("<h1>Hello</h1>") ?? false),
      ),
    ).toBeInTheDocument();
    // The observation tool_result content, labelled as Output (proves the observation
    // event — not just the action — survived into the inspector).
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
  });
});
