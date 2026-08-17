/**
 * W-27 — the first-run Inspect explainer tip inside SelectionOverlay: shows
 * once (localStorage flag), counts down visibly, auto-hides at 0, and can be
 * dismissed early — persisting the seen flag either way.
 */

import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";

const TIP_TEXT = /highlight an item and discuss or iterate on it/i;
const SEEN_KEY = "disco-inspect-tip-seen";

const noop = () => {};
function renderOverlay(untrusted = false) {
  return render(
    <SelectionOverlay
      armed={false}
      untrusted={untrusted}
      selection={null}
      onArm={noop}
      onDisarm={noop}
      onWalkUp={noop}
    />,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("SelectionOverlay inspect tip (W-27)", () => {
  it("shows the explainer on first run with a visible countdown", () => {
    renderOverlay();
    expect(screen.getByText(TIP_TEXT)).toBeInTheDocument();
    expect(screen.getByText(/Hiding in 10s/)).toBeInTheDocument();
  });

  it("counts down and auto-hides at 0, persisting the seen flag", () => {
    vi.useFakeTimers();
    renderOverlay();
    expect(screen.getByText(/Hiding in 10s/)).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByText(/Hiding in 7s/)).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(7000);
    });
    expect(screen.queryByText(TIP_TEXT)).not.toBeInTheDocument();
    expect(window.localStorage.getItem(SEEN_KEY)).toBe("1");
  });

  it("dismisses on the X and persists the seen flag", async () => {
    const user = userEvent.setup();
    renderOverlay();
    await user.click(screen.getByRole("button", { name: /dismiss inspect tip/i }));
    expect(screen.queryByText(TIP_TEXT)).not.toBeInTheDocument();
    expect(window.localStorage.getItem(SEEN_KEY)).toBe("1");
  });

  it("does NOT show again once the flag is set", () => {
    window.localStorage.setItem(SEEN_KEY, "1");
    renderOverlay();
    expect(screen.queryByText(TIP_TEXT)).not.toBeInTheDocument();
  });

  it("does not show on untrusted runs (inspect disabled)", () => {
    renderOverlay(true);
    expect(screen.queryByText(TIP_TEXT)).not.toBeInTheDocument();
  });
});
