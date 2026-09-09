import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CheckCounts } from "@/lib/claimCheck";
import { SupportMeter } from "./SupportMeter";

function counts(over: Partial<CheckCounts> = {}): CheckCounts {
  return { supported: 0, contradicted: 0, unresolved: 0, unavailable: 0, ...over };
}

describe("SupportMeter", () => {
  it("renders 'N of M supported' text + a segment per state present", () => {
    const { container } = render(
      <SupportMeter counts={counts({ supported: 3, contradicted: 1, unresolved: 2 })} />,
    );
    expect(screen.getByText("3 of 6 supported")).toBeInTheDocument();
    expect(
      container.querySelectorAll(".bg-supported, .bg-unsupported, .bg-weak"),
    ).toHaveLength(3);
  });

  it("renders correct text when every claim is supported", () => {
    render(<SupportMeter counts={counts({ supported: 5 })} />);
    expect(screen.getByText("5 of 5 supported")).toBeInTheDocument();
  });

  it("returns null when total is 0 and no emptyLabel", () => {
    const { container } = render(<SupportMeter counts={counts()} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders emptyLabel when total is 0 and emptyLabel provided", () => {
    render(<SupportMeter counts={counts()} emptyLabel="No claim data" />);
    expect(screen.getByText("No claim data")).toBeInTheDocument();
  });

  it("names every state present in its title", () => {
    render(<SupportMeter counts={counts({ supported: 4, unresolved: 1, unavailable: 2 })} />);
    expect(
      screen.getByTitle("4 supported, 1 unresolved, 2 not checked"),
    ).toBeInTheDocument();
  });

  // UI-20: "25 OF 104 SUPPORTED" over a bar with no key read as 79 failures.
  it("names the colours when a legend is asked for, so the rest is not read as failure", () => {
    render(
      <SupportMeter
        counts={counts({ supported: 25, contradicted: 7, unresolved: 72 })}
        legend
      />,
    );
    expect(screen.getByText("25 supported")).toBeInTheDocument();
    expect(screen.getByText("7 possible contradiction")).toBeInTheDocument();
    expect(screen.getByText("72 unresolved")).toBeInTheDocument();
  });

  it("leaves a state with no claims out of the legend entirely", () => {
    render(<SupportMeter counts={counts({ supported: 4, unresolved: 1 })} legend />);
    expect(screen.queryByText(/possible contradiction/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not checked/)).not.toBeInTheDocument();
  });
});
