import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryInput } from "./QueryInput";

// Mock the child controls so the test focuses on QueryInput's cluster gating,
// not the pickers' internals (they pull react-query/model catalogue otherwise).
vi.mock("./ModelLeaderPill", () => ({
  ModelLeaderPill: () => <div data-testid="model-pill" />,
}));
vi.mock("./ScopeControl", () => ({
  ScopeControl: () => <div data-testid="scope-control" />,
}));
vi.mock("./ThinkToggle", () => ({
  ThinkToggle: () => <button type="button" role="switch" aria-label="Think" />,
}));

describe("QueryInput — Think toggle gating (BW-04)", () => {
  it("renders the Think toggle alongside model/scope when onThinkChange is wired", () => {
    render(
      <QueryInput
        onSubmit={() => {}}
        onLeaderChange={() => {}}
        onScopeChange={() => {}}
        think={false}
        onThinkChange={() => {}}
      />,
    );
    expect(screen.getByTestId("model-pill")).toBeInTheDocument();
    expect(screen.getByTestId("scope-control")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Think/i })).toBeInTheDocument();
  });

  it("OMITS the Think toggle (no inert affordance) when onThinkChange is absent, but keeps model/scope", () => {
    // This is the Deep Research path: it can't honor reasoning effort, so it
    // omits onThinkChange — the toggle must vanish, the cluster must remain.
    render(
      <QueryInput
        onSubmit={() => {}}
        onLeaderChange={() => {}}
        onScopeChange={() => {}}
      />,
    );
    expect(screen.getByTestId("model-pill")).toBeInTheDocument();
    expect(screen.getByTestId("scope-control")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /Think/i })).not.toBeInTheDocument();
  });
});
