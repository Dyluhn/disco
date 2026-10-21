import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ActionEvent, AgentEvent, ConversationStatus } from "@/types/agent";
import type { StreamingFile } from "@/hooks/useBuildStream";
import { ExecutionCanvas } from "./ExecutionCanvas";

vi.mock("./canvas/PreviewPane", () => ({ PreviewPane: () => <div>App preview</div> }));

function write(seq: number, tool = "file_write", content = "phase one"): ActionEvent {
  return {
    id: `write-${seq}`, seq, kind: "action", thought: "",
    tool_call: { tool_name: tool, arguments: { path: "probe.txt", content, old: "phase one", new: content } },
  };
}
const partial: StreamingFile = { path: "probe.txt", tool: "file_write", content: "long partial contents for streaming" };
type Values = {
  events: AgentEvent[]; streamingFile: StreamingFile | null; replayThrough: number;
  cid: string; status: ConversationStatus;
};
function canvasFixture(initial: Partial<Values> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let props: Values = { events: [], streamingFile: null, replayThrough: 0, cid: "snap-session", status: "RUNNING", ...initial };
  const canvas = () => <QueryClientProvider client={client}><ExecutionCanvas {...props} /></QueryClientProvider>;
  const view = render(canvas());
  return { ...view, update(next: Partial<Values>) { props = { ...props, ...next }; view.rerender(canvas()); } };
}
const expectTab = (name: string) => expect(screen.getByRole("tab", { name: name === "Files" ? /^Files(?: writing)?$/ : name })).toHaveAttribute("aria-selected", "true");

beforeEach(() => localStorage.clear());

describe("ExecutionCanvas durable file action snapping", () => {
  it.each(["file_write", "file_append", "file_edit"])("snaps to Files for a short nonstreamed %s action", async (tool) => {
    const user = userEvent.setup();
    const view = canvasFixture();
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ events: [write(1, tool)] }); // Nine characters, no ephemeral buffer.
    expectTab("Files");
  });

  it("does not snap twice when a streamed write lands after the user navigates away", async () => {
    const user = userEvent.setup();
    const view = canvasFixture();
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ streamingFile: partial });
    expectTab("Files");
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ streamingFile: null, events: [write(1, "file_write", partial.content)] });
    expectTab("Preview");
    // Another actual write to the same path is a new action, not completion.
    view.update({ events: [write(1), write(2, "file_write", "phase two")] });
    expectTab("Files");
  });

  it("honors OFF and does not snap retrospectively when reenabled", async () => {
    const user = userEvent.setup();
    const view = canvasFixture();
    const toggle = screen.getByRole("switch", { name: /snap to action/i });
    await user.click(toggle);
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ events: [write(1)] });
    expectTab("Preview");
    await user.click(toggle);
    expectTab("Preview");
    view.update({ events: [write(1), write(2)] });
    expectTab("Files");
  });

  it("ignores mounted history, replayed history and duplicate action updates after manual navigation", async () => {
    const user = userEvent.setup();
    const view = canvasFixture({ events: [write(1)], replayThrough: 3 });
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ events: [write(1), write(2), write(3)] });
    expectTab("Preview");
    view.update({ events: [write(1), write(2), write(3), write(4)] });
    expectTab("Files");
    await user.click(screen.getByRole("tab", { name: "Terminal" }));
    view.update({ events: [write(1), write(2), write(3), { ...write(4), thought: "updated metadata" }] });
    expectTab("Terminal");
    view.update({ replayThrough: 8, events: [write(1), write(2), write(3), write(4), write(7), write(8)] });
    expectTab("Terminal");
    view.update({ events: [write(9)] });
    expectTab("Files");
  });

  it("forgets a pre-reconnect stream when a later independent write uses the same path", async () => {
    const user = userEvent.setup();
    const view = canvasFixture();
    view.update({ streamingFile: partial });
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ replayThrough: 5, streamingFile: null, events: [write(5)] });
    expectTab("Preview");
    view.update({ events: [write(5), write(6)] });
    expectTab("Files");
  });

  it("does not let an abandoned partial stream suppress a fresh write after resume", async () => {
    const user = userEvent.setup();
    const view = canvasFixture();
    view.update({ streamingFile: partial });
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ streamingFile: null, status: "PAUSED" });
    view.update({ status: "RUNNING", events: [write(1)] });
    expectTab("Files");
  });

  it("baselines another conversation's history and does not treat reads or stopped history as writes", async () => {
    const user = userEvent.setup();
    const view = canvasFixture({ events: [write(1)] });
    await user.click(screen.getByRole("tab", { name: "Preview" }));
    view.update({ cid: "another-session", events: [write(20)], replayThrough: 20 });
    expectTab("Preview");
    view.update({ events: [write(20), write(21, "file_read")] });
    expectTab("Preview");
    view.update({ events: [write(22)], status: "PAUSED" });
    expectTab("Preview");
    view.update({ status: "RUNNING" });
    expectTab("Preview");
    view.update({ events: [write(23)] });
    expectTab("Files");
  });
});
