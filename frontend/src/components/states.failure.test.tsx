/**
 * The terminal error wall renders all FOUR parts.
 *
 * `ErrorEvent.failure` (L26) is a `RunFailure{failure_class, why, state, next,
 * allowed}` — the producer builds those fields precisely so nothing downstream
 * has to read an explanation back out of a sentence, and the UI was rendering
 * only `detail`. The fixture is a VERBATIM `ErrorEvent`, produced by running the
 * real chain (`exhaustion_failure` → `ResearchAgentError` → `run_failure_for` →
 * `ErrorEvent`, which is what `_append_terminal_error` does) and dumped with
 * `model_dump(mode="json")`. No captured run in the campaign's evidence carried
 * an ErrorEvent at all, so producing one through the producer was the honest
 * way to get a real frame.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ErrorState } from "@/components/states";
import type { ErrorEvent } from "@/types/agent";
import errorEvent from "@/lib/__fixtures__/deep-research-error-event.json";

const event = errorEvent as unknown as ErrorEvent;

describe("ErrorState with a typed RunFailure", () => {
  it("renders why, state, next and what is still allowed as four parts", () => {
    render(
      <ErrorState message={event.detail ?? ""} onRetry={() => {}} failure={event.failure} />,
    );
    const wall = screen.getByRole("alert");
    for (const label of ["Why", "Where the run stands", "Next", "Still available"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    const failure = event.failure!;
    expect(wall).toHaveTextContent(failure.why);
    expect(wall).toHaveTextContent(failure.state);
    expect(wall).toHaveTextContent(failure.next);
    expect(wall).toHaveTextContent(failure.allowed);
  });

  it("labels the boundary that raised instead of blaming the model", () => {
    render(
      <ErrorState message={event.detail ?? ""} onRetry={() => {}} failure={event.failure} />,
    );
    expect(screen.getByText("The run stopped")).toBeInTheDocument();
    expect(screen.getByText("Search infrastructure")).toBeInTheDocument();
    expect(screen.queryByText("The model returned an error")).not.toBeInTheDocument();
  });

  it("keeps `detail` as the fallback for an event with no failure", () => {
    render(<ErrorState message="ValueError: boom" onRetry={() => {}} failure={null} />);
    expect(screen.getByText("The model returned an error")).toBeInTheDocument();
    expect(screen.getByText("ValueError: boom")).toBeInTheDocument();
    expect(screen.queryByText("Still available")).not.toBeInTheDocument();
  });

  it("still offers the retry in both forms", () => {
    const { rerender } = render(
      <ErrorState message="boom" onRetry={() => {}} failure={null} />,
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    rerender(<ErrorState message="boom" onRetry={() => {}} failure={event.failure} />);
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
