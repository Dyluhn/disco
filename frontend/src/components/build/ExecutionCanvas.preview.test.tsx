/**
 * Deliverable handoff: when a build FINISHES and there's a renderable artifact,
 * the inspector auto-switches to the Preview tab so the user sees the result —
 * but only on the rising edge into FINISHED (never yanking mid-run).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import type { AgentEvent } from "@/types/agent";

// The Preview pane uses useBuildPreview (react-query), so renders need a client.
function withClient(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

function fileWrite(path: string, content: string): AgentEvent {
  return {
    id: `w-${path}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  } as AgentEvent;
}

const HTML = fileWrite("index.html", "<html><body><h1>Done</h1></body></html>");
const NOTE = fileWrite("notes.txt", "no html here");

describe("ExecutionCanvas — auto-switch to Preview on finish", () => {
  it("switches to Preview when the run finishes with a renderable artifact", () => {
    // start mid-run with a non-renderable file → Preview is NOT the initial tab
    const { rerender } = render(
      withClient(<ExecutionCanvas events={[NOTE]} status="RUNNING" cid="c1" streamingFile={null} />),
    );
    expect(screen.getByRole("tab", { name: /preview/i })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    // the html artifact lands and the run finishes → auto-switch to Preview
    rerender(
      withClient(
        <ExecutionCanvas events={[NOTE, HTML]} status="FINISHED" cid="c1" streamingFile={null} />,
      ),
    );
    expect(screen.getByRole("tab", { name: /preview/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("does not force Preview on finish when there's nothing renderable", () => {
    const { rerender } = render(
      withClient(<ExecutionCanvas events={[NOTE]} status="RUNNING" cid="c2" streamingFile={null} />),
    );
    rerender(
      withClient(<ExecutionCanvas events={[NOTE]} status="FINISHED" cid="c2" streamingFile={null} />),
    );
    expect(screen.getByRole("tab", { name: /preview/i })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });
});
