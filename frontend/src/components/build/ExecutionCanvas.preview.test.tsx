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
import { previewHostUrl } from "@/api/client";
import type { AgentEvent } from "@/types/agent";

describe("previewHostUrl (DC-01)", () => {
  it("returns null when base is unconfigured", () => {
    expect(previewHostUrl("conv_12345678", 8000, "")).toBeNull();
  });

  it("extracts cid8 and maps 127.0.0.1 to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://127.0.0.1:8000")).toBe("http://abcdef12-8000.localhost:8000");
  });

  it("extracts cid8 and maps localhost to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 5173, "http://localhost:8000")).toBe("http://abcdef12-5173.localhost:8000");
  });

  it("prepends cid8-port to other hostnames", () => {
    expect(previewHostUrl("conv_11112222", 8000, "http://my-magic-dns.net")).toBe("http://11112222-8000.my-magic-dns.net");
    expect(previewHostUrl("conv_11112222", 8000, "https://my-magic-dns.net:443")).toBe("https://11112222-8000.my-magic-dns.net");
  });
});

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
