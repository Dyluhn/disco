/**
 * BP-11 upload composer tests.
 *
 * Covers:
 *   • UploadComposer renders the paperclip button
 *   • Drop state: data-dragging attribute is set on dragover, cleared on drop/leave
 *   • "Drop files to upload" label appears while dragging
 *   • Disabled when cid is null
 *   • Upload announcement ("User uploaded: …" environment message) surfaces in
 *     the activity feed as a NEUTRAL system_note — not hidden by the BP-05
 *     env-message valve, not ⚠-styled as a warning
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { UploadComposer } from "@/components/build/BuildSurface";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { deriveActivity } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";
import * as agentApi from "@/api/agent";

// Silence the toast (no provider in test)
vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

// Stub uploadFiles so we don't hit network
vi.spyOn(agentApi, "uploadFiles").mockResolvedValue({ saved: [], rejected: [] });

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UploadComposer", () => {
  it("renders the paperclip attach button", () => {
    render(<UploadComposer cid="conv_test" />);
    expect(screen.getByRole("button", { name: /attach files/i })).toBeInTheDocument();
  });

  it("disables the button when cid is null", () => {
    render(<UploadComposer cid={null} />);
    expect(screen.getByRole("button", { name: /attach files/i })).toBeDisabled();
  });

  it("shows drop highlight while dragging over", () => {
    render(<UploadComposer cid="conv_test" />);
    const group = screen.getByRole("group", { name: /upload files/i });

    // No dragging initially
    expect(group.getAttribute("data-dragging")).toBeNull();

    fireEvent.dragOver(group, {
      dataTransfer: { files: [] },
      preventDefault: vi.fn(),
    });

    expect(group.getAttribute("data-dragging")).not.toBeNull();
  });

  it("shows 'Drop files to upload' text while dragging", () => {
    render(<UploadComposer cid="conv_test" />);
    const group = screen.getByRole("group");

    fireEvent.dragOver(group, { preventDefault: vi.fn() });

    expect(screen.getByText(/drop files to upload/i)).toBeInTheDocument();
  });

  it("clears drop highlight on drop", () => {
    render(<UploadComposer cid="conv_test" />);
    const group = screen.getByRole("group");

    fireEvent.dragOver(group, { preventDefault: vi.fn() });
    expect(group.getAttribute("data-dragging")).not.toBeNull();

    fireEvent.drop(group, {
      preventDefault: vi.fn(),
      dataTransfer: { files: [] },
    });

    expect(group.getAttribute("data-dragging")).toBeNull();
  });

  // W-07: Attach is usable BEFORE a cid exists when an `ensureCid` lazy-create is
  // supplied (the surface's pre-create path). The button is ENABLED with a null cid.
  it("W-07: is enabled with a null cid when ensureCid is provided", () => {
    const ensureCid = vi.fn().mockResolvedValue("conv_lazy");
    render(<UploadComposer cid={null} ensureCid={ensureCid} />);
    expect(screen.getByRole("button", { name: /attach files/i })).toBeEnabled();
  });

  // W-07: with a null cid + ensureCid, selecting files lazily creates the cid and
  // routes the upload to THAT conversation (so the attachment rides the first send).
  it("W-07: lazily creates a cid then uploads the attachment to it", async () => {
    const ensureCid = vi.fn().mockResolvedValue("conv_lazy");
    const spy = vi.spyOn(agentApi, "uploadFiles").mockResolvedValue({
      saved: [{ name: "a.csv", bytes: 5 }],
      rejected: [],
    });
    render(<UploadComposer cid={null} ensureCid={ensureCid} />);
    const group = screen.getByRole("group");

    const file = new File(["a,b,c"], "a.csv", { type: "text/csv" });
    fireEvent.drop(group, { preventDefault: vi.fn(), dataTransfer: { files: [file] } });

    await waitFor(() => expect(ensureCid).toHaveBeenCalled());
    await waitFor(() => expect(spy).toHaveBeenCalledWith("conv_lazy", [file]));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /attach files/i })).toBeEnabled(),
    );
  });

  // W-07: when ensureCid resolves null (e.g. offline) the upload is a no-op — never
  // a 404 against a missing conversation.
  it("W-07: does not upload when ensureCid resolves null (offline)", async () => {
    const ensureCid = vi.fn().mockResolvedValue(null);
    const spy = vi.spyOn(agentApi, "uploadFiles").mockResolvedValue({ saved: [], rejected: [] });
    render(<UploadComposer cid={null} ensureCid={ensureCid} />);
    const group = screen.getByRole("group");

    const file = new File(["x"], "x.txt", { type: "text/plain" });
    fireEvent.drop(group, { preventDefault: vi.fn(), dataTransfer: { files: [file] } });

    await waitFor(() => expect(ensureCid).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /attach files/i })).toBeEnabled(),
    );
    expect(spy).not.toHaveBeenCalled();
  });

  it("calls uploadFiles with dropped files", async () => {
    const onUploaded = vi.fn();
    const spy = vi.spyOn(agentApi, "uploadFiles").mockResolvedValue({
      saved: [{ name: "test.csv", bytes: 100 }],
      rejected: [],
    });

    render(<UploadComposer cid="conv_abc" onUploaded={onUploaded} />);
    const group = screen.getByRole("group");

    const file = new File(["a,b,c"], "test.csv", { type: "text/csv" });
    const dt = { files: [file] };

    fireEvent.dragOver(group, { preventDefault: vi.fn() });
    fireEvent.drop(group, { preventDefault: vi.fn(), dataTransfer: dt });

    // Wait for async upload
    await waitFor(() => expect(spy).toHaveBeenCalledWith("conv_abc", [file]));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /attach files/i })).toBeEnabled(),
    );
  });
});

describe("upload announcement in the activity feed", () => {
  const envMessage = (id: string, content: string): AgentEvent => ({
    id,
    kind: "message",
    source: "environment",
    message: { role: "user", content },
  });

  it("derives a neutral system_note for 'User uploaded:' environment messages", () => {
    const items = deriveActivity(
      [
        envMessage("e1", "User uploaded: uploads/data.csv (1 file, 293 bytes)"),
        envMessage("e2", "reminder: the loop nudge nobody should see"),
        envMessage("e3", "⚠ finished WITHOUT a clean browser verification"),
      ],
      null,
      "RUNNING",
    );

    // The announcement surfaces, neutrally — visible but NOT an alarm.
    const note = items.find((i) => i.kind === "system_note");
    expect(note).toBeDefined();
    expect(note!.label).toMatch(/User uploaded: uploads\/data\.csv/);
    expect(note!.attention).toBe(false);

    // The BP-05 valve still holds: other env meta stays hidden, ⚠ stays a warning.
    expect(items.find((i) => i.label.includes("nudge"))).toBeUndefined();
    expect(items.find((i) => i.kind === "system_warning")).toBeDefined();
  });

  it("renders the announcement with the Note label, not Warning", () => {
    const items = deriveActivity(
      [envMessage("e1", "User uploaded: uploads/data.csv")],
      null,
      "RUNNING",
    );
    render(<ActivityFeed items={items} />);

    expect(screen.getByText(/User uploaded: uploads\/data\.csv/)).toBeInTheDocument();
    expect(screen.getByText(/^note$/i)).toBeInTheDocument();
    expect(screen.queryByText(/^warning$/i)).not.toBeInTheDocument();
  });
});
