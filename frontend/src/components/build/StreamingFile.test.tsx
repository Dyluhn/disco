/**
 * Watch-it-write: the ExecutionCanvas renders the file the driver is composing
 * RIGHT NOW (the `streamingFile` buffer), live, before the authoritative
 * ActionEvent lands. Proves the streamed content shows with its filename and a
 * writing indicator — the "I can see it's not hung" guarantee.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StreamingFile } from "@/hooks/useBuildStream";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";

const streaming: StreamingFile = {
  path: "styles.css",
  tool: "file_write",
  content: "/* dark theme */\nbody { background: #0b0b0f; }",
};

describe("ExecutionCanvas — watch-it-write", () => {
  it("shows the streaming file's content + filename while it writes", () => {
    render(<ExecutionCanvas events={[]} status="RUNNING" cid="c1" streamingFile={streaming} />);
    // the live content is on screen (not waiting for the final event)
    expect(screen.getByText(/background: #0b0b0f/)).toBeInTheDocument();
    // the filename is shown whole (the path-completeness fix), more than once is fine
    expect(screen.getAllByText("styles.css").length).toBeGreaterThan(0);
    // a "writing" affordance is present (the byte/status line)
    expect(screen.getByText(/writing/i)).toBeInTheDocument();
  });

  it("falls back to the empty files state when nothing is streaming", () => {
    render(<ExecutionCanvas events={[]} status="RUNNING" cid="c1" streamingFile={null} />);
    expect(screen.getByText(/No files written yet/i)).toBeInTheDocument();
  });
});
