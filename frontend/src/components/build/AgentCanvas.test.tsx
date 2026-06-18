import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const fileWrite = (path: string, content: string): AgentEvent =>
  ({ id: `w-${path}`, kind: "action", thought: "", tool_call: { tool_name: "file_write", arguments: { path, content } } }) as AgentEvent;

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
});
