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

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { StreamingFile } from "@/hooks/useBuildStream";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";

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

function renderCanvas(streamingFile: StreamingFile | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ExecutionCanvas events={[]} status="RUNNING" cid="c1" streamingFile={streamingFile} />
    </QueryClientProvider>,
  );
}

describe("ExecutionCanvas — watch-it-write", () => {
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
});
