import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactsPane } from "./ArtifactsPane";
import type { AgentEvent } from "@/types/agent";
import type { PreviewLaunch } from "@/api/canvas";

const { mint, disarm, reset } = vi.hoisted(() => ({ mint: vi.fn(), disarm: vi.fn(), reset: vi.fn() }));
vi.mock("@/api/canvas", async (importOriginal) => ({ ...await importOriginal<typeof import("@/api/canvas")>(), mintPathPreviewLaunch: mint }));
vi.mock("@/hooks/useElementSelect", () => ({ useElementSelect: () => ({
  armed: true, arm: vi.fn(), disarm, resetSelection: reset, selection: null, walkUp: vi.fn(),
}) }));
const events = [
  { id: "file", kind: "action", thought: "", tool_call: { tool_name: "file_write", arguments: { path: "index.html", content: "<input>" } } },
  { id: "app", kind: "deliverable", title: "App", path: "index.html", artifact_kind: "app", deployment_url: "" },
  { id: "version", kind: "workspace_version", version_seq: 1, tree_digest: "tree", trigger: "finish" },
] as AgentEvent[];
const first = { url: "http://preview/bootstrap", intent: "first", accessDeadline: 1 };
beforeEach(() => { vi.clearAllMocks(); mint.mockResolvedValue(first); });

describe("artifact access expiry", () => {
  it("preserves the frame on cancelled or failed reload, disarms selection, and keeps StrictMode mint singular", async () => {
    const { getByTitle } = render(<StrictMode><ArtifactsPane cid="cid" events={events} status="FINISHED" untrusted={false} /></StrictMode>);
    await screen.findByText(/Preview access expired/);
    expect(mint).toHaveBeenCalledTimes(1);
    expect(disarm).toHaveBeenCalled();
    expect(reset).toHaveBeenCalled();
    const frame = getByTitle("Artifact preview");
    expect(frame.parentElement).toHaveAttribute("inert");
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(getByTitle("Artifact preview")).toBe(frame);
    mint.mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    await act(async () => fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reload Preview" })));
    expect(screen.getByRole("alert")).toHaveTextContent("could not be reloaded");
    expect(getByTitle("Artifact preview")).toBe(frame);
    expect(mint).toHaveBeenCalledTimes(2);
  });

  it("ignores an old artifact reload reply after the selected conversation changes", async () => {
    const { rerender } = render(<ArtifactsPane cid="old" events={events} status="FINISHED" untrusted={false} />);
    await screen.findByText(/Preview access expired/);
    let resolve!: (launch: PreviewLaunch) => void;
    mint.mockReturnValueOnce(new Promise<PreviewLaunch>((r) => { resolve = r; }));
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reload Preview" }));
    mint.mockResolvedValueOnce({ ...first, intent: "new", accessDeadline: Date.now() + 900_000 });
    rerender(<ArtifactsPane cid="new" events={events} status="FINISHED" untrusted={false} />);
    await waitFor(() => expect(screen.queryByText(/Preview access expired/)).toBeNull());
    const frame = screen.getByTitle("Artifact preview");
    await act(async () => resolve({ ...first, intent: "stale" }));
    expect(screen.getByTitle("Artifact preview")).toBe(frame);
    expect(screen.queryByText(/Preview access expired/)).toBeNull();
  });
  it("keeps a confirmed reload when an older navigation mint and later same-generation render settle", async () => {
    const { rerender } = render(<ArtifactsPane cid="old" events={events} status="FINISHED" untrusted={false} />);
    await screen.findByText(/Preview access expired/);
    let resolve!: (launch: PreviewLaunch) => void;
    mint.mockReturnValueOnce(new Promise<PreviewLaunch>((r) => { resolve = r; }));
    rerender(<ArtifactsPane cid="new" events={events} status="FINISHED" untrusted={false} />);
    mint.mockResolvedValueOnce({ ...first, intent: "fresh", accessDeadline: Date.now() + 900_000 });
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    await act(async () => fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reload Preview" })));
    const frame = screen.getByTitle("Artifact preview");
    await act(async () => resolve({ ...first, intent: "old-navigation" }));
    const changed = [...events, { id: "updated-file", kind: "action", thought: "", tool_call: {
      tool_name: "file_write", arguments: { path: "index.html", content: "<input><p>updated</p>" },
    } } as AgentEvent];
    rerender(<ArtifactsPane cid="new" events={changed} status="FINISHED" untrusted={false} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTitle("Artifact preview")).toBe(frame);
    expect(screen.queryByText(/Preview access expired/)).toBeNull();
    expect(mint).toHaveBeenCalledTimes(3);
  });

});
