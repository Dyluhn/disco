/**
 * SelectionOverlay — component tests.
 *
 * Covers:
 *  1. Arm: clicking "Inspect" button fires onArm.
 *  2. Disarm: when armed, clicking "Stop inspecting" fires onDisarm.
 *  3. Rect mirror: when selection is non-null, the rect div is rendered at the
 *     correct position with the correct label.
 *  4. Walk-up: clicking the "↑ parent" button fires onWalkUp.
 *  5. Untrusted: the arm toggle is replaced with a disabled notice; onArm is
 *     never called regardless of DOM interactions.
 *  6. Null rect: no rect highlight when selection is null.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import type { SelectionEnvelope } from "@/lib/selectionBridge";

// Seed the W-27 inspect-tip "seen" flag so the first-run tip (whose "Dismiss inspect
// tip" button otherwise also matches /inspect/i) doesn't interfere with these
// arm/disarm assertions. Deterministic in isolation, not order-dependent.
beforeEach(() => {
  window.localStorage.setItem("disco-inspect-tip-seen", "1");
});

// ─── Fixture ──────────────────────────────────────────────────────────────────

const NONCE = "aabbccddeeff00112233445566778899";

function makeSelection(
  rect = { x: 10, y: 20, width: 120, height: 40 },
  label = "button.cta — “Submit”",
): SelectionEnvelope {
  return {
    v: 1,
    channel: "disco-select",
    nonce: NONCE,
    type: "disco:selection",
    selection_ref: { kind: "source", oid: "src/App.tsx:5:2", file: "src/App.tsx", line: 5 },
    human_label: label,
    rect,
  };
}

// ─── Default (disarmed, no selection) ────────────────────────────────────────

describe("SelectionOverlay — default state", () => {
  it("renders the Inspect button when not armed and not untrusted", () => {
    render(
      <SelectionOverlay
        armed={false}
        untrusted={false}
        selection={null}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /inspect/i })).toBeInTheDocument();
  });

  it("does NOT render a selection rect when selection is null", () => {
    const { container } = render(
      <SelectionOverlay
        armed={false}
        untrusted={false}
        selection={null}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    // No element with explicit rect positioning when no selection
    const rects = container.querySelectorAll("[style*='border-2']");
    expect(rects.length).toBe(0);
  });
});

// ─── Arm / disarm ─────────────────────────────────────────────────────────────

describe("SelectionOverlay — arm / disarm", () => {
  it("fires onArm when the Inspect button is clicked", () => {
    const onArm = vi.fn();
    render(
      <SelectionOverlay
        armed={false}
        untrusted={false}
        selection={null}
        onArm={onArm}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /inspect/i }));
    expect(onArm).toHaveBeenCalledOnce();
  });

  it("shows 'Stop inspecting' when armed", () => {
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={null}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /stop inspecting/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^inspect$/i })).toBeNull();
  });

  it("fires onDisarm when 'Stop inspecting' is clicked", () => {
    const onDisarm = vi.fn();
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={null}
        onArm={vi.fn()}
        onDisarm={onDisarm}
        onWalkUp={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /stop inspecting/i }));
    expect(onDisarm).toHaveBeenCalledOnce();
  });
});

// ─── Rect mirroring ───────────────────────────────────────────────────────────

describe("SelectionOverlay — mirrors selection rect + label", () => {
  it("renders a positioned div matching the selection rect", () => {
    const selection = makeSelection({ x: 10, y: 20, width: 120, height: 40 });
    const { container } = render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={selection}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    // The rect highlight is absolutely positioned with inline style
    const highlighted = container.querySelector("[style*='width: 120px']");
    expect(highlighted).not.toBeNull();
    expect(highlighted).toHaveStyle({ left: "10px", top: "20px", width: "120px", height: "40px" });
  });

  it("renders the human_label in the label bar", () => {
    const selection = makeSelection(undefined, 'h2.title — "Revenue"');
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={selection}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    expect(screen.getByText(/Revenue/)).toBeInTheDocument();
  });

  it("renders walk-up button when a selection is present", () => {
    const selection = makeSelection();
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={selection}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    expect(screen.getByTitle(/parent element/i)).toBeInTheDocument();
  });

  it("fires onWalkUp when the parent-select button is clicked", () => {
    const onWalkUp = vi.fn();
    const selection = makeSelection();
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={selection}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={onWalkUp}
      />,
    );
    fireEvent.click(screen.getByTitle(/parent element/i));
    expect(onWalkUp).toHaveBeenCalledOnce();
  });
});

// ─── W-26: Discuss with agent (steerable-gated) ─────────────────────────────────

describe("SelectionOverlay — Discuss with agent (W-26)", () => {
  it("does NOT render Discuss when onDiscuss is absent (non-steerable)", () => {
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={makeSelection()}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /discuss/i })).toBeNull();
  });

  it("renders Discuss for a selection when onDiscuss is wired (steerable)", () => {
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={makeSelection()}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
        onDiscuss={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /discuss/i })).toBeInTheDocument();
  });

  it("does NOT render Discuss when there is no selection (nothing to discuss)", () => {
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={null}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
        onDiscuss={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /discuss/i })).toBeNull();
  });

  it("fires onDiscuss with the current selection when clicked", () => {
    const onDiscuss = vi.fn();
    const selection = makeSelection();
    render(
      <SelectionOverlay
        armed={true}
        untrusted={false}
        selection={selection}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
        onDiscuss={onDiscuss}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /discuss/i }));
    expect(onDiscuss).toHaveBeenCalledOnce();
    expect(onDiscuss).toHaveBeenCalledWith(selection);
  });
});

// ─── Untrusted content ────────────────────────────────────────────────────────

describe("SelectionOverlay — untrusted", () => {
  it("shows 'Inspect disabled (untrusted)' instead of the arm button", () => {
    render(
      <SelectionOverlay
        armed={false}
        untrusted={true}
        selection={null}
        onArm={vi.fn()}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    expect(screen.getByText(/disabled \(untrusted\)/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /inspect/i })).toBeNull();
  });

  it("does not call onArm even if rendered in armed state (no arm button)", () => {
    const onArm = vi.fn();
    render(
      <SelectionOverlay
        armed={true}
        untrusted={true}
        selection={null}
        onArm={onArm}
        onDisarm={vi.fn()}
        onWalkUp={vi.fn()}
      />,
    );
    // No arm/disarm button should be in the DOM
    expect(screen.queryByRole("button", { name: /stop inspecting/i })).toBeNull();
    expect(onArm).not.toHaveBeenCalled();
  });
});
