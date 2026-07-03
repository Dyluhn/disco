/**
 * EditAffordance — P8 unit tests.
 *
 * The inline edit box that appears for a selected element in edit mode. It shows the
 * element's human label and forwards the typed instruction to `onApply`; the PARENT
 * submits the element's typed ref + instruction over `selection_edit`, and the HOST
 * builds the precisely-anchored, targeted-edit directive (core/selection_edit.py).
 * Here we verify the box renders the label, Apply forwards the instruction, Apply is
 * disabled (no-op) on blank input, and Cancel dismisses.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EditAffordance } from "@/components/build/canvas/EditAffordance";

const RECT = { x: 10, y: 20, width: 100, height: 30 };

describe("EditAffordance", () => {
  it("shows the element label and forwards the typed instruction on Apply", () => {
    const onApply = vi.fn();
    render(
      <EditAffordance
        targetLabel='h1 — "Nightshift Coffee"'
        rect={RECT}
        onApply={onApply}
        onCancel={vi.fn()}
      />,
    );

    // The header shows the human-readable element description.
    expect(screen.getByText('Edit h1 — "Nightshift Coffee"')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Describe the change/i), {
      target: { value: "make the heading larger" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Apply/ }));

    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply.mock.calls[0][0]).toBe("make the heading larger");
  });

  it("disables Apply for a blank instruction (no edit on empty input)", () => {
    const onApply = vi.fn();
    render(
      <EditAffordance targetLabel="the CTA button" rect={RECT} onApply={onApply} onCancel={vi.fn()} />,
    );
    const apply = screen.getByRole("button", { name: /Apply/ });
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(onApply).not.toHaveBeenCalled();
  });

  it("cancels via the Cancel button", () => {
    const onCancel = vi.fn();
    render(
      <EditAffordance targetLabel="the CTA button" rect={RECT} onApply={vi.fn()} onCancel={onCancel} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
