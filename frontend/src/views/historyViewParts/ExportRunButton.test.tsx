import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExportRunButton } from "./ExportRunButton";
const load = vi.hoisted(() => vi.fn());
vi.mock("@/api/agentParts/share", () => ({ exportConversationBundle: load }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("Export run", () => {
  it("downloads the server bundle without issuing a public link", async () => {
    load.mockResolvedValue({ version: 1, events: [{ kind: "message" }] });
    const create = vi.fn<(blob: Blob) => string>(() => "blob:test");
    vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: vi.fn() });
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<ExportRunButton cid="conv_test" title="Example" />);
    fireEvent.click(screen.getByRole("button", { name: "Export run: Example" }));
    await waitFor(() => expect(clicked).toHaveBeenCalledOnce());
    expect(load).toHaveBeenCalledWith("conv_test");
    expect(create.mock.calls[0][0]).toBeInstanceOf(Blob);
  });
  it("keeps a failed export retryable and displays a readable error", async () => {
    load.mockRejectedValue(new Error('{"detail":"unavailable"}'));
    render(<ExportRunButton cid="conv_error" title="Error example" />);
    fireEvent.click(screen.getByRole("button", { name: "Export run: Error example" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't export this run");
    expect(screen.getByRole("button", { name: "Export run: Error example" })).toBeEnabled();
  });
});
