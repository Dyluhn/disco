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
  it("describes writing recovery without promising more research", () => {
    render(<DeepResearchCheckpoint checkpoint={{ ...checkpoint, meta: {
      recovery_reason: "server_restart", recovery_stage: "writing", recovery_source_count: 30,
    } }} />);
    expect(screen.getByText(/recovered after a server restart/i)).toBeInTheDocument();
    expect(screen.getByText(/30 sources retained.*restart writing/i)).toBeInTheDocument();
    expect(screen.getByText(/completed research will not be repeated/i)).toBeInTheDocument();
    expect(screen.queryByText(/continue gathering/i)).not.toBeInTheDocument();
  });

  it("discloses earlier recovery and the remaining original budget", () => {
    render(<DeepResearchCheckpoint checkpoint={{ ...checkpoint, meta: {
      recovery_reason: "server_restart", recovery_stage: "research", recovery_fallback: true,
      turns_remaining: 3, sources_remaining: 2,
    } }} />);
    expect(screen.getByText(/remaining budget: 3 research turns and 2 new sources/i)).toBeInTheDocument();
    expect(screen.getByText(/earlier saved state was recovered/i)).toBeInTheDocument();
  });

  it("renders paused evidence without a report shell", () => {
    render(<DeepResearchCheckpoint checkpoint={checkpoint} />);
    expect(screen.getByRole("status", { name: "Research paused" })).toBeInTheDocument();
    expect(screen.getByText(/before a report was written/i)).toBeInTheDocument();
    expect(screen.getByText(/1 source retained/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /report/i })).not.toBeInTheDocument();
  });
});
