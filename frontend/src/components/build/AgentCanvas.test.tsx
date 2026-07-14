import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import type { AgentEvent } from "@/types/agent";

// BrowserPane now uses useLiveBrowserConfig (React Query); provide a client
// and a stable mock so these structural tests don't depend on a real server.
vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useLiveBrowserConfig: vi.fn(() => ({ data: { enabled: false }, isLoading: false })),
  };
});

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    pathPreviewBootstrapUrl: vi.fn((cid: string) =>
      Promise.resolve(`http://localhost:8000/__disco/path-preview-auth/${cid}`),
    ),
  };
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const fileWrite = (path: string, content: string): AgentEvent =>
  ({ id: `w-${path}`, kind: "action", thought: "", tool_call: { tool_name: "file_write", arguments: { path, content } } }) as AgentEvent;

const appDeliverable = (path: string): AgentEvent =>
  ({
    id: `d-${path}`,
    kind: "deliverable",
    title: "Selected app",
    path,
    artifact_kind: "app",
    deployment_url: "",
  }) as AgentEvent;

describe("AgentCanvas — the operator inspector", () => {
  it("renders Browser / Artifacts / Console tabs (not the build IDE's Files/Terminal/Preview)", () => {
    wrap(<AgentCanvas events={[]} status="RUNNING" cid="c1" />);
    expect(screen.getByRole("tab", { name: /browser/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /artifacts/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /console/i })).toBeInTheDocument();
    // no IDE-flavored tabs
    expect(screen.queryByRole("tab", { name: /^files$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /preview/i })).not.toBeInTheDocument();
  });

  it("defaults to Browser with an honest empty state when nothing has run", () => {
    wrap(<AgentCanvas events={[]} status="RUNNING" cid="c1" />);
    expect(screen.getByRole("tab", { name: /browser/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/the pages it visits show here/i)).toBeInTheDocument();
    // honest framing: a screenshot reel, not a live video stream
    expect(screen.getByText(/frame-by-frame reel, not a live video/i)).toBeInTheDocument();
  });

  it("defaults to Artifacts when files exist but no screenshot", () => {
    wrap(<AgentCanvas events={[fileWrite("notes.md", "# hi")]} status="FINISHED" cid="c1" />);
    expect(screen.getByRole("tab", { name: /artifacts/i })).toHaveAttribute("aria-selected", "true");
  });

  it("previews the selected nested app instead of an earlier stale root", () => {
    wrap(
      <AgentCanvas
        events={[
          fileWrite("index.html", "<h1>STALE ROOT MUST NEVER OPEN</h1>"),
          fileWrite("release/index.html", "<h1>SELECTED RELEASE ONE</h1>"),
          appDeliverable("release/index.html"),
        ]}
        status="FINISHED"
        cid="c-selected"
      />,
    );

    const srcdoc = screen.getByTitle("Artifact preview").getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("SELECTED RELEASE ONE");
    expect(srcdoc).not.toContain("STALE ROOT MUST NEVER OPEN");
  });

  it("loads a committed multi-file app through the isolated static capability", async () => {
    wrap(
      <AgentCanvas
        events={[
          fileWrite("release/index.html", '<script src="assets/app.js"></script>'),
          fileWrite("release/assets/app.js", "document.body.dataset.loaded='true'"),
          appDeliverable("release/index.html"),
          {
            id: "v-selected",
            kind: "workspace_version",
            version_seq: 1,
            tree_digest: "selected-tree",
            trigger: "finish",
          } as AgentEvent,
        ]}
        status="FINISHED"
        cid="conv_a1b2c3d4agent"
      />,
    );
    await waitFor(() => {
      const frame = screen.getByTitle("Artifact preview");
      expect(frame).toHaveAttribute(
        "src",
        "http://localhost:8000/__disco/path-preview-auth/conv_a1b2c3d4agent",
      );
      expect(frame).not.toHaveAttribute("srcdoc");
      expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-same-origin");
    });
  });
});
