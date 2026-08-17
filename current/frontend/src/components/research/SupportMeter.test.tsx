import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SupportMeter } from "./SupportMeter";

describe("SupportMeter", () => {
  it("renders 'N of M supported' text + 3-segment bar for mixed claims", () => {
    render(<SupportMeter supported={3} weak={2} unsupported={1} />);
    expect(screen.getByText("3 of 6 supported")).toBeInTheDocument();
  });

  it("shows all segments when all counts positive", () => {
    const { container } = render(<SupportMeter supported={2} weak={2} unsupported={2} />);
    const segments = container.querySelectorAll(".bg-supported, .bg-weak, .bg-unsupported");
    expect(segments.length).toBeGreaterThanOrEqual(3);
  });

  it("renders correct text when all claims supported", () => {
    render(<SupportMeter supported={5} weak={0} unsupported={0} />);
    expect(screen.getByText("5 of 5 supported")).toBeInTheDocument();
  });

  it("renders only unsupported segment when only unsupported claims exist", () => {
    render(<SupportMeter supported={0} weak={0} unsupported={3} />);
    expect(screen.getByText("0 of 3 supported")).toBeInTheDocument();
  });

  it("returns null when total is 0 and no emptyLabel", () => {
    const { container } = render(<SupportMeter supported={0} weak={0} unsupported={0} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders emptyLabel when total is 0 and emptyLabel provided", () => {
    render(<SupportMeter supported={0} weak={0} unsupported={0} emptyLabel="No claim data" />);
    expect(screen.getByText("No claim data")).toBeInTheDocument();
  });

  it("includes title attribute with full counts", () => {
    render(<SupportMeter supported={4} weak={1} unsupported={2} />);
    const el = screen.getByTitle("4 supported, 1 weak, 2 unsupported");
    expect(el).toBeInTheDocument();
  });
});
