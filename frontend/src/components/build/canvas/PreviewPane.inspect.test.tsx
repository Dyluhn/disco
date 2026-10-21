/** Real selection hooks: Inspect and Point never intercept together. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PreviewPane } from "./PreviewPane";
import type { AgentEvent } from "@/types/agent";

vi.mock("@/hooks/useBuildPreview", () => ({ useBuildPreview: () => ({ data: {
  available: true, status: "running", generation: "inspect-generation", reload_strategy: "reload",
} }) }));
vi.mock("@/hooks/useWorkspaceVersions", () => ({
  useWorkspaceVersions: () => ({ versions: [], loading: false, refetch: vi.fn() }),
}));
vi.mock("@/api/client", async () => ({
  ...await vi.importActual<typeof import("@/api/client")>("@/api/client"),
  canonicalPreviewBootstrapUrl: vi.fn(async () => ({
    url: "http://127.77.0.5:8000/__disco/preview-auth", intent: "canonical-inspect",
  })),
}));
const HTML = {
  id: "write-index", kind: "action", thought: "",
  tool_call: { tool_name: "file_write", arguments: { path: "index.html", content: "<h1>Done</h1>" } },
} as AgentEvent;
beforeEach(() => window.localStorage.setItem("disco-inspect-tip-seen", "1"));
afterEach(() => vi.restoreAllMocks());

function mount(editable = false, untrusted = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <PreviewPane status="RUNNING" cid="conv_inspect" events={[HTML]} untrusted={untrusted}
      onSteer={vi.fn()} onElementMention={vi.fn()} onSelectionEdit={editable ? vi.fn() : undefined} />
  </QueryClientProvider>);
}
async function setup(editable = false) {
  mount(editable);
  const frame = await screen.findByTitle("Preview") as HTMLIFrameElement;
  return { user: userEvent.setup(), postMessage: vi.spyOn(frame.contentWindow!, "postMessage") };
}
const inspect = () => screen.getByRole("button", { name: "Inspect" });
const point = () => screen.getByRole("button", { name: "Point at element" });
const edit = () => screen.getByRole("button", { name: /click-to-edit mode/i });
function select(nonce: string) {
  act(() => window.dispatchEvent(new MessageEvent("message", {
    origin: "http://127.77.0.5:8000",
    data: {
      v: 1, channel: "disco-select", nonce, type: "disco:selection",
      selection_ref: { kind: "source", oid: "index.html:1", file: "index.html", line: 1 },
      human_label: "Selected heading", rect: { x: 5, y: 5, width: 80, height: 30 },
    },
  })));
}

it("plain Inspect exposes Stop inspecting and rejects the dismissed nonce without Edit", async () => {
  const { user, postMessage } = await setup();
  expect(screen.queryByRole("button", { name: /click-to-edit mode/i })).not.toBeInTheDocument();
  await user.click(inspect());
  const command = postMessage.mock.calls.at(-1)![0];
  expect(command.type).toBe("disco:overlay:arm");
  select(command.nonce);
  expect(screen.getByText("Selected heading")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Discuss" })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/describe the change/i)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Stop inspecting" }));
  expect(postMessage.mock.calls.at(-1)![0]).toMatchObject({ type: "disco:overlay:disarm", nonce: command.nonce });
  expect(inspect()).toBeInTheDocument();
  expect(screen.queryByText("Selected heading")).not.toBeInTheDocument();
  select(command.nonce);
  expect(screen.queryByText("Selected heading")).not.toBeInTheDocument();
});

it("switching Inspect to Point disarms inspection and clears the old selection", async () => {
  const { user, postMessage } = await setup();
  await user.click(inspect());
  const nonce = postMessage.mock.calls.at(-1)![0].nonce;
  select(nonce);
  postMessage.mockClear();
  await user.click(point());
  expect(postMessage.mock.calls.map(([command]) => command.type)).toEqual([
    "disco:overlay:disarm", "disco-element-mention:arm",
  ]);
  expect(screen.getByRole("button", { name: "Cancel element mention" })).toHaveAttribute("aria-pressed", "true");
  expect(inspect()).toBeInTheDocument();
  expect(screen.queryByText("Selected heading")).not.toBeInTheDocument();
  select(nonce);
  expect(screen.queryByText("Selected heading")).not.toBeInTheDocument();
});

it("switching Point to Inspect disarms Point before arming inspection", async () => {
  const { user, postMessage } = await setup();
  await user.click(point());
  postMessage.mockClear();
  await user.click(inspect());
  expect(postMessage.mock.calls.map(([command]) => command.type)).toEqual([
    "disco-element-mention:disarm", "disco:overlay:arm",
  ]);
  expect(point()).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "Stop inspecting" })).toBeInTheDocument();
});

it("Edit retains its edit box and switching to Point exits both editing and selection", async () => {
  const { user, postMessage } = await setup(true);
  await user.click(edit());
  select(postMessage.mock.calls.at(-1)![0].nonce);
  expect(screen.getByPlaceholderText(/describe the change/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Stop inspecting" })).toBeInTheDocument();
  await user.click(point());
  expect(screen.queryByPlaceholderText(/describe the change/i)).not.toBeInTheDocument();
  expect(edit()).toHaveAttribute("aria-pressed", "false");
  expect(inspect()).toBeInTheDocument();
});

it("untrusted artifacts expose no Inspect, Point, or Edit controls", () => {
  mount(true, true);
  expect(screen.getByTitle("Static artifact viewer")).toHaveAttribute("sandbox", "");
  expect(screen.queryByRole("button", { name: "Inspect" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Point at element" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /click-to-edit mode/i })).not.toBeInTheDocument();
});
