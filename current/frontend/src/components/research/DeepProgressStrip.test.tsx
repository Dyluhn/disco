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
  it("collapses live activity by default and keeps the choice while events arrive", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <DeepProgressStrip
        brief="A live research brief"
        trace={[{ id: "a1", kind: "search", label: "Searching", detail: "first" }]}
        stats={stats}
        status="RUNNING"
        followUpStatus={null}
      />,
    );

    const toggle = screen.getByRole("button", { name: /How it researched/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "deep-research-activity");
    expect(screen.getByText("Research activity")).toBeVisible();
    expect(screen.queryByText("A live research brief")).not.toBeInTheDocument();

    await user.click(toggle);
    expect(screen.getByRole("button", { name: /Collapse progress/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("A live research brief")).toBeInTheDocument();

    rerender(
      <DeepProgressStrip
        brief="A live research brief"
        trace={[
          { id: "a1", kind: "search", label: "Searching", detail: "first" },
          { id: "a2", kind: "search", label: "Searching", detail: "second" },
        ]}
        stats={{ ...stats, searches: 5 }}
        status="RUNNING"
        followUpStatus={null}
      />,
    );
    expect(screen.getByText("A live research brief")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Collapse progress/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

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
