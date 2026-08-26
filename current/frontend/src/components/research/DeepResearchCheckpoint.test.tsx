import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ResearchCheckpointEvent } from "@/types/agent";
import { DeepResearchCheckpoint } from "./DeepResearchCheckpoint";

const checkpoint: ResearchCheckpointEvent = {
  id: "checkpoint-1",
  kind: "research_checkpoint",
  query: "Which products shipped?",
  passages: [{ id: "p1" }],
  all_hits: [{ url: "https://example.com" }],
  trail: [],
  completed_queries: [],
  depth_tier: "standard_deep",
  recency_window: "month",
};

describe("DeepResearchCheckpoint", () => {
  it("renders paused evidence without a report shell", () => {
    render(<DeepResearchCheckpoint checkpoint={checkpoint} />);
    expect(screen.getByRole("status", { name: "Research paused" })).toBeInTheDocument();
    expect(screen.getByText(/before a report was written/i)).toBeInTheDocument();
    expect(screen.getByText(/1 source retained/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /report/i })).not.toBeInTheDocument();
  });
});
