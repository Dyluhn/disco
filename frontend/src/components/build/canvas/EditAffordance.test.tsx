/**
 * EditAffordance — A1.4 unit tests.
 *
 * The inline edit box that appears for a source-kind selection. Together with the
 * `formatEditSteer` tests (appResolver.test.ts), these prove the full A1.4 path:
 * a source selection → the user's instruction → a `steer("In {file} near line
 * {line}, …")` call. Here we verify the box renders the file:line, Apply forwards
 * the typed instruction, Apply is disabled (no-op) on blank input, and the steer
 * string the host would send is correctly formed.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EditAffordance } from "@/components/build/canvas/EditAffordance";
import { formatEditSteer } from "@/lib/resolvers/appResolver";

const RECT = { x: 10, y: 20, width: 100, height: 30 };

describe("EditAffordance", () => {
  it("shows the source file:line and forwards the typed instruction on Apply", () => {
    const onApply = vi.fn();
    render(
      <EditAffordance
        file="index.html"
        line={42}
        rect={RECT}
        onApply={onApply}
        onCancel={vi.fn()}
      />,
    );

    // The header shows the real source location (no false affordance — a real ref).
    expect(screen.getByText("Edit index.html:42")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Describe the change/i), {
      target: { value: "make the heading larger" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(onApply).toHaveBeenCalledTimes(1);
    const instruction = onApply.mock.calls[0][0] as string;
    expect(instruction).toBe("make the heading larger");

    // The host turns that into the steer instruction including file + line.
    expect(formatEditSteer({ file: "index.html", line: 42 }, instruction)).toBe(
      "In index.html near line 42, make the heading larger",
    );
  });

  it("disables Apply for a blank instruction (no steer on empty input)", () => {
    const onApply = vi.fn();
    render(
      <EditAffordance file="a.html" line={3} rect={RECT} onApply={onApply} onCancel={vi.fn()} />,
    );
    const apply = screen.getByRole("button", { name: "Apply" });
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(onApply).not.toHaveBeenCalled();
  });

  it("cancels via the Cancel button", () => {
    const onCancel = vi.fn();
    render(
      <EditAffordance file="a.html" line={3} rect={RECT} onApply={vi.fn()} onCancel={onCancel} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
