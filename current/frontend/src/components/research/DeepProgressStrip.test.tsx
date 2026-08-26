import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { DeepStats } from "@/lib/deepResearchTrace";
import { DeepProgressStrip } from "./DeepProgressStrip";

const stats: DeepStats = {
  searches: 4,
  sectionsDone: 4,
  sourcesDiscovered: 12,
  elapsedSeconds: 20,
  phase: null,
  activeSection: null,
  lastThought: null,
};

describe("DeepProgressStrip", () => {
  it("collapses completed research by default while preserving an explicit expansion", async () => {
    const user = userEvent.setup();
    render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={stats}
        status="FINISHED"
        followUpStatus={null}
      />,
    );

    const summary = screen.getByRole("button", { name: /How it researched/i });
    expect(summary).toBeInTheDocument();
    await user.click(summary);
    expect(screen.getByRole("button", { name: /Collapse progress/i })).toBeInTheDocument();
  });
});
