/**
 * Deliverable handoff — `deriveDeliverable` + the DeliverablePanel. Proves the
 * panel is a real affordance (one wired action, kind-driven) and that it stays
 * hidden until there's an actual thing to hand off (no false affordance).
 *
 * F1: tests for per-file artifact route download (not whole-project ZIP).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { deriveDeliverable } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";
import { DeliverablePanel } from "@/components/build/DeliverablePanel";

function deliverableEvent(
  id: string,
  title: string,
  path: string,
  kind: "app" | "files",
): AgentEvent {
  return { kind: "deliverable", id, title, path, artifact_kind: kind, source: "agent" };
}

describe("deriveDeliverable", () => {
  it("returns null before any handoff (panel stays hidden)", () => {
    expect(deriveDeliverable([])).toBeNull();
    expect(
      deriveDeliverable([{ kind: "message", id: "m", message: { role: "user", content: "hi" } }]),
    ).toBeNull();
  });

  it("returns the latest handoff — a newer serve supersedes an older one", () => {
    const events: AgentEvent[] = [
      deliverableEvent("d1", "Draft", "draft", "app"),
      { kind: "message", id: "m", message: { role: "assistant", content: "refining" } },
      deliverableEvent("d2", "Final site", "dist", "app"),
    ];
    expect(deriveDeliverable(events)).toEqual({
      id: "d2",
      title: "Final site",
      path: "dist",
      kind: "app",
    });
  });
});

describe("DeliverablePanel", () => {
  it("renders nothing when there is no deliverable", () => {
    const { container } = render(<DeliverablePanel deliverable={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("an app deliverable offers Open and calls onOpen", async () => {
    const onOpen = vi.fn();
    render(
      <DeliverablePanel
        deliverable={{ id: "d", title: "Landing page", path: "dist", kind: "app" }}
        onOpen={onOpen}
        onDownload={vi.fn()}
      />,
    );
    expect(screen.getByText("Landing page")).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /open the deliverable/i });
    await userEvent.click(btn);
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("a files deliverable with cid + file path renders direct download anchor", () => {
    // F1: when cid is provided and it's a file (has extension), render an anchor
    // to the per-file artifact route, NOT a button calling onDownload
    render(
      <DeliverablePanel
        deliverable={{ id: "d", title: "Sales report", path: "report.pdf", kind: "files" }}
        cid="conv-123"
        onOpen={vi.fn()}
        onDownload={vi.fn()}
      />,
    );
    // Should render an anchor, not a button
    const anchor = screen.getByRole("link", { name: /download the deliverable/i });
    expect(anchor).toHaveAttribute(
      "href",
      expect.stringContaining("/conversations/conv-123/artifacts/report.pdf"),
    );
    expect(anchor).toHaveAttribute("download");
  });

  it("a files deliverable without cid falls back to onDownload callback", () => {
    const onDownload = vi.fn();
    render(
      <DeliverablePanel
        deliverable={{ id: "d", title: "Sales report", path: "report.pdf", kind: "files" }}
        onOpen={vi.fn()}
        onDownload={onDownload}
      />,
    );
    const btn = screen.getByRole("button", { name: /download the deliverable/i });
    await userEvent.click(btn);
    expect(onDownload).toHaveBeenCalledOnce();
  });

  it("a files deliverable with directory path (no extension) falls back to onDownload", () => {
    // F1: directories don't have extensions, so they can't use the per-file route
    const onDownload = vi.fn();
    render(
      <DeliverablePanel
        deliverable={{ id: "d", title: "Project files", path: "dist", kind: "files" }}
        cid="conv-123"
        onOpen={vi.fn()}
        onDownload={onDownload}
      />,
    );
    const btn = screen.getByRole("button", { name: /download the deliverable/i });
    await userEvent.click(btn);
    expect(onDownload).toHaveBeenCalledOnce();
  });

  it("disables the action when no handler is wired (no dead button)", () => {
    render(
      <DeliverablePanel
        deliverable={{ id: "d", title: "Thing", path: "x", kind: "app" }}
      />,
    );
    expect(screen.getByRole("button", { name: /open the deliverable/i })).toBeDisabled();
  });

  it("exports the manifest + surfaces a deployed-URL link when present", async () => {
    const onExportManifest = vi.fn();
    render(
      <DeliverablePanel
        deliverable={{
          id: "d",
          title: "Landing page",
          path: "index.html",
          kind: "app",
          deploymentUrl: "https://example.test/app",
        }}
        onOpen={vi.fn()}
        onExportManifest={onExportManifest}
      />,
    );
    const link = screen.getByRole("link", { name: /open the deployed app/i });
    expect(link).toHaveAttribute("href", "https://example.test/app");
    await userEvent.click(screen.getByRole("button", { name: /export the project manifest/i }));
    expect(onExportManifest).toHaveBeenCalledOnce();
  });

  it("shows no Deployed link when the agent served no URL (no false affordance)", () => {
    render(
      <DeliverablePanel
        deliverable={{ id: "d", title: "Thing", path: "x", kind: "app" }}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.queryByRole("link", { name: /deployed/i })).not.toBeInTheDocument();
  });
});
