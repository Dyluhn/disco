/**
 * Watch-it-write: the ExecutionCanvas renders the file the driver is composing
 * RIGHT NOW (the `streamingFile` buffer), live, before the authoritative
 * ActionEvent lands. Proves the streamed content shows with its filename and a
 * writing indicator — the "I can see it's not hung" guarantee.
 *
 * BP-14 moved the useSessions polling hook to the ExecutionCanvas level (the
 * Terminal tab badge needs canvas-level polling), so rendering the canvas now
 * requires a QueryClientProvider. agentLive() is false under vitest (empty
 * AGENT_BASE) → getSessions short-circuits to the fixture, no network.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { StreamingFile } from "@/hooks/useBuildStream";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

vi.mock("@/components/build/canvas/PreviewPane", () => ({
  PreviewPane: () => <div data-testid="preview-pane" />,
}));

const streaming: StreamingFile = {
  path: "styles.css",
  tool: "file_write",
  field: "content",
  content: "/* dark theme */\nbody { background: #0b0b0f; }",
};

const streamingEdit: StreamingFile = {
  path: "src/App.tsx",
  tool: "file_edit",
  field: "new",
  content: "return <main>Live edit</main>;",
};

function renderCanvas(
  streamingFile: StreamingFile | null,
  events: AgentEvent[] = [],
  status: ConversationStatus = "RUNNING",
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const canvas = (
    value: StreamingFile | null,
    eventValue: AgentEvent[],
    statusValue: ConversationStatus,
  ) => (
    <QueryClientProvider client={qc}>
      <ExecutionCanvas
        events={eventValue}
        status={statusValue}
        cid="c1"
        streamingFile={value}
      />
    </QueryClientProvider>
  );
  const view = render(canvas(streamingFile, events, status));
  return {
    ...view,
    rerenderCanvas: (
      value: StreamingFile | null,
      eventValue: AgentEvent[] = events,
      statusValue: ConversationStatus = status,
    ) => view.rerender(canvas(value, eventValue, statusValue)),
  };
}

const NOTE: AgentEvent = {
  id: "note",
  kind: "action",
  thought: "",
  tool_call: {
    tool_name: "file_write",
    arguments: { path: "notes.txt", content: "not web" },
  },
} as AgentEvent;

const HTML: AgentEvent = {
  id: "html",
  kind: "action",
  thought: "",
  tool_call: {
    tool_name: "file_write",
    arguments: { path: "index.html", content: "<h1>Done</h1>" },
  },
} as AgentEvent;

describe("ExecutionCanvas — watch-it-write", () => {
  beforeEach(() => window.localStorage.clear());

  it("shows the streaming file's content + filename while it writes", () => {
    renderCanvas(streaming);
    // the live content is on screen (not waiting for the final event)
    expect(screen.getByText(/background: #0b0b0f/)).toBeInTheDocument();
    // the filename is shown whole (the path-completeness fix), more than once is fine
    expect(screen.getAllByText("styles.css").length).toBeGreaterThan(0);
    // a "writing" affordance is present (the byte/status line)
    expect(screen.getByText(/writing/i)).toBeInTheDocument();
  });

  it("falls back to the empty files state when nothing is streaming", () => {
    renderCanvas(null);
    expect(screen.getByText(/No files written yet/i)).toBeInTheDocument();
  });

  it("shows file_edit replacement text as an editing stream", () => {
    renderCanvas(streamingEdit);
    expect(screen.getByText(/Live edit/)).toBeInTheDocument();
    expect(screen.getAllByText("src/App.tsx").length).toBeGreaterThan(0);
    expect(screen.getByText(/editing/i)).toBeInTheDocument();
  });

  it("snaps to Files on a new write when Snap to action is enabled", async () => {
    const user = userEvent.setup();
    const { container, rerenderCanvas } = renderCanvas(null);

    await user.click(screen.getByRole("tab", { name: "Preview" }));
    expect(container.querySelector("[data-active-tab]"))?.toHaveAttribute(
      "data-active-tab",
      "preview",
    );

    rerenderCanvas(streaming);
    expect(container.querySelector("[data-active-tab]"))?.toHaveAttribute(
      "data-active-tab",
      "files",
    );
  });

  it("keeps the selected tab on a new write when Snap to action is disabled", async () => {
    const user = userEvent.setup();
    const { container, rerenderCanvas } = renderCanvas(null);
    const toggle = screen.getByRole("switch", { name: /snap to action/i });

    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(window.localStorage.getItem("disco-snap-to-action")).toBe("0");

    await user.click(screen.getByRole("tab", { name: "Preview" }));
    rerenderCanvas(streaming);
    expect(container.querySelector("[data-active-tab]"))?.toHaveAttribute(
      "data-active-tab",
      "preview",
    );
  });

  it("snaps only on the rising edge of one write", async () => {
    const user = userEvent.setup();
    const { container, rerenderCanvas } = renderCanvas(null);

    await user.click(screen.getByRole("tab", { name: "Preview" }));
    rerenderCanvas(streaming);
    expect(container.querySelector("[data-active-tab]")).toHaveAttribute(
      "data-active-tab",
      "files",
    );

    await user.click(screen.getByRole("tab", { name: "Preview" }));
    rerenderCanvas(streamingEdit);
    expect(container.querySelector("[data-active-tab]")).toHaveAttribute(
      "data-active-tab",
      "preview",
    );

    rerenderCanvas(null);
    rerenderCanvas(streaming);
    expect(container.querySelector("[data-active-tab]")).toHaveAttribute(
      "data-active-tab",
      "files",
    );
  });

  it("persists the local preference across canvas remounts", async () => {
    const user = userEvent.setup();
    const first = renderCanvas(null);
    await user.click(screen.getByRole("switch", { name: /snap to action/i }));
    first.unmount();

    renderCanvas(null);
    expect(screen.getByRole("switch", { name: /snap to action/i })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("keeps the Snap switch outside the ARIA tablist", () => {
    renderCanvas(null);
    const tablist = screen.getByRole("tablist");
    expect(
      within(tablist).queryByRole("switch", { name: /snap to action/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /snap to action/i })).toBeInTheDocument();
  });

  it("still switches once to Preview on finish when Snap is off", async () => {
    const user = userEvent.setup();
    const { container, rerenderCanvas } = renderCanvas(null, [NOTE]);
    await user.click(screen.getByRole("switch", { name: /snap to action/i }));
    await user.click(screen.getByRole("tab", { name: "Terminal" }));

    rerenderCanvas(null, [NOTE, HTML], "FINISHED");
    expect(container.querySelector("[data-active-tab]")).toHaveAttribute(
      "data-active-tab",
      "preview",
    );
  });
});
