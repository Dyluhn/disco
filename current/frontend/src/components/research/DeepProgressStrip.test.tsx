import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { deriveActivity, type DeepStats } from "@/lib/deepResearchTrace";
import { DeepProgressStrip } from "./DeepProgressStrip";

const stats: DeepStats = {
  searches: 4,
  refusals: 0,
  sectionsDone: 4,
  sourcesDiscovered: 12,
  elapsedSeconds: 20,
  phase: null,
  activeSection: null,
  lastThought: null,
  lastThoughtLeg: null,
  carriedSources: null,
};

/** A run that reported nothing: every activity field is null, which is what
 *  the strip must render as "no claim", not as a blank spinner. */
const noActivity = deriveActivity([]);

/** A run whose newest event just landed — the state that earns the Live dot. */
function liveActivity() {
  return deriveActivity([
    {
      id: "a1",
      kind: "action",
      seq: 1,
      timestamp: new Date().toISOString(),
      thought: "",
      tool_call: { tool_name: "search", arguments: { query: "q" } },
    },
  ]);
}

describe("DeepProgressStrip", () => {
  it("collapses completed research by default while preserving an explicit expansion", async () => {
    const user = userEvent.setup();
    render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={stats}
        status="FINISHED"
        activity={noActivity}
        followUpStatus={null}
      />,
    );

    const summary = screen.getByRole("button", { name: /How it researched/i });
    expect(summary).toBeInTheDocument();
    await user.click(summary);
    expect(screen.getByRole("button", { name: /Collapse progress/i })).toBeInTheDocument();
  });
  // UI-33: during a 45 s server outage the run view kept the blue "Live" dot.
  it("shows the reconnecting pill instead of Live once the stream is gone", () => {
    const { container } = render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={stats}
        status="RUNNING"
        activity={liveActivity()}
        followUpStatus={null}
        connectionState="degraded"
      />,
    );
    expect(screen.getByText("reconnecting…")).toBeInTheDocument();
    expect(container.querySelector("[data-dr-signal]")).toBeNull();
  });

  it("keeps the Live signal while the stream is delivering", () => {
    const { container } = render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={stats}
        status="RUNNING"
        activity={liveActivity()}
        followUpStatus={null}
      />,
    );
    expect(screen.queryByText("reconnecting…")).not.toBeInTheDocument();
    expect(container.querySelector('[data-dr-signal="live"]')).not.toBeNull();
  });
});
